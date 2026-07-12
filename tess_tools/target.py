from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mast_api import invoke_mast_service, mast_data_rows
from .models import ResolvedTarget


@dataclass(frozen=True)
class TargetSpec:
    raw: str
    kind: str
    tic_id: str | None = None
    toi_id: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    row_number: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "tic_id": self.tic_id,
            "toi_id": self.toi_id,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "row_number": self.row_number,
            "error": self.error,
        }

    def to_cache_payload(self) -> dict[str, Any]:
        return self.to_dict()


def parse_target_args(tokens: list[str]) -> TargetSpec:
    if len(tokens) == 2 and looks_float(tokens[0]) and looks_float(tokens[1]):
        return TargetSpec(raw=" ".join(tokens), kind="coords", ra_deg=float(tokens[0]), dec_deg=float(tokens[1]))

    raw = " ".join(tokens).strip()
    tic = parse_tic(raw)
    if tic:
        return TargetSpec(raw=raw, kind="tic", tic_id=tic)

    toi = parse_toi(raw)
    if toi:
        return TargetSpec(raw=raw, kind="toi", toi_id=toi)

    raise ValueError(
        "Unsupported target input. Use 'TIC 123', 'TOI-700', a bare TIC ID, an RA Dec pair, or a CSV file."
    )


def read_targets_csv(path: Path) -> list[TargetSpec]:
    specs: list[TargetSpec] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return specs
        lower_names = {name.lower(): name for name in reader.fieldnames}
        for row_number, row in enumerate(reader, start=2):
            tic_name = first_present(lower_names, "tic_id", "tic", "tid")
            toi_name = first_present(lower_names, "toi_id", "toi")
            ra_name = first_present(lower_names, "ra", "ra_deg")
            dec_name = first_present(lower_names, "dec", "dec_deg")
            if tic_name and row.get(tic_name):
                tic = parse_tic(str(row[tic_name]))
                if tic:
                    specs.append(TargetSpec(raw=str(row[tic_name]), kind="tic", tic_id=tic, row_number=row_number))
                    continue
                specs.append(
                    TargetSpec(
                        raw=str(row[tic_name]),
                        kind="invalid",
                        row_number=row_number,
                        error=f"invalid TIC value in column {tic_name!r}",
                    )
                )
                continue
            if toi_name and row.get(toi_name):
                toi = parse_toi(str(row[toi_name]))
                if toi:
                    specs.append(TargetSpec(raw=str(row[toi_name]), kind="toi", toi_id=toi, row_number=row_number))
                    continue
                specs.append(
                    TargetSpec(
                        raw=str(row[toi_name]),
                        kind="invalid",
                        row_number=row_number,
                        error=f"invalid TOI value in column {toi_name!r}",
                    )
                )
                continue
            if ra_name and dec_name and row.get(ra_name) and row.get(dec_name):
                raw = f"{row[ra_name]} {row[dec_name]}"
                try:
                    specs.append(
                        TargetSpec(
                            raw=raw,
                            kind="coords",
                            ra_deg=float(row[ra_name]),
                            dec_deg=float(row[dec_name]),
                            row_number=row_number,
                        )
                    )
                except ValueError:
                    specs.append(
                        TargetSpec(
                            raw=raw,
                            kind="invalid",
                            row_number=row_number,
                            error=f"invalid RA/Dec values in columns {ra_name!r}/{dec_name!r}",
                        )
                    )
                continue
            specs.append(
                TargetSpec(
                    raw=str(row),
                    kind="invalid",
                    row_number=row_number,
                    error="missing usable tic_id, toi_id, or ra/dec columns",
                )
            )
    return specs


def resolve_target(
    spec: TargetSpec,
    *,
    errors: list[str],
    toi_catalog_path: Path | None = None,
) -> ResolvedTarget:
    if spec.kind == "coords":
        return ResolvedTarget(raw=spec.raw, kind="coords", ra_deg=spec.ra_deg, dec_deg=spec.dec_deg)
    if spec.kind == "tic" and spec.tic_id:
        resolved = resolve_tic_with_mast_http(spec.tic_id, raw=spec.raw, errors=errors)
        if resolved is not None:
            return resolved
        resolved = resolve_tic_with_astroquery(spec.tic_id, raw=spec.raw, errors=errors)
        if resolved is not None:
            return resolved
        return ResolvedTarget(raw=spec.raw, kind="tic", tic_id=spec.tic_id)
    if spec.kind == "toi" and spec.toi_id:
        return resolve_toi(spec.toi_id, raw=spec.raw, errors=errors, catalog_path=toi_catalog_path)
    return ResolvedTarget(raw=spec.raw, kind=spec.kind)


