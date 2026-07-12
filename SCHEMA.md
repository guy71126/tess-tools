# tess-where JSON Schema Notes

Current schema version: `tess-where.inventory.v0.3`

The JSON output is intended to be the stable handoff from `tess-where` to the future `tess-fetch` command. It is not a formal JSON Schema file yet, but the top-level shape should remain stable across near-term development.

## Top-Level Document

```json
{
  "schema_version": "tess-where.inventory.v0.3",
  "targets": []
}
```

## Target Inventory

Each entry in `targets` contains:

- `schema_version`: inventory schema version.
- `input`: original user-supplied target string.
- `target`: resolved target metadata.
- `sector_summary`: target-level sector counts, groups, span, and CVZ-like flag.
- `product_summary`: target-level product family/provider counts.
- `crowding_summary`: optional target-level neighbor/crowding audit.
- `known_object_summary`: optional known-object crossmatch audit.
- `sectors`: TESS sector records.
- `products`: product records discovered from providers.
- `providers`: provider status records.
- `fetch_plan`: recommended future fetch plan, or `null`.
- `recommendations`: backward-compatible list currently mirroring `fetch_plan`.
- `errors`: warnings and recoverable failures.
- `cache`: cache hit/key metadata.
- `queried_at_utc`: timestamp for non-cached live queries.

## Fetch Plan

The `fetch_plan` object is the future `tess-fetch` contract:

- `schema_version`: `tess-where.fetch-plan.v0.4`
- `target_label`: command-friendly target label.
- `best_for`: science mode used for product recommendation.
- `product`: recommended product key, such as `spoc`, `qlp`, or `cutout`.
- `sectors`: explicit sector list.
- `sector_argument`: command-friendly sector selector, usually `all`.
- `n_selected_product_references`: count of product records matching the chosen product with fetch references.
- `selected_product_references`: compact file/observation references selected for the chosen product.
  - Defaults to light-curve file references when they exist for the selected product.
  - Reference rows include target/product sector metadata, `fetch_reference`, `fetch_reference_kind`, optional `access_url`, `file_name`, `file_role`, `file_product_subgroup`, `file_cadence_sec`, and `file_size`.
- `command`: human-readable future `tess-fetch` command.
- `reason`: transparent recommendation reason.
- `caveats`: known limitations or things `tess-fetch` must handle.

## Provider Status

Provider status records make partial failures explicit:

- `name`
- `status`: `ok`, `empty`, `skipped`, `unavailable`, or `error`
- `message`
- `records`

This lets batch users distinguish "no products exist" from "the provider could not be queried."
When the `mast` provider is enabled for a TIC target, the provider list also includes exact collection statuses named `MAST QLP` and `MAST TESS-SPOC`. These are independent of the aggregate `MAST observations` status and report whether each HLSP-specific query was `ok`, `empty`, or `error`.

## Crowding Summary

When the opt-in `gaia` provider is enabled, `crowding_summary` estimates nearby-source contamination risk for TESS-scale pixels:

- `schema_version`: `tess-where.crowding.v0.1`
- `source`: currently `gaia`
- `query_source`: Gaia query backend that supplied retained neighbors when known, such as a MAST catalog cone service or `astroquery.mast.Catalogs.Gaia`.
- `radius_arcsec`: search radius used for neighbor audit.
- `tess_pixel_arcsec`: nominal TESS pixel scale, currently `21.0`.
- `contamination_model`: short description of the heuristic used for weighted contamination estimates.
- `risk`: `low`, `medium`, `high`, or `unknown`.
- `n_neighbors`: nearby sources within `radius_arcsec`, excluding the target itself when separable.
- `n_neighbors_within_1_pixel`: nearby sources within one nominal TESS pixel.
- `n_neighbors_within_2_pixels` and `n_neighbors_within_3_pixels`: nearby-source counts in wider nominal TESS pixel radii.
- `nearest_neighbor_arcsec`
- `brightest_delta_mag`: brightest neighbor magnitude minus target TESS magnitude when target and neighbor magnitudes are available.
- `total_neighbor_flux_ratio`: summed neighbor-to-target flux ratio when target and neighbor magnitudes are available.
- `total_neighbor_flux_ratio_within_1_pixel`, `total_neighbor_flux_ratio_within_2_pixels`, and `total_neighbor_flux_ratio_within_3_pixels`: unweighted flux-ratio sums by nominal TESS-pixel radius.
- `heuristic_aperture_contamination_ratio`: weighted neighbor-to-target flux ratio using a simple radial taper from full weight inside 0.5 nominal TESS pixels to zero at 3 pixels. It uses Gaia G as a TESS-band proxy and is intended for triage, not calibrated aperture photometry.
- `heuristic_dilution_factor`: `1 / (1 + heuristic_aperture_contamination_ratio)`, useful for quick transit-depth dilution triage when the heuristic ratio is available.
- `sector_geometry`: camera/CCD grouping for the sectors in which this crowding field was observed. It records `n_sectors`, `n_sectors_with_camera_ccd`, `n_camera_ccd_geometries`, `single_camera_ccd_geometry`, and `camera_ccd_groups`.
  - `camera_ccd_groups` rows include `camera`, `ccd`, `n_sectors`, and `sectors`.
  - This is geometry context for later PRF/aperture calibration; the current flux-ratio estimates still use the nominal pixel-scale model above.
