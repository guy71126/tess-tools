from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from .mast_api import invoke_mast_service, mast_data_rows
from .models import ProductRecord, ProviderStatus, ResolvedTarget, SectorRecord


MAST_DOWNLOAD_URL = "https://mast.stsci.edu/api/v0.1/Download/file?uri={uri}"
MAST_PRODUCT_LOOKUP_CHUNK_SIZE = 100
MAST_HLSP_COLLECTIONS = (
    ("QLP", "QLP", "mast.caom.filtered.qlp.http"),
    ("TESS-SPOC", "TESS-SPOC", "mast.caom.filtered.tess-spoc.http"),
)


PRODUCT_CLASSIFIERS = [
    {
        "family": "TESS-SPOC",
        "provider": "tess-spoc",
        "required": ("tess-spoc",),
        "excluded": (),
    },
    {
        "family": "SPOC",
        "provider": "spoc",
        "required": ("spoc",),
        "excluded": ("tess-spoc",),
    },
    {
        "family": "QLP",
        "provider": "qlp",
        "required": ("qlp",),
        "excluded": (),
    },
    {
        "family": "TGLC",
        "provider": "tglc",
        "required": ("tglc",),
        "excluded": (),
    },
    {
        "family": "T16",
        "provider": "t16",
        "required": ("t16",),
        "excluded": (),
    },
    {
        "family": "CDIPS",
        "provider": "cdips",
        "required": ("cdips",),
        "excluded": (),
    },
]

MAST_CAOM_COLUMNS = ",".join(
    [
        "obsid",
        "obs_id",
        "obs_collection",
        "dataproduct_type",
        "provenance_name",
        "target_name",
        "sequence_number",
    ]
)


class ProductDiscovery:
    def __init__(self, products: list[ProductRecord], providers: list[ProviderStatus]) -> None:
        self.products = products
        self.providers = providers


def discover_products(
    target: ResolvedTarget,
    sectors: list[SectorRecord],
    *,
    radius_arcsec: float,
    errors: list[str],
    enabled_providers: set[str] | None = None,
) -> ProductDiscovery:
    if enabled_providers is None:
        enabled_providers = {"tesscut"}
    products: list[ProductRecord] = []
    providers: list[ProviderStatus] = []

    if "tesscut" in enabled_providers:
        tesscut_products = discover_tesscut_products(sectors)
        products.extend(tesscut_products)
        providers.append(
            ProviderStatus(
                name="TESSCut",
                status="ok" if tesscut_products else "empty",
                records=len(tesscut_products),
                message="FFI cutout availability inferred from sector discovery.",
            )
        )
    else:
        providers.append(
            ProviderStatus(
                name="TESSCut",
                status="skipped",
                records=0,
                message="disabled by provider selection",
            )
        )

    if "mast" in enabled_providers:
        mast_discovery = discover_mast_product_collections(
            target,
            radius_arcsec=radius_arcsec,
            errors=errors,
        )
        products.extend(mast_discovery.products)
        providers.extend(mast_discovery.providers)
    else:
        providers.append(
            ProviderStatus(
                name="MAST observations",
                status="skipped",
                records=0,
                message="disabled by provider selection",
            )
        )
    return ProductDiscovery(dedupe_products(products), providers)


def discover_tesscut_products(sectors: list[SectorRecord]) -> list[ProductRecord]:
    # TESSCut cutout availability is known from sector discovery and is a valid
    # future fetch target even when no pre-extracted light curve is found.
    return [
        ProductRecord(
            family="TESSCut",
            provider="tesscut",
            sector=sector.sector,
            product_id=sector.sector_name,
            description="FFI cutout available through TESSCut",
            source=sector.source,
            extra={
                "product_scope": "per_sector",
                "sector_start": sector.sector,
                "sector_end": sector.sector,
            },
        )
        for sector in sectors
    ]


def discover_mast_timeseries_products(
    target: ResolvedTarget,
    *,
    radius_arcsec: float,
    errors: list[str],
) -> tuple[list[ProductRecord], ProviderStatus]:
    """Retain the original aggregate MAST discovery API for library callers."""
    return _discover_mast_core_products(target, radius_arcsec=radius_arcsec, errors=errors)


