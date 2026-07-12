from __future__ import annotations

import csv
import json
import math
from html import escape
from pathlib import Path
from typing import Any, Iterable

from .normalize import clean_scalar, quality_policy_for_item, should_skip_quality, write_normalized_csv


def extract_cutout_lightcurve_file(
    path: Path,
    metadata: dict[str, Any],
    *,
    aperture_mode: str = "circle",
    aperture_radius: float = 1.5,
    aperture_pixels: str | None = None,
    threshold_sigma: float = 3.0,
    background: str = "none",
    quality_mask: int | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    quality_filter_stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    try:
        from astropy.io import fits
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("extracting TESSCut light curves requires astropy") from exc

    with fits.open(path) as hdul:
        table = hdul[1].data
        columns = {str(name).upper(): str(name) for name in table.columns.names}
        if "TIME" not in columns or "FLUX" not in columns:
            raise RuntimeError("TESSCut table must contain TIME and FLUX columns")
        flux_cube = table[columns["FLUX"]]
        if len(flux_cube) == 0:
            return []
        first_frame = flux_cube[0]
        height = len(first_frame)
        width = len(first_frame[0]) if height else 0
        selected_pixels = select_aperture_pixels(
            width,
            height,
            flux_cube,
            mode=aperture_mode,
            radius=aperture_radius,
            pixel_spec=aperture_pixels,
            threshold_sigma=threshold_sigma,
        )
        background_pixels = outside_pixels(width, height, set(selected_pixels)) if background == "median-outside" else []
        if background not in {"none", "median-outside"}:
            raise RuntimeError(f"unsupported cutout background method: {background}")
        if not selected_pixels:
            raise RuntimeError("cutout aperture selected no pixels")
        if diagnostics is not None:
            median_image = median_flux_image(flux_cube, width=width, height=height)
            diagnostics.append(
                build_aperture_diagnostic(
                    path,
                    metadata,
                    width=width,
                    height=height,
                    aperture_pixels=selected_pixels,
                    background_pixels=background_pixels,
                    aperture_mode=aperture_mode,
                    aperture_radius=aperture_radius,
                    aperture_pixel_spec=aperture_pixels,
                    threshold_sigma=threshold_sigma,
                    background=background,
                    median_image=median_image,
                )
            )

        rows: list[dict[str, Any]] = []
        for index in range(len(table)):
            if quality_filter_stats is not None:
                quality_filter_stats["input_rows"] = quality_filter_stats.get("input_rows", 0) + 1
            quality = clean_scalar(table[columns["QUALITY"]][index]) if "QUALITY" in columns else None
            if should_skip_quality(quality, quality_mask):
                if quality_filter_stats is not None:
                    quality_filter_stats["quality_dropped_rows"] = quality_filter_stats.get("quality_dropped_rows", 0) + 1
                continue
            frame = table[columns["FLUX"]][index]
            aperture_values = finite_pixel_values(frame, selected_pixels)
            if not aperture_values:
                if quality_filter_stats is not None:
                    quality_filter_stats["empty_aperture_rows"] = quality_filter_stats.get("empty_aperture_rows", 0) + 1
                continue
            aperture_flux = sum(aperture_values)
            background_level = None
            background_count = 0
            if background_pixels:
                background_values = finite_pixel_values(frame, background_pixels)
                background_count = len(background_values)
                if background_values:
                    background_level = median(background_values)
            corrected_flux = aperture_flux
            if background_level is not None:
                corrected_flux = aperture_flux - background_level * len(selected_pixels)

            row = {
                "tic_id": metadata.get("tic_id"),
                "sector": metadata.get("sector"),
                "cadence_sec": metadata.get("cadence_sec"),
                "product": metadata.get("product", "cutout"),
                "time_btjd": clean_scalar(table[columns["TIME"]][index]),
                "flux": corrected_flux,
                "flux_err": cutout_flux_error(table, columns, index, selected_pixels),
                "quality": quality,
                "source_file": str(path),
                "aperture_mode": aperture_mode,
                "aperture_radius_px": aperture_radius,
                "aperture_n_pixels": len(selected_pixels),
                "aperture_pixel_spec": aperture_pixels,
                "aperture_threshold_sigma": threshold_sigma if aperture_mode == "threshold" else None,
                "background_method": background,
                "background_flux_per_pixel": background_level,
                "background_n_pixels": background_count,
                "raw_aperture_flux": aperture_flux,
            }
            if "TIMECORR" in columns:
                row["raw_timecorr"] = clean_scalar(table[columns["TIMECORR"]][index])
            if "CADENCENO" in columns:
                row["raw_cadenceno"] = clean_scalar(table[columns["CADENCENO"]][index])
            rows.append(row)
        if quality_filter_stats is not None:
            quality_filter_stats["output_rows"] = quality_filter_stats.get("output_rows", 0) + len(rows)
        return rows


def extract_cutout_manifest_files(
    items: list[dict[str, Any]],
    output_path: Path,
    *,
    aperture_mode: str = "circle",
    aperture_radius: float = 1.5,
    aperture_pixels: str | None = None,
    threshold_sigma: float = 3.0,
    background: str = "none",
    quality_mask: int | None = None,
    quality_preset: str | None = None,
    aperture_summary_json: Path | None = None,
    aperture_mask_csv: Path | None = None,
    aperture_report_html: Path | None = None,
) -> None:
    rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for item in items:
        if item.get("file_role") != "cutout":
            item["cutout_lightcurve_status"] = "skipped_role"
            item["cutout_lightcurve_input_rows"] = 0
            item["cutout_lightcurve_rows"] = 0
            item["cutout_lightcurve_quality_dropped_rows"] = 0
            item["cutout_lightcurve_empty_aperture_rows"] = 0
            continue
        if item.get("status") not in {"downloaded", "exists"}:
            item["cutout_lightcurve_status"] = "skipped_status"
            item["cutout_lightcurve_input_rows"] = 0
            item["cutout_lightcurve_rows"] = 0
            item["cutout_lightcurve_quality_dropped_rows"] = 0
            item["cutout_lightcurve_empty_aperture_rows"] = 0
            continue
        paths = [Path(path) for path in item.get("local_paths", [])]
        if not paths:
            item["cutout_lightcurve_status"] = "skipped_no_files"
            item["cutout_lightcurve_input_rows"] = 0
            item["cutout_lightcurve_rows"] = 0
            item["cutout_lightcurve_quality_dropped_rows"] = 0
            item["cutout_lightcurve_empty_aperture_rows"] = 0
            continue
        try:
            policy = quality_policy_for_item(item, raw_mask=quality_mask, preset=quality_preset)
            item.update(policy)
            resolved_quality_mask = policy.get("quality_mask")
            item_rows: list[dict[str, Any]] = []
            quality_filter_stats: dict[str, int] = {}
            for path in paths:
                item_rows.extend(
                    extract_cutout_lightcurve_file(
                        path,
                        item,
                        aperture_mode=aperture_mode,
                        aperture_radius=aperture_radius,
                        aperture_pixels=aperture_pixels,
                        threshold_sigma=threshold_sigma,
                        background=background,
                        quality_mask=resolved_quality_mask,
                        diagnostics=diagnostics,
                        quality_filter_stats=quality_filter_stats,
                    )
                )
            rows.extend(item_rows)
            item["cutout_lightcurve_status"] = "ok"
            item["cutout_lightcurve_rows"] = len(item_rows)
            item["cutout_lightcurve_input_rows"] = quality_filter_stats.get("input_rows", len(item_rows))
            item["cutout_lightcurve_quality_dropped_rows"] = quality_filter_stats.get("quality_dropped_rows", 0)
            item["cutout_lightcurve_empty_aperture_rows"] = quality_filter_stats.get("empty_aperture_rows", 0)
        except Exception as exc:
            item["cutout_lightcurve_status"] = "error"
            item["cutout_lightcurve_input_rows"] = 0
            item["cutout_lightcurve_quality_dropped_rows"] = 0
            item["cutout_lightcurve_empty_aperture_rows"] = 0
            item["cutout_lightcurve_error"] = str(exc)
    write_normalized_csv(output_path, rows)
    write_aperture_diagnostics(
        diagnostics,
        summary_json=aperture_summary_json,
        mask_csv=aperture_mask_csv,
        report_html=aperture_report_html,
    )


def build_aperture_diagnostic(
    path: Path,
    metadata: dict[str, Any],
    *,
    width: int,
    height: int,
    aperture_pixels: list[tuple[int, int]],
    background_pixels: list[tuple[int, int]],
    aperture_mode: str,
    aperture_radius: float,
    aperture_pixel_spec: str | None,
    threshold_sigma: float,
    background: str,
    median_image: list[list[float | None]] | None = None,
) -> dict[str, Any]:
    ys = [pixel[0] for pixel in aperture_pixels]
    xs = [pixel[1] for pixel in aperture_pixels]
    return {
        "source_file": str(path),
        "target_label": metadata.get("target_label"),
        "tic_id": metadata.get("tic_id"),
        "sector": metadata.get("sector"),
        "product": metadata.get("product", "cutout"),
        "shape": {"height": height, "width": width},
        "aperture_mode": aperture_mode,
        "aperture_radius_px": aperture_radius,
        "aperture_pixel_spec": aperture_pixel_spec,
        "aperture_threshold_sigma": threshold_sigma if aperture_mode == "threshold" else None,
        "aperture_n_pixels": len(aperture_pixels),
        "aperture_bounds": {
            "y_min": min(ys),
            "y_max": max(ys),
            "x_min": min(xs),
            "x_max": max(xs),
        },
        "background_method": background,
        "background_n_pixels": len(background_pixels),
        "aperture_pixels": [{"y": y, "x": x} for y, x in aperture_pixels],
        "median_flux_image": median_image,
    }


def write_aperture_diagnostics(
    diagnostics: list[dict[str, Any]],
    *,
    summary_json: Path | None,
    mask_csv: Path | None,
    report_html: Path | None = None,
) -> None:
    if summary_json is not None:
        summary_json.parent.mkdir(parents=True, exist_ok=True)
        with summary_json.open("w", encoding="utf-8") as handle:
            json.dump({"schema_version": "tess-fetch.aperture-summary.v0.1", "apertures": diagnostics}, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if mask_csv is not None:
        write_aperture_mask_csv(mask_csv, diagnostics)
    if report_html is not None:
        write_aperture_report_html(report_html, diagnostics)


def write_aperture_mask_csv(path: Path, diagnostics: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_file",
        "target_label",
        "tic_id",
        "sector",
        "product",
        "aperture_mode",
        "height",
        "width",
        "y",
        "x",
        "in_aperture",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for diagnostic in diagnostics:
            aperture_set = {(pixel["y"], pixel["x"]) for pixel in diagnostic.get("aperture_pixels", [])}
            shape = diagnostic.get("shape", {})
            height = int(shape.get("height", 0) or 0)
            width = int(shape.get("width", 0) or 0)
            for y in range(height):
                for x in range(width):
                    writer.writerow(
                        {
                            "source_file": diagnostic.get("source_file"),
                            "target_label": diagnostic.get("target_label"),
                            "tic_id": diagnostic.get("tic_id"),
                            "sector": diagnostic.get("sector"),
                            "product": diagnostic.get("product"),
                            "aperture_mode": diagnostic.get("aperture_mode"),
                            "height": height,
                            "width": width,
                            "y": y,
                            "x": x,
                            "in_aperture": (y, x) in aperture_set,
                        }
                    )


def write_aperture_report_html(path: Path, diagnostics: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cards = "\n".join(aperture_report_card(diagnostic) for diagnostic in diagnostics)
    if not cards:
        cards = '<section class="empty">No aperture diagnostics were generated.</section>'
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TESSCut Aperture Report</title>
<style>
:root {{
  color-scheme: light;
  --ink: #182026;
  --muted: #5b6570;
  --line: #c8d1d9;
  --selected: #1f7a5a;
  --selected-border: #0f513a;
  --bg: #f6f8fa;
  --panel: #ffffff;
}}
body {{
  margin: 0;
  font-family: Arial, Helvetica, sans-serif;
  color: var(--ink);
  background: var(--bg);
}}
main {{
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
}}
h1 {{
  margin: 0 0 18px;
  font-size: 24px;
  font-weight: 700;
}}
.aperture-card {{
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 16px;
  margin: 0 0 16px;
}}
.card-head {{
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: baseline;
  margin-bottom: 12px;
}}
.title {{
  font-size: 16px;
  font-weight: 700;
}}
.meta {{
  color: var(--muted);
  font-size: 13px;
}}
.details {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px 16px;
  margin: 0 0 14px;
  font-size: 13px;
}}
.details div {{
  min-width: 0;
}}
.label {{
  color: var(--muted);
  display: block;
  margin-bottom: 2px;
}}
.pixel-grid {{
  display: grid;
  grid-template-columns: repeat(var(--width), 24px);
  grid-auto-rows: 24px;
  gap: 2px;
  width: max-content;
  max-width: 100%;
  overflow: auto;
  padding: 2px;
  border: 1px solid var(--line);
  background: #eef2f5;
}}
.pixel {{
  width: 24px;
  height: 24px;
  border: 1px solid #d5dde5;
  background: var(--heat, #fff);
  box-sizing: border-box;
}}
.pixel.selected {{
  border-color: var(--selected-border);
  box-shadow: inset 0 0 0 3px var(--selected);
}}
.empty {{
  border: 1px dashed var(--line);
  background: var(--panel);
  border-radius: 6px;
  padding: 16px;
  color: var(--muted);
}}
</style>
</head>
<body>
<main>
<h1>TESSCut Aperture Report</h1>
{cards}
</main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def aperture_report_card(diagnostic: dict[str, Any]) -> str:
    shape = diagnostic.get("shape", {})
    height = int(shape.get("height", 0) or 0)
    width = int(shape.get("width", 0) or 0)
    selected = {(int(pixel["y"]), int(pixel["x"])) for pixel in diagnostic.get("aperture_pixels", [])}
    heatmap = diagnostic_heatmap(diagnostic, width=width, height=height)
    cells = []
    for y in range(height):
        for x in range(width):
            selected_class = " selected" if (y, x) in selected else ""
            heat = heatmap[y][x] if y < len(heatmap) and x < len(heatmap[y]) else None
            style = f' style="--heat: {heat}"' if heat is not None else ""
            cells.append(f'<span class="pixel{selected_class}" title="y={y}, x={x}"{style}></span>')
    title = diagnostic.get("target_label") or diagnostic.get("tic_id") or diagnostic.get("source_file") or "cutout"
    sector = diagnostic.get("sector")
    bounds = diagnostic.get("aperture_bounds", {})
    bounds_text = (
        f"y {bounds.get('y_min')}..{bounds.get('y_max')}, "
        f"x {bounds.get('x_min')}..{bounds.get('x_max')}"
        if bounds
        else ""
    )
    return f"""<section class="aperture-card">
  <div class="card-head">
    <div class="title">{escape(str(title))}</div>
    <div class="meta">sector {escape(str(sector))}</div>
  </div>
  <div class="details">
    <div><span class="label">Source</span>{escape(str(diagnostic.get("source_file", "")))}</div>
    <div><span class="label">Shape</span>{height} x {width}</div>
    <div><span class="label">Mode</span>{escape(str(diagnostic.get("aperture_mode", "")))}</div>
    <div><span class="label">Selected pixels</span>{escape(str(diagnostic.get("aperture_n_pixels", "")))}</div>
    <div><span class="label">Bounds</span>{escape(bounds_text)}</div>
    <div><span class="label">Background</span>{escape(str(diagnostic.get("background_method", "")))}</div>
    <div><span class="label">Pixel color</span>median flux</div>
  </div>
  <div class="pixel-grid" style="--width: {max(width, 1)}">
    {"".join(cells)}
  </div>
</section>"""


def diagnostic_heatmap(diagnostic: dict[str, Any], *, width: int, height: int) -> list[list[str | None]]:
    image = diagnostic.get("median_flux_image")
    if not isinstance(image, list):
        return [[None for _ in range(width)] for _ in range(height)]
    finite_values: list[float] = []
    parsed: list[list[float | None]] = []
    for y in range(height):
        source_row = image[y] if y < len(image) and isinstance(image[y], list) else []
        row: list[float | None] = []
        for x in range(width):
            value = source_row[x] if x < len(source_row) else None
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = math.nan
            if math.isfinite(number):
                row.append(number)
                finite_values.append(number)
            else:
                row.append(None)
        parsed.append(row)
    if not finite_values:
        return [[None for _ in range(width)] for _ in range(height)]
    low = min(finite_values)
    high = max(finite_values)
    span = high - low
    heatmap: list[list[str | None]] = []
    for row in parsed:
        heat_row: list[str | None] = []
        for value in row:
            if value is None:
                heat_row.append(None)
                continue
            fraction = 0.5 if span == 0 else (value - low) / span
            heat_row.append(gray_heat_color(fraction))
        heatmap.append(heat_row)
    return heatmap


def gray_heat_color(fraction: float) -> str:
    clipped = min(1.0, max(0.0, fraction))
    level = int(round(245 - clipped * 185))
    return f"rgb({level}, {level}, {level})"


def select_aperture_pixels(
    width: int,
    height: int,
    flux_cube: Any,
    *,
    mode: str,
    radius: float,
    pixel_spec: str | None,
    threshold_sigma: float,
) -> list[tuple[int, int]]:
    if mode == "circle":
        return circular_aperture_pixels(width, height, radius)
    if mode == "pixels":
        return parse_pixel_mask(pixel_spec, width=width, height=height)
    if mode == "threshold":
        return threshold_aperture_pixels(flux_cube, width=width, height=height, sigma=threshold_sigma)
    raise RuntimeError(f"unsupported cutout aperture mode: {mode}")


def circular_aperture_pixels(width: int, height: int, radius: float) -> list[tuple[int, int]]:
    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    pixels: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            if math.hypot(x - center_x, y - center_y) <= radius:
                pixels.append((y, x))
    return pixels


def parse_pixel_mask(spec: str | None, *, width: int, height: int) -> list[tuple[int, int]]:
    if spec is None or not spec.strip():
        raise RuntimeError("--cutout-aperture-pixels is required when aperture mode is pixels")
    pixels: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for token in spec.replace(" ", "").split(";"):
        if not token:
            continue
        parts = token.split(",")
        if len(parts) != 2:
            raise RuntimeError(f"invalid aperture pixel {token!r}; expected y,x")
        try:
            y, x = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise RuntimeError(f"invalid aperture pixel {token!r}; expected integer y,x") from exc
        if y < 0 or y >= height or x < 0 or x >= width:
            raise RuntimeError(f"aperture pixel {token!r} is outside cutout shape {height}x{width}")
        pixel = (y, x)
        if pixel not in seen:
            seen.add(pixel)
            pixels.append(pixel)
    if not pixels:
        raise RuntimeError("aperture pixel mask selected no pixels")
    return pixels


def threshold_aperture_pixels(flux_cube: Any, *, width: int, height: int, sigma: float) -> list[tuple[int, int]]:
    median_image = median_flux_image(flux_cube, width=width, height=height)
    image_values: list[float] = []
    for row in median_image:
        image_values.extend(value for value in row if value is not None)
    if not image_values:
        raise RuntimeError("threshold aperture could not find finite cutout pixels")
    background = median(image_values)
    deviations = [abs(value - background) for value in image_values]
    robust_sigma = 1.4826 * median(deviations) if deviations else 0.0
    if robust_sigma == 0.0:
        robust_sigma = standard_deviation(image_values)
    threshold = background + float(sigma) * robust_sigma
    pixels = [
        (y, x)
        for y, row in enumerate(median_image)
        for x, value in enumerate(row)
        if value is not None and value > threshold
    ]
    if not pixels:
        raise RuntimeError("threshold aperture selected no pixels")
    return pixels


def median_flux_image(flux_cube: Any, *, width: int, height: int) -> list[list[float | None]]:
    image: list[list[float | None]] = []
    for y in range(height):
        row: list[float | None] = []
        for x in range(width):
            values = finite_pixel_values_by_position(flux_cube, y, x)
            row.append(median(values) if values else None)
        image.append(row)
    return image


def outside_pixels(width: int, height: int, excluded: set[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(y, x) for y in range(height) for x in range(width) if (y, x) not in excluded]


def finite_pixel_values(frame: Any, pixels: Iterable[tuple[int, int]]) -> list[float]:
    values: list[float] = []
    for y, x in pixels:
        value = clean_scalar(frame[y][x])
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    return values


def finite_pixel_values_by_position(flux_cube: Any, y: int, x: int) -> list[float]:
    values: list[float] = []
    for index in range(len(flux_cube)):
        value = clean_scalar(flux_cube[index][y][x])
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    return values


def cutout_flux_error(table: Any, columns: dict[str, str], index: int, aperture_pixels: list[tuple[int, int]]) -> float | None:
    if "FLUX_ERR" not in columns:
        return None
    values = finite_pixel_values(table[columns["FLUX_ERR"]][index], aperture_pixels)
    if not values:
        return None
    return math.sqrt(sum(value * value for value in values))


def standard_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