- `neighbors`: compact list of the nearest normalized neighbor rows. Neighbor rows may also include `query_source` provenance.

Flattened CSV exports include a `<prefix>_crowding_neighbors.csv` table with one row per retained neighbor. It repeats the target identifier and crowding risk, adds `neighbor_rank`, and includes normalized neighbor fields such as `source_id`, `ra_deg`, `dec_deg`, `mag`, `separation_arcsec`, `separation_pixels`, `delta_mag`, `flux_ratio`, `aperture_weight`, `weighted_flux_ratio`, and query provenance when available.

## Known Object Summary

When the opt-in `toi` provider is enabled, `known_object_summary` records TOI matches for TIC targets. By default this uses the NASA Exoplanet Archive TOI TAP service; `--toi-catalog` can instead point at a local CSV, TSV, or JSON snapshot.

- `schema_version`: `tess-where.known-objects.v0.1`
- `n_matches`: total known-object matches across enabled known-object catalogs.
- `catalogs`: catalog labels with matches, currently `TOI` and `TESS EB`.
- `toi`: TOI-specific summary.
- `tess_eb`: TESS EB-specific summary when the local `tesseb` provider is enabled.

The `toi` object includes:

- `source`: currently `NASA Exoplanet Archive TOI TAP` or a local TOI catalog path when `--toi-catalog` is used.
- `n_matches`
- `dispositions`: distinct TOI disposition values present in the matches.
- `matches`: normalized TOI rows with `toi`, `tic_id`, `disposition`, `period_days`, `epoch_bjd`, `duration_hours`, `depth_ppm`, `radius_rearth`, and `source` when available.

The `tess_eb` object includes:

- `source`: local TESS EB catalog path.
- `n_matches`
- `matches`: normalized local rows with `eb_id`, `tic_id`, `period_days`, `epoch_bjd`, `duration_hours`, `morphology`, and `source` when available.

Flattened target CSV rows include `known_object_n_matches`, `known_object_catalogs`, `toi_n_matches`, `toi_dispositions`, `toi_ids`, `tess_eb_n_matches`, and `tess_eb_ids`.

## v0.3 Notes

`v0.3` adds `sector_summary` and `product_summary`. These are redundant summaries derived from detailed rows, but they make batch triage and future `tess-fetch` planning faster and less error-prone.

`product_summary` includes:

- `by_family`
- `by_provider`
- `by_scope`: counts for `per_sector`, `multi_sector`, and `unknown` product records.
- `n_products_with_fetch_reference`: products with a MAST fetch reference, URL, or observation identifier.
- `n_preextracted_products_with_fetch_reference`: non-TESSCut products with a future-fetchable reference.
- `n_products_with_file_uri`: products with a concrete MAST file `dataURI`.
- `n_preextracted_products_with_file_uri`: non-TESSCut products with a concrete MAST file `dataURI`.
- `fetch_reference_kinds`: reference types found, such as `mast_data_uri`, `mast_data_url`, `mast_obsid`, or `mast_obs_id`.
- `file_product_subgroups`: MAST product subgroups found after file lookup, such as `LC`, `DVT`, or `TP`.
- `product_count_by_sector`: per-sector products only.
- `products_by_sector`: per-sector product family/provider/scope counts.
- `multi_sector_products`: product records whose IDs represent a sector range rather than one sector.
- `n_preextracted_products`
- `n_tesscut_products`

