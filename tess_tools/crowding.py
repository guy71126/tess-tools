from __future__ import annotations

import math
from typing import Any

from .mast_api import invoke_mast_service, mast_data_rows
from .models import ProviderStatus, ResolvedTarget, SectorRecord
from .target import coerce_float


TESS_PIXEL_ARCSEC = 21.0
DEFAULT_CROWDING_RADIUS_ARCSEC = 60.0
DEFAULT_CONTAMINATION_RADIUS_PIXELS = 3.0
GAIA_CATALOG_CONE_SERVICES = (
    "Mast.Catalogs.GaiaDR3.Cone",
    "Mast.Catalogs.Gaia.Cone",
    "Mast.Catalogs.GaiaDR2.Cone",
)
GAIA_CATALOG_COLUMNS = "source_id,ra,dec,phot_g_mean_mag"


def discover_crowding(
    target: ResolvedTarget,
    *,
    sectors: list[SectorRecord] | None = None,
    radius_arcsec: float = DEFAULT_CROWDING_RADIUS_ARCSEC,
    errors: list[str],
) -> tuple[dict[str, Any], ProviderStatus]:
    if target.ra_deg is None or target.dec_deg is None:
        message = "skipped Gaia neighbor audit because target coordinates are unavailable"
        return empty_crowding_summary(radius_arcsec, sectors=sectors, message=message), ProviderStatus(
            name="Gaia neighbors",
            status="skipped",
            message=message,
            records=0,
        )
    try:
        rows = query_gaia_neighbors(target.ra_deg, target.dec_deg, radius_arcsec)
    except Exception as exc:  # pragma: no cover - optional dependency/network dependent
        message = f"Gaia neighbor query failed: {exc}"
        errors.append(message)
        return empty_crowding_summary(radius_arcsec, sectors=sectors, message=message), ProviderStatus(
            name="Gaia neighbors",
            status="error",
            message=message,
            records=0,
        )

    summary = build_crowding_summary(target, rows, radius_arcsec=radius_arcsec, sectors=sectors)
    return summary, ProviderStatus(
        name="Gaia neighbors",
        status="ok",
        message=f"{summary['n_neighbors']} neighbor(s) within {radius_arcsec:g} arcsec",
        records=summary["n_neighbors"],
    )


def query_gaia_neighbors(ra_deg: float, dec_deg: float, radius_arcsec: float) -> list[dict[str, Any]]:
    try:
        return query_mast_gaia_neighbors(ra_deg, dec_deg, radius_arcsec)
    except Exception as mast_exc:  # pragma: no cover - network/service dependent
        mast_message = str(mast_exc)

    try:
        from astropy import units as u
        from astropy.coordinates import SkyCoord
        from astroquery.mast import Catalogs
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            f"MAST Gaia HTTP query failed ({mast_message}); Gaia neighbor audit fallback requires astroquery and astropy"
        ) from exc

    coord = SkyCoord(float(ra_deg), float(dec_deg), unit="deg")
    table = Catalogs.query_region(coord, radius=float(radius_arcsec) * u.arcsec, catalog="Gaia")
    return [tag_neighbor_source({name: row[name] for name in table.colnames}, "astroquery.mast.Catalogs.Gaia") for row in table]


def query_mast_gaia_neighbors(ra_deg: float, dec_deg: float, radius_arcsec: float) -> list[dict[str, Any]]:
    messages: list[str] = []
    base_params = {
        "ra": float(ra_deg),
        "dec": float(dec_deg),
        "radius": float(radius_arcsec) / 3600.0,
    }
    for service in GAIA_CATALOG_CONE_SERVICES:
        for params in (dict(base_params, columns=GAIA_CATALOG_COLUMNS), base_params):
            try:
                payload = invoke_mast_service(
                    service,
                    params,
                    timeout_sec=12,
                    pagesize=2000,
                )
            except Exception as exc:  # pragma: no cover - network/service dependent
                suffix = " with columns" if "columns" in params else ""
                messages.append(f"{service}{suffix}: {exc}")
                continue
            return [tag_neighbor_source(row, service) for row in mast_data_rows(payload)]
    raise RuntimeError("all MAST Gaia HTTP attempts failed: " + " | ".join(messages))


def tag_neighbor_source(row: dict[str, Any], source: str) -> dict[str, Any]:
    tagged = dict(row)
    tagged.setdefault("_query_source", source)
    return tagged


