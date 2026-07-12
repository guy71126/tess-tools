from __future__ import annotations

from .models import ProductRecord, SectorRecord


def build_sector_summary(sectors: list[SectorRecord]) -> dict:
    sector_values = sorted({sector.sector for sector in sectors})
    cameras = sorted({sector.camera for sector in sectors if sector.camera is not None})
    ccds = sorted({sector.ccd for sector in sectors if sector.ccd is not None})
    groups = contiguous_groups(sector_values)
    return {
        "n_sectors": len(sector_values),
        "sectors": sector_values,
        "first_sector": sector_values[0] if sector_values else None,
        "last_sector": sector_values[-1] if sector_values else None,
        "sector_span": (sector_values[-1] - sector_values[0] + 1) if sector_values else 0,
        "sector_groups": [{"start": start, "end": end, "count": end - start + 1} for start, end in groups],
        "n_sector_groups": len(groups),
        "max_contiguous_sector_count": max((end - start + 1 for start, end in groups), default=0),
        "cameras": cameras,
        "ccds": ccds,
        "cvz_like": len(sector_values) >= 20,
    }


def build_product_summary(products: list[ProductRecord]) -> dict:
    by_family: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    cadence_values = sorted({product.cadence_sec for product in products if product.cadence_sec is not None})
    for product in products:
        by_family[product.family] = by_family.get(product.family, 0) + 1
        by_provider[product.provider] = by_provider.get(product.provider, 0) + 1
        scope = str(product.extra.get("product_scope") or "unknown")
        by_scope[scope] = by_scope.get(scope, 0) + 1
    products_by_sector = build_products_by_sector(products)
    multi_sector_products = build_multi_sector_products(products)
    products_with_fetch_reference = [product for product in products if product.extra.get("fetch_reference")]
    products_with_file_uri = [product for product in products if product.extra.get("file_data_uri")]
    return {
        "n_products": len(products),
        "families": sorted(by_family),
        "providers": sorted(by_provider),
        "by_family": by_family,
        "by_provider": by_provider,
        "by_scope": by_scope,
        "cadence_sec": cadence_values,
        "n_preextracted_products": sum(1 for product in products if product.family != "TESSCut"),
        "n_tesscut_products": sum(1 for product in products if product.family == "TESSCut"),
        "n_multi_sector_products": by_scope.get("multi_sector", 0),
        "n_products_with_fetch_reference": len(products_with_fetch_reference),
        "n_preextracted_products_with_fetch_reference": sum(
            1 for product in products_with_fetch_reference if product.family != "TESSCut"
        ),
        "n_products_with_file_uri": len(products_with_file_uri),
        "n_preextracted_products_with_file_uri": sum(1 for product in products_with_file_uri if product.family != "TESSCut"),
        "fetch_reference_kinds": sorted(
            {
                str(product.extra.get("fetch_reference_kind"))
                for product in products_with_fetch_reference
                if product.extra.get("fetch_reference_kind")
            }
        ),
        "file_product_subgroups": sorted(
            {
                str(product.extra.get("file_product_subgroup"))
                for product in products_with_file_uri
                if product.extra.get("file_product_subgroup")
            }
        ),
        "has_preextracted_lightcurve": any(product.family != "TESSCut" for product in products),
        "has_tesscut": any(product.family == "TESSCut" for product in products),
        "product_count_by_sector": {str(row["sector"]): row["n_products"] for row in products_by_sector},
        "products_by_sector": products_by_sector,
        "multi_sector_products": multi_sector_products,
    }


def build_products_by_sector(products: list[ProductRecord]) -> list[dict]:
    by_sector: dict[int, list[ProductRecord]] = {}
    for product in products:
        if product.extra.get("product_scope") not in (None, "per_sector"):
            continue
        if product.sector is None:
            continue
        by_sector.setdefault(product.sector, []).append(product)
    rows: list[dict] = []
    for sector in sorted(by_sector):
        sector_products = by_sector[sector]
        families = sorted({product.family for product in sector_products})
        providers = sorted({product.provider for product in sector_products})
        scopes = sorted({str(product.extra.get("product_scope") or "unknown") for product in sector_products})
        rows.append(
            {
                "sector": sector,
                "n_products": len(sector_products),
                "n_preextracted_products": sum(1 for product in sector_products if product.family != "TESSCut"),
                "n_tesscut_products": sum(1 for product in sector_products if product.family == "TESSCut"),
                "families": families,
                "providers": providers,
                "scopes": scopes,
            }
        )
    return rows


def build_multi_sector_products(products: list[ProductRecord]) -> list[dict]:
    rows: list[dict] = []
    for product in products:
        if product.extra.get("product_scope") != "multi_sector":
            continue
        rows.append(
            {
                "family": product.family,
                "provider": product.provider,
                "sector": product.sector,
                "sector_start": product.extra.get("sector_start"),
                "sector_end": product.extra.get("sector_end"),
                "product_id": product.product_id,
                "fetch_reference_kind": product.extra.get("fetch_reference_kind"),
                "fetch_reference": product.extra.get("fetch_reference"),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row.get("sector_start") if row.get("sector_start") is not None else 999999,
            row.get("sector_end") if row.get("sector_end") is not None else 999999,
            str(row.get("product_id") or ""),
        ),
    )


def contiguous_groups(values: list[int]) -> list[tuple[int, int]]:
    if not values:
        return []
    groups: list[tuple[int, int]] = []
    start = values[0]
    prev = values[0]
    for value in values[1:]:
        if value == prev + 1:
            prev = value
            continue
        groups.append((start, prev))
        start = prev = value
    groups.append((start, prev))
    return groups
