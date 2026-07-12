# TESS Tools Project Plan

## Scope

This project starts with `tess-where`, a metadata-first command-line tool that answers:

> What TESS observations and public data products exist for this target?

The design should leave room for a later companion command, `tess-fetch`, which will download and normalize the selected light curves or cutouts using the same target-resolution, sector-inventory, product-discovery, cache, and output-schema layers.

## Commands

### `tess-where`

Purpose: query metadata only. It should not download large light-curve or cutout products by default.

Example usage:

```powershell
tess-where TIC 21002564
tess-where "TOI-700"
tess-where 102.7 -70.5
tess-where targets.csv --out inventory.csv
```

Outputs:

- Human-readable terminal summary.
- Machine-readable CSV and/or JSON inventory.
- Optional local metadata cache.

Target-level fields:

- Input identifier and resolved canonical target.
- TIC ID when available.
- RA/Dec.
- TESS magnitude when available.
- Gaia crossmatch fields when available.
- Observed sectors.
- Number of sectors.
- Sector groups and gaps.
- Ecliptic-pole or continuous-viewing-zone indicators when available.
- Data-product availability summary.
- Basic crowding/neighbor-risk summary.
- Optional known-object flags: TOI, eclipsing binary, variable-star catalog matches.

Sector-level fields:

- TIC ID or resolved target key.
- Sector.
- Camera.
- CCD.
- Cadence availability.
- Public product availability.
- FFI cutout availability.
- Observation start/end times when available.
- Metadata source.

Product-level fields:

- Product family: SPOC, TESS-SPOC HLSP, QLP, TGLC, T16/CDIPS, TESSCut cutout.
- Sector.
- Cadence.
- File/product identifier.
- Access URL or MAST product reference.
- Recommended science use when known.
- Product caveats.

### `tess-fetch`

Purpose: download and later normalize data products discovered by `tess-where`.

Example usage:

```powershell
tess-fetch inventory_with_mast.json --dry-run --manifest-out fetch_manifest.json
tess-fetch inventory_with_mast.json --target "TIC 21002564" --sectors 14,15,16 --out-dir lightcurves/
```

Future `--best-for` modes:

- `transits`
- `rotation`
- `flares`
- `eclipses`
- `raw-variability`

Normalized light-curve schema:

- `tic_id`
- `sector`
- `time_btjd`
- `flux`
- `flux_err`
- `quality`
- `cadence_sec`
- `product`
- `camera`
- `ccd`
- `source_file`
- product-specific raw columns preserved where useful

## Shared Architecture

Both commands should use the same internal layers:

1. Target resolver
   - Accept TIC IDs, coordinates, names, and batch CSV input.
   - Return a canonical target object.

2. Sector resolver
   - Determine TESS sectors, camera, CCD, and cutout availability.
   - Prefer official MAST/TESSCut/astroquery-backed metadata.

3. Product inventory resolver
   - Discover available light-curve products without downloading large files.
   - Support pluggable product providers.

4. Cache
   - Store metadata query results separately from future downloaded data.
   - Include source, query time, and schema version.

5. Output schemas
   - Stable JSON and CSV schemas.
   - One target summary table and one sector/product detail table.

6. Fetch planner
   - Initially used only to print recommended `tess-fetch` commands.
   - Later reused by `tess-fetch` to execute downloads.

## Initial Provider Priorities

MVP provider support:

1. MAST/TESSCut sector lookup.
2. SPOC and/or TESS-SPOC availability where discoverable through MAST.
3. QLP HLSP availability.

Later provider support:

1. TGLC.
2. T16/CDIPS.
3. Other TESS HLSP products.
4. Moving-target support.
5. Catalog crossmatches: TOI, TESS EB, VSX, SIMBAD, Gaia neighbors.

## MVP Definition

Version 0.1 should:

1. Support TIC ID and RA/Dec input.
2. Query observed sectors.
3. Report camera/CCD where available.
4. Detect at least QLP and SPOC/TESS-SPOC availability.
5. Produce terminal, JSON, and CSV outputs.
6. Keep a local metadata cache.
7. Print a recommended future `tess-fetch` command.
8. Avoid custom aperture photometry and bulk light-curve downloads.

## Design Principles

