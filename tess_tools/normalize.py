from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable


TIME_COLUMN = "TIME"
QUALITY_COLUMN = "QUALITY"
FLUX_COLUMN_CANDIDATES = ("PDCSAP_FLUX", "SAP_FLUX", "KSPSAP_FLUX", "FLUX")
FLUX_ERR_COLUMN_CANDIDATES = ("PDCSAP_FLUX_ERR", "SAP_FLUX_ERR", "KSPSAP_FLUX_ERR", "FLUX_ERR")
BASE_NORMALIZED_COLUMNS = [
    "tic_id",
    "sector",
    "cadence_sec",
    "product",
    "time_btjd",
    "flux",
    "flux_err",
    "quality",
    "source_file",
]
DEFAULT_PRESERVED_COLUMNS = (
    "CADENCENO",
    "TIMECORR",
    "SAP_FLUX",
    "SAP_FLUX_ERR",
    "PDCSAP_FLUX",
    "PDCSAP_FLUX_ERR",
    "KSPSAP_FLUX",
    "KSPSAP_FLUX_ERR",
    "KSPSAP_FLUX_SML",
    "KSPSAP_FLUX_LAG",
    "ORBITID",
    "SAP_X",
    "SAP_Y",
    "SAP_BKG",
    "SAP_BKG_ERR",
    "MOM_CENTR1",
    "MOM_CENTR1_ERR",
    "MOM_CENTR2",
    "MOM_CENTR2_ERR",
    "PSF_CENTR1",
    "PSF_CENTR1_ERR",
    "PSF_CENTR2",
    "PSF_CENTR2_ERR",
    "POS_CORR1",
    "POS_CORR2",
)
QUALITY_MASK_PRESETS: dict[str, int | None] = {
    "none": None,
    "bit0": 1,
    "conservative": 65535,
    "spoc-recommended": 21183,
    "tess-spoc-recommended": 21183,
    "qlp-recommended": 7357,
}
CONTEXTUAL_QUALITY_PRESETS = {"recommended"}
QUALITY_PRESET_CHOICES = tuple(sorted((*QUALITY_MASK_PRESETS, *CONTEXTUAL_QUALITY_PRESETS)))
RECOMMENDED_PRESET_BY_PRODUCT = {
    "spoc": "spoc-recommended",
    "tess-spoc": "tess-spoc-recommended",
    "qlp": "qlp-recommended",
    "cutout": "spoc-recommended",
    "tesscut": "spoc-recommended",
}


def validate_quality_policy_args(raw_mask: int | None, preset: str | None) -> None:
    if raw_mask is not None and preset not in (None, "none"):
        raise ValueError("--quality-mask and --quality-preset cannot be used together")


def resolve_quality_mask(raw_mask: int | None, preset: str | None, *, product: str | None = None) -> int | None:
    validate_quality_policy_args(raw_mask, preset)
    if raw_mask is not None:
        return raw_mask
    if preset is None:
        return None
    preset = resolve_quality_preset_name(preset, product=product)
    try:
        return QUALITY_MASK_PRESETS[preset]
    except KeyError as exc:
        choices = ", ".join(QUALITY_PRESET_CHOICES)
        raise ValueError(f"unknown quality preset {preset!r}; expected one of: {choices}") from exc


def resolve_quality_preset_name(preset: str | None, *, product: str | None = None) -> str | None:
    if preset is None:
        return None
    normalized = preset.strip().lower()
    if normalized == "recommended":
        product_key = normalize_product_key(product)
        if product_key in RECOMMENDED_PRESET_BY_PRODUCT:
            return RECOMMENDED_PRESET_BY_PRODUCT[product_key]
        choices = ", ".join(sorted(RECOMMENDED_PRESET_BY_PRODUCT))
        raise ValueError(f"quality preset 'recommended' needs a known product; expected one of: {choices}")
    if normalized in QUALITY_MASK_PRESETS:
        return normalized
    choices = ", ".join(QUALITY_PRESET_CHOICES)
    raise ValueError(f"unknown quality preset {preset!r}; expected one of: {choices}")


def quality_policy_for_item(
    item: dict[str, Any],
    *,
    raw_mask: int | None = None,
    preset: str | None = None,
) -> dict[str, Any]:
    resolved_preset = resolve_quality_preset_name(preset, product=item.get("product")) if preset is not None else None
    mask = resolve_quality_mask(raw_mask, preset, product=item.get("product"))
    policy: dict[str, Any] = {}
    if raw_mask is not None:
        policy["quality_mask"] = raw_mask
        return policy
    if preset is not None:
        policy["quality_preset"] = preset
    if resolved_preset is not None and resolved_preset != "none":
        policy["quality_policy"] = resolved_preset
    if mask is not None:
        policy["quality_mask"] = mask
    return policy


