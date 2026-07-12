# TESS Tools

[![CI](https://github.com/guy71126/tess-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/guy71126/tess-tools/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Small command-line tools for making TESS observation metadata and data-product access less fragmented.

The first command is `tess-where`, which answers what TESS sectors and public products appear to exist for a TIC or sky position. It is metadata-first and does not download large light-curve files.

`tess-fetch` is the companion command that consumes `tess-where` JSON and downloads selected file references. It is intentionally narrow at this stage: it fetches selected products already discovered by `tess-where`.

## Installation

TESS Tools requires Python 3.10 or newer. Until the first PyPI release, install directly from GitHub:

```powershell
python -m pip install "tess-tools[mast,fits] @ git+https://github.com/guy71126/tess-tools.git"
tess-where --help
tess-fetch --help
```

The base package has no required third-party dependencies. Install the `mast` extra for astroquery-backed fallbacks and the `fits` extra for FITS normalization and TESSCut light-curve extraction.

After the first PyPI release, the normal installation command will be:

```powershell
python -m pip install "tess-tools[mast,fits]"
```

## Development Usage

From this directory:

```powershell
python -m tess_tools --help
python -m tess_tools TIC 21002564 --json-out inventory.json
python -m tess_tools 102.7 -70.5 --csv-out inventory
python -m tess_tools targets.csv --sleep-sec 0.5 --json-out batch_inventory.json
python -m tess_tools targets.csv --providers tesscut --csv-out fast_sector_inventory
python -m tess_tools TIC 21002564 --providers tesscut,mast --json-out inventory_with_mast.json
python -m tess_tools TIC 150428135 --providers tesscut,toi --json-out toi_inventory.json
python -m tess_tools TOI-700 --providers tesscut,toi --json-out toi_inventory.json
python -m tess_tools TIC 150428135 --providers tesscut,toi --toi-catalog toi_snapshot.csv --json-out toi_inventory.json
python -m tess_tools TIC 123456789 --providers tesscut,tesseb --tess-eb-catalog tess_eb_snapshot.csv --json-out eb_inventory.json
python -m tess_tools TIC 21002564 --best-for rotation --json-out rotation_inventory.json
python -m tess_tools.fetch inventory_with_mast.json --dry-run --manifest-out fetch_manifest.json
python -m tess_tools.fetch inventory_with_mast.json --out-dir lightcurves
```

The core TIC and coordinate inventory paths use direct bounded HTTP calls and do not require third-party packages. Optional dependencies enable astroquery-backed fallbacks where available:

```powershell
python -m pip install -e .[mast]
```

Without `astroquery` and `astropy`, `tess-where` can still resolve TIC IDs through direct MAST HTTP, query TESSCut sectors through the direct TESSCut HTTP fallback, and use the direct MAST HTTP product-inventory provider.

The default provider set is `tesscut`, which gives a fast and stable sector/cutout inventory. Use `--providers tesscut,mast` when you explicitly want MAST observation metadata for pre-extracted products; that query is bounded but still more service-dependent than TESSCut sector lookup.
When `mast` is enabled for a TIC target, `tess-where` performs exact `provenance_name` availability queries for the QLP and TESS-SPOC HLSP collections in addition to the mission-produced SPOC query. Provider rows named `MAST QLP` and `MAST TESS-SPOC` distinguish an empty collection from a failed query. QLP `_llc.fits` and TESS-SPOC `_lc.fits` files are recognized as light curves, enriched with concrete MAST data URIs, and assigned the appropriate 30-minute, 10-minute, or 200-second FFI cadence from the sector era.
Use `--providers tesscut,gaia` to add an opt-in Gaia neighbor/crowding audit. The Gaia provider tries direct MAST HTTP catalog queries first, keeps optional `astroquery`/`astropy` as a fallback, and records `crowding_summary` risk, neighbor counts, nearest-neighbor separation, query provenance, and flux-ratio estimates when magnitudes are available. It also reports a heuristic aperture-contamination ratio and dilution factor using nominal TESS-pixel separations; treat those as triage aids, not calibrated aperture photometry. When sector camera/CCD metadata is available, `sector_geometry` records how many observing geometries the same crowding field spans.
Use `--providers tesscut,toi` to add an opt-in TOI crossmatch by TIC ID through the NASA Exoplanet Archive TOI TAP service. Results are recorded in `known_object_summary`. Use `--toi-catalog` with a local CSV, TSV, or JSON snapshot to avoid live TOI queries during repeatable batch runs.
TOI host and planet identifiers such as `TOI-700` and `TOI-700.01` are accepted as targets. They resolve to the host TIC before sector and product discovery; the original TOI identifier remains in `target.extra.toi_id`. A local `--toi-catalog` snapshot is also used for this resolution path.
Use `--providers tesscut,tesseb --tess-eb-catalog <path>` to crossmatch against a local TESS EB snapshot by TIC ID. The alias `tess-eb` is also accepted in `--providers`.
For TIC targets, the MAST provider first tries a filtered `target_name=<TIC>` observation query, which is much faster and cleaner than the cone fallback when products are indexed by TIC.

Use `--best-for general|transits|rotation|flares|eclipses|raw-variability|asteroseismology` to change the future `tess-fetch` recommendation. This is rule-based and transparent; the selected product and reason are written into `fetch_plan`.

## Outputs

The JSON output is the primary contract for future `tess-fetch` support. Each target inventory includes:

- `target`: resolved TIC/coordinate metadata
- `sectors`: TESS sector/camera/CCD records
- `products`: discoverable product records
- `crowding_summary`: optional Gaia neighbor/crowding audit
- `known_object_summary`: optional known-object crossmatch audit, currently TOI and local TESS EB when enabled
- `providers`: provider status records
- `fetch_plan`: the recommended future `tess-fetch` plan
- `errors`: warnings and provider failures

CSV output is a flattened convenience export and currently writes sector, product, and fetch-plan tables.
The current CSV outputs are:

- `<prefix>_targets.csv`
- `<prefix>_sectors.csv`
- `<prefix>_products.csv`
- `<prefix>_providers.csv`
- `<prefix>_fetch_plans.csv`
- `<prefix>_crowding_neighbors.csv`

Invalid rows in batch CSV input are preserved as target rows with errors, rather than being silently skipped.
Target CSV rows include compact product summaries such as product families, providers, product scopes, pre-extracted product count, sectors with product records, known-object fields, and crowding summary fields when available. Crowding-neighbor CSV rows expose the retained Gaia neighbor list for audit and filtering, including nominal TESS-pixel separations and weighted flux-ratio estimates. Target rows also include compact crowding sector-geometry fields for filtering targets whose contamination estimate spans multiple camera/CCD configurations.
When available from MAST, product rows also preserve fetch-reference metadata such as `extra_mast_obsid`, `extra_data_uri`, `extra_file_name`, `extra_fetch_reference_kind`, and `extra_fetch_reference`; target rows summarize how many products have observation references and concrete file URIs.
Fetch plans include `selected_product_references` for the recommended product when file or observation references are available. When light-curve files are present, `tess-where` prefers those over DVT, target-pixel, and report files for the default fetch handoff.

## tess-fetch

`tess-fetch` reads a `tess-where` JSON inventory:

```powershell
python -m tess_tools.fetch inventory_with_mast.json --dry-run --manifest-out fetch_manifest.json
python -m tess_tools.fetch inventory_with_mast.json --target "TIC 261136679" --sectors 27 --out-dir lightcurves
python -m tess_tools.fetch inventory_with_mast.json --resume --normalize-csv normalized_lightcurves.csv
python -m tess_tools.fetch inventory_with_mast.json --resume --normalize-csv normalized_clean.csv --quality-mask 1
python -m tess_tools.fetch inventory_with_mast.json --resume --normalize-csv normalized_conservative.csv --quality-preset conservative
python -m tess_tools.fetch inventory_with_mast.json --resume --normalize-csv normalized_recommended.csv --quality-preset recommended
python -m tess_tools.fetch inventory_with_mast.json --resume --normalize-csv normalized_qlp.csv --quality-preset qlp-recommended
python -m tess_tools.fetch cutout_inventory.json --product cutout --sectors 2 --cutout-size 7 --out-dir cutouts
python -m tess_tools.fetch cutout_inventory.json --product cutout --sectors 2 --cutout-lightcurve-csv cutout_lc.csv --cutout-background median-outside
python -m tess_tools.fetch cutout_inventory.json --product cutout --sectors 2 --cutout-lightcurve-csv cutout_lc.csv --cutout-aperture-mode pixels --cutout-aperture-pixels "2,2;2,3;3,2"
python -m tess_tools.fetch cutout_inventory.json --product cutout --sectors 2 --cutout-lightcurve-csv cutout_lc.csv --cutout-aperture-summary-json aperture_summary.json --cutout-aperture-mask-csv aperture_mask.csv --cutout-aperture-report-html aperture_report.html
```

By default it fetches selected references with `file_role = lightcurve`. Use `--file-role all` to include other selected reference types if they exist. `--dry-run` writes or prints the fetch manifest without downloading files.

For `product=cutout`, `tess-fetch` builds TESSCut requests from the target coordinates and sector list in the `tess-where` fetch plan. `--cutout-size` controls the square cutout size in pixels, defaulting to `5`. TESSCut zip responses are extracted by default; use `--keep-cutout-zip` to retain the archive. `--cutout-lightcurve-csv` extracts a simple aperture light curve from the downloaded TESSCut FITS files. Aperture modes are `circle`, `pixels`, and `threshold`; explicit pixel masks use zero-based `y,x` pairs separated by semicolons. `--cutout-background none|median-outside` controls optional background subtraction. `--cutout-aperture-summary-json`, `--cutout-aperture-mask-csv`, and `--cutout-aperture-report-html` write diagnostics showing which pixels were selected; the HTML report shades pixels by median flux.

Downloads verify expected file sizes when `tess-where` provided them and record local SHA-256 hashes for completed files. Existing matching files are marked `exists_verified`; use `--resume` to continue partial files when MAST supports HTTP range requests. When `--manifest-out` is used, derivative output paths and the selected quality policy are recorded in the manifest `outputs` object. Normalized and cutout file rows include input, output, and quality-dropped row counts for quick filtering audits. `--normalize-csv` writes a combined light-curve CSV after download/verification and requires `astropy`:

```powershell
python -m pip install -e .[fits]
```

Normalized CSV output includes common columns (`time_btjd`, `flux`, `flux_err`, `quality`) plus preserved source columns with `raw_` prefixes when present, such as `raw_sap_flux`, `raw_pdcsap_flux`, centroid columns, background columns, cadence number, and time correction. Use `--quality-mask` to drop rows where `QUALITY & mask` is nonzero; for example, `--quality-mask 1` drops rows with quality bit 0 set.

`--quality-preset` provides named repeatable policies:

- `none`: no filtering.
- `bit0`: mask `1`.
- `conservative`: mask `65535`, dropping rows with any low 16 QUALITY bits set.
- `spoc-recommended`: mask `21183`, the TESS archive suggested starting mask for cadences likely to be lower quality.
- `tess-spoc-recommended`: mask `21183`, because TESS-SPOC light curves use the same SPOC-style light-curve format and quality semantics.
- `qlp-recommended`: mask `7357`, covering the QLP-documented FFI quality bits: attitude tweak, coarse point, Earth point, Argabrightening, reaction wheel desaturation, manual exclude, collateral cosmic ray, stray light, and low-precision points.
- `recommended`: context-aware. It resolves per selected file row to `spoc-recommended`, `tess-spoc-recommended`, `qlp-recommended`, or the SPOC-style mask for TESSCut cutout extraction.

When `recommended` is used on a mixed batch, the manifest-level `outputs` object records `quality_preset = "recommended"` and each normalized file row records its resolved `quality_policy` and `quality_mask`.

## Testing

The core behavior has an offline standard-library test suite:

```powershell
python -m unittest discover -s tests -v
```

These tests avoid network calls and cover parsing, TIC/TOI resolver helpers, summaries, product classification, CSV exports, provider selection, and the JSON schema version.

For bounded live checks against current MAST/TESSCut services:

```powershell
python -m tess_tools.live_validate validation/live_targets.json --max-targets 1 --refresh
```

The live validation manifest is intentionally separate from the offline unit tests. It prints `PASS`, `FAIL`, or `INFRA`; `INFRA` means the target could not be validated because an external service or network path failed. Exit codes are `0` for pass, `1` for expectation failures, and `2` for infrastructure-only failures.

Some live manifest entries also declare opt-in fetch smoke checks. These download and normalize a bounded sample only when explicitly requested:

```powershell
python -m tess_tools.live_validate validation/live_targets.json --include-fetch-smoke --max-targets 3
```

The permanent live manifest includes exact-availability fixtures for QLP (`TIC 21278334`) and TESS-SPOC (`TIC 7547522`). Their opt-in fetch smokes each download one public light-curve FITS file and verify provider-specific normalized columns.