- Metadata first: `tess-where` should be cheap, fast, and safe to run on large target lists.
- Provenance always: every row should record where the metadata came from.
- Stable schemas: `tess-fetch` should be able to consume `tess-where` JSON directly.
- Product-neutral core: SPOC, QLP, TGLC, and T16 should be provider plugins, not hardcoded through the whole app.
- Science-mode aware: product recommendations should eventually depend on the intended science case.
- Batch-friendly: all APIs and CLIs should work for one target or a table of targets.
- Auditable: avoid opaque rankings without explaining why a product was recommended.

## Open Design Decisions Before Coding

1. Project packaging
   - Use one package with multiple commands, likely `tess_tools`, exposing `tess-where` now and `tess-fetch` later.
   - Alternative: separate packages for each command, with a shared package. This is more overhead early.

2. First metadata backend
   - Use `astroquery.mast` as the primary Python interface, with direct HTTP requests only where astroquery lacks coverage.
   - Alternative: direct MAST APIs only. This may be lighter but would require more custom API handling.

3. Output schema shape
   - Recommended: one JSON document with `targets`, `sectors`, `products`, and `recommendations`, plus optional flattened CSV exports.
   - This is easier for `tess-fetch` to consume later than a terminal-only or single-table design.

4. Cache location
   - Recommended local project cache for development, with a future user cache option.
   - Example development path: `tess-tools/.cache/metadata/`.

5. Product recommendation policy
   - Start with transparent rules, not ML or hidden scores.
   - Recommendation fields should include both the selected product and the reason.

6. Supported identifiers in v0.1
   - Recommended v0.1: TIC ID and RA/Dec.
   - Name resolution and TOI/Gaia aliases can follow once the core inventory path works.

7. Dependency policy
   - Likely dependencies: `astropy`, `astroquery`, `pandas`, `requests`, and `typer` or `argparse`.
   - To keep installation simple, avoid heavier dependencies until needed.

8. Network behavior
   - All network calls should be explicit, cached, retried politely, and record query provenance.
   - Batch mode should rate-limit requests.

## Suggested First Implementation Milestones

1. Create package skeleton and CLI entry point for `tess-where`.
2. Implement target parsing for TIC ID and RA/Dec.
3. Implement sector lookup.
4. Define JSON schema and terminal formatter.
5. Add product provider abstraction.
6. Implement first product provider for one source.
7. Add CSV output.
8. Add metadata cache.
9. Add batch CSV input.
10. Add recommendation generation.

## Current Implementation Notes

### v0.1 Foundation

- Created a single `tess_tools` package so `tess-where` and future `tess-fetch` can share backend code.
- Added TIC, RA/Dec, and CSV target parsing.
- Added TESSCut sector discovery through `astroquery` with a direct HTTP fallback.
- Added a metadata cache.
- Added JSON and flattened CSV outputs.
- Added product records and recommendation output.

### v0.2 Schema And Batch Hardening

- Promoted the inventory schema to `tess-where.inventory.v0.2`.
- Added a structured `fetch_plan` object as the future `tess-fetch` contract.
- Added provider status records so partial failures are explicit.
- Added explicit product-family classifier rules for SPOC, TESS-SPOC, QLP, TGLC, T16, and CDIPS.
- Added flattened fetch-plan CSV output.
- Added `--sleep-sec` and `--max-targets` for safer CSV batch runs and smoke tests.
- Added `SCHEMA.md` and `VALIDATION_TARGETS.md`.

### v0.3 Batch Auditability And Summaries

- Promoted the inventory schema to `tess-where.inventory.v0.3`.
- Added `sector_summary` with sector counts, contiguous groups, span, and CVZ-like flag.
- Added `product_summary` with product-family/provider counts and pre-extracted-light-curve availability.
- Added flattened target and provider CSV outputs.
- Preserved invalid CSV rows as explicit error inventories instead of silently skipping them.
- Added `--providers` so users can run fast TESSCut-only inventory batches or explicitly include MAST product discovery when needed.
- Changed the default provider set to `tesscut` after live testing showed broad MAST observation queries can still hang or disconnect; MAST remains opt-in with `--providers tesscut,mast`.

### v0.4 Reliability And Tests

