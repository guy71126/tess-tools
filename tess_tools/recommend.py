from __future__ import annotations

from .models import ProductRecord, ResolvedTarget, SectorRecord

BEST_FOR_CHOICES = (
    "general",
    "transits",
    "rotation",
    "flares",
    "eclipses",
    "raw-variability",
    "asteroseismology",
)


def recommend_fetch(
    target: ResolvedTarget,
    sectors: list[SectorRecord],
    products: list[ProductRecord],
    *,
    best_for: str = "general",
) -> list[dict]:
    fetch_plan = build_fetch_plan(target, sectors, products, best_for=best_for)
    return [] if fetch_plan is None else [fetch_plan]


def build_fetch_plan(
    target: ResolvedTarget,
    sectors: list[SectorRecord],
    products: list[ProductRecord],
    *,
    best_for: str = "general",
) -> dict | None:
    if not sectors and not products:
        return None

    label = f"TIC {target.tic_id}" if target.tic_id else target.label()
    sector_values = sorted({sector.sector for sector in sectors})
    sector_arg = "all" if sector_values else "available"
    caveats: list[str] = []
    product, reason = choose_product(products, best_for=best_for)

    if product == "cutout":
        product = "cutout"
        caveats.append("Requires aperture choice and background handling in tess-fetch.")
    selected_references = selected_product_references(products, product)

    return {
        "schema_version": "tess-where.fetch-plan.v0.4",
        "target_label": label,
        "best_for": best_for,
        "product": product,
        "sectors": sector_values,
        "sector_argument": sector_arg,
        "n_selected_product_references": len(selected_references),
        "selected_product_references": selected_references,
        "command": build_fetch_command(label, product, sector_arg, best_for),
        "reason": reason,
        "caveats": caveats,
    }


def choose_product(products: list[ProductRecord], *, best_for: str) -> tuple[str, str]:
    families = {product.family for product in products}
    has_cutout = "TESSCut" in families

    if best_for == "transits":
        if "SPOC" in families:
            return "spoc", "SPOC products were detected; this mode prefers SPOC for transit-like work when available."
        if "TESS-SPOC" in families:
            return "tess-spoc", "TESS-SPOC products were detected; this mode prefers them for transit-like work."
        if "QLP" in families:
            return "qlp", "QLP products were detected; this mode uses them as the broad FFI transit-search default."
    elif best_for in {"rotation", "raw-variability"}:
        if has_cutout:
            return "cutout", "This mode prefers TESSCut so tess-fetch can preserve low-frequency variability with explicit aperture choices."
        if "QLP" in families:
            return "qlp", "QLP products were detected; this mode uses them when cutouts are unavailable."
    elif best_for == "flares":
        fastest = fastest_preextracted_product(products)
        if fastest is not None:
            provider, cadence = fastest
            return provider, f"This mode prefers the fastest detected pre-extracted cadence ({cadence} seconds)."
        if has_cutout:
            return "cutout", "This mode uses TESSCut because no pre-extracted cadence metadata was detected."
    elif best_for == "eclipses":
        if "SPOC" in families:
            return "spoc", "SPOC products were detected; this mode prefers them for sharp eclipse-like events when available."
        if "QLP" in families:
            return "qlp", "QLP products were detected; this mode uses them as a broad FFI eclipse-search default."
    elif best_for == "asteroseismology":
        fastest = fastest_preextracted_product(products)
        if fastest is not None:
            provider, cadence = fastest
            return provider, f"This mode prefers the fastest detected pre-extracted cadence ({cadence} seconds)."

    if "SPOC" in families:
        return "spoc", "SPOC products were detected; these are a strong default when available."
    if "TESS-SPOC" in families:
        return "tess-spoc", "TESS-SPOC products were detected through MAST metadata."
    if "QLP" in families:
        return "qlp", "QLP products were detected and are a broad default for FFI light curves."
    if has_cutout:
        return "cutout", "No pre-extracted light-curve product was detected, but TESSCut sectors are available."
    return "best", "TESS observations were detected, but no preferred product family was identified."


def fastest_preextracted_product(products: list[ProductRecord]) -> tuple[str, int] | None:
    candidates: list[tuple[int, str]] = []
    for product in products:
        if product.family == "TESSCut" or product.cadence_sec is None:
            continue
        candidates.append((product.cadence_sec, product.provider))
    if not candidates:
        return None
    cadence, provider = sorted(candidates)[0]
    return provider, cadence


def selected_product_references(products: list[ProductRecord], product_key: str) -> list[dict]:
    matching_products = [
        product
        for product in products
        if product_matches_key(product, product_key) and product.extra.get("fetch_reference")
    ]
    lightcurve_products = [product for product in matching_products if product.extra.get("file_role") == "lightcurve"]
    if lightcurve_products:
        matching_products = lightcurve_products

    rows: list[dict] = []
    for product in matching_products:
        rows.append(
            {
                "family": product.family,
                "provider": product.provider,
                "sector": product.sector,
                "cadence_sec": product.cadence_sec,
                "product_id": product.product_id,
                "product_scope": product.extra.get("product_scope"),
                "sector_start": product.extra.get("sector_start"),
                "sector_end": product.extra.get("sector_end"),
                "fetch_reference_kind": product.extra.get("fetch_reference_kind"),
                "fetch_reference": product.extra.get("fetch_reference"),
                "access_url": product.access_url,
                "file_name": product.extra.get("file_name"),
                "file_role": product.extra.get("file_role"),
                "file_product_subgroup": product.extra.get("file_product_subgroup"),
                "file_cadence_sec": product.extra.get("file_cadence_sec"),
                "file_size": product.extra.get("file_size"),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row.get("sector_start") if row.get("sector_start") is not None else row.get("sector") or 999999,
            row.get("sector_end") if row.get("sector_end") is not None else row.get("sector") or 999999,
            str(row.get("file_name") or row.get("product_id") or ""),
        ),
    )


def product_matches_key(product: ProductRecord, product_key: str) -> bool:
    if product_key == "cutout":
        return product.family == "TESSCut" or product.provider == "tesscut"
    return product.provider == product_key or product.family.lower() == product_key


def build_fetch_command(label: str, product: str, sector_arg: str, best_for: str) -> str:
    command = f"tess-fetch {label} --product {product} --sectors {sector_arg}"
    if best_for != "general":
        command += f" --best-for {best_for}"
    return command