def discover_mast_product_collections(
    target: ResolvedTarget,
    *,
    radius_arcsec: float,
    errors: list[str],
) -> ProductDiscovery:
    products, mast_status = _discover_mast_core_products(
        target,
        radius_arcsec=radius_arcsec,
        errors=errors,
    )
    providers = [mast_status]

    if target.tic_id is None:
        for family, _, _ in MAST_HLSP_COLLECTIONS:
            providers.append(
                ProviderStatus(
                    name=f"MAST {family}",
                    status="skipped",
                    records=0,
                    message="exact HLSP availability query requires a TIC ID",
                )
            )
        return ProductDiscovery(dedupe_products(products), providers)

    for family, provenance_name, source in MAST_HLSP_COLLECTIONS:
        try:
            rows = query_mast_caom_filtered_tic(target.tic_id, provenance_name=provenance_name)
        except Exception as exc:  # pragma: no cover - network/service dependent
            message = f"MAST {family} query failed for TIC {target.tic_id}: {exc}"
            errors.append(message)
            providers.append(ProviderStatus(name=f"MAST {family}", status="error", records=0, message=message))
            continue

        family_products = [product for product in mast_rows_to_products(rows, source=source) if product.family == family]
        family_products, lookup_message = enrich_products_with_mast_file_metadata(family_products)
        products.extend(family_products)
        message = f"exact provenance_name={provenance_name} query returned {len(rows)} row(s)"
        if lookup_message:
            message += f"; {lookup_message}"
        providers.append(
            ProviderStatus(
                name=f"MAST {family}",
                status="ok" if family_products else "empty",
                records=len(family_products),
                message=message,
            )
        )

    return ProductDiscovery(dedupe_products(products), providers)


def _discover_mast_core_products(
    target: ResolvedTarget,
    *,
    radius_arcsec: float,
    errors: list[str],
) -> tuple[list[ProductRecord], ProviderStatus]:
    if target.tic_id is None and (target.ra_deg is None or target.dec_deg is None):
        message = "skipped MAST product inventory because target TIC and coordinates are unavailable"
        errors.append(message)
        return [], ProviderStatus(name="MAST observations", status="skipped", message=message)

    rows: list[dict[str, Any]] | None = None
    rows_source: str | None = None
    attempt_messages: list[str] = []

    if target.tic_id is not None:
        try:
            rows = query_mast_caom_filtered_tic(target.tic_id)
            rows_source = "mast.caom.filtered.http"
            attempt_messages.append(f"CAOM filtered target_name={target.tic_id} ok rows={len(rows)}")
        except Exception as exc:  # pragma: no cover - network/service dependent
            attempt_messages.append(f"CAOM filtered target_name={target.tic_id} failed: {exc}")
            rows = None
            rows_source = None

    if rows is None or (not rows and target.ra_deg is not None and target.dec_deg is not None):
        for attempt_radius in mast_query_radii(radius_arcsec):
            if target.ra_deg is None or target.dec_deg is None:
                break
            try:
                rows = query_mast_caom_cone(target.ra_deg, target.dec_deg, attempt_radius)
                raw_row_count = len(rows)
                if target.tic_id is not None:
                    rows = [row for row in rows if mast_row_matches_tic(row, target.tic_id)]
                rows_source = "mast.caom.cone.http"
                attempt_messages.append(
                    f"CAOM cone radius={attempt_radius:g} arcsec ok rows={raw_row_count} retained={len(rows)}"
                )
                break
            except Exception as exc:  # pragma: no cover - network/service dependent
                attempt_messages.append(f"CAOM cone radius={attempt_radius:g} arcsec failed: {exc}")

    if rows is None:
        message = "MAST observation query failed after retries: " + " | ".join(attempt_messages)
        errors.append(message)
        return [], ProviderStatus(name="MAST observations", status="error", message=message)

    products = mast_rows_to_products(rows, source=rows_source or "mast.caom.unknown.http")
    products, product_lookup_message = enrich_products_with_mast_file_metadata(products)
    if product_lookup_message:
        attempt_messages.append(product_lookup_message)
    return products, ProviderStatus(
        name="MAST observations",
        status="ok",
        records=len(products),
        message="; ".join(attempt_messages),
    )


def query_mast_caom_filtered_tic(tic_id: str, *, provenance_name: str | None = None) -> list[dict[str, Any]]:
    filters = [{"paramName": "target_name", "values": [str(int(tic_id))]}]
    if provenance_name is None:
        filters.insert(0, {"paramName": "obs_collection", "values": ["TESS"]})
    else:
        filters.insert(0, {"paramName": "provenance_name", "values": [provenance_name]})
    payload = invoke_mast_service(
        "Mast.Caom.Filtered",
        {
            "columns": MAST_CAOM_COLUMNS,
            "filters": filters,
        },
        timeout_sec=12,
        pagesize=2000,
    )
    return mast_data_rows(payload)