- Added an offline standard-library `unittest` suite under `tests/`.
- Covered target CSV parsing, invalid-row preservation, sector/product summaries, product family classifiers, deduplication, fetch-plan generation, provider selection, and CSV/JSON output creation.
- Replaced the opt-in MAST observations provider's `astroquery.Observations` call with bounded direct MAST CAOM HTTP requests and smaller-radius retries.
- Live evaluation showed the TESSCut-only path is fast and reliable. The direct MAST provider is now bounded, but next MAST work should still move toward product-specific queries rather than broad observation-region discovery.

### v0.5 Direct TIC Resolution

- Added a shared direct MAST invoke helper.
- Added direct MAST TIC lookup through `Mast.Catalogs.Filtered.Tic`, with astroquery retained as a fallback.
- `tess-where TIC ...` now resolves coordinates and Tmag without requiring optional dependencies.
- Added offline tests for TIC row parsing and resolver failure handling.
- Live evaluation on `TIC 261136679` resolved RA/Dec/Tmag through `mast.catalogs.filtered.tic.http` and then found 25 TESSCut sectors in about three seconds.

### v0.6 Science Modes And Live Validation

- Added `--best-for general|transits|rotation|flares|eclipses|raw-variability|asteroseismology`.
- Promoted fetch-plan schema to `tess-where.fetch-plan.v0.3` with explicit `best_for`.
- Added rule-based product selection for science modes while keeping reasons visible in the fetch plan.
- Added `tess_tools.live_validate` and `validation/live_targets.json` for bounded live checks outside the offline unit suite.
- Added offline tests for science-mode recommendation logic and live-validation expectation handling.

### v0.7 Filtered MAST Product Inventory

- Added filtered MAST CAOM observation lookup for TIC targets using `target_name=<TIC>`.
- The opt-in `--providers tesscut,mast` path now tries filtered TIC product discovery before cone search fallback.
- Live evaluation on `TIC 261136679` found 50 SPOC timeseries records plus 25 TESSCut products in about seven seconds and correctly switched the fetch plan to `spoc`.
- Tightened cadence inference to avoid mistaking SPOC pipeline suffixes such as `0120-s` for 20-second cadence.
- Added live validation coverage for the filtered MAST/SPOC path.

### v0.8 Product Shape Summaries

- Added `product_scope` metadata to product records: `per_sector`, `multi_sector`, or `unknown`.
- Added `sector_start` and `sector_end` metadata for product records when inferable from product IDs.
- Added `product_summary.by_scope`, `product_count_by_sector`, and detailed `products_by_sector`.
- Added flattened target CSV fields for pre-extracted product count, TESSCut product count, product scopes, and sectors with products.
- Tightened sector-token parsing so timestamps such as `tess2020...` are not mistaken for sector IDs.
- Split per-sector product summaries from multi-sector/combined product records so range products do not masquerade as ordinary sector-local products.
- Added live-validation infrastructure failure classification so DNS, timeout, and connection failures are reported as `INFRA` rather than being confused with expectation regressions.

### v0.9 Fetch Reference Metadata

- Added MAST fetch-reference fields to product records when available: `mast_obsid`, `data_uri`, `access_url`, `fetch_reference_kind`, and `fetch_reference`.
- Added product-summary counts for products with fetch references, including pre-extracted products with fetch references.
- Added flattened target CSV fields summarizing fetch-reference availability.
- Kept this as an additive `v0.3` inventory-schema extension so future `tess-fetch` work can begin without breaking existing inventory consumers.

### v0.10 File-Level Product References

- Added batched `Mast.Caom.Products` lookup by comma-separated `obsid` to enrich MAST observation records with file-level metadata.
- Added selection logic that prefers science LC FITS files over DVT/TP/report products when multiple files exist for an observation.
- Added file-level product fields such as `file_data_uri`, `file_name`, `file_product_subgroup`, `file_product_type`, `file_size`, and MAST download `access_url`.
- Added product-summary fields for concrete file URI availability and MAST file subgroups.
- Promoted fetch-plan schema to `tess-where.fetch-plan.v0.4` and added `selected_product_references` for the recommended product.
- Extended live validation expectations so the SPOC target must prove file URI enrichment, not just product-family detection.

### v0.11 Initial tess-fetch And File Roles

