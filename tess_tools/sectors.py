from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import urlopen

from .models import ResolvedTarget, SectorRecord


def discover_sectors(target: ResolvedTarget, *, errors: list[str]) -> list[SectorRecord]:
    if target.ra_deg is None or target.dec_deg is None:
        errors.append("skipped sector lookup because target coordinates are unavailable")
        return []

    sectors = discover_sectors_astroquery(target, errors=errors)
    if sectors:
        return sectors
    return discover_sectors_tesscut_http(target, errors=errors)


def discover_sectors_astroquery(target: ResolvedTarget, *, errors: list[str]) -> list[SectorRecord]:
    try:
        from astropy.coordinates import SkyCoord
        from astroquery.mast import Tesscut
    except Exception:
        return []

    try:
        coord = SkyCoord(target.ra_deg, target.dec_deg, unit="deg")
        table = Tesscut.get_sectors(coordinates=coord)
    except Exception as exc:  # pragma: no cover - network/service dependent
        errors.append(f"astroquery TESSCut sector lookup failed: {exc}")
        return []

    records = []
    for row in table:
        row_dict = {name: row[name] for name in row.colnames}
        records.append(
            SectorRecord(
                sector=safe_int(row_dict.get("sector")),
                camera=safe_int(row_dict.get("camera")),
                ccd=safe_int(row_dict.get("ccd")),
                sector_name=str(row_dict.get("sectorName") or ""),
                source="astroquery.mast.Tesscut",
            )
        )
    return sorted([record for record in records if record.sector is not None], key=lambda item: item.sector)


def discover_sectors_tesscut_http(target: ResolvedTarget, *, errors: list[str]) -> list[SectorRecord]:
    params = urlencode({"ra": target.ra_deg, "dec": target.dec_deg, "radius": "1m"})
    url = f"https://mast.stsci.edu/tesscut/api/v0.1/sector?{params}"
    try:
        with urlopen(url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - network/service dependent
        errors.append(f"TESSCut HTTP sector lookup failed: {exc}")
        return []

    records = []
    for item in payload.get("results", []):
        records.append(
            SectorRecord(
                sector=safe_int(item.get("sector")),
                camera=safe_int(item.get("camera")),
                ccd=safe_int(item.get("ccd")),
                sector_name=item.get("sectorName"),
                source="mast.tesscut.http",
            )
        )
    return sorted([record for record in records if record.sector is not None], key=lambda item: item.sector)


def safe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