def build_crowding_summary(
    target: ResolvedTarget,
    rows: list[dict[str, Any]],
    *,
    radius_arcsec: float,
    sectors: list[SectorRecord] | None = None,
) -> dict[str, Any]:
    target_mag = target.tmag
    neighbors: list[dict[str, Any]] = []
    for row in rows:
        neighbor = normalize_neighbor_row(target, row)
        if neighbor is None:
            continue
        if neighbor["separation_arcsec"] <= 0.01:
            continue
        if neighbor["separation_arcsec"] > radius_arcsec:
            continue
        if target_mag is not None and neighbor.get("mag") is not None:
            neighbor["delta_mag"] = neighbor["mag"] - target_mag
            neighbor["flux_ratio"] = flux_ratio_from_delta_mag(neighbor["delta_mag"])
            neighbor["aperture_weight"] = aperture_contamination_weight(neighbor["separation_arcsec"])
            neighbor["weighted_flux_ratio"] = neighbor["flux_ratio"] * neighbor["aperture_weight"]
        neighbors.append(neighbor)

    neighbors.sort(key=lambda item: item["separation_arcsec"])
    nearest = neighbors[0] if neighbors else None
    flux_ratios = [neighbor["flux_ratio"] for neighbor in neighbors if neighbor.get("flux_ratio") is not None]
    total_flux_ratio = sum(flux_ratios) if flux_ratios else None
    weighted_flux_ratios = [
        neighbor["weighted_flux_ratio"] for neighbor in neighbors if neighbor.get("weighted_flux_ratio") is not None
    ]
    contamination_ratio = sum(weighted_flux_ratios) if weighted_flux_ratios else None
    dilution_factor = 1.0 / (1.0 + contamination_ratio) if contamination_ratio is not None else None
    brightest_delta = min((neighbor["delta_mag"] for neighbor in neighbors if neighbor.get("delta_mag") is not None), default=None)
    one_pixel_neighbors = [neighbor for neighbor in neighbors if neighbor["separation_arcsec"] <= TESS_PIXEL_ARCSEC]
    two_pixel_neighbors = [neighbor for neighbor in neighbors if neighbor["separation_arcsec"] <= 2.0 * TESS_PIXEL_ARCSEC]
    three_pixel_neighbors = [neighbor for neighbor in neighbors if neighbor["separation_arcsec"] <= 3.0 * TESS_PIXEL_ARCSEC]
    risk = crowding_risk(neighbors, contamination_ratio=contamination_ratio)

    return {
        "schema_version": "tess-where.crowding.v0.1",
        "source": "gaia",
        "query_source": first_neighbor_query_source(neighbors),
        "radius_arcsec": radius_arcsec,
        "tess_pixel_arcsec": TESS_PIXEL_ARCSEC,
        "contamination_model": f"linear radial weight to {DEFAULT_CONTAMINATION_RADIUS_PIXELS:g} nominal TESS pixels using Gaia G as a TESS-band proxy",
        "risk": risk,
        "n_neighbors": len(neighbors),
        "n_neighbors_within_1_pixel": len(one_pixel_neighbors),
        "n_neighbors_within_2_pixels": len(two_pixel_neighbors),
        "n_neighbors_within_3_pixels": len(three_pixel_neighbors),
        "nearest_neighbor_arcsec": nearest["separation_arcsec"] if nearest else None,
        "brightest_delta_mag": brightest_delta,
        "total_neighbor_flux_ratio": total_flux_ratio,
        "total_neighbor_flux_ratio_within_1_pixel": sum_flux_ratio(one_pixel_neighbors),
        "total_neighbor_flux_ratio_within_2_pixels": sum_flux_ratio(two_pixel_neighbors),
        "total_neighbor_flux_ratio_within_3_pixels": sum_flux_ratio(three_pixel_neighbors),
        "heuristic_aperture_contamination_ratio": contamination_ratio,
        "heuristic_dilution_factor": dilution_factor,
        "sector_geometry": build_sector_geometry_summary(sectors),
        "neighbors": neighbors[:20],
    }


def normalize_neighbor_row(target: ResolvedTarget, row: dict[str, Any]) -> dict[str, Any] | None:
    ra = coerce_float(row_value(row, ("ra", "RA", "ra_deg", "RA_ICRS")))
    dec = coerce_float(row_value(row, ("dec", "DEC", "dec_deg", "DE_ICRS")))
    if ra is None or dec is None or target.ra_deg is None or target.dec_deg is None:
        return None
    separation = angular_separation_arcsec(target.ra_deg, target.dec_deg, ra, dec)
    return {
        "source_id": row_value(row, ("source_id", "SOURCE_ID", "Source", "designation")),
        "ra_deg": ra,
        "dec_deg": dec,
        "mag": coerce_float(row_value(row, ("phot_g_mean_mag", "Gmag", "gmag", "mag", "Tmag"))),
        "separation_arcsec": separation,
        "separation_pixels": separation / TESS_PIXEL_ARCSEC,
        "query_source": row_value(row, ("_query_source",)),
    }


