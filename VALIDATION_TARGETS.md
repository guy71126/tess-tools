# Validation Targets

This file records target classes that should be covered by repeatable `tess-where` checks. Exact TIC IDs can be refined as the provider layer matures.

## Priority 1

1. Many-sector coordinate target
   - Example: `102.7 -70.5`
   - Expected behavior: many TESSCut sectors, non-null cutout fetch plan, optional smoke can fetch one TESSCut cutout and extract a quick-look light curve.

2. Known SPOC light-curve target
   - Purpose: verify SPOC product identification and recommendation priority.
   - Expected behavior: `SPOC` family present; file-level MAST `dataURI` references found; fetch plan prefers `spoc` and selects light-curve file references.

3. QLP-dominant or QLP-only FFI target
   - Current fixture: `TIC 21278334`.
   - Purpose: verify QLP provider detection when SPOC is absent.
   - Expected behavior: exact `MAST QLP` query succeeds; `QLP` family and light-curve data URIs are present; fetch plan prefers `qlp`; opt-in smoke normalizes QLP `KSPSAP_FLUX` columns.

4. Known TESS-SPOC FFI target
   - Current fixture: `TIC 7547522`, selected from the official Sector 14 TESS-SPOC target list.
   - Expected behavior: exact `MAST TESS-SPOC` query succeeds; `TESS-SPOC` family and light-curve data URIs are present; fetch plan prefers `tess-spoc`; opt-in smoke normalizes SPOC-style SAP/PDCSAP columns.

5. TESSCut-only target
   - Purpose: verify the fallback path for targets with sectors but no detected pre-extracted light curve.
   - Expected behavior: `TESSCut` products; fetch plan uses `cutout` and records caveats.

6. No-observation coordinate
   - Purpose: verify clean empty responses.
   - Expected behavior: zero sectors, zero products, no fetch plan, no crash.

## Priority 2

1. Known TOI system
   - Current use: validate TOI known-object crossmatch through the opt-in `toi` provider.
   - Example: `TIC 150428135`, TOI-700.
   - Future use: add local snapshot fallback and disposition-specific checks.

2. Known TESS EB system
   - Current use: validate local TESS EB snapshot crossmatch through the opt-in `tesseb` provider.
   - Future use: add a stable public snapshot fixture or live-source workflow once a durable endpoint is selected.

3. Crowded-field target
   - Current use: validate Gaia neighbor/crowding audit through the opt-in `gaia` provider and `crowding_summary` expectations.
   - Future use: broaden coverage with calibrated contamination-risk examples.

4. Saturated bright target
   - Future use: validate product caveats and recommendation warnings.

5. CVZ target
   - Future use: validate sector grouping and long-baseline summaries.

6. Batch CSV with mixed valid and invalid rows
   - Future use: validate partial failures, cache reuse, and CSV output.