def normalize_product_key(product: str | None) -> str | None:
    if product is None:
        return None
    return str(product).strip().lower().replace("_", "-")


def normalize_lightcurve_file(path: Path, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from astropy.io import fits
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("normalizing FITS light curves requires astropy") from exc

    with fits.open(path) as hdul:
        table = hdul[1].data
        columns = list(table.columns.names)
        source_rows = ({column: table[column][index] for column in columns} for index in range(len(table)))
        quality_filter_stats: dict[str, int] = {}
        rows = normalize_lightcurve_rows(
            source_rows,
            columns=columns,
            metadata=metadata,
            source_file=path,
            quality_mask=metadata.get("quality_mask"),
            quality_filter_stats=quality_filter_stats,
        )
        metadata["normalization_input_rows"] = quality_filter_stats.get("input_rows", 0)
        metadata["normalization_quality_dropped_rows"] = quality_filter_stats.get("quality_dropped_rows", 0)
        return rows


def normalize_lightcurve_rows(
    source_rows: Iterable[dict[str, Any]],
    *,
    columns: Iterable[str],
    metadata: dict[str, Any],
    source_file: Path | str,
    preserve_columns: Iterable[str] = DEFAULT_PRESERVED_COLUMNS,
    quality_mask: int | None = None,
    quality_filter_stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    column_names = {str(column).upper(): str(column) for column in columns}
    flux_column = choose_column(column_names, FLUX_COLUMN_CANDIDATES)
    flux_err_column = choose_column(column_names, FLUX_ERR_COLUMN_CANDIDATES)
    time_column = column_names.get(TIME_COLUMN)
    quality_column = column_names.get(QUALITY_COLUMN)
    if time_column is None or flux_column is None:
        raise RuntimeError("light-curve table must contain TIME and a recognized flux column")
    preserved = preserved_column_map(column_names, preserve_columns)

    rows: list[dict[str, Any]] = []
    for source in source_rows:
        if quality_filter_stats is not None:
            quality_filter_stats["input_rows"] = quality_filter_stats.get("input_rows", 0) + 1
        quality = clean_scalar(source.get(quality_column)) if quality_column else None
        if should_skip_quality(quality, quality_mask):
            if quality_filter_stats is not None:
                quality_filter_stats["quality_dropped_rows"] = quality_filter_stats.get("quality_dropped_rows", 0) + 1
            continue
        row = {
            "tic_id": metadata.get("tic_id"),
            "sector": metadata.get("sector"),
            "cadence_sec": metadata.get("cadence_sec"),
            "product": metadata.get("product"),
            "time_btjd": clean_scalar(source.get(time_column)),
            "flux": clean_scalar(source.get(flux_column)),
            "flux_err": clean_scalar(source.get(flux_err_column)) if flux_err_column else None,
            "quality": quality,
            "source_file": str(source_file),
        }
        for output_name, source_name in preserved.items():
            row[output_name] = clean_scalar(source.get(source_name))
        rows.append(row)
    if quality_filter_stats is not None:
        quality_filter_stats["output_rows"] = len(rows)
    return rows


def choose_column(column_names: dict[str, str], candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        if candidate in column_names:
            return column_names[candidate]
    return None


def preserved_column_map(column_names: dict[str, str], preserve_columns: Iterable[str]) -> dict[str, str]:
    preserved: dict[str, str] = {}
    for column in preserve_columns:
        canonical = str(column).upper()
        source_name = column_names.get(canonical)
        if source_name is None:
            continue
        output_name = "raw_" + canonical.lower()
        preserved[output_name] = source_name
    return preserved


def write_normalized_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra_fieldnames = sorted({key for row in rows for key in row if key not in BASE_NORMALIZED_COLUMNS})
    fieldnames = BASE_NORMALIZED_COLUMNS + extra_fieldnames
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def should_skip_quality(quality: Any, quality_mask: int | None) -> bool:
    if quality_mask is None or quality is None:
        return False
    try:
        return (int(quality) & int(quality_mask)) != 0
    except (TypeError, ValueError):
        return False


def clean_scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    return value