- Added normalized file roles for MAST product rows: `lightcurve`, `target-pixel`, `dv-timeseries`, `report`, and `unknown`.
- Added file cadence hints for SPOC LC products: 120 seconds for standard LC files and 20 seconds for FAST-LC files.
- Changed fetch-plan selected references to prefer light-curve files when available, keeping DVT/TP/report records in detailed product rows.
- Added initial `tess-fetch` command that consumes `tess-where` JSON, filters by target/product/sector/file role, supports dry-run, writes JSON/CSV manifests, and downloads selected files when not in dry-run mode.
- Added `tess-fetch.manifest.v0.1` as the first download manifest contract.

### v0.12 Safer Fetching And Normalized CSV

- Added expected-size verification for `tess-fetch` downloads and existing files.
- Added `--resume` support using HTTP range requests for partial files.
- Added per-file manifest statuses and error fields so batch downloads can report partial success.
- Added optional `--normalize-csv` output for downloaded or verified light-curve FITS files, with `astropy` loaded only when normalization is requested.
- Added the `fits` optional dependency extra for normalization installs.

### v0.13 Source-Column Preservation And Live Normalization

- Preserved useful FITS source columns in normalized CSV output using `raw_` prefixes, including SAP/PDC fluxes, flux errors, centroids, background, cadence number, and time correction.
- Made normalized CSV headers dynamic so product-specific preserved columns can be included without changing the common schema columns.
- Live-smoke tested one public SPOC LC FITS file for `TIC 261136679`: the expected file size matched, normalization completed, and the output contained 20,076 rows with preserved raw columns.

### v0.14 Quality Filtering And Opt-In Fetch Smoke

- Added `--quality-mask` to `tess-fetch --normalize-csv`, dropping rows where `QUALITY & mask` is nonzero while preserving the default no-filter behavior.
- Added opt-in fetch/normalization smoke checks to `tess_tools.live_validate`; declared smoke checks are skipped by default and run only with `--include-fetch-smoke`.
- Added a bounded live smoke target for `TIC 261136679` that can download one SPOC light-curve file, normalize it, and verify expected normalized CSV columns.
- Extended live-validation infrastructure classification so fetch-smoke network/download failures can report `INFRA` rather than ordinary expectation failure.

### v0.15 TESSCut Fetch Execution

- Added TESSCut cutout execution to `tess-fetch` for inventories whose recommended product is `cutout`.
- Synthesized TESSCut astrocut requests from target coordinates, requested sector, and `--cutout-size`, so cutout fetching works even when `selected_product_references` is empty.
- Added safe extraction for TESSCut zip responses, recording extracted FITS paths in fetch manifests as `local_paths`.
- Live-smoke tested one sector-2, 5x5-pixel TESSCut cutout for `102.7 -70.5`; the service returned one extracted FITS file of 745,920 bytes.

### v0.16 TESSCut Quick-Look Aperture Extraction

- Added `--cutout-lightcurve-csv` to `tess-fetch` for simple center-aperture extraction from downloaded TESSCut target-pixel FITS files.
- Added aperture controls with `--cutout-aperture-radius` and optional `--cutout-background median-outside` subtraction.
- Wrote extracted cutout light curves into the same common CSV shape as pre-extracted light curves, with provenance columns for aperture size, background method, raw aperture flux, cadence number, and time correction.
- Added opt-in live validation support for TESSCut cutout smoke checks.
- Live-smoke tested one sector-2 cutout extraction for `102.7 -70.5`; the output contained 1,245 cadence rows.

### v0.17 Configurable TESSCut Aperture Masks

- Added `--cutout-aperture-mode circle|pixels|threshold` for TESSCut aperture extraction.
- Added explicit zero-based pixel masks via `--cutout-aperture-pixels`, using semicolon-separated `y,x` pairs.
- Added threshold-derived masks via `--cutout-threshold-sigma`, computed from the median cutout image with a robust sigma estimate.
- Added aperture provenance columns for mode, pixel specification, and threshold sigma.

### v0.18 TESSCut Aperture Diagnostics

- Added `--cutout-aperture-summary-json` to write one aperture diagnostic record per extracted TESSCut FITS file.
- Added `--cutout-aperture-mask-csv` to write one row per cutout pixel with an `in_aperture` flag.
- Recorded aperture bounds, cutout shape, background-pixel count, selected pixel coordinates, and source-file provenance for quick auditing.

### v0.19 Quality-Mask Presets

