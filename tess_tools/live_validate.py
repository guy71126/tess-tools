from __future__ import annotations

import argparse
import csv
import json
import shlex
from pathlib import Path
from typing import Any

from .cache import MetadataCache
from .cli import parse_providers
from .cutout import extract_cutout_manifest_files
from .fetch import build_fetch_manifest, download_reference, normalize_manifest_files, parse_sector_filter, safe_path_part
from .inventory import build_inventory
from .target import parse_target_args


INFRASTRUCTURE_ERROR_MARKERS = (
    "connection aborted",
    "connection refused",
    "failed to establish a new connection",
    "failed to resolve",
    "getaddrinfo failed",
    "gateway timeout",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "internal server error",
    "max retries exceeded",
    "nameresolutionerror",
    "remotedisconnected",
    "requires astroquery",
    "astroquery unavailable",
    "temporary failure in name resolution",
    "timed out",
    "timeout",
    "urlopen error",
    "service unavailable",
)

NON_INFRASTRUCTURE_ERROR_MARKERS = (
    "bad request",
    "invalid column",
    "invalid parameter",
    "invalid service",
    "syntax error",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tess-where-live-validate",
        description="Run bounded live validation checks for tess-where targets.",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path("validation") / "live_targets.json",
        help="Validation manifest JSON. Defaults to validation/live_targets.json.",
    )
    parser.add_argument("--max-targets", type=int, help="Run at most this many manifest targets.")
    parser.add_argument("--json-out", type=Path, help="Write validation results to this JSON file.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache") / "live-validation",
        help="Metadata cache directory for validation runs.",
    )
    parser.add_argument("--refresh", action="store_true", help="Refresh cached inventory during validation.")
    parser.add_argument(
        "--include-fetch-smoke",
        action="store_true",
        help="Run opt-in fetch/normalization smoke checks declared in the manifest.",
    )
    parser.add_argument(
        "--fetch-smoke-dir",
        type=Path,
        default=Path(".cache") / "live-fetch-smoke",
        help="Directory for opt-in fetch smoke downloads and normalization outputs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    targets = manifest.get("targets", [])
    if args.max_targets is not None:
        targets = targets[: args.max_targets]

    cache = MetadataCache(args.cache_dir)
    results = []
    for entry in targets:
        result = run_manifest_entry(
            entry,
            cache=cache,
            refresh=args.refresh,
            include_fetch_smoke=args.include_fetch_smoke,
            fetch_smoke_dir=args.fetch_smoke_dir,
        )
        results.append(result)
        status = validation_status(result)
        print(f"{status} {result['name']}: {result['summary']}")
        for failure in result["failures"]:
            print(f"  - {failure}")

    passed = all(result["passed"] for result in results)
    infrastructure_error = any(result["infrastructure_error"] for result in results)
    expectation_failed = any(not result["passed"] and not result["infrastructure_error"] for result in results)
    document = {
        "schema_version": "tess-where.live-validation.v0.1",
        "manifest": str(args.manifest),
        "passed": passed,
        "infrastructure_error": infrastructure_error,
        "expectation_failed": expectation_failed,
        "results": results,
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if passed:
        return 0
    return 2 if infrastructure_error and not expectation_failed else 1


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_manifest_entry(
    entry: dict[str, Any],
    *,
    cache: MetadataCache,
    refresh: bool,
    include_fetch_smoke: bool = False,
    fetch_smoke_dir: Path | None = None,
) -> dict[str, Any]:
    target_tokens = target_tokens_from_entry(entry)
    providers = parse_providers(entry.get("providers", "tesscut"))
    spec = parse_target_args(target_tokens)
    inventory = build_inventory(
        spec,
        cache=cache,
        refresh=refresh,
        radius_arcsec=float(entry.get("radius_arcsec", 60.0)),
        enabled_providers=providers,
        best_for=entry.get("best_for", "general"),
        toi_catalog_path=Path(entry["toi_catalog"]) if entry.get("toi_catalog") else None,
        tess_eb_catalog_path=Path(entry["tess_eb_catalog"]) if entry.get("tess_eb_catalog") else None,
    )
    failures = evaluate_inventory(inventory, entry.get("expect", {}))
    fetch_smoke = None
    if entry.get("fetch_smoke"):
        fetch_smoke = run_fetch_smoke(
            inventory,
            entry["fetch_smoke"],
            name=entry.get("name", inventory.get("input", "target")),
            include=include_fetch_smoke,
            out_dir=fetch_smoke_dir or Path(".cache") / "live-fetch-smoke",
        )
        failures.extend(fetch_smoke.get("failures", []))
    infrastructure_error = bool(failures) and (is_infrastructure_error(inventory) or is_infrastructure_error_text(failures))
    summary = summarize_inventory(inventory)
    result = {
        "name": entry.get("name", inventory.get("input", "target")),
        "target": inventory.get("target", {}),
        "summary": summary,
        "passed": not failures,
        "infrastructure_error": infrastructure_error,
        "failures": failures,
        "inventory": inventory,
    }
    if fetch_smoke is not None:
        result["fetch_smoke"] = fetch_smoke
    return result


def run_fetch_smoke(
    inventory: dict[str, Any],
    config: dict[str, Any],
    *,
    name: str,
    include: bool,
    out_dir: Path,
) -> dict[str, Any]:
    if not include:
        return {"status": "skipped", "reason": "requires --include-fetch-smoke", "failures": []}
    smoke_dir = out_dir / safe_path_part(name)
    manifest = build_fetch_manifest(
        {"schema_version": inventory.get("schema_version"), "targets": [inventory]},
        target_filter=None,
        product_filter=config.get("product"),
        sector_filter=parse_smoke_sectors(config.get("sectors", "all")),
        file_role=config.get("file_role", "lightcurve"),
        out_dir=smoke_dir / "downloads",
        max_files=int(config.get("max_files", 1)),
    )
    failures: list[str] = []
    if manifest.get("n_files", 0) == 0:
        failures.append("fetch smoke selected no files")
    for item in manifest["files"]:
        try:
            download_reference(item, overwrite=bool(config.get("overwrite", False)), resume=bool(config.get("resume", True)))
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)
            failures.append(f"fetch smoke download failed for {item.get('file_name')}: {exc}")

    normalize_csv = smoke_dir / "normalized.csv"
    output_csv = None
    if config.get("normalize_csv", True):
        try:
            normalize_manifest_files(manifest["files"], normalize_csv, quality_mask=config.get("quality_mask"))
            output_csv = normalize_csv
        except Exception as exc:
            failures.append(f"fetch smoke normalization failed: {exc}")
        for item in manifest["files"]:
            if item.get("normalization_status") == "error":
                failures.append(f"fetch smoke normalization failed for {item.get('file_name')}: {item.get('normalization_error')}")

    cutout_lightcurve_csv = smoke_dir / "cutout_lightcurve.csv"
    if config.get("cutout_lightcurve_csv", False):
        try:
            extract_cutout_manifest_files(
                manifest["files"],
                cutout_lightcurve_csv,
                aperture_mode=str(config.get("cutout_aperture_mode", "circle")),
                aperture_radius=float(config.get("cutout_aperture_radius", 1.5)),
                aperture_pixels=config.get("cutout_aperture_pixels"),
                threshold_sigma=float(config.get("cutout_threshold_sigma", 3.0)),
                background=str(config.get("cutout_background", "none")),
                quality_mask=config.get("quality_mask"),
            )
            output_csv = cutout_lightcurve_csv
        except Exception as exc:
            failures.append(f"fetch smoke cutout light-curve extraction failed: {exc}")
        for item in manifest["files"]:
            if item.get("cutout_lightcurve_status") == "error":
                failures.append(
                    f"fetch smoke cutout light-curve extraction failed for {item.get('file_name')}: "
                    f"{item.get('cutout_lightcurve_error')}"
                )

    row_count = count_csv_rows(output_csv) if output_csv and output_csv.exists() else 0
    min_rows = config.get("expect_min_rows")
    if min_rows is not None and row_count < int(min_rows):
        failures.append(f"expected fetch smoke CSV rows >= {min_rows}, found {row_count}")
    expected_columns = [str(column) for column in config.get("expect_columns", [])]
    if expected_columns:
        columns = csv_columns(output_csv) if output_csv and output_csv.exists() else []
        missing = [column for column in expected_columns if column not in columns]
        if missing:
            failures.append(f"expected fetch smoke CSV columns {missing}, found {columns}")

    return {
        "status": "ok" if not failures else "failed",
        "failures": failures,
        "n_files": manifest.get("n_files", 0),
        "row_count": row_count,
        "manifest": manifest,
        "normalized_csv": str(normalize_csv),
        "cutout_lightcurve_csv": str(cutout_lightcurve_csv),
    }


def parse_smoke_sectors(raw: Any) -> set[int] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return parse_sector_filter(raw)
    if isinstance(raw, int):
        return {raw}
    return {int(value) for value in raw}


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return sum(1 for _ in reader)


def csv_columns(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or [])


def target_tokens_from_entry(entry: dict[str, Any]) -> list[str]:
    target = entry.get("target")
    if isinstance(target, list):
        return [str(item) for item in target]
    if isinstance(target, str):
        return shlex.split(target)
    raise ValueError("manifest entry target must be a string or list")


def evaluate_inventory(inventory: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    target = inventory.get("target", {})
    sector_summary = inventory.get("sector_summary", {})
    product_summary = inventory.get("product_summary", {})
    crowding_summary = inventory.get("crowding_summary", {})
    known_object_summary = inventory.get("known_object_summary", {})
    toi_summary = known_object_summary.get("toi", {})
    tess_eb_summary = known_object_summary.get("tess_eb", {})
    fetch_plan = inventory.get("fetch_plan") or {}

    min_sectors = expect.get("min_sectors")
    if min_sectors is not None and sector_summary.get("n_sectors", 0) < int(min_sectors):
        failures.append(f"expected at least {min_sectors} sectors, found {sector_summary.get('n_sectors', 0)}")

    tic_id = expect.get("tic_id")
    if tic_id is not None and str(target.get("tic_id")) != str(tic_id):
        failures.append(f"expected TIC {tic_id}, found {target.get('tic_id')}")

    source_contains = expect.get("target_source_contains")
    if source_contains and source_contains not in str(target.get("source", "")):
        failures.append(f"target source did not contain {source_contains!r}: {target.get('source')}")

    fetch_product = expect.get("fetch_product")
    if fetch_product and fetch_plan.get("product") != fetch_product:
        failures.append(f"expected fetch product {fetch_product!r}, found {fetch_plan.get('product')!r}")

    for key, min_value in expect.get("fetch_plan_min", {}).items():
        actual = fetch_plan.get(key, 0)
        if actual is None or int(actual) < int(min_value):
            failures.append(f"expected fetch_plan.{key} >= {min_value}, found {actual}")

    selected_references = fetch_plan.get("selected_product_references", [])
    selected_reference_values: dict[str, set[str]] = {}
    for reference in selected_references:
        if not isinstance(reference, dict):
            continue
        for key, value in reference.items():
            selected_reference_values.setdefault(key, set()).add(str(value))
    for key, expected_values in expect.get("selected_references_contains", {}).items():
        actual_values = selected_reference_values.get(key, set())
        for value in expected_values:
            if str(value) not in actual_values:
                failures.append(f"expected selected references {key} to contain {value!r}, found {sorted(actual_values)}")

    required_families = expect.get("product_families_contains", [])
    product_families = set(product_summary.get("families", []))
    for family in required_families:
        if family not in product_families:
            failures.append(f"expected product family {family!r}, found {sorted(product_families)}")

    for key, min_value in expect.get("product_summary_min", {}).items():
        actual = product_summary.get(key, 0)
        if actual is None or int(actual) < int(min_value):
            failures.append(f"expected product_summary.{key} >= {min_value}, found {actual}")

    for key, expected_values in expect.get("product_summary_contains", {}).items():
        actual_values = product_summary.get(key, [])
        if not isinstance(actual_values, list):
            actual_values = [actual_values]
        actual_set = {str(value) for value in actual_values}
        for value in expected_values:
            if str(value) not in actual_set:
                failures.append(f"expected product_summary.{key} to contain {value!r}, found {sorted(actual_set)}")

    for key, min_value in expect.get("crowding_summary_min", {}).items():
        actual = crowding_summary.get(key, 0)
        if actual is None or int(actual) < int(min_value):
            failures.append(f"expected crowding_summary.{key} >= {min_value}, found {actual}")

    for key, expected_values in expect.get("crowding_summary_contains", {}).items():
        actual_value = crowding_summary.get(key)
        if isinstance(expected_values, list):
            allowed = {str(value) for value in expected_values}
            if str(actual_value) not in allowed:
                failures.append(f"expected crowding_summary.{key} in {sorted(allowed)}, found {actual_value!r}")
        elif str(actual_value) != str(expected_values):
            failures.append(f"expected crowding_summary.{key}={expected_values!r}, found {actual_value!r}")

    for key, min_value in expect.get("known_object_summary_min", {}).items():
        actual = known_object_summary.get(key, 0)
        if actual is None or int(actual) < int(min_value):
            failures.append(f"expected known_object_summary.{key} >= {min_value}, found {actual}")

    for key, expected_values in expect.get("known_object_summary_contains", {}).items():
        actual_values = known_object_summary.get(key, [])
        if not isinstance(actual_values, list):
            actual_values = [actual_values]
        actual_set = {str(value) for value in actual_values}
        for value in expected_values:
            if str(value) not in actual_set:
                failures.append(f"expected known_object_summary.{key} to contain {value!r}, found {sorted(actual_set)}")

    for key, min_value in expect.get("toi_summary_min", {}).items():
        actual = toi_summary.get(key, 0)
        if actual is None or int(actual) < int(min_value):
            failures.append(f"expected toi_summary.{key} >= {min_value}, found {actual}")

    for key, expected_values in expect.get("toi_summary_contains", {}).items():
        actual_values = toi_summary.get(key, [])
        if not isinstance(actual_values, list):
            actual_values = [actual_values]
        actual_set = {str(value) for value in actual_values}
        for value in expected_values:
            if str(value) not in actual_set:
                failures.append(f"expected toi_summary.{key} to contain {value!r}, found {sorted(actual_set)}")

    for key, min_value in expect.get("tess_eb_summary_min", {}).items():
        actual = tess_eb_summary.get(key, 0)
        if actual is None or int(actual) < int(min_value):
            failures.append(f"expected tess_eb_summary.{key} >= {min_value}, found {actual}")

    provider_status = expect.get("provider_status", {})
    providers = {provider.get("name"): provider for provider in inventory.get("providers", [])}
    for name, status in provider_status.items():
        actual = providers.get(name, {}).get("status")
        if actual != status:
            failures.append(f"expected provider {name!r} status {status!r}, found {actual!r}")

    if expect.get("no_errors") and inventory.get("errors"):
        failures.append("expected no errors, found: " + " | ".join(inventory.get("errors", [])))

    return failures


def is_infrastructure_error(inventory: dict[str, Any]) -> bool:
    return is_infrastructure_error_text(inventory.get("errors", []))


def is_infrastructure_error_text(errors: list[str]) -> bool:
    haystack = "\n".join(str(error).lower() for error in errors)
    if not haystack:
        return False
    if any(marker in haystack for marker in NON_INFRASTRUCTURE_ERROR_MARKERS):
        return False
    return any(marker in haystack for marker in INFRASTRUCTURE_ERROR_MARKERS)


def validation_status(result: dict[str, Any]) -> str:
    if result["passed"]:
        return "PASS"
    if result.get("infrastructure_error"):
        return "INFRA"
    return "FAIL"


def summarize_inventory(inventory: dict[str, Any]) -> str:
    target = inventory.get("target", {})
    sector_summary = inventory.get("sector_summary", {})
    fetch_plan = inventory.get("fetch_plan") or {}
    label = f"TIC {target.get('tic_id')}" if target.get("tic_id") else inventory.get("input")
    return (
        f"{label}, sectors={sector_summary.get('n_sectors', 0)}, "
        f"product={fetch_plan.get('product')}, source={target.get('source')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
