from __future__ import annotations

from typing import TextIO


def print_summary(document: dict, *, stream: TextIO) -> None:
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        label = target.get("tic_id")
        if label:
            title = f"TIC {label}"
        elif target.get("ra_deg") is not None and target.get("dec_deg") is not None:
            title = f"{target['ra_deg']:.6f} {target['dec_deg']:.6f}"
        else:
            title = inventory.get("input", "target")

        sectors = inventory.get("sectors", [])
        products = inventory.get("products", [])
        sector_summary = inventory.get("sector_summary", {})
        product_summary = inventory.get("product_summary", {})
        crowding_summary = inventory.get("crowding_summary", {})
        known_object_summary = inventory.get("known_object_summary", {})
        recommendations = inventory.get("recommendations", [])
        fetch_plan = inventory.get("fetch_plan")
        errors = inventory.get("errors", [])
        cache = inventory.get("cache", {})

        print(title, file=stream)
        print(f"  Cache: {'hit' if cache.get('hit') else 'miss'} ({cache.get('key', 'no-key')})", file=stream)
        if target.get("tmag") is not None:
            print(f"  Tmag: {target['tmag']}", file=stream)
        if target.get("ra_deg") is not None and target.get("dec_deg") is not None:
            print(f"  Coordinates: RA {target['ra_deg']:.6f}, Dec {target['dec_deg']:.6f}", file=stream)
        n_sectors = sector_summary.get("n_sectors", len(sectors))
        print(f"  Sectors: {n_sectors}{format_sector_list(sectors)}", file=stream)
        if sector_summary.get("n_sector_groups"):
            print(
                "  Sector groups: "
                + ", ".join(format_sector_group(group) for group in sector_summary.get("sector_groups", [])),
                file=stream,
            )
        if sector_summary.get("cvz_like"):
            print("  Coverage note: CVZ-like sector count", file=stream)

        families = product_summary.get("families") or sorted(
            {product.get("family") for product in products if product.get("family")}
        )
        print(f"  Product families: {', '.join(families) if families else 'none detected'}", file=stream)
        if crowding_summary and crowding_summary.get("risk") != "unknown":
            print(
                "  Crowding: "
                f"{crowding_summary.get('risk')} risk, "
                f"{crowding_summary.get('n_neighbors', 0)} Gaia neighbor(s), "
                f"{crowding_summary.get('n_neighbors_within_1_pixel', 0)} within one TESS pixel",
                file=stream,
            )
        if known_object_summary and known_object_summary.get("n_matches", 0):
            print(
                "  Known objects: "
                f"{known_object_summary.get('n_matches', 0)} match(es) in "
                f"{', '.join(known_object_summary.get('catalogs', []))}",
                file=stream,
            )

        if fetch_plan:
            print(f"  Recommended future fetch: {fetch_plan.get('command')}", file=stream)
            print(f"    Reason: {fetch_plan.get('reason')}", file=stream)
            for caveat in fetch_plan.get("caveats", []):
                print(f"    Caveat: {caveat}", file=stream)
        elif recommendations:
            for rec in recommendations:
                print(f"  Recommended future fetch: {rec.get('command')}", file=stream)
                print(f"    Reason: {rec.get('reason')}", file=stream)
        else:
            print("  Recommended future fetch: unavailable", file=stream)

        for error in errors:
            print(f"  Warning: {error}", file=stream)
        print("", file=stream)


def format_sector_list(sectors: list[dict]) -> str:
    if not sectors:
        return ""
    values = sorted({sector.get("sector") for sector in sectors if sector.get("sector") is not None})
    if len(values) > 12:
        shown = ", ".join(str(value) for value in values[:12])
        return f" ({shown}, ...)"
    return " (" + ", ".join(str(value) for value in values) + ")"


def format_sector_group(group: dict) -> str:
    start = group.get("start")
    end = group.get("end")
    if start == end:
        return str(start)
    return f"{start}-{end}"


