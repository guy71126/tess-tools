from __future__ import annotations

import json
import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import ProviderStatus, ResolvedTarget
from .target import coerce_float


NASA_EXOPLANET_ARCHIVE_TAP_SYNC_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
USER_AGENT = "tess-where/0.1"


def empty_known_summary(*, message: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "tess-where.known-objects.v0.1",
        "n_matches": 0,
        "catalogs": [],
        "toi": {
            "source": "NASA Exoplanet Archive TOI TAP",
            "n_matches": 0,
            "dispositions": [],
            "matches": [],
        },
        "tess_eb": {
            "source": "local TESS EB catalog",
            "n_matches": 0,
            "matches": [],
        },
        "message": message,
    }


def discover_known_objects(
    target: ResolvedTarget,
    *,
    enabled_providers: set[str],
    errors: list[str],
    toi_catalog_path: Path | None = None,
    tess_eb_catalog_path: Path | None = None,
) -> tuple[dict[str, Any], list[ProviderStatus]]:
    providers: list[ProviderStatus] = []
    if not enabled_providers.intersection({"toi", "tesseb"}):
        return empty_known_summary(message="disabled by provider selection"), providers

    toi_summary = empty_toi_summary(message="disabled by provider selection")
    if "toi" in enabled_providers:
        toi_summary, toi_status = discover_toi_matches(target, errors=errors, catalog_path=toi_catalog_path)
        providers.append(toi_status)

    tess_eb_summary = empty_tess_eb_summary(message="disabled by provider selection")
    if "tesseb" in enabled_providers:
        tess_eb_summary, tess_eb_status = discover_tess_eb_matches(
            target,
            errors=errors,
            catalog_path=tess_eb_catalog_path,
        )
        providers.append(tess_eb_status)

    catalogs = []
    if toi_summary["n_matches"]:
        catalogs.append("TOI")
    if tess_eb_summary["n_matches"]:
        catalogs.append("TESS EB")
    return {
        "schema_version": "tess-where.known-objects.v0.1",
        "n_matches": toi_summary["n_matches"] + tess_eb_summary["n_matches"],
        "catalogs": catalogs,
        "toi": toi_summary,
        "tess_eb": tess_eb_summary,
    }, providers


def discover_toi_matches(
    target: ResolvedTarget,
    *,
    errors: list[str],
    catalog_path: Path | None = None,
) -> tuple[dict[str, Any], ProviderStatus]:
    if not target.tic_id:
        message = "skipped TOI lookup because TIC ID is unavailable"
        return empty_toi_summary(message=message), ProviderStatus(
            name="TOI catalog",
            status="skipped",
            message=message,
            records=0,
        )
    try:
        if catalog_path is not None:
            rows = query_toi_catalog_file_by_tic(catalog_path, target.tic_id)
            source = f"local TOI catalog: {catalog_path}"
        else:
            rows = query_toi_by_tic(target.tic_id)
            source = "NASA Exoplanet Archive TOI TAP"
    except Exception as exc:  # pragma: no cover - network/service dependent
        message = f"TOI catalog query failed for TIC {target.tic_id}: {exc}"
        errors.append(message)
        return empty_toi_summary(message=message), ProviderStatus(
            name="TOI catalog",
            status="error",
            message=message,
            records=0,
        )

    matches = [normalize_toi_row(row, source=source) for row in rows]
    matches = [match for match in matches if match]
    dispositions = sorted({str(match["disposition"]) for match in matches if match.get("disposition")})
    summary = {
        "source": source,
        "n_matches": len(matches),
        "dispositions": dispositions,
        "matches": matches,
    }
    return summary, ProviderStatus(
        name="TOI catalog",
        status="ok" if matches else "empty",
        message=f"{len(matches)} TOI match(es) for TIC {target.tic_id}",
        records=len(matches),
    )


def empty_toi_summary(*, message: str | None = None) -> dict[str, Any]:
    return {
        "source": "NASA Exoplanet Archive TOI TAP",
        "n_matches": 0,
        "dispositions": [],
        "matches": [],
        "message": message,
    }


def discover_tess_eb_matches(
    target: ResolvedTarget,
    *,
    errors: list[str],
    catalog_path: Path | None = None,
) -> tuple[dict[str, Any], ProviderStatus]:
    if not target.tic_id:
        message = "skipped TESS EB lookup because TIC ID is unavailable"
        return empty_tess_eb_summary(message=message), ProviderStatus(
            name="TESS EB catalog",
            status="skipped",
            message=message,
            records=0,
        )
    if catalog_path is None:
        message = "skipped TESS EB lookup because --tess-eb-catalog was not provided"
        return empty_tess_eb_summary(message=message), ProviderStatus(
            name="TESS EB catalog",
            status="skipped",
            message=message,
            records=0,
        )
    try:
        rows = query_tess_eb_catalog_file_by_tic(catalog_path, target.tic_id)
        source = f"local TESS EB catalog: {catalog_path}"
    except Exception as exc:
        message = f"TESS EB catalog query failed for TIC {target.tic_id}: {exc}"
        errors.append(message)
        return empty_tess_eb_summary(message=message), ProviderStatus(
            name="TESS EB catalog",
            status="error",
            message=message,
            records=0,
        )

    matches = [normalize_tess_eb_row(row, source=source) for row in rows]
    matches = [match for match in matches if match]
    summary = {
        "source": source,
        "n_matches": len(matches),
        "matches": matches,
    }
    return summary, ProviderStatus(
        name="TESS EB catalog",
        status="ok" if matches else "empty",
        message=f"{len(matches)} TESS EB match(es) for TIC {target.tic_id}",
        records=len(matches),
    )