def resolve_toi(
    toi_id: str,
    *,
    raw: str,
    errors: list[str],
    catalog_path: Path | None = None,
) -> ResolvedTarget:
    # Imported lazily because known-object normalization also uses target helpers.
    from .known import normalize_tic_value, query_toi_by_identifier, query_toi_catalog_file_by_identifier

    try:
        rows = (
            query_toi_catalog_file_by_identifier(catalog_path, toi_id)
            if catalog_path is not None
            else query_toi_by_identifier(toi_id)
        )
    except Exception as exc:  # pragma: no cover - network/service dependent
        errors.append(f"TOI lookup failed for TOI {toi_id}: {exc}")
        return ResolvedTarget(raw=raw, kind="toi", extra={"toi_id": toi_id})

    if not rows:
        errors.append(f"TOI lookup returned no rows for TOI {toi_id}")
        return ResolvedTarget(raw=raw, kind="toi", extra={"toi_id": toi_id})

    row = rows[0]
    tic_id = normalize_tic_value(row_value(row, ("tid", "TIC ID", "tic_id")))
    if not tic_id:
        errors.append(f"TOI lookup returned no TIC ID for TOI {toi_id}")
        return ResolvedTarget(raw=raw, kind="toi", extra={"toi_id": toi_id})

    resolved = resolve_tic_with_mast_http(tic_id, raw=raw, errors=errors)
    if resolved is None:
        resolved = resolve_tic_with_astroquery(tic_id, raw=raw, errors=errors)

    source = f"local TOI catalog: {catalog_path}" if catalog_path is not None else "nasa.exoplanetarchive.toi.tap"
    if resolved is not None:
        return ResolvedTarget(
            raw=raw,
            kind="toi",
            tic_id=resolved.tic_id,
            ra_deg=resolved.ra_deg,
            dec_deg=resolved.dec_deg,
            tmag=resolved.tmag,
            source=f"{source} -> {resolved.source}",
            extra={"toi_id": toi_id},
        )

    return ResolvedTarget(
        raw=raw,
        kind="toi",
        tic_id=tic_id,
        ra_deg=coerce_float(row_value(row, ("ra", "RA"))),
        dec_deg=coerce_float(row_value(row, ("dec", "DEC"))),
        tmag=coerce_float(row_value(row, ("st_tmag", "Tmag", "tmag"))),
        source=source,
        extra={"toi_id": toi_id},
    )


def resolve_tic_with_mast_http(tic_id: str, *, raw: str, errors: list[str]) -> ResolvedTarget | None:
    try:
        rows = query_mast_tic(tic_id)
    except Exception as exc:  # pragma: no cover - network/service dependent
        errors.append(f"MAST TIC HTTP query failed for TIC {tic_id}: {exc}")
        return None

    if not rows:
        errors.append(f"MAST TIC HTTP query returned no rows for TIC {tic_id}")
        return None
    return resolved_target_from_tic_row(rows[0], tic_id=tic_id, raw=raw, source="mast.catalogs.filtered.tic.http")


def query_mast_tic(tic_id: str) -> list[dict[str, Any]]:
    payload = invoke_mast_service(
        "Mast.Catalogs.Filtered.Tic",
        {
            "columns": "ID,ra,dec,Tmag",
            "filters": [{"paramName": "ID", "values": [str(tic_id)]}],
        },
        timeout_sec=12,
        pagesize=5,
    )
    return mast_data_rows(payload)


def resolved_target_from_tic_row(
    row: dict[str, Any],
    *,
    tic_id: str,
    raw: str,
    source: str,
) -> ResolvedTarget:
    return ResolvedTarget(
        raw=raw,
        kind="tic",
        tic_id=str(row_value(row, ("ID", "id", "tic_id", "TICID")) or tic_id),
        ra_deg=coerce_float(row_value(row, ("ra", "RA", "ra_orig"))),
        dec_deg=coerce_float(row_value(row, ("dec", "DEC", "dec_orig"))),
        tmag=coerce_float(row_value(row, ("Tmag", "tmag", "TESSmag"))),
        source=source,
    )


def resolve_tic_with_astroquery(tic_id: str, *, raw: str, errors: list[str]) -> ResolvedTarget | None:
    try:
        from astroquery.mast import Catalogs
    except Exception as exc:
        errors.append(f"astroquery unavailable for TIC resolution: {exc}")
        return None

    try:
        table = Catalogs.query_criteria(catalog="TIC", ID=int(tic_id))
    except Exception as exc:  # pragma: no cover - network/service dependent
        errors.append(f"TIC catalog query failed for TIC {tic_id}: {exc}")
        return None

    if len(table) == 0:
        errors.append(f"TIC catalog query returned no rows for TIC {tic_id}")
        return None

    row = table[0]
    return resolved_target_from_tic_row(
        {name: row[name] for name in row.colnames},
        tic_id=tic_id,
        raw=raw,
        source="astroquery.mast.Catalogs.TIC",
    )


def row_value(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row:
            value = row[name]
            try:
                value = value.item()
            except AttributeError:
                pass
            text = str(value)
            if text not in {"", "--", "nan", "None"}:
                return value
    return None


def coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_tic(raw: str) -> str | None:
    text = raw.strip()
    match = re.fullmatch(r"(?i)tic\s*([0-9]+)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9]+", text):
        return text
    return None


def parse_toi(raw: str) -> str | None:
    match = re.fullmatch(r"(?i)toi(?:\s*[- ]\s*)?([0-9]+(?:\.[0-9]+)?)", raw.strip())
    if not match:
        return None
    whole, dot, fractional = match.group(1).partition(".")
    canonical = str(int(whole))
    if canonical == "0":
        return None
    fractional = fractional.rstrip("0")
    return f"{canonical}.{fractional}" if dot and fractional else canonical


def looks_float(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def first_present(names: dict[str, str], *candidates: str) -> str | None:
    for candidate in candidates:
        if candidate in names:
            return names[candidate]
    return None