def crowding_risk(neighbors: list[dict[str, Any]], *, contamination_ratio: float | None) -> str:
    one_pixel = [neighbor for neighbor in neighbors if neighbor["separation_arcsec"] <= TESS_PIXEL_ARCSEC]
    two_pixels = [neighbor for neighbor in neighbors if neighbor["separation_arcsec"] <= 2.0 * TESS_PIXEL_ARCSEC]
    if any(neighbor.get("delta_mag") is not None and neighbor["delta_mag"] <= 3.0 for neighbor in one_pixel):
        return "high"
    if contamination_ratio is not None and contamination_ratio >= 0.1:
        return "high"
    if any(neighbor.get("delta_mag") is not None and neighbor["delta_mag"] <= 3.0 for neighbor in two_pixels):
        return "medium"
    if one_pixel:
        return "medium"
    if any(neighbor.get("delta_mag") is not None and neighbor["delta_mag"] <= 5.0 for neighbor in two_pixels):
        return "medium"
    if contamination_ratio is not None and contamination_ratio >= 0.01:
        return "medium"
    return "low"


def empty_crowding_summary(
    radius_arcsec: float,
    *,
    sectors: list[SectorRecord] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "tess-where.crowding.v0.1",
        "source": "gaia",
        "query_source": None,
        "radius_arcsec": radius_arcsec,
        "tess_pixel_arcsec": TESS_PIXEL_ARCSEC,
        "contamination_model": f"linear radial weight to {DEFAULT_CONTAMINATION_RADIUS_PIXELS:g} nominal TESS pixels using Gaia G as a TESS-band proxy",
        "risk": "unknown",
        "n_neighbors": 0,
        "n_neighbors_within_1_pixel": 0,
        "n_neighbors_within_2_pixels": 0,
        "n_neighbors_within_3_pixels": 0,
        "nearest_neighbor_arcsec": None,
        "brightest_delta_mag": None,
        "total_neighbor_flux_ratio": None,
        "total_neighbor_flux_ratio_within_1_pixel": None,
        "total_neighbor_flux_ratio_within_2_pixels": None,
        "total_neighbor_flux_ratio_within_3_pixels": None,
        "heuristic_aperture_contamination_ratio": None,
        "heuristic_dilution_factor": None,
        "sector_geometry": build_sector_geometry_summary(sectors),
        "neighbors": [],
        "message": message,
    }


def flux_ratio_from_delta_mag(delta_mag: float) -> float:
    return 10 ** (-0.4 * float(delta_mag))


def aperture_contamination_weight(separation_arcsec: float) -> float:
    separation_pixels = max(0.0, float(separation_arcsec) / TESS_PIXEL_ARCSEC)
    if separation_pixels <= 0.5:
        return 1.0
    if separation_pixels >= DEFAULT_CONTAMINATION_RADIUS_PIXELS:
        return 0.0
    return (DEFAULT_CONTAMINATION_RADIUS_PIXELS - separation_pixels) / (DEFAULT_CONTAMINATION_RADIUS_PIXELS - 0.5)


def sum_flux_ratio(neighbors: list[dict[str, Any]]) -> float | None:
    ratios = [neighbor["flux_ratio"] for neighbor in neighbors if neighbor.get("flux_ratio") is not None]
    return sum(ratios) if ratios else None


def build_sector_geometry_summary(sectors: list[SectorRecord] | None) -> dict[str, Any]:
    sectors = sectors or []
    groups: dict[tuple[int | None, int | None], list[int]] = {}
    for sector in sectors:
        if sector.camera is None and sector.ccd is None:
            continue
        groups.setdefault((sector.camera, sector.ccd), []).append(sector.sector)

    group_rows = [
        {
            "camera": camera,
            "ccd": ccd,
            "n_sectors": len(sector_numbers),
            "sectors": sorted(sector_numbers),
        }
        for (camera, ccd), sector_numbers in sorted(
            groups.items(),
            key=lambda item: (
                item[0][0] if item[0][0] is not None else 999,
                item[0][1] if item[0][1] is not None else 999,
            ),
        )
    ]
    return {
        "schema_version": "tess-where.crowding-sector-geometry.v0.1",
        "n_sectors": len(sectors),
        "n_sectors_with_camera_ccd": sum(len(row["sectors"]) for row in group_rows),
        "n_camera_ccd_geometries": len(group_rows),
        "single_camera_ccd_geometry": len(group_rows) == 1,
        "camera_ccd_groups": group_rows,
        "note": "Crowding flux ratios use a nominal pixel-scale model; camera/CCD grouping records where sector-specific PRF or aperture calibration should be applied later.",
    }


def angular_separation_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    sin_d_dec = math.sin((dec2 - dec1) / 2.0)
    sin_d_ra = math.sin((ra2 - ra1) / 2.0)
    value = sin_d_dec * sin_d_dec + math.cos(dec1) * math.cos(dec2) * sin_d_ra * sin_d_ra
    angle = 2.0 * math.asin(min(1.0, math.sqrt(value)))
    return math.degrees(angle) * 3600.0


def row_value(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row:
            value = row[name]
            if hasattr(value, "item"):
                value = value.item()
            text = str(value)
            if text not in {"", "--", "nan", "None"}:
                return value
    return None


def first_neighbor_query_source(neighbors: list[dict[str, Any]]) -> str | None:
    for neighbor in neighbors:
        source = neighbor.get("query_source")
        if source:
            return str(source)
    return None