- Added `--quality-preset none|bit0|conservative` to `tess-fetch` for repeatable normalization and TESSCut extraction filtering policies.
- Kept raw `--quality-mask` support for expert workflows, while rejecting ambiguous runs that provide both a raw mask and a non-`none` preset.
- Defined `conservative` as mask `65535`, dropping rows where any low 16 TESS QUALITY bits are set.

### v0.20 Fetch Output Auditability

- Added an optional fetch-manifest `outputs` object recording derivative CSV/diagnostic paths and the selected quality policy.
- Added per-file `normalization_rows` counts so batch manifests show which downloaded light curves contributed rows to normalized output.
- Propagated resolved `quality_mask` into TESSCut cutout manifest rows as well as pre-extracted light-curve rows.

### v0.21 TESSCut Aperture HTML Reports

- Added `--cutout-aperture-report-html` to write a compact static aperture preview report from TESSCut extraction diagnostics.
- Rendered one pixel-grid preview per extracted cutout FITS file, highlighting selected aperture pixels alongside source, sector, mode, bounds, and background metadata.
- Kept the report dependency-free and local-file friendly so it can be shared with CSV/JSON diagnostics from the same run.

### v0.22 Row-Filter Accounting

- Added pre-filter, post-filter, and quality-dropped row counts for normalized pre-extracted light curves.
- Added equivalent TESSCut extraction counters for input rows, output rows, quality-dropped rows, and empty-aperture rows.
- Kept the normalizer and extractor return types backward-compatible while exposing the counters through fetch manifest rows.

### v0.23 Median-Flux Aperture Heatmaps

- Added `median_flux_image` to TESSCut aperture diagnostics.
- Reused the same median-image calculation for threshold apertures and report heatmap rendering.
- Updated aperture HTML reports so pixel color is scaled by median flux while selected aperture pixels remain outlined.

### v0.24 Local File Hashes

- Recorded `local_sha256` for completed ordinary downloads and verified existing light-curve files.
- Recorded `local_hashes` for extracted TESSCut FITS files.
- Added optional `expected_sha256` validation for future providers that expose trusted SHA-256 metadata.

### v0.25 Gaia Crowding Summary

- Added an opt-in `gaia` metadata provider for target-level neighbor/crowding audits.
- Added `crowding_summary` with risk, neighbor counts, nearest-neighbor separation, brightest-neighbor delta magnitude, and total neighbor flux-ratio estimates.
- Added flattened target CSV fields and terminal-summary output for crowding risk.

### v0.26 Crowding Neighbor CSV

- Added `<prefix>_crowding_neighbors.csv` to flattened CSV exports.
- Flattened retained Gaia neighbor rows with target identifiers, crowding risk, neighbor rank, separation, magnitude, delta magnitude, and flux-ratio fields.
- Updated `--csv-out` help and schema notes so the new audit table is discoverable.

### v0.27 Gaia Crowding Live Validation

- Added live-validation expectation support for `crowding_summary` minimum values and allowed categorical values.
- Added a bounded `tic-gaia-crowding` validation target using `--providers tesscut,gaia`.
- Classified missing optional Gaia tooling such as `astroquery` as validation infrastructure rather than a science expectation failure.

### v0.28 Gaia Crowding HTTP Fallback

- Added direct MAST HTTP Gaia cone queries for the `gaia` crowding provider, using the shared MAST invoke helper before falling back to `astroquery`.
- Tried multiple Gaia catalog service aliases and retried without explicit column selection when a catalog service rejects the column-limited query.
- Recorded the successful Gaia query source in `crowding_summary.query_source` and retained neighbor-level query provenance.
- Added offline tests for MAST invoke parameters, fallback retry behavior, and combined HTTP/optional-dependency failure reporting.
- Live validation through `tic-gaia-crowding` passed using `Mast.Catalogs.GaiaDR3.Cone`, returning 9 retained Gaia neighbors for `TIC 261136679`.

### v0.29 Heuristic TESS-Pixel Contamination Metrics

