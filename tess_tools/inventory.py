from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .cache import MetadataCache
from .crowding import empty_crowding_summary, discover_crowding
from .known import discover_known_objects, empty_known_summary
from .products import discover_products
from .recommend import build_fetch_plan, recommend_fetch
from .sectors import discover_sectors
from .summaries import build_product_summary, build_sector_summary
from .target import TargetSpec, resolve_target

INVENTORY_SCHEMA_VERSION = "tess-where.inventory.v0.3"


def build_inventory(
    spec: TargetSpec,
    *,
    cache: MetadataCache,
    refresh: bool = False,
    offline: bool = False,
    radius_arcsec: float = 60.0,
    enabled_providers: set[str] | None = None,
    best_for: str = "general",
    toi_catalog_path: Path | None = None,
    tess_eb_catalog_path: Path | None = None,
) -> dict:
    if enabled_providers is None:
        enabled_providers = {"tesscut"}
    if spec.kind == "invalid":
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "input": spec.raw,
            "target": spec.to_dict(),
            "sector_summary": build_sector_summary([]),
            "product_summary": build_product_summary([]),
            "crowding_summary": empty_crowding_summary(radius_arcsec, message="invalid target specification"),
            "known_object_summary": empty_known_summary(message="invalid target specification"),
            "sectors": [],
            "products": [],
            "providers": [],
            "fetch_plan": None,
            "recommendations": [],
            "errors": [spec.error or "invalid target specification"],
            "cache": {"hit": False, "key": None},
            "queried_at_utc": now_utc(),
        }

    cache_key = cache.key_for(
        {
            "schema": INVENTORY_SCHEMA_VERSION,
            "target": spec.to_cache_payload(),
            "radius_arcsec": radius_arcsec,
            "enabled_providers": sorted(enabled_providers),
            "best_for": best_for,
            "toi_catalog": catalog_cache_payload(toi_catalog_path),
            "tess_eb_catalog": catalog_cache_payload(tess_eb_catalog_path),
        }
    )
    if not refresh:
        cached = cache.read(cache_key)
        if cached is not None:
            cached["cache"] = {"hit": True, "key": cache_key}
            return cached

    if offline:
        return {
            "input": spec.raw,
            "target": spec.to_dict(),
            "sector_summary": build_sector_summary([]),
            "product_summary": build_product_summary([]),
            "crowding_summary": empty_crowding_summary(radius_arcsec, message="cache miss while offline"),
            "known_object_summary": empty_known_summary(message="cache miss while offline"),
            "sectors": [],
            "products": [],
            "providers": [],
            "fetch_plan": None,
            "recommendations": [],
            "errors": [f"cache miss for {spec.raw!r} while --offline is active"],
            "cache": {"hit": False, "key": cache_key},
            "queried_at_utc": now_utc(),
        }

    errors: list[str] = []
    resolved = resolve_target(spec, errors=errors, toi_catalog_path=toi_catalog_path)
    sectors = discover_sectors(resolved, errors=errors)
    product_discovery = discover_products(
        resolved,
        sectors,
        radius_arcsec=radius_arcsec,
        errors=errors,
        enabled_providers=enabled_providers,
    )
    if "gaia" in enabled_providers:
        crowding_summary, crowding_status = discover_crowding(
            resolved,
            sectors=sectors,
            radius_arcsec=radius_arcsec,
            errors=errors,
        )
        provider_statuses = product_discovery.providers + [crowding_status]
    else:
        crowding_summary = empty_crowding_summary(radius_arcsec, sectors=sectors, message="disabled by provider selection")
        provider_statuses = product_discovery.providers
    known_object_summary, known_provider_statuses = discover_known_objects(
        resolved,
        enabled_providers=enabled_providers,
        errors=errors,
        toi_catalog_path=toi_catalog_path,
        tess_eb_catalog_path=tess_eb_catalog_path,
    )
    provider_statuses.extend(known_provider_statuses)
    products = product_discovery.products
    fetch_plan = build_fetch_plan(resolved, sectors, products, best_for=best_for)
    recommendations = recommend_fetch(resolved, sectors, products, best_for=best_for)
    sector_summary = build_sector_summary(sectors)
    product_summary = build_product_summary(products)

    payload = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "input": spec.raw,
        "target": resolved.to_dict(),
        "sector_summary": sector_summary,
        "product_summary": product_summary,
        "crowding_summary": crowding_summary,
        "known_object_summary": known_object_summary,
        "sectors": [sector.to_dict() for sector in sectors],
        "products": [product.to_dict() for product in products],
        "providers": [provider.to_dict() for provider in provider_statuses],
        "fetch_plan": fetch_plan,
        "recommendations": recommendations,
        "errors": errors,
        "cache": {"hit": False, "key": cache_key},
        "queried_at_utc": now_utc(),
    }
    cache.write(cache_key, payload)
    return payload


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def catalog_cache_payload(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