def query_mast_caom_cone(ra_deg: float, dec_deg: float, radius_arcsec: float) -> list[dict[str, Any]]:
    payload = invoke_mast_service(
        "Mast.Caom.Cone",
        {
            "ra": ra_deg,
            "dec": dec_deg,
            "radius": radius_arcsec / 3600.0,
        },
        timeout_sec=12,
    )
    return mast_data_rows(payload)


def query_mast_caom_products(obsids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    clean_obsids = [str(obsid) for obsid in obsids if str(obsid).strip()]
    for start in range(0, len(clean_obsids), MAST_PRODUCT_LOOKUP_CHUNK_SIZE):
        chunk = clean_obsids[start : start + MAST_PRODUCT_LOOKUP_CHUNK_SIZE]
        payload = invoke_mast_service(
            "Mast.Caom.Products",
            {"obsid": ",".join(chunk)},
            timeout_sec=12,
            pagesize=20000,
        )
        rows.extend(mast_data_rows(payload))
    return rows


def enrich_products_with_mast_file_metadata(products: list[ProductRecord]) -> tuple[list[ProductRecord], str | None]:
    obsids = sorted(
        {
            str(product.extra.get("mast_obsid"))
            for product in products
            if product.extra.get("mast_obsid") and not product.extra.get("data_uri")
        },
        key=obsid_sort_key,
    )
    if not obsids:
        return products, None
    try:
        file_rows = query_mast_caom_products(obsids)
    except Exception as exc:  # pragma: no cover - network/service dependent
        return products, f"CAOM products lookup failed for {len(obsids)} obsids: {exc}"

    best_file_by_obsid: dict[str, dict[str, Any]] = {}
    for row in file_rows:
        obsid = clean_optional(row.get("obsID") or row.get("obsid") or row.get("parent_obsid"))
        if not obsid:
            continue
        current = best_file_by_obsid.get(str(obsid))
        if current is None or product_file_score(row) > product_file_score(current):
            best_file_by_obsid[str(obsid)] = row

    enriched: list[ProductRecord] = []
    n_enriched = 0
    for product in products:
        obsid = clean_optional(product.extra.get("mast_obsid"))
        file_row = best_file_by_obsid.get(str(obsid)) if obsid else None
        if file_row is None:
            enriched.append(product)
            continue
        enriched.append(enrich_product_with_file_row(product, file_row))
        n_enriched += 1
    return enriched, f"CAOM products lookup ok obsids={len(obsids)} rows={len(file_rows)} enriched={n_enriched}"


def enrich_product_with_file_row(product: ProductRecord, file_row: dict[str, Any]) -> ProductRecord:
    data_uri = clean_optional(file_row.get("dataURI") or file_row.get("dataUri"))
    filename = clean_optional(file_row.get("productFilename"))
    file_cadence_sec = infer_file_cadence_sec(file_row)
    extra = dict(product.extra)
    extra.update(
        {
            "file_data_uri": data_uri,
            "file_name": filename,
            "file_description": clean_optional(file_row.get("description")),
            "file_product_type": clean_optional(file_row.get("productType")),
            "file_product_subgroup": clean_optional(file_row.get("productSubGroupDescription")),
            "file_role": infer_file_role(file_row),
            "file_cadence_sec": file_cadence_sec,
            "file_size": clean_optional(file_row.get("size")),
            "file_calib_level": clean_optional(file_row.get("calib_level")),
        }
    )
    if data_uri:
        extra["data_uri"] = data_uri
        extra["fetch_reference_kind"] = "mast_data_uri"
        extra["fetch_reference"] = data_uri
    access_url = product.access_url
    if data_uri:
        access_url = MAST_DOWNLOAD_URL.format(uri=quote(data_uri, safe=":"))
    cadence_sec = product.cadence_sec if product.cadence_sec is not None else file_cadence_sec
    return replace(product, cadence_sec=cadence_sec, access_url=access_url, extra=extra)


def product_file_score(row: dict[str, Any]) -> tuple[int, int, int, int, int]:
    product_type = str(row.get("productType", "")).upper()
    filename = str(row.get("productFilename", "")).lower()
    data_uri = clean_optional(row.get("dataURI") or row.get("dataUri"))
    role = infer_file_role(row)
    return (
        1 if product_type == "SCIENCE" else 0,
        {"lightcurve": 5, "dv-timeseries": 4, "target-pixel": 3, "report": 1}.get(role, 0),
        1 if filename.endswith(".fits") else 0,
        1 if data_uri else 0,
        -len(filename),
    )


def infer_file_role(row: dict[str, Any]) -> str:
    product_type = str(row.get("productType", "")).upper()
    subgroup = str(row.get("productSubGroupDescription", "")).upper()
    filename = str(row.get("productFilename", "")).lower()
    if product_type == "INFO" or filename.endswith((".pdf", ".xml", ".txt")):
        return "report"
    if subgroup == "LC" or filename.endswith(("_lc.fits", "_fast-lc.fits", "_llc.fits", "_llc.fits.gz")):
        return "lightcurve"
    if subgroup == "TP" or filename.endswith("_tp.fits"):
        return "target-pixel"
    if subgroup == "DVT" or filename.endswith("_dvt.fits"):
        return "dv-timeseries"
    return "unknown"


def infer_file_cadence_sec(row: dict[str, Any]) -> int | None:
    filename = str(row.get("productFilename", "")).lower()
    if "_fast-lc.fits" in filename or "-fast-lc.fits" in filename:
        return 20
    if filename.endswith("_lc.fits"):
        return 120
    if filename.endswith("_tp.fits"):
        return 120
    family = infer_product_family(row)
    sector = infer_sector(row)
    if family in {"QLP", "TESS-SPOC"}:
        return default_ffi_cadence_sec(family, sector)
    return None


def obsid_sort_key(obsid: str) -> tuple[int, str]:
    try:
        return int(obsid), obsid
    except ValueError:
        return 999999999, obsid


def mast_rows_to_products(rows: list[dict[str, Any]], *, source: str) -> list[ProductRecord]:
    products: list[ProductRecord] = []
    for row_dict in rows:
        family = infer_product_family(row_dict)
        if family is None:
            continue
        obs_collection = str(row_dict.get("obs_collection", "")).upper()
        if obs_collection not in {"", "TESS", "HLSP"}:
            continue
        sector = infer_sector(row_dict)
        cadence = infer_cadence(row_dict) or default_ffi_cadence_sec(family, sector)
        scope = classify_product_scope(row_dict, fallback_sector=sector)
        products.append(
            ProductRecord(
                family=family,
                provider=provider_for_family(family),
                sector=sector,
                cadence_sec=cadence,
                product_id=str(row_dict.get("obs_id") or row_dict.get("productFilename") or ""),
                description=str(row_dict.get("dataproduct_type") or row_dict.get("intentType") or ""),
                access_url=clean_optional(row_dict.get("dataURL")),
                source=source,
                extra={
                    "mast_obsid": clean_optional(row_dict.get("obsid") or row_dict.get("obsID")),
                    "data_uri": clean_optional(row_dict.get("dataURI") or row_dict.get("dataUri")),
                    "proposal_id": clean_optional(row_dict.get("proposal_id") or row_dict.get("proposalId")),
                    "provenance_name": clean_optional(row_dict.get("provenance_name")),
                    "target_name": clean_optional(row_dict.get("target_name")),
                    "sequence_number": clean_optional(row_dict.get("sequence_number")),
                    "s_ra": clean_optional(row_dict.get("s_ra")),
                    "s_dec": clean_optional(row_dict.get("s_dec")),
                    "fetch_reference_kind": infer_fetch_reference_kind(row_dict),
                    "fetch_reference": infer_fetch_reference(row_dict),
                    "product_scope": scope["product_scope"],
                    "sector_start": scope["sector_start"],
                    "sector_end": scope["sector_end"],
                },
            )
        )
    return products


def infer_fetch_reference_kind(row: dict[str, Any]) -> str:
    if clean_optional(row.get("dataURI") or row.get("dataUri")):
        return "mast_data_uri"
    if clean_optional(row.get("dataURL")):
        return "mast_data_url"
    if clean_optional(row.get("obsid") or row.get("obsID")):
        return "mast_obsid"
    if clean_optional(row.get("obs_id")):
        return "mast_obs_id"
    return "none"


def infer_fetch_reference(row: dict[str, Any]) -> Any:
    for key in ("dataURI", "dataUri", "dataURL", "obsid", "obsID", "obs_id"):
        value = clean_optional(row.get(key))
        if value:
            return value
    return None


def classify_product_scope(row: dict[str, Any], *, fallback_sector: int | None = None) -> dict[str, int | str | None]:
    text = " ".join(str(row.get(key, "")) for key in ("obs_id", "productFilename")).lower()
    multi = re.search(r"(?:^|[-_])s(\d{4})[-_]s(\d{4})(?:$|[-_])", text)
    if multi:
        return {
            "product_scope": "multi_sector",
            "sector_start": int(multi.group(1)),
            "sector_end": int(multi.group(2)),
        }
    single = re.search(r"(?:^|[-_])s(\d{4})(?:$|[-_])", text)
    if single:
        sector = int(single.group(1))
        return {
            "product_scope": "per_sector",
            "sector_start": sector,
            "sector_end": sector,
        }
    if fallback_sector is not None:
        return {
            "product_scope": "per_sector",
            "sector_start": fallback_sector,
            "sector_end": fallback_sector,
        }
    return {
        "product_scope": "unknown",
        "sector_start": None,
        "sector_end": None,
    }


def mast_query_radii(radius_arcsec: float) -> list[float]:
    candidates = [radius_arcsec]
    if radius_arcsec > 10.0:
        candidates.append(10.0)
    if radius_arcsec > 3.0:
        candidates.append(3.0)
    radii: list[float] = []
    for candidate in candidates:
        if candidate <= 0:
            continue
        value = round(float(candidate), 6)
        if value not in radii:
            radii.append(value)
    return radii


def infer_product_family(row: dict[str, Any]) -> str | None:
    text = " ".join(
        str(row.get(key, ""))
        for key in ("obs_id", "provenance_name", "project", "target_name", "productFilename")
    ).lower()
    for classifier in PRODUCT_CLASSIFIERS:
        if all(term in text for term in classifier["required"]) and not any(
            term in text for term in classifier["excluded"]
        ):
            return str(classifier["family"])
    return None


def provider_for_family(family: str) -> str:
    if family == "TESSCut":
        return "tesscut"
    for classifier in PRODUCT_CLASSIFIERS:
        if classifier["family"] == family:
            return str(classifier["provider"])
    return family.lower()


def infer_sector(row: dict[str, Any]) -> int | None:
    for key in ("sequence_number", "sequenceNumber", "sector"):
        value = row.get(key)
        if value is None or str(value).strip() in {"", "--"}:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    text = " ".join(str(row.get(key, "")) for key in ("obs_id", "productFilename"))
    match = re.search(r"(?:^|[-_])s(\d{4})(?:$|[-_])", text.lower())
    if match:
        return int(match.group(1))
    return None


def infer_cadence(row: dict[str, Any]) -> int | None:
    text = " ".join(str(row.get(key, "")) for key in ("obs_id", "provenance_name", "productFilename")).lower()
    for cadence in (20, 120, 200, 600, 1800):
        if re.search(rf"(?<!\d){cadence}[-_\s]?s(?:ec(?:ond)?s?)?(?![a-z0-9])", text):
            return cadence
    if re.search(r"(?<!\d)2[-_\s]?min(?:ute)?s?(?![a-z0-9])", text):
        return 120
    if re.search(r"(?<!\d)10[-_\s]?min(?:ute)?s?(?![a-z0-9])", text):
        return 600
    if re.search(r"(?<!\d)30[-_\s]?min(?:ute)?s?(?![a-z0-9])", text):
        return 1800
    return None


def default_ffi_cadence_sec(family: str, sector: int | None) -> int | None:
    if family not in {"QLP", "TESS-SPOC"} or sector is None:
        return None
    if sector <= 26:
        return 1800
    if sector <= 55:
        return 600
    return 200


def table_row_to_dict(row: Any) -> dict[str, Any]:
    return {name: row[name].item() if hasattr(row[name], "item") else row[name] for name in row.colnames}


def clean_optional(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    return None if text in {"", "--", "nan"} else text


def mast_row_matches_tic(row: dict[str, Any], tic_id: str) -> bool:
    target_name = clean_optional(row.get("target_name"))
    if target_name is None:
        return False
    text = str(target_name).strip()
    if text.lower().startswith("tic"):
        text = text[3:].strip()
    try:
        return str(int(float(text))) == str(int(tic_id))
    except ValueError:
        return False


def product_record_score(product: ProductRecord) -> tuple[int, int, int, int]:
    return (
        1 if product.extra.get("fetch_reference_kind") == "mast_data_uri" else 0,
        1 if product.extra.get("file_role") == "lightcurve" else 0,
        1 if product.extra.get("file_name") else 0,
        1 if product.access_url else 0,
    )


def dedupe_products(products: list[ProductRecord]) -> list[ProductRecord]:
    best_by_key: dict[tuple, ProductRecord] = {}
    for product in products:
        key = (product.family, product.provider, product.sector, product.cadence_sec, product.product_id)
        current = best_by_key.get(key)
        if current is None or product_record_score(product) > product_record_score(current):
            best_by_key[key] = product
    return sorted(
        best_by_key.values(),
        key=lambda item: (item.sector if item.sector is not None else 999999, item.family),
    )