def empty_tess_eb_summary(*, message: str | None = None) -> dict[str, Any]:
    return {
        "source": "local TESS EB catalog",
        "n_matches": 0,
        "matches": [],
        "message": message,
    }


def query_toi_by_tic(tic_id: str) -> list[dict[str, Any]]:
    tic = int(str(tic_id))
    query = f"select * from toi where tid = {tic}"
    params = urlencode({"query": query, "format": "json"})
    url = f"{NASA_EXOPLANET_ARCHIVE_TAP_SYNC_URL}?{params}"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("TOI TAP response did not contain a JSON row list")
    return [row for row in payload if isinstance(row, dict)]


def query_toi_by_identifier(toi_id: str) -> list[dict[str, Any]]:
    normalized = normalize_toi_value(toi_id)
    if normalized is None:
        raise ValueError(f"invalid TOI identifier: {toi_id!r}")
    if "." in normalized:
        where = f"toi = {normalized}"
    else:
        host = int(normalized)
        where = f"toi >= {host} and toi < {host + 1}"
    query = f"select * from toi where {where} order by toi"
    params = urlencode({"query": query, "format": "json"})
    url = f"{NASA_EXOPLANET_ARCHIVE_TAP_SYNC_URL}?{params}"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("TOI TAP response did not contain a JSON row list")
    return [row for row in payload if isinstance(row, dict)]


def query_toi_catalog_file_by_tic(path: Path, tic_id: str) -> list[dict[str, Any]]:
    rows = read_catalog_rows(path)
    return [row for row in rows if normalize_tic_value(row_value(row, ("tid", "TIC ID", "tic_id", "tic", "TIC"))) == str(int(tic_id))]


def query_toi_catalog_file_by_identifier(path: Path, toi_id: str) -> list[dict[str, Any]]:
    requested = normalize_toi_value(toi_id)
    if requested is None:
        raise ValueError(f"invalid TOI identifier: {toi_id!r}")
    rows = read_catalog_rows(path)
    if "." in requested:
        return [row for row in rows if normalize_toi_value(row_value(row, ("toi", "TOI", "toi_id"))) == requested]
    return [
        row
        for row in rows
        if (value := normalize_toi_value(row_value(row, ("toi", "TOI", "toi_id")))) is not None
        and value.split(".", 1)[0] == requested
    ]


def query_tess_eb_catalog_file_by_tic(path: Path, tic_id: str) -> list[dict[str, Any]]:
    rows = read_catalog_rows(path)
    tic_names = ("tic_id", "tic", "TIC", "TIC ID", "tid", "source_id", "target_id")
    return [row for row in rows if normalize_tic_value(row_value(row, tic_names)) == str(int(tic_id))]


def read_catalog_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
        raise RuntimeError("local TOI JSON catalog must be a list or contain a data list")

    delimiter = "\t" if suffix in {".tsv", ".tab"} else ","
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        return [dict(row) for row in reader]


def normalize_toi_row(row: dict[str, Any], *, source: str = "NASA Exoplanet Archive TOI TAP") -> dict[str, Any]:
    return {
        "toi": clean_optional(row_value(row, ("toi", "TOI"))),
        "tic_id": clean_optional(row_value(row, ("tid", "TIC ID", "tic_id"))),
        "disposition": clean_optional(row_value(row, ("tfopwg_disp", "toi_disposition", "disposition"))),
        "period_days": coerce_float(row_value(row, ("pl_p", "period", "period_days"))),
        "epoch_bjd": coerce_float(row_value(row, ("pl_tranmid", "epoch", "epoch_bjd"))),
        "duration_hours": coerce_float(row_value(row, ("pl_trandurh", "duration", "duration_hours"))),
        "depth_ppm": coerce_float(row_value(row, ("pl_trandep", "depth", "depth_ppm"))),
        "radius_rearth": coerce_float(row_value(row, ("pl_rade", "planet_radius", "radius_rearth"))),
        "source": source,
    }


def normalize_tess_eb_row(row: dict[str, Any], *, source: str = "local TESS EB catalog") -> dict[str, Any]:
    return {
        "eb_id": clean_optional(row_value(row, ("eb_id", "id", "ID", "name", "Name"))),
        "tic_id": clean_optional(row_value(row, ("tic_id", "tic", "TIC", "TIC ID", "tid", "source_id", "target_id"))),
        "period_days": coerce_float(row_value(row, ("period", "period_days", "Period", "period_d"))),
        "epoch_bjd": coerce_float(row_value(row, ("epoch", "epoch_bjd", "Epoch", "t0", "t0_bjd"))),
        "duration_hours": coerce_float(row_value(row, ("duration", "duration_hours", "Duration"))),
        "morphology": clean_optional(row_value(row, ("morphology", "morph", "Morphology"))),
        "source": source,
    }


def normalize_tic_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower().startswith("tic"):
        text = text[3:].strip()
    try:
        return str(int(float(text)))
    except ValueError:
        return None


def normalize_toi_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower().startswith("toi"):
        text = text[3:].lstrip(" -")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return format(number.normalize(), "f")


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


def clean_optional(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    return None if text in {"", "--", "nan", "None"} else value
