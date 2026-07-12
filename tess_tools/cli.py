from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from .cache import MetadataCache
from .inventory import INVENTORY_SCHEMA_VERSION, build_inventory
from .output import (
    flatten_crowding_neighbors,
    flatten_fetch_plans,
    flatten_products,
    flatten_providers,
    flatten_sectors,
    flatten_targets,
    print_summary,
)
from .target import parse_target_args, read_targets_csv
from .recommend import BEST_FOR_CHOICES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tess-where",
        description="Report which TESS sectors and public products appear to exist for a target.",
    )
    parser.add_argument(
        "target",
        nargs="+",
        help="TIC/TOI identifier, RA Dec pair, or CSV file with tic_id, toi_id, or ra/dec columns.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Write the full inventory document to this JSON file.",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        help=(
            "Write flattened CSV files using this path as a prefix. "
            "Creates target, sector, product, provider, fetch-plan, and crowding-neighbor CSV files."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache") / "metadata",
        help="Metadata cache directory. Defaults to tess-tools/.cache/metadata when run from the project.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached metadata and query providers again.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use cached metadata only. No network-backed providers will be queried.",
    )
    parser.add_argument(
        "--radius-arcsec",
        type=float,
        default=60.0,
        help="Search radius for product metadata around resolved coordinates.",
    )
    parser.add_argument(
        "--providers",
        default="tesscut",
        help="Comma-separated metadata providers to use. Supported: tesscut,mast,gaia,toi,tesseb.",
    )
    parser.add_argument(
        "--toi-catalog",
        type=Path,
        help=(
            "Optional local TOI catalog snapshot as CSV, TSV, or JSON. "
            "Used for TOI target resolution and when the toi provider is enabled."
        ),
    )
    parser.add_argument(
        "--tess-eb-catalog",
        type=Path,
        help="Optional local TESS EB catalog snapshot as CSV, TSV, or JSON. Used only when the tesseb provider is enabled.",
    )
    parser.add_argument(
        "--best-for",
        choices=BEST_FOR_CHOICES,
        default="general",
        help="Science mode used to build the future tess-fetch recommendation.",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=0.0,
        help="Sleep this many seconds between targets in CSV batch mode.",
    )
    parser.add_argument(
        "--max-targets",
        type=int,
        help="Process at most this many targets from a CSV batch. Useful for smoke tests.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    target_tokens = args.target
    possible_csv = Path(target_tokens[0])
    if len(target_tokens) == 1 and possible_csv.exists() and possible_csv.suffix.lower() == ".csv":
        target_specs = read_targets_csv(possible_csv)
    else:
        target_specs = [parse_target_args(target_tokens)]
    if args.max_targets is not None:
        target_specs = target_specs[: args.max_targets]
    try:
        enabled_providers = parse_providers(args.providers)
    except ValueError as exc:
        parser.error(str(exc))

    cache = MetadataCache(args.cache_dir)
    inventories = []
    for index, spec in enumerate(target_specs):
        try:
            inventories.append(
                build_inventory(
                    spec,
                    cache=cache,
                    refresh=args.refresh,
                    offline=args.offline,
                    radius_arcsec=args.radius_arcsec,
                    enabled_providers=enabled_providers,
                    best_for=args.best_for,
                    toi_catalog_path=args.toi_catalog,
                    tess_eb_catalog_path=args.tess_eb_catalog,
                )
            )
        except Exception as exc:
            inventories.append(
                {
                    "schema_version": INVENTORY_SCHEMA_VERSION,
                    "input": spec.raw,
                    "target": spec.to_dict(),
                    "sectors": [],
                    "products": [],
                    "providers": [],
                    "fetch_plan": None,
                    "recommendations": [],
                    "errors": [f"inventory build failed: {exc}"],
                    "cache": {"hit": False, "key": None},
                }
            )
        if args.sleep_sec > 0 and index < len(target_specs) - 1:
            time.sleep(args.sleep_sec)

    document = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "targets": inventories,
    }

    print_summary(document, stream=sys.stdout)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")

    if args.csv_out:
        write_csv_outputs(args.csv_out, document)

    return 0


def parse_providers(raw: str) -> set[str]:
    aliases = {"tess-eb": "tesseb", "tess_eb": "tesseb"}
    providers = {aliases.get(item.strip().lower(), item.strip().lower()) for item in raw.split(",") if item.strip()}
    supported = {"tesscut", "mast", "gaia", "toi", "tesseb"}
    unknown = sorted(providers - supported)
    if unknown:
        raise ValueError(f"unsupported providers: {', '.join(unknown)}")
    return providers


def write_csv_outputs(prefix: Path, document: dict) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    sector_rows = flatten_sectors(document)
    product_rows = flatten_products(document)
    target_rows = flatten_targets(document)
    provider_rows = flatten_providers(document)
    fetch_plan_rows = flatten_fetch_plans(document)
    crowding_neighbor_rows = flatten_crowding_neighbors(document)

    write_csv(prefix.with_name(prefix.name + "_targets.csv"), target_rows)
    write_csv(prefix.with_name(prefix.name + "_sectors.csv"), sector_rows)
    write_csv(prefix.with_name(prefix.name + "_products.csv"), product_rows)
    write_csv(prefix.with_name(prefix.name + "_providers.csv"), provider_rows)
    write_csv(prefix.with_name(prefix.name + "_fetch_plans.csv"), fetch_plan_rows)
    write_csv(prefix.with_name(prefix.name + "_crowding_neighbors.csv"), crowding_neighbor_rows)


def write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fieldnames:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