MAST-derived product records may include these `extra` fields. CAOM observation rows provide `mast_obsid`; CAOM product-file lookup can upgrade selected records to file-level URI references.

- `mast_obsid`
- `data_uri`
- `file_data_uri`
- `file_name`
- `file_product_subgroup`
- `file_product_type`
- `file_role`: normalized role such as `lightcurve`, `target-pixel`, `dv-timeseries`, `report`, or `unknown`.
- `file_cadence_sec`: cadence inferred from file naming where possible; currently `20` for FAST-LC files and `120` for standard LC/TP files.
- `file_size`
- `proposal_id`
- `fetch_reference_kind`
- `fetch_reference`

`v0.3` also preserves invalid CSV rows as target inventories with `kind = "invalid"` and an error message. Batch runs should therefore be auditable: a bad row is reported instead of silently skipped.

The cache key includes enabled metadata providers. For example, the default `--providers tesscut` and opt-in provider sets such as `--providers tesscut,mast` or `--providers tesscut,gaia` produce separate cached inventory records.

The cache key also includes `best_for`, because two science modes can legitimately produce different fetch plans from the same inventory.

## tess-fetch Manifest

`tess-fetch` writes an optional download manifest:

- `schema_version`: `tess-fetch.manifest.v0.1`
- `source_schema_version`: source `tess-where` inventory schema.
- `n_files`: number of files selected for fetch.
- `files`: selected destination rows.
- `outputs`: optional derivative-output paths and policies requested during the run.

The optional `outputs` object may include:

- `normalize_csv`
- `cutout_lightcurve_csv`
- `cutout_aperture_summary_json`
- `cutout_aperture_mask_csv`
- `cutout_aperture_report_html`
- `quality_mask`
- `quality_preset`

Each file row includes:

- `target_label`
- `tic_id`
- `product`
- `sector`
- `cadence_sec`
- `file_role`
- `file_product_subgroup`
- `file_name`
- `expected_size`
- `expected_sha256`: optional expected SHA-256 digest if a provider supplies one.
- `fetch_reference_kind`
- `fetch_reference`
- `access_url`
- `destination`: destination file path for ordinary downloads; for TESSCut cutouts this is the planned zip path and extracted FITS files are listed in `local_paths`.
- `status`: `planned`, `downloaded`, `resumed`, `exists`, `exists_verified`, or `error`.
- `local_size`: downloaded or existing file size when known.
- `local_sha256`: SHA-256 digest for ordinary downloaded or verified files.
- `local_paths`: extracted local file paths for TESSCut cutouts.
- `local_hashes`: per-file SHA-256 digests for extracted TESSCut cutout files.
- `n_local_files`: count of extracted local files for TESSCut cutouts.
- `archive_path`: retained TESSCut zip path when `--keep-cutout-zip` is used.
- `error`: download error text when `status = error`.
- `quality_mask`: optional normalization filter copied from `--quality-mask` or resolved from `--quality-preset` when used.
- `quality_preset`: optional user-requested preset copied from `--quality-preset`.
- `quality_policy`: optional resolved provider-specific preset name when a preset maps through the file product, such as `spoc-recommended`, `tess-spoc-recommended`, or `qlp-recommended`.
- `normalization_status`: `ok`, `skipped_role`, `skipped_status`, or `error` when normalization is requested.
- `normalization_input_rows`: number of FITS table rows considered before quality filtering.
- `normalization_rows`: number of normalized CSV rows contributed by this file row, or `0` when skipped/error.
- `normalization_quality_dropped_rows`: number of rows dropped because `QUALITY & quality_mask` was nonzero.
- `normalization_error`: normalization error text when applicable.
- `cutout_lightcurve_status`: `ok`, `skipped_role`, `skipped_status`, `skipped_no_files`, or `error` when TESSCut aperture extraction is requested.
- `cutout_lightcurve_input_rows`: number of cutout cadence rows considered before quality filtering and finite-aperture checks.
- `cutout_lightcurve_rows`: number of aperture-extracted CSV rows for this cutout item when extraction succeeds.
- `cutout_lightcurve_quality_dropped_rows`: number of cutout cadence rows dropped because `QUALITY & quality_mask` was nonzero.
- `cutout_lightcurve_empty_aperture_rows`: number of cutout cadence rows skipped because no finite selected-aperture pixels were available.
- `cutout_lightcurve_error`: aperture extraction error text when applicable.