def flatten_targets(document: dict) -> list[dict]:
    rows: list[dict] = []
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        sector_summary = inventory.get("sector_summary", {})
        product_summary = inventory.get("product_summary", {})
        crowding_summary = inventory.get("crowding_summary", {})
        sector_geometry = crowding_summary.get("sector_geometry", {})
        known_object_summary = inventory.get("known_object_summary", {})
        toi_summary = known_object_summary.get("toi", {})
        tess_eb_summary = known_object_summary.get("tess_eb", {})
        cache = inventory.get("cache", {})
        rows.append(
            {
                "input": inventory.get("input"),
                "kind": target.get("kind"),
                "tic_id": target.get("tic_id"),
                "ra_deg": target.get("ra_deg"),
                "dec_deg": target.get("dec_deg"),
                "tmag": target.get("tmag"),
                "source": target.get("source"),
                "row_number": target.get("row_number"),
                "target_error": target.get("error"),
                "n_sectors": sector_summary.get("n_sectors", 0),
                "first_sector": sector_summary.get("first_sector"),
                "last_sector": sector_summary.get("last_sector"),
                "sector_span": sector_summary.get("sector_span", 0),
                "n_sector_groups": sector_summary.get("n_sector_groups", 0),
                "max_contiguous_sector_count": sector_summary.get("max_contiguous_sector_count", 0),
                "cvz_like": sector_summary.get("cvz_like", False),
                "sector_groups": ";".join(format_sector_group(group) for group in sector_summary.get("sector_groups", [])),
                "product_families": ",".join(product_summary.get("families", [])),
                "product_providers": ",".join(product_summary.get("providers", [])),
                "n_products": product_summary.get("n_products", 0),
                "n_preextracted_products": product_summary.get("n_preextracted_products", 0),
                "n_tesscut_products": product_summary.get("n_tesscut_products", 0),
                "n_multi_sector_products": product_summary.get("n_multi_sector_products", 0),
                "n_products_with_fetch_reference": product_summary.get("n_products_with_fetch_reference", 0),
                "n_preextracted_products_with_fetch_reference": product_summary.get(
                    "n_preextracted_products_with_fetch_reference", 0
                ),
                "n_products_with_file_uri": product_summary.get("n_products_with_file_uri", 0),
                "n_preextracted_products_with_file_uri": product_summary.get("n_preextracted_products_with_file_uri", 0),
                "fetch_reference_kinds": ",".join(product_summary.get("fetch_reference_kinds", [])),
                "file_product_subgroups": ",".join(product_summary.get("file_product_subgroups", [])),
                "product_scopes": format_count_map(product_summary.get("by_scope", {})),
                "sectors_with_per_sector_products": ",".join(str(row.get("sector")) for row in product_summary.get("products_by_sector", [])),
                "multi_sector_product_ranges": format_multi_sector_ranges(product_summary.get("multi_sector_products", [])),
                "has_preextracted_lightcurve": product_summary.get("has_preextracted_lightcurve", False),
                "has_tesscut": product_summary.get("has_tesscut", False),
                "crowding_risk": crowding_summary.get("risk"),
                "crowding_n_neighbors": crowding_summary.get("n_neighbors", 0),
                "crowding_n_neighbors_within_1_pixel": crowding_summary.get("n_neighbors_within_1_pixel", 0),
                "crowding_n_neighbors_within_2_pixels": crowding_summary.get("n_neighbors_within_2_pixels", 0),
                "crowding_n_neighbors_within_3_pixels": crowding_summary.get("n_neighbors_within_3_pixels", 0),
                "crowding_nearest_neighbor_arcsec": crowding_summary.get("nearest_neighbor_arcsec"),
                "crowding_brightest_delta_mag": crowding_summary.get("brightest_delta_mag"),
                "crowding_total_neighbor_flux_ratio": crowding_summary.get("total_neighbor_flux_ratio"),
                "crowding_total_neighbor_flux_ratio_within_1_pixel": crowding_summary.get(
                    "total_neighbor_flux_ratio_within_1_pixel"
                ),
                "crowding_total_neighbor_flux_ratio_within_2_pixels": crowding_summary.get(
                    "total_neighbor_flux_ratio_within_2_pixels"
                ),
                "crowding_total_neighbor_flux_ratio_within_3_pixels": crowding_summary.get(
                    "total_neighbor_flux_ratio_within_3_pixels"
                ),
                "crowding_heuristic_aperture_contamination_ratio": crowding_summary.get(
                    "heuristic_aperture_contamination_ratio"
                ),
                "crowding_heuristic_dilution_factor": crowding_summary.get("heuristic_dilution_factor"),
                "crowding_contamination_model": crowding_summary.get("contamination_model"),
                "crowding_query_source": crowding_summary.get("query_source"),
                "crowding_sector_geometry_count": sector_geometry.get("n_camera_ccd_geometries", 0),
                "crowding_sector_geometry_single": sector_geometry.get("single_camera_ccd_geometry"),
                "crowding_sector_geometry_groups": format_camera_ccd_groups(
                    sector_geometry.get("camera_ccd_groups", [])
                ),
                "known_object_n_matches": known_object_summary.get("n_matches", 0),
                "known_object_catalogs": ",".join(known_object_summary.get("catalogs", [])),
                "toi_n_matches": toi_summary.get("n_matches", 0),
                "toi_dispositions": ",".join(toi_summary.get("dispositions", [])),
                "toi_ids": ",".join(str(match.get("toi")) for match in toi_summary.get("matches", []) if match.get("toi")),
                "tess_eb_n_matches": tess_eb_summary.get("n_matches", 0),
                "tess_eb_ids": ",".join(
                    str(match.get("eb_id")) for match in tess_eb_summary.get("matches", []) if match.get("eb_id")
                ),
                "cache_hit": cache.get("hit"),
                "cache_key": cache.get("key"),
                "n_errors": len(inventory.get("errors", [])),
                "errors": " | ".join(inventory.get("errors", [])),
            }
        )
    return rows


