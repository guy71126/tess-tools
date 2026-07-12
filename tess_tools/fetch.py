from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cutout import extract_cutout_manifest_files
from .mast_api import USER_AGENT
from .normalize import (
    QUALITY_PRESET_CHOICES,
    normalize_lightcurve_file,
    quality_policy_for_item,
    resolve_quality_mask,
    validate_quality_policy_args,
    write_normalized_csv,
)


DEFAULT_FILE_ROLE = "lightcurve"
TESSCUT_ASTROCUT_URL = "https://mast.stsci.edu/tesscut/api/v0.1/astrocut"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tess-fetch",
        description="Download selected TESS products from a tess-where JSON inventory.",
    )
    parser.add_argument("inventory", type=Path, help="tess-where JSON inventory.")
    parser.add_argument("--out-dir", type=Path, default=Path("tess-fetch-downloads"), help="Output directory.")
    parser.add_argument("--target", help="Only fetch inventory entries matching this target label, TIC ID, or input.")
    parser.add_argument("--product", help="Only fetch plans whose recommended product matches this key.")
    parser.add_argument("--sectors", default="all", help="Comma-separated sectors, or all.")
    parser.add_argument(
        "--file-role",
        default=DEFAULT_FILE_ROLE,
        help="Selected reference role to fetch. Use all to disable role filtering. Defaults to lightcurve.",
    )
    parser.add_argument("--max-files", type=int, help="Fetch at most this many selected files.")
    parser.add_argument(
        "--cutout-size",
        type=int,
        default=5,
        help="Square TESSCut cutout size in pixels when fetching product=cutout. Defaults to 5.",
    )
    parser.add_argument(
        "--keep-cutout-zip",
        action="store_true",
        help="Keep the downloaded TESSCut zip archive after extracting FITS files.",
    )
    parser.add_argument(
        "--cutout-lightcurve-csv",
        type=Path,
        help="Extract a simple aperture-sum light-curve CSV from downloaded TESSCut FITS cutouts. Requires astropy.",
    )
    parser.add_argument(
        "--cutout-aperture-mode",
        choices=("circle", "pixels", "threshold"),
        default="circle",
        help="Aperture mode for --cutout-lightcurve-csv. Defaults to circle.",
    )
    parser.add_argument(
        "--cutout-aperture-radius",
        type=float,
        default=1.5,
        help="Circular aperture radius in pixels when --cutout-aperture-mode circle. Defaults to 1.5.",
    )
    parser.add_argument(
        "--cutout-aperture-pixels",
        help="Semicolon-separated zero-based y,x pixels when --cutout-aperture-mode pixels, for example 2,2;2,3;3,2.",
    )
    parser.add_argument(
        "--cutout-threshold-sigma",
        type=float,
        default=3.0,
        help="Median-image threshold in robust sigma when --cutout-aperture-mode threshold. Defaults to 3.",
    )
    parser.add_argument(
        "--cutout-background",
        choices=("none", "median-outside"),
        default="none",
        help="Background method for --cutout-lightcurve-csv. Defaults to none.",
    )
    parser.add_argument(
        "--cutout-aperture-summary-json",
        type=Path,
        help="Write per-cutout aperture diagnostics JSON when extracting TESSCut light curves.",
    )
    parser.add_argument(
        "--cutout-aperture-mask-csv",
        type=Path,
        help="Write per-pixel aperture mask CSV when extracting TESSCut light curves.",
    )
    parser.add_argument(
        "--cutout-aperture-report-html",
        type=Path,
        help="Write a compact HTML aperture preview report when extracting TESSCut light curves.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print/write the manifest without downloading files.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files.")
    parser.add_argument("--resume", action="store_true", help="Resume partial files when possible.")
    parser.add_argument("--manifest-out", type=Path, help="Write fetch manifest JSON to this path.")
    parser.add_argument("--csv-out", type=Path, help="Write fetch manifest CSV to this path.")
    parser.add_argument(
        "--normalize-csv",
        type=Path,
        help="Write a normalized light-curve CSV after downloading or verifying selected FITS files. Requires astropy.",
    )
    parser.add_argument(
        "--quality-mask",
        type=int,
        help="When normalizing, drop rows where QUALITY & mask is nonzero. Example: 1 drops rows with bit 0 set.",
    )
    parser.add_argument(
        "--quality-preset",
        choices=QUALITY_PRESET_CHOICES,
        help=(
            "Named QUALITY filtering policy for normalization and cutout extraction: "
            "none=no filtering, recommended=provider-specific default, bit0=drop bit 0, "
            "conservative=drop rows with any low 16 bits set."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_quality_policy_args(args.quality_mask, args.quality_preset)
    except ValueError as exc:
        parser.error(str(exc))
    document = load_inventory(args.inventory)
    sector_filter = parse_sector_filter(args.sectors)
    manifest = build_fetch_manifest(
        document,
        target_filter=args.target,
        product_filter=args.product,
        sector_filter=sector_filter,
        file_role=args.file_role,
        out_dir=args.out_dir,
        max_files=args.max_files,
        cutout_size=args.cutout_size,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        for item in manifest["files"]:
            try:
                download_reference(item, overwrite=args.overwrite, resume=args.resume, keep_cutout_zip=args.keep_cutout_zip)
            except Exception as exc:
                item["status"] = "error"
                item["error"] = str(exc)
        if args.normalize_csv:
            normalize_manifest_files(
                manifest["files"],
                args.normalize_csv,
                quality_mask=args.quality_mask,
                quality_preset=args.quality_preset,
            )
        if args.cutout_lightcurve_csv:
            extract_cutout_manifest_files(
                manifest["files"],
                args.cutout_lightcurve_csv,
                aperture_mode=args.cutout_aperture_mode,
                aperture_radius=args.cutout_aperture_radius,
                aperture_pixels=args.cutout_aperture_pixels,
                threshold_sigma=args.cutout_threshold_sigma,
                background=args.cutout_background,
                quality_mask=args.quality_mask,
                quality_preset=args.quality_preset,
                aperture_summary_json=args.cutout_aperture_summary_json,
                aperture_mask_csv=args.cutout_aperture_mask_csv,
                aperture_report_html=args.cutout_aperture_report_html,
            )
    record_fetch_outputs(
        manifest,
        normalize_csv=args.normalize_csv,
        cutout_lightcurve_csv=args.cutout_lightcurve_csv,
        cutout_aperture_summary_json=args.cutout_aperture_summary_json,
        cutout_aperture_mask_csv=args.cutout_aperture_mask_csv,
        cutout_aperture_report_html=args.cutout_aperture_report_html,
        quality_mask=args.quality_mask,
        quality_preset=args.quality_preset,
    )

    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest_out.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if args.csv_out:
        write_manifest_csv(args.csv_out, manifest["files"])

    print_fetch_summary(manifest, dry_run=args.dry_run)
    return (
        1
        if any(
            item.get("status") == "error"
            or item.get("normalization_status") == "error"
            or item.get("cutout_lightcurve_status") == "error"
            for item in manifest["files"]
        )
        else 0
    )


def load_inventory(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_fetch_manifest(
    document: dict[str, Any],
    *,
    target_filter: str | None = None,
    product_filter: str | None = None,
    sector_filter: set[int] | None = None,
    file_role: str = DEFAULT_FILE_ROLE,
    out_dir: Path,
    max_files: int | None = None,
    cutout_size: int = 5,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        fetch_plan = inventory.get("fetch_plan") or {}
        if not fetch_plan:
            continue
        if target_filter and not target_matches(inventory, fetch_plan, target_filter):
            continue
        if product_filter and fetch_plan.get("product") != product_filter:
            continue
        if fetch_plan.get("product") == "cutout":
            for reference in cutout_references(inventory, fetch_plan, sector_filter=sector_filter, file_role=file_role, cutout_size=cutout_size):
                files.append(build_manifest_file(inventory, fetch_plan, reference, out_dir))
                if max_files is not None and len(files) >= max_files:
                    break
            if max_files is not None and len(files) >= max_files:
                break
            continue
        for reference in fetch_plan.get("selected_product_references", []):
            if sector_filter is not None and reference.get("sector") not in sector_filter:
                continue
            if file_role != "all" and reference.get("file_role") != file_role:
                continue
            files.append(build_manifest_file(inventory, fetch_plan, reference, out_dir))
            if max_files is not None and len(files) >= max_files:
                break
        if max_files is not None and len(files) >= max_files:
            break
    return {
        "schema_version": "tess-fetch.manifest.v0.1",
        "source_schema_version": document.get("schema_version"),
        "n_files": len(files),
        "files": files,
    }


def build_manifest_file(inventory: dict[str, Any], fetch_plan: dict[str, Any], reference: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    target = inventory.get("target", {})
    filename = reference.get("file_name") or filename_from_reference(reference)
    target_dir = safe_path_part(target.get("tic_id") or fetch_plan.get("target_label") or inventory.get("input") or "target")
    destination = out_dir / target_dir / safe_path_part(str(filename))
    return {
        "input": inventory.get("input"),
        "target_label": fetch_plan.get("target_label"),
        "tic_id": target.get("tic_id"),
        "product": fetch_plan.get("product"),
        "sector": reference.get("sector"),
        "cadence_sec": reference.get("cadence_sec") or reference.get("file_cadence_sec"),
        "file_role": reference.get("file_role"),
        "file_product_subgroup": reference.get("file_product_subgroup"),
        "file_name": filename,
        "expected_size": parse_optional_int(reference.get("file_size")),
        "expected_sha256": reference.get("sha256") or reference.get("checksum_sha256"),
        "fetch_reference_kind": reference.get("fetch_reference_kind"),
        "fetch_reference": reference.get("fetch_reference"),
        "access_url": reference.get("access_url"),
        "destination": str(destination),
        "status": "planned",
    }


def cutout_references(
    inventory: dict[str, Any],
    fetch_plan: dict[str, Any],
    *,
    sector_filter: set[int] | None,
    file_role: str,
    cutout_size: int,
) -> list[dict[str, Any]]:
    if file_role not in {DEFAULT_FILE_ROLE, "all", "cutout", "target-pixel"}:
        return []
    target = inventory.get("target", {})
    ra_deg = target.get("ra_deg")
    dec_deg = target.get("dec_deg")
    if ra_deg is None or dec_deg is None:
        return []
    references = []
    for sector in fetch_plan.get("sectors", []):
        sector_value = int(sector)
        if sector_filter is not None and sector_value not in sector_filter:
            continue
        references.append(build_tesscut_reference(ra_deg, dec_deg, sector_value, cutout_size))
    return references


def build_tesscut_reference(ra_deg: float, dec_deg: float, sector: int, cutout_size: int) -> dict[str, Any]:
    params = {
        "ra": ra_deg,
        "dec": dec_deg,
        "x": int(cutout_size),
        "y": int(cutout_size),
        "units": "px",
        "sector": int(sector),
    }
    access_url = f"{TESSCUT_ASTROCUT_URL}?{urlencode(params)}"
    return {
        "family": "TESSCut",
        "provider": "tesscut",
        "sector": int(sector),
        "cadence_sec": None,
        "product_id": f"tesscut-s{int(sector):04d}-{int(cutout_size)}x{int(cutout_size)}",
        "product_scope": "per_sector",
        "sector_start": int(sector),
        "sector_end": int(sector),
        "fetch_reference_kind": "tesscut_astrocut",
        "fetch_reference": access_url,
        "access_url": access_url,
        "file_name": f"tesscut-s{int(sector):04d}-{int(cutout_size)}x{int(cutout_size)}.zip",
        "file_role": "cutout",
        "file_product_subgroup": "TESSCUT",
        "cutout_size": int(cutout_size),
    }


def download_reference(
    item: dict[str, Any],
    *,
    overwrite: bool = False,
    resume: bool = False,
    keep_cutout_zip: bool = False,
) -> None:
    if item.get("fetch_reference_kind") == "tesscut_astrocut":
        download_tesscut_cutout(item, overwrite=overwrite, keep_zip=keep_cutout_zip)
        return
    access_url = item.get("access_url")
    if not access_url:
        raise RuntimeError(f"no access_url for {item.get('file_name')}")
    destination = Path(item["destination"])
    expected_size = parse_optional_int(item.get("expected_size"))
    expected_sha256 = normalize_hash(item.get("expected_sha256"))
    if destination.exists() and not overwrite and not resume:
        actual_size = destination.stat().st_size
        item["local_size"] = actual_size
        if expected_size is not None and actual_size != expected_size:
            raise RuntimeError(f"existing file size {actual_size} did not match expected {expected_size}")
        record_local_sha256(item, destination, expected_sha256=expected_sha256)
        item["status"] = "exists_verified" if expected_size is not None else "exists"
        return
    destination.parent.mkdir(parents=True, exist_ok=True)

    resume_from = 0
    if resume and destination.exists() and not overwrite:
        resume_from = destination.stat().st_size
        if expected_size is not None and resume_from == expected_size:
            item["local_size"] = resume_from
            record_local_sha256(item, destination, expected_sha256=expected_sha256)
            item["status"] = "exists_verified"
            return

    headers = {"User-Agent": USER_AGENT}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"
    request = Request(access_url, headers=headers)
    with urlopen(request, timeout=60) as response:
        response_code = response.getcode() if hasattr(response, "getcode") else None
        mode = "ab" if resume_from > 0 and response_code == 206 else "wb"
        with destination.open(mode) as handle:
            shutil.copyfileobj(response, handle)

    actual_size = destination.stat().st_size
    item["local_size"] = actual_size
    if expected_size is not None and actual_size != expected_size:
        raise RuntimeError(f"downloaded file size {actual_size} did not match expected {expected_size}")
    record_local_sha256(item, destination, expected_sha256=expected_sha256)
    item["status"] = "resumed" if resume_from > 0 and response_code == 206 else "downloaded"


def download_tesscut_cutout(item: dict[str, Any], *, overwrite: bool = False, keep_zip: bool = False) -> None:
    access_url = item.get("access_url")
    if not access_url:
        raise RuntimeError(f"no access_url for {item.get('file_name')}")
    destination = Path(item["destination"]).with_suffix("")
    existing_files = sorted(path for path in destination.glob("*.fits") if path.is_file()) if destination.exists() else []
    if existing_files and not overwrite:
        item["local_paths"] = [str(path) for path in existing_files]
        item["n_local_files"] = len(existing_files)
        record_local_path_hashes(item, existing_files)
        item["status"] = "exists"
        return

    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination.with_suffix(".zip")
    request = Request(access_url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        with archive_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)

    if not zipfile.is_zipfile(archive_path):
        message = archive_path.read_text(encoding="utf-8", errors="replace")
        if not keep_zip:
            archive_path.unlink(missing_ok=True)
        raise RuntimeError(f"TESSCut did not return a zip archive: {message[:500]}")

    extracted_paths = extract_zip_safely(archive_path, destination)
    if not keep_zip:
        archive_path.unlink(missing_ok=True)
    item["archive_path"] = str(archive_path) if keep_zip else None
    item["local_paths"] = [str(path) for path in extracted_paths]
    item["n_local_files"] = len(extracted_paths)
    record_local_path_hashes(item, extracted_paths)
    item["status"] = "downloaded"


def extract_zip_safely(archive_path: Path, destination: Path) -> list[Path]:
    destination_root = destination.resolve()
    extracted_paths: list[Path] = []
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            target_path = (destination / member.filename).resolve()
            if destination_root != target_path and destination_root not in target_path.parents:
                raise RuntimeError(f"refusing to extract zip member outside destination: {member.filename}")
            archive.extract(member, destination)
            extracted_paths.append(target_path)
    return sorted(extracted_paths)


def record_local_sha256(item: dict[str, Any], path: Path, *, expected_sha256: str | None = None) -> None:
    digest = file_sha256(path)
    item["local_sha256"] = digest
    if expected_sha256 is not None and digest.lower() != expected_sha256.lower():
        raise RuntimeError(f"local sha256 {digest} did not match expected {expected_sha256}")


def record_local_path_hashes(item: dict[str, Any], paths: list[Path]) -> None:
    item["local_hashes"] = [{"path": str(path), "sha256": file_sha256(path)} for path in paths]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_hash(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return text


def normalize_manifest_files(
    items: list[dict[str, Any]],
    output_path: Path,
    *,
    quality_mask: int | None = None,
    quality_preset: str | None = None,
) -> None:
    rows: list[dict[str, Any]] = []
    for item in items:
        if item.get("file_role") != DEFAULT_FILE_ROLE:
            item["normalization_status"] = "skipped_role"
            item["normalization_input_rows"] = 0
            item["normalization_rows"] = 0
            item["normalization_quality_dropped_rows"] = 0
            continue
        if item.get("status") not in {"downloaded", "resumed", "exists", "exists_verified"}:
            item["normalization_status"] = "skipped_status"
            item["normalization_input_rows"] = 0
            item["normalization_rows"] = 0
            item["normalization_quality_dropped_rows"] = 0
            continue
        try:
            policy = quality_policy_for_item(item, raw_mask=quality_mask, preset=quality_preset)
            item.update(policy)
            item_rows = normalize_lightcurve_file(Path(item["destination"]), item)
            rows.extend(item_rows)
            item["normalization_status"] = "ok"
            item["normalization_rows"] = len(item_rows)
            item.setdefault("normalization_input_rows", len(item_rows))
            item.setdefault("normalization_quality_dropped_rows", 0)
        except Exception as exc:
            item["normalization_status"] = "error"
            item["normalization_input_rows"] = 0
            item["normalization_rows"] = 0
            item["normalization_quality_dropped_rows"] = 0
            item["normalization_error"] = str(exc)
    write_normalized_csv(output_path, rows)


def record_fetch_outputs(
    manifest: dict[str, Any],
    *,
    normalize_csv: Path | None,
    cutout_lightcurve_csv: Path | None,
    cutout_aperture_summary_json: Path | None,
    cutout_aperture_mask_csv: Path | None,
    cutout_aperture_report_html: Path | None,
    quality_mask: int | None,
    quality_preset: str | None,
) -> None:
    outputs: dict[str, Any] = {}
    if normalize_csv is not None:
        outputs["normalize_csv"] = str(normalize_csv)
    if cutout_lightcurve_csv is not None:
        outputs["cutout_lightcurve_csv"] = str(cutout_lightcurve_csv)
    if cutout_aperture_summary_json is not None:
        outputs["cutout_aperture_summary_json"] = str(cutout_aperture_summary_json)
    if cutout_aperture_mask_csv is not None:
        outputs["cutout_aperture_mask_csv"] = str(cutout_aperture_mask_csv)
    if cutout_aperture_report_html is not None:
        outputs["cutout_aperture_report_html"] = str(cutout_aperture_report_html)
    if quality_mask is not None:
        outputs["quality_mask"] = quality_mask
    if quality_preset is not None:
        outputs["quality_preset"] = quality_preset
        if quality_preset != "recommended":
            outputs["quality_mask"] = resolve_quality_mask(quality_mask, quality_preset)
    if outputs:
        manifest["outputs"] = outputs


def parse_sector_filter(raw: str) -> set[int] | None:
    if raw.lower() == "all":
        return None
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        values.add(int(item))
    return values


def target_matches(inventory: dict[str, Any], fetch_plan: dict[str, Any], target_filter: str) -> bool:
    needle = target_filter.strip().lower()
    target = inventory.get("target", {})
    candidates = [
        inventory.get("input"),
        fetch_plan.get("target_label"),
        target.get("tic_id"),
        f"TIC {target.get('tic_id')}" if target.get("tic_id") else None,
    ]
    return any(str(candidate).lower() == needle for candidate in candidates if candidate is not None)


def filename_from_reference(reference: dict[str, Any]) -> str:
    value = str(reference.get("fetch_reference") or "product.fits")
    return value.rstrip("/").split("/")[-1]


def safe_path_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value).strip("_") or "item"


def parse_optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def print_fetch_summary(manifest: dict[str, Any], *, dry_run: bool) -> None:
    action = "Would fetch" if dry_run else "Fetched"
    print(f"{action} {manifest['n_files']} file(s)")
    for item in manifest["files"]:
        status = item.get("status", "planned")
        detail = f" [{status}]"
        if item.get("error"):
            detail += f" {item['error']}"
        print(
            f"  {item.get('target_label')}: sector {item.get('sector')} "
            f"{item.get('file_name')} -> {item.get('destination')}{detail}"
        )


def write_manifest_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fieldnames:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