For `product = "cutout"`, `tess-fetch` may synthesize `files` rows from a target's coordinates and `fetch_plan.sectors` even when `fetch_plan.selected_product_references` is empty. These rows use `fetch_reference_kind = "tesscut_astrocut"` and `file_role = "cutout"`.

## Normalized Light-Curve CSV

`tess-fetch --normalize-csv` writes a combined CSV with these columns:

- `tic_id`
- `sector`
- `cadence_sec`
- `product`
- `time_btjd`
- `flux`
- `flux_err`
- `quality`
- `source_file`

Rows may be filtered before writing by passing `--quality-mask` or `--quality-preset`. A row is dropped when `QUALITY & mask` is nonzero. A mask of `0` drops no rows.

Current presets are:

- `none`: no filtering.
- `bit0`: mask `1`.
- `conservative`: mask `65535`, any low 16 QUALITY bits set.
- `spoc-recommended`: mask `21183`, the SPOC/TESS archive starting mask for cadences likely to be lower quality.
- `tess-spoc-recommended`: mask `21183`, matching SPOC-style TESS-SPOC light curves.
- `qlp-recommended`: mask `7357`, covering the QLP-documented FFI quality flags.
- `recommended`: resolves per file row from `product`; currently `spoc` and `tess-spoc` resolve to mask `21183`, `qlp` resolves to mask `7357`, and `cutout`/`tesscut` resolve to the SPOC-style mask.

When `recommended` is used in a mixed batch, `outputs.quality_preset` remains `recommended` and each file row records its resolved `quality_policy` and `quality_mask`.

When present in the FITS table, useful source columns are preserved with `raw_` prefixes. Current preserved columns include:

- `raw_cadenceno`
- `raw_timecorr`
- `raw_sap_flux`
- `raw_sap_flux_err`
- `raw_pdcsap_flux`
- `raw_pdcsap_flux_err`
- `raw_sap_bkg`
- `raw_sap_bkg_err`
- `raw_mom_centr1`
- `raw_mom_centr1_err`
- `raw_mom_centr2`
- `raw_mom_centr2_err`
- `raw_psf_centr1`
- `raw_psf_centr1_err`
- `raw_psf_centr2`
- `raw_psf_centr2_err`
- `raw_pos_corr1`
- `raw_pos_corr2`

## TESSCut Aperture Light-Curve CSV

`tess-fetch --cutout-lightcurve-csv` writes the same common normalized columns as `--normalize-csv`, using a simple centered circular aperture on downloaded TESSCut target-pixel FITS files. It also adds extraction provenance columns:

- `aperture_radius_px`
- `aperture_mode`
- `aperture_n_pixels`
- `aperture_pixel_spec`
- `aperture_threshold_sigma`
- `background_method`
- `background_flux_per_pixel`
- `background_n_pixels`
- `raw_aperture_flux`
- `raw_cadenceno`
- `raw_timecorr`

Supported aperture modes are `circle`, `pixels`, and `threshold`. Pixel masks use zero-based `y,x` pairs such as `2,2;2,3;3,2`; threshold masks are derived from the median cutout image using `--cutout-threshold-sigma`. This is a quick-look extraction, not a replacement for explicit aperture design or contamination analysis.

## TESSCut Aperture Diagnostics

`tess-fetch --cutout-aperture-summary-json` writes:

- `schema_version`: `tess-fetch.aperture-summary.v0.1`
- `apertures`: one row per extracted cutout FITS file.

Each aperture summary includes target/sector/source-file metadata, cutout shape, aperture mode/options, selected-pixel count and bounds, background method, background-pixel count, the selected aperture pixels, and `median_flux_image` values for visual audit.

`tess-fetch --cutout-aperture-mask-csv` writes one row per pixel per extracted cutout FITS file:

- `source_file`
- `target_label`
- `tic_id`
- `sector`
- `product`
- `aperture_mode`
- `height`
- `width`
- `y`
- `x`
- `in_aperture`

`tess-fetch --cutout-aperture-report-html` writes a compact static HTML report from the same aperture diagnostics, with one pixel-grid preview per extracted cutout FITS file. Pixel color is scaled from each cutout's median flux image, and selected aperture pixels are highlighted for quick visual audit. The report is intentionally simple and local-file friendly.