def format_count_map(values: dict) -> str:
    return ";".join(f"{key}:{values[key]}" for key in sorted(values))


def format_multi_sector_ranges(rows: list[dict]) -> str:
    ranges = []
    for row in rows:
        start = row.get("sector_start")
        end = row.get("sector_end")
        family = row.get("family")
        if start is None or end is None:
            continue
        ranges.append(f"{family}:{start}-{end}")
    return ";".join(ranges)


def format_camera_ccd_groups(rows: list[dict]) -> str:
    groups = []
    for row in rows:
        camera = row.get("camera")
        ccd = row.get("ccd")
        sectors = ",".join(str(value) for value in row.get("sectors", []))
        groups.append(f"cam{camera}-ccd{ccd}:{sectors}")
    return ";".join(groups)


def flatten_sectors(document: dict) -> list[dict]:
    rows: list[dict] = []
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        for sector in inventory.get("sectors", []):
            rows.append(
                {
                    "input": inventory.get("input"),
                    "tic_id": target.get("tic_id"),
                    "ra_deg": target.get("ra_deg"),
                    "dec_deg": target.get("dec_deg"),
                    **sector,
                }
            )
    return rows


def flatten_products(document: dict) -> list[dict]:
    rows: list[dict] = []
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        for product in inventory.get("products", []):
            product_row = dict(product)
            extra = product_row.pop("extra", {}) if "extra" in product_row else {}
            rows.append(
                {
                    "input": inventory.get("input"),
                    "tic_id": target.get("tic_id"),
                    "ra_deg": target.get("ra_deg"),
                    "dec_deg": target.get("dec_deg"),
                    **product_row,
                    **{f"extra_{key}": value for key, value in extra.items()},
                }
            )
    return rows


def flatten_crowding_neighbors(document: dict) -> list[dict]:
    rows: list[dict] = []
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        crowding_summary = inventory.get("crowding_summary", {})
        for index, neighbor in enumerate(crowding_summary.get("neighbors", []), start=1):
            rows.append(
                {
                    "input": inventory.get("input"),
                    "tic_id": target.get("tic_id"),
                    "ra_deg": target.get("ra_deg"),
                    "dec_deg": target.get("dec_deg"),
                    "crowding_risk": crowding_summary.get("risk"),
                    "neighbor_rank": index,
                    **neighbor,
                }
            )
    return rows


def flatten_fetch_plans(document: dict) -> list[dict]:
    rows: list[dict] = []
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        fetch_plan = inventory.get("fetch_plan")
        if not fetch_plan:
            continue
        rows.append(
            {
                "input": inventory.get("input"),
                "tic_id": target.get("tic_id"),
                "ra_deg": target.get("ra_deg"),
                "dec_deg": target.get("dec_deg"),
                "product": fetch_plan.get("product"),
                "best_for": fetch_plan.get("best_for"),
                "sector_argument": fetch_plan.get("sector_argument"),
                "sectors": ",".join(str(value) for value in fetch_plan.get("sectors", [])),
                "n_selected_product_references": fetch_plan.get("n_selected_product_references", 0),
                "selected_reference_kinds": format_selected_reference_kinds(
                    fetch_plan.get("selected_product_references", [])
                ),
                "command": fetch_plan.get("command"),
                "reason": fetch_plan.get("reason"),
                "caveats": " | ".join(fetch_plan.get("caveats", [])),
            }
        )
    return rows


def format_selected_reference_kinds(rows: list[dict]) -> str:
    return ",".join(sorted({str(row.get("fetch_reference_kind")) for row in rows if row.get("fetch_reference_kind")}))


def flatten_providers(document: dict) -> list[dict]:
    rows: list[dict] = []
    for inventory in document.get("targets", []):
        target = inventory.get("target", {})
        for provider in inventory.get("providers", []):
            rows.append(
                {
                    "input": inventory.get("input"),
                    "tic_id": target.get("tic_id"),
                    "ra_deg": target.get("ra_deg"),
                    "dec_deg": target.get("dec_deg"),
                    **provider,
                }
            )
    return rows