- Added nominal TESS-pixel separations for retained Gaia neighbors.
- Added neighbor counts and unweighted flux-ratio sums within 1, 2, and 3 nominal TESS pixels.
- Added a named heuristic aperture-contamination ratio using Gaia G as a TESS-band proxy and a radial taper from full weight inside 0.5 pixels to zero at 3 pixels.
- Added a heuristic dilution factor for quick transit-depth triage.
- Exposed the new contamination metrics in target CSV rows and retained neighbor rows.
- Live validation for `TIC 261136679` reported 9 retained neighbors, 3 within 2 nominal TESS pixels, heuristic contamination ratio about `0.596`, and dilution factor about `0.627`.

### v0.30 Crowding Sector Geometry Context

- Threaded discovered sector camera/CCD metadata into the opt-in Gaia crowding summary.
- Added `crowding_summary.sector_geometry` with sector counts, camera/CCD groups, and a `single_camera_ccd_geometry` flag.
- Added compact target CSV fields for crowding sector-geometry count, single-geometry status, and camera/CCD sector groups.
- Kept the current contamination ratios explicitly nominal; the new geometry context identifies where future PRF/aperture-specific calibration should branch.
- Live validation for `TIC 261136679` reported 25 sectors across 6 camera/CCD geometries.

### v0.31 TOI Known-Object Crossmatch

- Added an opt-in `toi` provider that queries the NASA Exoplanet Archive TOI TAP service by TIC ID.
- Added `known_object_summary` with total match count, matched catalog labels, and TOI-specific normalized match rows.
- Added flattened target CSV fields for known-object match count, matched catalogs, TOI count, TOI dispositions, and TOI IDs.
- Added live-validation expectations for known-object summaries and a bounded `tic-toi-known-object` target.
- Live validation for `TIC 150428135` returned 4 TOI matches: `700.01`, `700.02`, `700.03`, and `700.04`, all with disposition `CP`.

### v0.32 Local TOI Snapshot Support

- Added `--toi-catalog` for local TOI CSV, TSV, or JSON snapshots when the opt-in `toi` provider is enabled.
- Local snapshot matching uses TIC IDs and avoids live Exoplanet Archive TOI TAP calls for repeatable batch runs.
- Included local catalog path, file size, and mtime in the inventory cache key so live and snapshot-backed TOI inventories do not collide.
- Added offline tests for CSV and JSON TOI snapshots and live-query bypass when a local catalog is provided.

### v0.33 Local TESS EB Snapshot Support

- Added an opt-in `tesseb` known-object provider with `tess-eb` accepted as a provider alias.
- Added `--tess-eb-catalog` for local TESS EB CSV, TSV, or JSON snapshots matched by TIC ID.
- Added normalized TESS EB match rows with EB ID, TIC ID, period, epoch, duration, morphology, and source fields when available.
- Added flattened target CSV fields for TESS EB match count and EB IDs.
- Added live-validation expectation support for `tess_eb_summary_min`, plus offline tests for TESS EB catalog parsing, provider behavior, inventory wiring, and CSV flattening.

### v0.34 TOI Target Resolution

- Added direct target parsing for TOI host and planet identifiers, including `TOI-700`, `TOI 700`, and `TOI-700.01` forms.
- Resolve TOIs through the NASA Exoplanet Archive TOI table to a canonical TIC ID, then reuse normal TIC metadata, sector, product, crowding, known-object, and fetch-plan paths.
- Preserve the requested TOI identifier in `target.extra.toi_id` while exposing the TIC ID as the stable cross-provider key.
- Use `--toi-catalog` snapshots for TOI target resolution as well as known-object matching, enabling repeatable runs without a live TOI TAP request.
- Added TOI columns to batch CSV target parsing and a bounded live validation target for `TOI-700`.

### v0.35 Explicit QLP And TESS-SPOC Availability

- Added exact MAST `provenance_name` queries by TIC for QLP and TESS-SPOC instead of relying on incidental generic cone results.
- Added separate `MAST QLP` and `MAST TESS-SPOC` provider statuses so empty availability and query failures remain machine-readable.
- Prevented TIC cone fallback from associating nearby stars' products with the requested target by retaining only exact `target_name` matches.
- Added QLP `_llc.fits` light-curve recognition, provider-aware FFI cadence inference for the 30-minute, 10-minute, and 200-second sector eras, and QLP-specific preserved normalization columns.
- Made duplicate product resolution prefer enriched light-curve records with concrete MAST data URIs.
- Live-validated QLP availability for `TIC 21278334`: 14 exact light-curve products and a QLP fetch recommendation.
- Live-validated TESS-SPOC availability for `TIC 7547522`: 8 exact light-curve products and a TESS-SPOC fetch recommendation.
- Downloaded and normalized one public QLP Sector 26 file (1,148 rows) and one TESS-SPOC Sector 14 file (1,288 rows).

### v0.36 Provider-Specific Quality Policies And Release Packaging

- Added provider-specific quality presets for normalized light curves and TESSCut aperture extraction.
- Added `spoc-recommended` and `tess-spoc-recommended` as mask `21183`, matching the TESS archive suggested starting mask for lower-quality SPOC-style cadences.
- Added `qlp-recommended` as mask `7357`, covering the QLP-documented FFI quality bits used by QLP light-curve products.
- Added contextual `recommended`, which resolves per manifest file row to the appropriate provider-specific preset. Mixed batches keep `outputs.quality_preset = "recommended"` while each file row records its resolved `quality_policy` and `quality_mask`.
- Kept `none`, `bit0`, `conservative`, and raw `--quality-mask` support for reproducible expert workflows.
- Added release packaging metadata: build backend, README metadata, license metadata, classifiers, authors, and package keywords.
- Added an MIT `LICENSE` file for source and wheel distributions.

## Ranked Future Features And Upgrades

This list is ordered by expected usefulness to users, not by implementation difficulty.

1. Robust product inventory across major TESS light-curve sources
   - SPOC, TESS-SPOC HLSP, QLP, and TESSCut now have explicit live-validated paths; add TGLC, T16, CDIPS, and TASOC as experimental providers before promoting them individually.
   - This is the highest-impact upgrade because it turns `tess-where` into a reliable map of the fragmented TESS product ecosystem.

2. `tess-fetch` integration
   - Add remote checksum mapping if MAST or HLSP product metadata exposes trusted checksums beyond local SHA-256 recording.
   - Add exportable PNG aperture previews if users need image artifacts outside the HTML report.

3. Product recommendation modes
   - Add explicit `--best-for transits|rotation|flares|eclipses|raw-variability|asteroseismology`.
   - Record transparent reasons for each recommendation.
   - This should remain rule-based until enough validation data exists to justify scoring.

4. Batch inventory at scale
   - Add robust CSV input, resumable runs, polite rate limiting, progress output, partial-output writes, and cache-only/offline modes.
   - This is essential for target-list building and survey-style work.

5. Crowding and neighbor audit
   - Calibrate Gaia neighbor audit with real sector camera/CCD PRF or aperture geometry, Gaia-to-TESS passband estimates, saturation caveats, and aperture-specific contamination metrics.
   - This helps exoplanets, eclipsing binaries, variables, flares, and any TESS analysis affected by the 21-arcsec pixels.

6. Known-object crossmatches
   - Add VSX/AAVSO, SIMBAD, Gaia DR tables, and selected specialized catalogs.
   - Add snapshot/catalog-file fallbacks for each optional known-object provider where possible.
   - Keep these as optional providers so the core tool remains lightweight.

7. Availability and quality scoring
   - Summarize sector count, time baseline, cadence, gaps, expected noise, saturation risk, crowding risk, and data-product suitability.
   - Useful for deciding whether an object is worth deeper analysis before downloading large files.

8. Name and alias resolution
   - TOI host/planet identifiers are implemented; add common names, Gaia DR3 source IDs, HD/HIP identifiers, and custom alias maps.
   - TIC and RA/Dec remain the foundation, but alias support improves usability for mixed target lists.

9. Moving-target support
   - Wrap TESSCut moving-target sector discovery and future cutout fetch planning.
   - Valuable for Solar System work, but separate enough to avoid delaying stellar-source support.

10. HTML/PDF report output
   - Produce compact shareable reports with sky position, sector timeline, product matrix, and contamination summary.
   - Useful for collaborators and triage, but lower priority than machine-readable outputs.

11. Plugin-style provider API
   - Let users add local or institution-specific TESS products without changing core code.
   - This becomes important once the core provider set is stable.

12. Validation test set
   - Maintain a small suite of well-known TICs covering common cases: many sectors, CVZ target, QLP-only, SPOC target, crowded field, saturated source, no observations.
   - This is not a user-facing feature, but it prevents silent regressions.
