from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tess_tools.cache import MetadataCache
from tess_tools.cli import main, parse_providers
from tess_tools.crowding import (
    aperture_contamination_weight,
    build_crowding_summary,
    flux_ratio_from_delta_mag,
    query_gaia_neighbors,
    query_mast_gaia_neighbors,
)
from tess_tools.cutout import (
    circular_aperture_pixels,
    extract_cutout_manifest_files,
    build_aperture_diagnostic,
    median,
    median_flux_image,
    parse_pixel_mask,
    threshold_aperture_pixels,
    write_aperture_diagnostics,
)
from tess_tools.fetch import build_fetch_manifest, download_reference, main as fetch_main, normalize_manifest_files, parse_sector_filter
from tess_tools.inventory import INVENTORY_SCHEMA_VERSION, build_inventory
from tess_tools.known import (
    discover_tess_eb_matches,
    discover_toi_matches,
    normalize_tess_eb_row,
    normalize_toi_row,
    query_toi_catalog_file_by_identifier,
    query_tess_eb_catalog_file_by_tic,
    query_toi_catalog_file_by_tic,
)
from tess_tools.live_validate import (
    evaluate_inventory,
    is_infrastructure_error,
    run_fetch_smoke,
    summarize_inventory,
    target_tokens_from_entry,
    validation_status,
)
from tess_tools.mast_api import mast_data_rows
from tess_tools.models import ProductRecord, ProviderStatus, ResolvedTarget, SectorRecord
from tess_tools.normalize import quality_policy_for_item, normalize_lightcurve_rows, resolve_quality_mask, should_skip_quality, write_normalized_csv
from tess_tools.output import flatten_crowding_neighbors, flatten_fetch_plans, flatten_providers, flatten_targets
from tess_tools.products import (
    classify_product_scope,
    default_ffi_cadence_sec,
    dedupe_products,
    discover_mast_product_collections,
    discover_mast_timeseries_products,
    enrich_products_with_mast_file_metadata,
    infer_file_cadence_sec,
    infer_file_role,
    infer_cadence,
    infer_fetch_reference,
    infer_fetch_reference_kind,
    infer_product_family,
    mast_query_radii,
    mast_row_matches_tic,
    mast_rows_to_products,
    product_record_score,
    product_file_score,
    query_mast_caom_filtered_tic,
)
from tess_tools.recommend import build_fetch_plan, choose_product, fastest_preextracted_product, selected_product_references
from tess_tools.summaries import build_product_summary, build_sector_summary
from tess_tools.target import (
    TargetSpec,
    parse_target_args,
    read_targets_csv,
    resolve_tic_with_mast_http,
    resolve_toi,
    resolved_target_from_tic_row,
)


class TargetCsvTests(unittest.TestCase):
    def test_parse_toi_identifier_accepts_host_and_planet_forms(self) -> None:
        host = parse_target_args(["TOI-700"])
        planet = parse_target_args(["TOI", "700.01"])
        normalized_planet = parse_target_args(["TOI-700.010"])

        self.assertEqual((host.kind, host.toi_id), ("toi", "700"))
        self.assertEqual((planet.kind, planet.toi_id), ("toi", "700.01"))
        self.assertEqual(normalized_planet.toi_id, "700.01")

    def test_read_targets_csv_accepts_toi_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.csv"
            path.write_text("toi_id\nTOI-700\n", encoding="utf-8")

            specs = read_targets_csv(path)

        self.assertEqual(len(specs), 1)
        self.assertEqual((specs[0].kind, specs[0].toi_id), ("toi", "700"))

    def test_resolve_toi_uses_tic_resolution_and_preserves_alias(self) -> None:
        tic_target = ResolvedTarget(
            raw="TOI-700",
            kind="tic",
            tic_id="150428135",
            ra_deg=101.295,
            dec_deg=-65.58,
            source="mast.catalogs.filtered.tic.http",
        )
        with patch("tess_tools.known.query_toi_by_identifier", return_value=[{"toi": "700.01", "tid": 150428135}]), patch(
            "tess_tools.target.resolve_tic_with_mast_http", return_value=tic_target
        ):
            errors: list[str] = []
            target = resolve_toi("700", raw="TOI-700", errors=errors)

        self.assertEqual(target.kind, "toi")
        self.assertEqual(target.tic_id, "150428135")
        self.assertEqual(target.extra, {"toi_id": "700"})
        self.assertIn("nasa.exoplanetarchive.toi.tap", target.source)
        self.assertEqual(errors, [])

    def test_resolve_toi_can_use_local_catalog_coordinates_when_tic_queries_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toi.csv"
            path.write_text("toi,tid,ra,dec,st_tmag\n700.01,150428135,101.295,-65.58,13.1\n", encoding="utf-8")
            with patch("tess_tools.target.resolve_tic_with_mast_http", return_value=None), patch(
                "tess_tools.target.resolve_tic_with_astroquery", return_value=None
            ):
                target = resolve_toi("700", raw="TOI-700", errors=[], catalog_path=path)

        self.assertEqual(target.tic_id, "150428135")
        self.assertEqual(target.ra_deg, 101.295)
        self.assertEqual(target.dec_deg, -65.58)
        self.assertEqual(target.tmag, 13.1)
        self.assertIn("local TOI catalog", target.source)

    def test_local_toi_identifier_query_distinguishes_host_and_planet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toi.csv"
            path.write_text("toi,tid\n700.01,1\n700.02,1\n701.01,2\n", encoding="utf-8")

            host_rows = query_toi_catalog_file_by_identifier(path, "700")
            planet_rows = query_toi_catalog_file_by_identifier(path, "TOI-700.02")

        self.assertEqual(len(host_rows), 2)
        self.assertEqual([row["toi"] for row in planet_rows], ["700.02"])

    def test_read_targets_csv_preserves_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.csv"
            path.write_text(
                "tic_id,ra,dec\n"
                "123,102.7,-70.5\n"
                "not_a_tic,101.0,-69.0\n"
                ",bad,-69.0\n",
                encoding="utf-8",
            )

            specs = read_targets_csv(path)

        self.assertEqual([spec.kind for spec in specs], ["tic", "invalid", "invalid"])
        self.assertEqual([spec.row_number for spec in specs], [2, 3, 4])
        self.assertIn("invalid TIC", specs[1].error or "")
        self.assertIn("invalid RA/Dec", specs[2].error or "")

    def test_resolved_target_from_tic_row_coerces_fields(self) -> None:
        target = resolved_target_from_tic_row(
            {"ID": 123, "ra": "102.7", "dec": "-70.5", "Tmag": "11.2"},
            tic_id="123",
            raw="TIC 123",
            source="test",
        )

        self.assertEqual(target.tic_id, "123")
        self.assertEqual(target.ra_deg, 102.7)
        self.assertEqual(target.dec_deg, -70.5)
        self.assertEqual(target.tmag, 11.2)
        self.assertEqual(target.source, "test")

    def test_resolve_tic_with_mast_http_uses_first_row(self) -> None:
        with patch("tess_tools.target.query_mast_tic", return_value=[{"ID": 123, "ra": 1.5, "dec": -2.5}]):
            errors: list[str] = []
            target = resolve_tic_with_mast_http("123", raw="TIC 123", errors=errors)

        self.assertIsNotNone(target)
        self.assertEqual(target.ra_deg, 1.5)
        self.assertEqual(errors, [])

    def test_resolve_tic_with_mast_http_records_failure(self) -> None:
        with patch("tess_tools.target.query_mast_tic", side_effect=RuntimeError("boom")):
            errors: list[str] = []
            target = resolve_tic_with_mast_http("123", raw="TIC 123", errors=errors)

        self.assertIsNone(target)
        self.assertIn("boom", errors[0])

    def test_mast_data_rows_filters_non_dict_rows(self) -> None:
        rows = mast_data_rows({"data": [{"a": 1}, "skip", {"b": 2}]})

        self.assertEqual(rows, [{"a": 1}, {"b": 2}])


class SummaryTests(unittest.TestCase):
    def test_sector_summary_groups_and_cvz_flag(self) -> None:
        sectors = [
            SectorRecord(sector=1, camera=1, ccd=1),
            SectorRecord(sector=2, camera=1, ccd=2),
            SectorRecord(sector=4, camera=2, ccd=1),
        ]

        summary = build_sector_summary(sectors)

        self.assertEqual(summary["n_sectors"], 3)
        self.assertEqual(summary["sector_groups"], [{"start": 1, "end": 2, "count": 2}, {"start": 4, "end": 4, "count": 1}])
        self.assertEqual(summary["max_contiguous_sector_count"], 2)
        self.assertFalse(summary["cvz_like"])

    def test_product_summary_counts_tesscut_and_preextracted(self) -> None:
        products = [
            ProductRecord(
                family="TESSCut",
                provider="tesscut",
                sector=1,
                extra={"product_scope": "per_sector"},
            ),
            ProductRecord(
                family="SPOC",
                provider="spoc",
                sector=1,
                cadence_sec=120,
                extra={
                    "product_scope": "per_sector",
                    "fetch_reference_kind": "mast_data_uri",
                    "fetch_reference": "mast:TESS/product.fits",
                    "file_data_uri": "mast:TESS/product.fits",
                    "file_product_subgroup": "LC",
                },
            ),
            ProductRecord(
                family="SPOC",
                provider="spoc",
                sector=6,
                extra={"product_scope": "multi_sector", "sector_start": 1, "sector_end": 6},
            ),
        ]

        summary = build_product_summary(products)

        self.assertEqual(summary["n_products"], 3)
        self.assertEqual(summary["by_family"]["TESSCut"], 1)
        self.assertEqual(summary["by_provider"]["spoc"], 2)
        self.assertEqual(summary["by_scope"]["per_sector"], 2)
        self.assertEqual(summary["by_scope"]["multi_sector"], 1)
        self.assertEqual(summary["n_preextracted_products"], 2)
        self.assertEqual(summary["n_multi_sector_products"], 1)
        self.assertEqual(summary["n_products_with_fetch_reference"], 1)
        self.assertEqual(summary["n_preextracted_products_with_fetch_reference"], 1)
        self.assertEqual(summary["n_products_with_file_uri"], 1)
        self.assertEqual(summary["n_preextracted_products_with_file_uri"], 1)
        self.assertEqual(summary["fetch_reference_kinds"], ["mast_data_uri"])
        self.assertEqual(summary["file_product_subgroups"], ["LC"])
        self.assertEqual(summary["product_count_by_sector"], {"1": 2})
        self.assertEqual(summary["products_by_sector"][0]["families"], ["SPOC", "TESSCut"])
        self.assertEqual(summary["multi_sector_products"][0]["sector_start"], 1)
        self.assertEqual(summary["multi_sector_products"][0]["sector_end"], 6)
        self.assertTrue(summary["has_tesscut"])
        self.assertTrue(summary["has_preextracted_lightcurve"])


class CrowdingTests(unittest.TestCase):
    def test_crowding_summary_estimates_neighbor_risk(self) -> None:
        target = ResolvedTarget(raw="TIC 123", kind="tic", tic_id="123", ra_deg=100.0, dec_deg=-20.0, tmag=10.0)
        summary = build_crowding_summary(
            target,
            [
                {"source_id": "self", "ra": 100.0, "dec": -20.0, "phot_g_mean_mag": 10.0},
                {"source_id": "near", "ra": 100.001, "dec": -20.0, "phot_g_mean_mag": 12.0},
                {"source_id": "far", "ra": 100.015, "dec": -20.0, "phot_g_mean_mag": 15.0},
            ],
            radius_arcsec=60.0,
            sectors=[
                SectorRecord(sector=1, camera=1, ccd=1),
                SectorRecord(sector=2, camera=1, ccd=1),
                SectorRecord(sector=3, camera=2, ccd=3),
            ],
        )

        self.assertEqual(summary["risk"], "high")
        self.assertEqual(summary["n_neighbors"], 2)
        self.assertEqual(summary["n_neighbors_within_1_pixel"], 1)
        self.assertEqual(summary["n_neighbors_within_2_pixels"], 1)
        self.assertEqual(summary["n_neighbors_within_3_pixels"], 2)
        self.assertLess(summary["nearest_neighbor_arcsec"], 4.0)
        self.assertLess(summary["neighbors"][0]["separation_pixels"], 0.2)
        self.assertEqual(summary["neighbors"][0]["aperture_weight"], 1.0)
        self.assertIsNotNone(summary["heuristic_aperture_contamination_ratio"])
        self.assertLess(summary["heuristic_dilution_factor"], 1.0)
        self.assertEqual(summary["sector_geometry"]["n_camera_ccd_geometries"], 2)
        self.assertFalse(summary["sector_geometry"]["single_camera_ccd_geometry"])
        self.assertEqual(summary["sector_geometry"]["camera_ccd_groups"][0]["sectors"], [1, 2])
        self.assertAlmostEqual(flux_ratio_from_delta_mag(2.0), 0.15848931924611134)

    def test_aperture_contamination_weight_tapers_to_three_pixels(self) -> None:
        self.assertEqual(aperture_contamination_weight(0.0), 1.0)
        self.assertEqual(aperture_contamination_weight(10.5), 1.0)
        self.assertAlmostEqual(aperture_contamination_weight(42.0), 0.4)
        self.assertEqual(aperture_contamination_weight(63.0), 0.0)

    def test_query_mast_gaia_neighbors_uses_invoke_api(self) -> None:
        rows = [{"source_id": "gaia-1", "ra": 100.001, "dec": -20.0, "phot_g_mean_mag": 12.0}]
        with patch("tess_tools.crowding.invoke_mast_service", return_value={"data": rows}) as invoke:
            result = query_mast_gaia_neighbors(100.0, -20.0, 60.0)

        invoke.assert_called_once()
        service, params = invoke.call_args.args
        self.assertEqual(service, "Mast.Catalogs.GaiaDR3.Cone")
        self.assertEqual(params["ra"], 100.0)
        self.assertEqual(params["dec"], -20.0)
        self.assertAlmostEqual(params["radius"], 60.0 / 3600.0)
        self.assertIn("phot_g_mean_mag", params["columns"])
        self.assertEqual(result[0]["_query_source"], "Mast.Catalogs.GaiaDR3.Cone")

    def test_query_mast_gaia_neighbors_retries_without_columns(self) -> None:
        def fake_invoke(service, params, **kwargs):
            if "columns" in params:
                raise RuntimeError("unknown column")
            return {"data": [{"source_id": "gaia-1", "ra": 100.001, "dec": -20.0}]}

        with patch("tess_tools.crowding.invoke_mast_service", side_effect=fake_invoke) as invoke:
            result = query_mast_gaia_neighbors(100.0, -20.0, 60.0)

        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(result[0]["_query_source"], "Mast.Catalogs.GaiaDR3.Cone")

    def test_query_gaia_neighbors_reports_http_and_optional_dependency_failure(self) -> None:
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name.startswith(("astropy", "astroquery")):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch("tess_tools.crowding.query_mast_gaia_neighbors", side_effect=RuntimeError("all MAST Gaia HTTP attempts failed")):
            with patch("builtins.__import__", side_effect=fake_import):
                with self.assertRaises(RuntimeError) as exc:
                    query_gaia_neighbors(100.0, -20.0, 60.0)

        message = str(exc.exception)
        self.assertIn("MAST Gaia HTTP query failed", message)
        self.assertIn("requires astroquery and astropy", message)


class KnownObjectTests(unittest.TestCase):
    def test_normalize_toi_row_maps_common_columns(self) -> None:
        row = {
            "toi": "700.01",
            "tid": "150428135",
            "tfopwg_disp": "KP",
            "pl_p": "9.98",
            "pl_tranmid": "2458380.1",
            "pl_trandurh": "3.2",
            "pl_trandep": "450",
            "pl_rade": "1.2",
        }

        match = normalize_toi_row(row)

        self.assertEqual(match["toi"], "700.01")
        self.assertEqual(match["tic_id"], "150428135")
        self.assertEqual(match["disposition"], "KP")
        self.assertEqual(match["period_days"], 9.98)
        self.assertEqual(match["depth_ppm"], 450.0)

    def test_discover_toi_matches_queries_by_tic(self) -> None:
        target = ResolvedTarget(raw="TIC 150428135", kind="tic", tic_id="150428135")
        rows = [{"toi": "700.01", "tid": 150428135, "tfopwg_disp": "KP"}]
        with patch("tess_tools.known.query_toi_by_tic", return_value=rows) as query:
            errors: list[str] = []
            summary, status = discover_toi_matches(target, errors=errors)

        query.assert_called_once_with("150428135")
        self.assertEqual(errors, [])
        self.assertEqual(status.status, "ok")
        self.assertEqual(summary["n_matches"], 1)
        self.assertEqual(summary["dispositions"], ["KP"])

    def test_discover_toi_matches_skips_without_tic(self) -> None:
        target = ResolvedTarget(raw="1 2", kind="coords", ra_deg=1.0, dec_deg=2.0)
        errors: list[str] = []
        summary, status = discover_toi_matches(target, errors=errors)

        self.assertEqual(errors, [])
        self.assertEqual(status.status, "skipped")
        self.assertEqual(summary["n_matches"], 0)

    def test_query_toi_catalog_file_by_tic_reads_csv_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toi.csv"
            path.write_text(
                "tid,toi,tfopwg_disp,pl_p\n"
                "150428135,700.01,CP,9.98\n"
                "261136679,example,FP,1.5\n",
                encoding="utf-8",
            )

            rows = query_toi_catalog_file_by_tic(path, "150428135")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["toi"], "700.01")

    def test_query_toi_catalog_file_by_tic_reads_json_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toi.json"
            path.write_text(
                json.dumps({"data": [{"tid": 150428135, "toi": "700.01"}, {"tid": 1, "toi": "other"}]}),
                encoding="utf-8",
            )

            rows = query_toi_catalog_file_by_tic(path, "150428135")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["toi"], "700.01")

    def test_discover_toi_matches_uses_local_catalog_path(self) -> None:
        target = ResolvedTarget(raw="TIC 150428135", kind="tic", tic_id="150428135")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toi.csv"
            path.write_text("tid,toi,tfopwg_disp\n150428135,700.01,CP\n", encoding="utf-8")
            with patch("tess_tools.known.query_toi_by_tic") as live_query:
                errors: list[str] = []
                summary, status = discover_toi_matches(target, errors=errors, catalog_path=path)

        live_query.assert_not_called()
        self.assertEqual(errors, [])
        self.assertEqual(status.status, "ok")
        self.assertEqual(summary["n_matches"], 1)
        self.assertIn("local TOI catalog", summary["source"])
        self.assertIn("local TOI catalog", summary["matches"][0]["source"])

    def test_normalize_tess_eb_row_maps_common_columns(self) -> None:
        row = {
            "ID": "EB-1",
            "TIC": "150428135",
            "period": "2.5",
            "epoch": "2459000.25",
            "duration": "4.2",
            "morphology": "detached",
        }

        match = normalize_tess_eb_row(row)

        self.assertEqual(match["eb_id"], "EB-1")
        self.assertEqual(match["tic_id"], "150428135")
        self.assertEqual(match["period_days"], 2.5)
        self.assertEqual(match["epoch_bjd"], 2459000.25)
        self.assertEqual(match["morphology"], "detached")

    def test_query_tess_eb_catalog_file_by_tic_reads_csv_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tess_eb.csv"
            path.write_text(
                "TIC,ID,period,morphology\n"
                "150428135,EB-1,2.5,detached\n"
                "261136679,EB-2,1.5,contact\n",
                encoding="utf-8",
            )

            rows = query_tess_eb_catalog_file_by_tic(path, "150428135")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ID"], "EB-1")

    def test_discover_tess_eb_matches_requires_local_catalog_path(self) -> None:
        target = ResolvedTarget(raw="TIC 150428135", kind="tic", tic_id="150428135")
        errors: list[str] = []
        summary, status = discover_tess_eb_matches(target, errors=errors)

        self.assertEqual(errors, [])
        self.assertEqual(status.status, "skipped")
        self.assertEqual(summary["n_matches"], 0)

    def test_discover_tess_eb_matches_uses_local_catalog_path(self) -> None:
        target = ResolvedTarget(raw="TIC 150428135", kind="tic", tic_id="150428135")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tess_eb.csv"
            path.write_text("TIC,ID,period\n150428135,EB-1,2.5\n", encoding="utf-8")
            errors: list[str] = []
            summary, status = discover_tess_eb_matches(target, errors=errors, catalog_path=path)

        self.assertEqual(errors, [])
        self.assertEqual(status.status, "ok")
        self.assertEqual(summary["n_matches"], 1)
        self.assertIn("local TESS EB catalog", summary["source"])


class ProductClassifierTests(unittest.TestCase):
    def test_infer_product_family_distinguishes_tess_spoc_from_spoc(self) -> None:
        self.assertEqual(infer_product_family({"obs_id": "hlsp_tess-spoc_tess"}), "TESS-SPOC")
        self.assertEqual(infer_product_family({"obs_id": "tess-s0001-spoc"}), "SPOC")
        self.assertEqual(infer_product_family({"provenance_name": "QLP"}), "QLP")

    def test_infer_cadence(self) -> None:
        self.assertEqual(infer_cadence({"obs_id": "target-2min"}), 120)
        self.assertEqual(infer_cadence({"productFilename": "lc_200s.fits"}), 200)
        self.assertEqual(infer_cadence({"productFilename": "lc_30-min.fits"}), 1800)
        self.assertIsNone(infer_cadence({"obs_id": "tess2018206045859-s0001-0000000261136679-0120-s"}))

    def test_dedupe_products_includes_provider_in_key(self) -> None:
        products = [
            ProductRecord(family="SPOC", provider="spoc", sector=1, product_id="a"),
            ProductRecord(family="SPOC", provider="spoc", sector=1, product_id="a"),
            ProductRecord(family="SPOC", provider="other", sector=1, product_id="a"),
        ]

        self.assertEqual(len(dedupe_products(products)), 2)

    def test_dedupe_products_keeps_enriched_fetch_record(self) -> None:
        plain = ProductRecord(family="QLP", provider="qlp", sector=26, cadence_sec=1800, product_id="same")
        enriched = ProductRecord(
            family="QLP",
            provider="qlp",
            sector=26,
            cadence_sec=1800,
            product_id="same",
            access_url="https://example.test/qlp.fits",
            extra={
                "fetch_reference_kind": "mast_data_uri",
                "fetch_reference": "mast:HLSP/qlp.fits",
                "file_role": "lightcurve",
                "file_name": "qlp_llc.fits",
            },
        )

        result = dedupe_products([plain, enriched])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].extra["file_name"], "qlp_llc.fits")
        self.assertGreater(product_record_score(enriched), product_record_score(plain))

    def test_mast_row_matches_tic_rejects_neighbor(self) -> None:
        self.assertTrue(mast_row_matches_tic({"target_name": "TIC 21278334"}, "21278334"))
        self.assertFalse(mast_row_matches_tic({"target_name": "21278335"}, "21278334"))
        self.assertFalse(mast_row_matches_tic({"obs_id": "contains-21278334"}, "21278334"))

    def test_mast_query_radii_only_get_narrower(self) -> None:
        self.assertEqual(mast_query_radii(60), [60.0, 10.0, 3.0])
        self.assertEqual(mast_query_radii(10), [10.0, 3.0])
        self.assertEqual(mast_query_radii(3), [3.0])

    def test_mast_rows_to_products_classifies_spoc_rows(self) -> None:
        rows = [
            {
                "obs_collection": "TESS",
                "provenance_name": "SPOC",
                "sequence_number": 27,
                "target_name": "261136679",
                "obs_id": "tess2020186164531-s0027-0000000261136679-0189-s",
                "dataproduct_type": "timeseries",
                "obsid": 12345,
                "dataURI": "mast:TESS/product.fits",
                "dataURL": "https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:TESS/product.fits",
            }
        ]

        products = mast_rows_to_products(rows, source="test-source")

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].family, "SPOC")
        self.assertEqual(products[0].provider, "spoc")
        self.assertEqual(products[0].sector, 27)
        self.assertEqual(products[0].source, "test-source")
        self.assertEqual(products[0].extra["product_scope"], "per_sector")
        self.assertEqual(products[0].extra["sector_start"], 27)
        self.assertEqual(products[0].extra["mast_obsid"], "12345")
        self.assertEqual(products[0].extra["data_uri"], "mast:TESS/product.fits")
        self.assertEqual(products[0].extra["fetch_reference_kind"], "mast_data_uri")
        self.assertEqual(products[0].extra["fetch_reference"], "mast:TESS/product.fits")
        self.assertEqual(
            products[0].access_url,
            "https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:TESS/product.fits",
        )

    def test_infer_fetch_reference_prefers_data_uri(self) -> None:
        row = {
            "dataURI": "mast:TESS/product.fits",
            "dataURL": "https://example.test/product.fits",
            "obsid": 12345,
            "obs_id": "obs",
        }

        self.assertEqual(infer_fetch_reference_kind(row), "mast_data_uri")
        self.assertEqual(infer_fetch_reference(row), "mast:TESS/product.fits")
        self.assertEqual(infer_fetch_reference_kind({"obsid": 12345}), "mast_obsid")
        self.assertEqual(infer_fetch_reference({"obs_id": "obs"}), "obs")

    def test_product_file_score_prefers_light_curve_fits(self) -> None:
        lc = {"productType": "SCIENCE", "productSubGroupDescription": "LC", "productFilename": "target_lc.fits", "dataURI": "mast:TESS/lc.fits"}
        dvt = {"productType": "SCIENCE", "productSubGroupDescription": "DVT", "productFilename": "target_dvt.fits", "dataURI": "mast:TESS/dvt.fits"}
        report = {"productType": "INFO", "productSubGroupDescription": "DVR", "productFilename": "target_dvr.pdf", "dataURI": "mast:TESS/dvr.pdf"}

        self.assertGreater(product_file_score(lc), product_file_score(dvt))
        self.assertGreater(product_file_score(dvt), product_file_score(report))

    def test_infer_file_role_and_cadence(self) -> None:
        self.assertEqual(infer_file_role({"productSubGroupDescription": "LC", "productFilename": "target_lc.fits"}), "lightcurve")
        self.assertEqual(infer_file_role({"productSubGroupDescription": "TP", "productFilename": "target_tp.fits"}), "target-pixel")
        self.assertEqual(infer_file_role({"productSubGroupDescription": "DVT", "productFilename": "target_dvt.fits"}), "dv-timeseries")
        self.assertEqual(infer_file_role({"productType": "INFO", "productFilename": "target.pdf"}), "report")
        self.assertEqual(infer_file_cadence_sec({"productFilename": "target_fast-lc.fits"}), 20)
        self.assertEqual(infer_file_cadence_sec({"productFilename": "target_lc.fits"}), 120)
        self.assertEqual(
            infer_file_role({"productFilename": "hlsp_qlp_tess_ffi_s0026-0000000021278334_tess_v01_llc.fits"}),
            "lightcurve",
        )
        self.assertEqual(
            infer_file_cadence_sec(
                {"productFilename": "hlsp_qlp_tess_ffi_s0026-0000000021278334_tess_v01_llc.fits"}
            ),
            1800,
        )

    def test_default_ffi_cadence_tracks_sector_eras(self) -> None:
        for family in ("QLP", "TESS-SPOC"):
            self.assertEqual(default_ffi_cadence_sec(family, 26), 1800)
            self.assertEqual(default_ffi_cadence_sec(family, 27), 600)
            self.assertEqual(default_ffi_cadence_sec(family, 56), 200)
        self.assertIsNone(default_ffi_cadence_sec("SPOC", 56))

    def test_query_mast_caom_filtered_tic_uses_explicit_hlsp_provenance(self) -> None:
        with patch("tess_tools.products.invoke_mast_service", return_value={"data": []}) as invoke:
            query_mast_caom_filtered_tic("21278334", provenance_name="QLP")

        filters = invoke.call_args.args[1]["filters"]
        self.assertEqual(filters[0], {"paramName": "provenance_name", "values": ["QLP"]})
        self.assertEqual(filters[1], {"paramName": "target_name", "values": ["21278334"]})
        self.assertNotIn("obs_collection", {item["paramName"] for item in filters})

    def test_explicit_hlsp_discovery_reports_qlp_and_tess_spoc_availability(self) -> None:
        target = ResolvedTarget(raw="TIC 21278334", kind="tic", tic_id="21278334", ra_deg=1.0, dec_deg=2.0)
        qlp_row = {
            "obs_collection": "HLSP",
            "provenance_name": "QLP",
            "sequence_number": 26,
            "target_name": "21278334",
            "obs_id": "hlsp_qlp_tess_ffi_s0026-0000000021278334_tess_v01_llc",
            "dataproduct_type": "timeseries",
            "obsid": 100,
            "dataURI": "mast:HLSP/qlp/qlp_llc.fits",
        }
        tess_spoc_row = {
            "obs_collection": "HLSP",
            "provenance_name": "TESS-SPOC",
            "sequence_number": 26,
            "target_name": "21278334",
            "obs_id": "hlsp_tess-spoc_tess_phot_0000000021278334-s0026_tess_v1_lc",
            "dataproduct_type": "timeseries",
            "obsid": 200,
            "dataURI": "mast:HLSP/tess-spoc/tess_spoc_lc.fits",
        }

        def query(_tic_id: str, *, provenance_name: str | None = None) -> list[dict]:
            return [qlp_row] if provenance_name == "QLP" else [tess_spoc_row]

        with patch(
            "tess_tools.products._discover_mast_core_products",
            return_value=([], ProviderStatus(name="MAST observations", status="empty", records=0)),
        ), patch("tess_tools.products.query_mast_caom_filtered_tic", side_effect=query):
            errors: list[str] = []
            discovery = discover_mast_product_collections(target, radius_arcsec=60, errors=errors)

        self.assertEqual(errors, [])
        self.assertEqual({product.family for product in discovery.products}, {"QLP", "TESS-SPOC"})
        self.assertTrue(all(product.cadence_sec == 1800 for product in discovery.products))
        statuses = {provider.name: provider.status for provider in discovery.providers}
        self.assertEqual(statuses["MAST QLP"], "ok")
        self.assertEqual(statuses["MAST TESS-SPOC"], "ok")

    def test_enrich_products_with_mast_file_metadata_prefers_lc_uri(self) -> None:
        products = [
            ProductRecord(
                family="SPOC",
                provider="spoc",
                sector=1,
                product_id="obs",
                extra={"mast_obsid": "12345", "fetch_reference_kind": "mast_obsid", "fetch_reference": "12345"},
            )
        ]
        file_rows = [
            {
                "obsID": 12345,
                "productType": "INFO",
                "productSubGroupDescription": "DVR",
                "productFilename": "target_dvr.pdf",
                "dataURI": "mast:TESS/product/target_dvr.pdf",
            },
            {
                "obsID": 12345,
                "productType": "SCIENCE",
                "productSubGroupDescription": "LC",
                "productFilename": "target_lc.fits",
                "dataURI": "mast:TESS/product/target_lc.fits",
                "size": 2048,
                "calib_level": 3,
            },
        ]

        with patch("tess_tools.products.query_mast_caom_products", return_value=file_rows) as query:
            enriched, message = enrich_products_with_mast_file_metadata(products)

        query.assert_called_once_with(["12345"])
        self.assertIn("enriched=1", message or "")
        self.assertEqual(enriched[0].extra["fetch_reference_kind"], "mast_data_uri")
        self.assertEqual(enriched[0].extra["fetch_reference"], "mast:TESS/product/target_lc.fits")
        self.assertEqual(enriched[0].extra["file_name"], "target_lc.fits")
        self.assertEqual(enriched[0].extra["file_role"], "lightcurve")
        self.assertEqual(enriched[0].extra["file_cadence_sec"], 120)
        self.assertEqual(enriched[0].cadence_sec, 120)
        self.assertEqual(enriched[0].extra["file_product_subgroup"], "LC")
        self.assertEqual(enriched[0].extra["file_size"], "2048")
        self.assertIn("uri=mast:TESS%2Fproduct%2Ftarget_lc.fits", enriched[0].access_url or "")

    def test_classify_product_scope_identifies_multi_sector_products(self) -> None:
        scope = classify_product_scope(
            {"obs_id": "tess2018206190142-s0001-s0006-0000000261136679"},
            fallback_sector=6,
        )

        self.assertEqual(scope["product_scope"], "multi_sector")
        self.assertEqual(scope["sector_start"], 1)
        self.assertEqual(scope["sector_end"], 6)

    def test_discover_mast_timeseries_products_uses_filtered_tic_first(self) -> None:
        target = ResolvedTarget(raw="TIC 261136679", kind="tic", tic_id="261136679", ra_deg=1.0, dec_deg=2.0)
        rows = [
            {
                "obs_collection": "TESS",
                "provenance_name": "SPOC",
                "sequence_number": 27,
                "target_name": "261136679",
                "obs_id": "tess-s0027-spoc",
            }
        ]
        with patch("tess_tools.products.query_mast_caom_filtered_tic", return_value=rows) as filtered:
            with patch("tess_tools.products.query_mast_caom_cone") as cone:
                errors: list[str] = []
                products, status = discover_mast_timeseries_products(target, radius_arcsec=60, errors=errors)

        filtered.assert_called_once_with("261136679")
        cone.assert_not_called()
        self.assertEqual(errors, [])
        self.assertEqual(status.status, "ok")
        self.assertEqual(status.records, 1)
        self.assertEqual(products[0].source, "mast.caom.filtered.http")

    def test_discover_mast_timeseries_products_falls_back_from_empty_filtered(self) -> None:
        target = ResolvedTarget(raw="TIC 261136679", kind="tic", tic_id="261136679", ra_deg=1.0, dec_deg=2.0)
        cone_rows = [
            {
                "obs_collection": "TESS",
                "provenance_name": "QLP",
                "sequence_number": 1,
                "target_name": "261136679",
                "obs_id": "qlp-s0001",
            }
        ]
        with patch("tess_tools.products.query_mast_caom_filtered_tic", return_value=[]):
            with patch("tess_tools.products.query_mast_caom_cone", return_value=cone_rows):
                errors: list[str] = []
                products, status = discover_mast_timeseries_products(target, radius_arcsec=60, errors=errors)

        self.assertEqual(errors, [])
        self.assertEqual(status.status, "ok")
        self.assertEqual(products[0].family, "QLP")
        self.assertEqual(products[0].source, "mast.caom.cone.http")

    def test_discover_mast_timeseries_products_rejects_neighbor_cone_rows(self) -> None:
        target = ResolvedTarget(raw="TIC 261136679", kind="tic", tic_id="261136679", ra_deg=1.0, dec_deg=2.0)
        cone_rows = [
            {
                "obs_collection": "TESS",
                "provenance_name": "SPOC",
                "sequence_number": 1,
                "target_name": "261136680",
                "obs_id": "neighbor-spoc-s0001",
            }
        ]
        with patch("tess_tools.products.query_mast_caom_filtered_tic", return_value=[]), patch(
            "tess_tools.products.query_mast_caom_cone", return_value=cone_rows
        ):
            products, status = discover_mast_timeseries_products(target, radius_arcsec=60, errors=[])

        self.assertEqual(products, [])
        self.assertEqual(status.status, "ok")
        self.assertIn("retained=0", status.message or "")


class InventoryAndOutputTests(unittest.TestCase):
    def test_invalid_inventory_has_v03_schema_and_error(self) -> None:
        spec = TargetSpec(raw="bad", kind="invalid", error="bad target", row_number=7)
        with tempfile.TemporaryDirectory() as tmp:
            inventory = build_inventory(spec, cache=MetadataCache(Path(tmp)), offline=True)

        self.assertEqual(inventory["schema_version"], INVENTORY_SCHEMA_VERSION)
        self.assertEqual(inventory["target"]["row_number"], 7)
        self.assertEqual(inventory["sector_summary"]["n_sectors"], 0)
        self.assertIn("bad target", inventory["errors"])

    def test_build_inventory_includes_gaia_crowding_when_enabled(self) -> None:
        spec = TargetSpec(raw="102.7 -70.5", kind="coords", ra_deg=102.7, dec_deg=-70.5)
        crowding = {
            "schema_version": "tess-where.crowding.v0.1",
            "risk": "low",
            "n_neighbors": 0,
            "n_neighbors_within_1_pixel": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tess_tools.inventory.discover_sectors", return_value=[]), patch(
                "tess_tools.inventory.discover_products"
            ) as products, patch("tess_tools.inventory.discover_crowding") as crowding_provider:
                products.return_value.products = []
                products.return_value.providers = []
                crowding_provider.return_value = (crowding, ProviderStatus(name="Gaia neighbors", status="ok", message="ok", records=0))

                inventory = build_inventory(
                    spec,
                    cache=MetadataCache(Path(tmp)),
                    enabled_providers={"tesscut", "gaia"},
                    refresh=True,
                )

        self.assertEqual(inventory["crowding_summary"]["risk"], "low")
        self.assertEqual(inventory["providers"][0]["name"], "Gaia neighbors")
        crowding_provider.assert_called_once()
        self.assertIn("sectors", crowding_provider.call_args.kwargs)

    def test_build_inventory_includes_toi_when_enabled(self) -> None:
        spec = TargetSpec(raw="TIC 150428135", kind="tic", tic_id="150428135")
        known = {
            "schema_version": "tess-where.known-objects.v0.1",
            "n_matches": 1,
            "catalogs": ["TOI"],
            "toi": {"n_matches": 1, "dispositions": ["KP"], "matches": [{"toi": "700.01"}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch("tess_tools.inventory.resolve_target", return_value=ResolvedTarget(raw="TIC 150428135", kind="tic", tic_id="150428135")), patch(
                "tess_tools.inventory.discover_sectors", return_value=[]
            ), patch("tess_tools.inventory.discover_products") as products, patch(
                "tess_tools.inventory.discover_known_objects"
            ) as known_provider:
                products.return_value.products = []
                products.return_value.providers = []
                known_provider.return_value = (known, [ProviderStatus(name="TOI catalog", status="ok", records=1)])

                inventory = build_inventory(
                    spec,
                    cache=MetadataCache(Path(tmp)),
                    enabled_providers={"tesscut", "toi"},
                    refresh=True,
                )

        self.assertEqual(inventory["known_object_summary"]["n_matches"], 1)
        self.assertEqual(inventory["providers"][-1]["name"], "TOI catalog")
        known_provider.assert_called_once()

    def test_build_inventory_includes_tess_eb_when_enabled(self) -> None:
        spec = TargetSpec(raw="TIC 150428135", kind="tic", tic_id="150428135")
        known = {
            "schema_version": "tess-where.known-objects.v0.1",
            "n_matches": 1,
            "catalogs": ["TESS EB"],
            "toi": {"n_matches": 0, "dispositions": [], "matches": []},
            "tess_eb": {"n_matches": 1, "matches": [{"eb_id": "EB-1"}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "tess_eb.csv"
            catalog.write_text("TIC,ID\n150428135,EB-1\n", encoding="utf-8")
            with patch("tess_tools.inventory.resolve_target", return_value=ResolvedTarget(raw="TIC 150428135", kind="tic", tic_id="150428135")), patch(
                "tess_tools.inventory.discover_sectors", return_value=[]
            ), patch("tess_tools.inventory.discover_products") as products, patch(
                "tess_tools.inventory.discover_known_objects"
            ) as known_provider:
                products.return_value.products = []
                products.return_value.providers = []
                known_provider.return_value = (known, [ProviderStatus(name="TESS EB catalog", status="ok", records=1)])

                inventory = build_inventory(
                    spec,
                    cache=MetadataCache(Path(tmp)),
                    enabled_providers={"tesscut", "tesseb"},
                    tess_eb_catalog_path=catalog,
                    refresh=True,
                )

        self.assertEqual(inventory["known_object_summary"]["catalogs"], ["TESS EB"])
        self.assertEqual(inventory["providers"][-1]["name"], "TESS EB catalog")
        known_provider.assert_called_once()
        self.assertEqual(known_provider.call_args.kwargs["tess_eb_catalog_path"], catalog)

    def test_flatten_outputs_include_targets_providers_and_fetch_plans(self) -> None:
        document = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "targets": [
                {
                    "input": "102.7 -70.5",
                    "target": {"kind": "coords", "tic_id": None, "ra_deg": 102.7, "dec_deg": -70.5},
                    "sector_summary": {"n_sectors": 1, "sector_groups": [{"start": 2, "end": 2, "count": 1}]},
                    "product_summary": {
                        "families": ["TESSCut"],
                        "providers": ["tesscut"],
                        "n_products": 1,
                        "n_products_with_fetch_reference": 0,
                        "n_preextracted_products_with_fetch_reference": 0,
                        "n_products_with_file_uri": 0,
                        "n_preextracted_products_with_file_uri": 0,
                        "fetch_reference_kinds": [],
                        "file_product_subgroups": [],
                    },
                    "providers": [{"name": "TESSCut", "status": "ok", "message": "ok", "records": 1}],
                    "crowding_summary": {
                        "risk": "medium",
                        "n_neighbors": 3,
                        "n_neighbors_within_1_pixel": 1,
                        "n_neighbors_within_2_pixels": 2,
                        "n_neighbors_within_3_pixels": 3,
                        "query_source": "Mast.Catalogs.GaiaDR3.Cone",
                        "nearest_neighbor_arcsec": 12.0,
                        "brightest_delta_mag": 4.5,
                        "total_neighbor_flux_ratio": 0.03,
                        "total_neighbor_flux_ratio_within_1_pixel": 0.0158,
                        "total_neighbor_flux_ratio_within_2_pixels": 0.02,
                        "total_neighbor_flux_ratio_within_3_pixels": 0.03,
                        "heuristic_aperture_contamination_ratio": 0.018,
                        "heuristic_dilution_factor": 0.9823182711198428,
                        "contamination_model": "linear radial weight to 3 nominal TESS pixels using Gaia G as a TESS-band proxy",
                        "sector_geometry": {
                            "n_camera_ccd_geometries": 2,
                            "single_camera_ccd_geometry": False,
                            "camera_ccd_groups": [
                                {"camera": 1, "ccd": 1, "n_sectors": 1, "sectors": [2]},
                                {"camera": 2, "ccd": 3, "n_sectors": 2, "sectors": [3, 4]},
                            ],
                        },
                        "neighbors": [
                            {
                                "source_id": "gaia-1",
                                "ra_deg": 102.701,
                                "dec_deg": -70.5,
                                "mag": 14.2,
                                "separation_arcsec": 3.1,
                                "separation_pixels": 0.14761904761904762,
                                "delta_mag": 4.5,
                                "flux_ratio": 0.0158,
                                "aperture_weight": 1.0,
                                "weighted_flux_ratio": 0.0158,
                                "query_source": "Mast.Catalogs.GaiaDR3.Cone",
                            }
                        ],
                    },
                    "known_object_summary": {
                        "n_matches": 2,
                        "catalogs": ["TOI", "TESS EB"],
                        "toi": {
                            "n_matches": 1,
                            "dispositions": ["KP"],
                            "matches": [{"toi": "700.01"}],
                        },
                        "tess_eb": {
                            "n_matches": 1,
                            "matches": [{"eb_id": "EB-1"}],
                        },
                    },
                    "fetch_plan": {
                        "product": "cutout",
                        "sector_argument": "all",
                        "sectors": [2],
                        "n_selected_product_references": 0,
                        "selected_product_references": [],
                        "command": "tess-fetch 102.700000 -70.500000 --product cutout --sectors all",
                        "reason": "test",
                        "caveats": ["choose aperture"],
                    },
                    "errors": [],
                    "cache": {"hit": False, "key": "abc"},
                }
            ],
        }

        self.assertEqual(flatten_targets(document)[0]["n_sectors"], 1)
        self.assertEqual(flatten_targets(document)[0]["n_products_with_fetch_reference"], 0)
        self.assertEqual(flatten_targets(document)[0]["n_products_with_file_uri"], 0)
        self.assertEqual(flatten_targets(document)[0]["crowding_risk"], "medium")
        self.assertEqual(flatten_targets(document)[0]["crowding_n_neighbors"], 3)
        self.assertEqual(flatten_targets(document)[0]["crowding_n_neighbors_within_2_pixels"], 2)
        self.assertEqual(flatten_targets(document)[0]["crowding_n_neighbors_within_3_pixels"], 3)
        self.assertEqual(flatten_targets(document)[0]["crowding_heuristic_aperture_contamination_ratio"], 0.018)
        self.assertEqual(flatten_targets(document)[0]["crowding_heuristic_dilution_factor"], 0.9823182711198428)
        self.assertEqual(flatten_targets(document)[0]["crowding_query_source"], "Mast.Catalogs.GaiaDR3.Cone")
        self.assertEqual(flatten_targets(document)[0]["crowding_sector_geometry_count"], 2)
        self.assertEqual(flatten_targets(document)[0]["crowding_sector_geometry_groups"], "cam1-ccd1:2;cam2-ccd3:3,4")
        self.assertEqual(flatten_targets(document)[0]["known_object_n_matches"], 2)
        self.assertEqual(flatten_targets(document)[0]["known_object_catalogs"], "TOI,TESS EB")
        self.assertEqual(flatten_targets(document)[0]["toi_n_matches"], 1)
        self.assertEqual(flatten_targets(document)[0]["toi_dispositions"], "KP")
        self.assertEqual(flatten_targets(document)[0]["toi_ids"], "700.01")
        self.assertEqual(flatten_targets(document)[0]["tess_eb_n_matches"], 1)
        self.assertEqual(flatten_targets(document)[0]["tess_eb_ids"], "EB-1")
        self.assertEqual(flatten_crowding_neighbors(document)[0]["source_id"], "gaia-1")
        self.assertEqual(flatten_crowding_neighbors(document)[0]["separation_pixels"], 0.14761904761904762)
        self.assertEqual(flatten_crowding_neighbors(document)[0]["weighted_flux_ratio"], 0.0158)
        self.assertEqual(flatten_crowding_neighbors(document)[0]["query_source"], "Mast.Catalogs.GaiaDR3.Cone")
        self.assertEqual(flatten_crowding_neighbors(document)[0]["neighbor_rank"], 1)
        self.assertEqual(flatten_crowding_neighbors(document)[0]["crowding_risk"], "medium")
        self.assertEqual(flatten_providers(document)[0]["status"], "ok")
        self.assertEqual(flatten_fetch_plans(document)[0]["product"], "cutout")
        self.assertEqual(flatten_fetch_plans(document)[0]["n_selected_product_references"], 0)

    def test_build_fetch_plan_prefers_spoc(self) -> None:
        plan = build_fetch_plan(
            target=ResolvedTarget(raw="TIC 123", kind="tic", tic_id="123"),
            sectors=[SectorRecord(sector=1)],
            products=[ProductRecord(family="SPOC", provider="spoc", sector=1)],
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan["product"], "spoc")

    def test_build_fetch_plan_records_best_for_mode(self) -> None:
        plan = build_fetch_plan(
            target=ResolvedTarget(raw="TIC 123", kind="tic", tic_id="123"),
            sectors=[SectorRecord(sector=1)],
            products=[ProductRecord(family="TESSCut", provider="tesscut", sector=1)],
            best_for="rotation",
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan["schema_version"], "tess-where.fetch-plan.v0.4")
        self.assertEqual(plan["best_for"], "rotation")
        self.assertIn("--best-for rotation", plan["command"])

    def test_build_fetch_plan_includes_selected_product_references(self) -> None:
        plan = build_fetch_plan(
            target=ResolvedTarget(raw="TIC 123", kind="tic", tic_id="123"),
            sectors=[SectorRecord(sector=1)],
            products=[
                ProductRecord(
                    family="SPOC",
                    provider="spoc",
                    sector=1,
                    product_id="obs",
                    extra={
                        "product_scope": "per_sector",
                        "sector_start": 1,
                        "sector_end": 1,
                        "fetch_reference_kind": "mast_data_uri",
                        "fetch_reference": "mast:TESS/product/target_lc.fits",
                        "file_name": "target_lc.fits",
                        "file_product_subgroup": "LC",
                    },
                )
            ],
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan["product"], "spoc")
        self.assertEqual(plan["n_selected_product_references"], 1)
        self.assertEqual(plan["selected_product_references"][0]["fetch_reference_kind"], "mast_data_uri")
        self.assertEqual(plan["selected_product_references"][0]["file_name"], "target_lc.fits")

    def test_selected_product_references_filters_by_product_key(self) -> None:
        rows = selected_product_references(
            [
                ProductRecord(
                    family="SPOC",
                    provider="spoc",
                    sector=1,
                    extra={"fetch_reference": "mast:TESS/spoc.fits", "fetch_reference_kind": "mast_data_uri"},
                ),
                ProductRecord(
                    family="QLP",
                    provider="qlp",
                    sector=1,
                    extra={"fetch_reference": "mast:TESS/qlp.fits", "fetch_reference_kind": "mast_data_uri"},
                ),
            ],
            "spoc",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "spoc")

    def test_selected_product_references_prefers_lightcurves_when_available(self) -> None:
        rows = selected_product_references(
            [
                ProductRecord(
                    family="SPOC",
                    provider="spoc",
                    sector=1,
                    extra={
                        "fetch_reference": "mast:TESS/dvt.fits",
                        "fetch_reference_kind": "mast_data_uri",
                        "file_role": "dv-timeseries",
                        "file_name": "target_dvt.fits",
                    },
                ),
                ProductRecord(
                    family="SPOC",
                    provider="spoc",
                    sector=1,
                    extra={
                        "fetch_reference": "mast:TESS/lc.fits",
                        "fetch_reference_kind": "mast_data_uri",
                        "file_role": "lightcurve",
                        "file_name": "target_lc.fits",
                    },
                ),
            ],
            "spoc",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_role"], "lightcurve")

    def test_choose_product_science_modes(self) -> None:
        products = [
            ProductRecord(family="TESSCut", provider="tesscut", sector=1),
            ProductRecord(family="SPOC", provider="spoc", sector=1, cadence_sec=120),
            ProductRecord(family="QLP", provider="qlp", sector=1, cadence_sec=1800),
        ]

        self.assertEqual(choose_product(products, best_for="transits")[0], "spoc")
        self.assertEqual(choose_product(products, best_for="rotation")[0], "cutout")
        self.assertEqual(choose_product(products, best_for="flares")[0], "spoc")

    def test_fastest_preextracted_product_ignores_cutouts(self) -> None:
        products = [
            ProductRecord(family="TESSCut", provider="tesscut", sector=1),
            ProductRecord(family="QLP", provider="qlp", sector=1, cadence_sec=1800),
            ProductRecord(family="SPOC", provider="spoc", sector=1, cadence_sec=120),
        ]

        self.assertEqual(fastest_preextracted_product(products), ("spoc", 120))


class CliTests(unittest.TestCase):
    def test_parse_providers_rejects_unknown(self) -> None:
        self.assertEqual(parse_providers("tesscut,mast,gaia,toi,tess-eb"), {"tesscut", "mast", "gaia", "toi", "tesseb"})
        with self.assertRaises(ValueError):
            parse_providers("tesscut,bad")

    def test_cli_writes_all_csv_outputs_for_offline_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            targets = tmp_path / "targets.csv"
            targets.write_text("tic_id,ra,dec\n123,102.7,-70.5\nbad,101,-69\n", encoding="utf-8")
            prefix = tmp_path / "inventory"

            with redirect_stdout(io.StringIO()):
                code = main([str(targets), "--offline", "--providers", "tesscut", "--best-for", "rotation", "--csv-out", str(prefix)])

            self.assertEqual(code, 0)
            for suffix in ("targets", "sectors", "products", "providers", "fetch_plans", "crowding_neighbors"):
                self.assertTrue((tmp_path / f"inventory_{suffix}.csv").exists(), suffix)

            with (tmp_path / "inventory_targets.csv").open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["kind"], "invalid")

    def test_cli_writes_json_for_offline_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "inventory.json"
            with redirect_stdout(io.StringIO()):
                code = main(["TIC", "123", "--offline", "--providers", "tesscut", "--json-out", str(out)])

            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], INVENTORY_SCHEMA_VERSION)
            self.assertEqual(payload["targets"][0]["target"]["tic_id"], "123")


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, code: int = 200) -> None:
        super().__init__(payload)
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False

    def getcode(self) -> int:
        return self.code


class FetchCliTests(unittest.TestCase):
    def test_parse_sector_filter(self) -> None:
        self.assertIsNone(parse_sector_filter("all"))
        self.assertEqual(parse_sector_filter("1, 2"), {1, 2})

    def test_build_fetch_manifest_filters_lightcurve_refs(self) -> None:
        document = sample_fetch_inventory()

        manifest = build_fetch_manifest(
            document,
            sector_filter={1},
            file_role="lightcurve",
            out_dir=Path("downloads"),
        )

        self.assertEqual(manifest["schema_version"], "tess-fetch.manifest.v0.1")
        self.assertEqual(manifest["n_files"], 1)
        self.assertEqual(manifest["files"][0]["file_name"], "target_lc.fits")
        self.assertEqual(manifest["files"][0]["cadence_sec"], 120)
        self.assertEqual(manifest["files"][0]["expected_size"], 4)
        self.assertEqual(manifest["files"][0]["status"], "planned")
        self.assertIn("261136679", manifest["files"][0]["destination"])

    def test_build_fetch_manifest_synthesizes_cutout_refs(self) -> None:
        manifest = build_fetch_manifest(
            sample_cutout_inventory(),
            product_filter="cutout",
            sector_filter={2},
            file_role="lightcurve",
            out_dir=Path("downloads"),
            cutout_size=7,
        )

        self.assertEqual(manifest["n_files"], 1)
        item = manifest["files"][0]
        self.assertEqual(item["product"], "cutout")
        self.assertEqual(item["sector"], 2)
        self.assertEqual(item["file_role"], "cutout")
        self.assertEqual(item["fetch_reference_kind"], "tesscut_astrocut")
        self.assertIn("x=7", item["access_url"])
        self.assertIn("sector=2", item["access_url"])

    def test_fetch_cli_dry_run_writes_manifest_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            inventory = tmp_path / "inventory.json"
            out_dir = tmp_path / "downloads"
            manifest_out = tmp_path / "fetch_manifest.json"
            inventory.write_text(json.dumps(sample_fetch_inventory()), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                code = fetch_main([str(inventory), "--out-dir", str(out_dir), "--dry-run", "--manifest-out", str(manifest_out)])

            self.assertEqual(code, 0)
            payload = json.loads(manifest_out.read_text(encoding="utf-8"))
            self.assertEqual(payload["n_files"], 1)
            self.assertFalse((out_dir / "261136679" / "target_lc.fits").exists())

    def test_fetch_cli_quality_preset_passes_resolved_mask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            inventory = tmp_path / "inventory.json"
            out_dir = tmp_path / "downloads"
            normalized = tmp_path / "normalized.csv"
            manifest_out = tmp_path / "fetch_manifest.json"
            inventory.write_text(json.dumps(sample_fetch_inventory()), encoding="utf-8")

            def fake_download(item, *, overwrite=False, resume=False, keep_cutout_zip=False):
                item["status"] = "exists_verified"

            with patch("tess_tools.fetch.download_reference", side_effect=fake_download), patch(
                "tess_tools.fetch.normalize_manifest_files"
            ) as normalize:
                with redirect_stdout(io.StringIO()):
                    code = fetch_main(
                        [
                            str(inventory),
                            "--out-dir",
                            str(out_dir),
                            "--normalize-csv",
                            str(normalized),
                            "--manifest-out",
                            str(manifest_out),
                            "--quality-preset",
                            "bit0",
                        ]
                    )

            self.assertEqual(code, 0)
            self.assertIsNone(normalize.call_args.kwargs["quality_mask"])
            self.assertEqual(normalize.call_args.kwargs["quality_preset"], "bit0")
            payload = json.loads(manifest_out.read_text(encoding="utf-8"))
            self.assertEqual(payload["outputs"]["normalize_csv"], str(normalized))
            self.assertEqual(payload["outputs"]["quality_mask"], 1)
            self.assertEqual(payload["outputs"]["quality_preset"], "bit0")

    def test_fetch_cli_rejects_quality_mask_and_preset_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Path(tmp) / "inventory.json"
            inventory.write_text(json.dumps(sample_fetch_inventory()), encoding="utf-8")

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                fetch_main([str(inventory), "--quality-mask", "1", "--quality-preset", "bit0"])

            self.assertEqual(raised.exception.code, 2)

    def test_fetch_cli_recommended_quality_preset_records_contextual_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            inventory = tmp_path / "inventory.json"
            out_dir = tmp_path / "downloads"
            normalized = tmp_path / "normalized.csv"
            manifest_out = tmp_path / "fetch_manifest.json"
            inventory.write_text(json.dumps(sample_fetch_inventory()), encoding="utf-8")

            def fake_download(item, *, overwrite=False, resume=False, keep_cutout_zip=False):
                item["status"] = "exists_verified"

            def fake_normalize(items, output_path, *, quality_mask=None, quality_preset=None):
                for item in items:
                    item.update(quality_policy_for_item(item, raw_mask=quality_mask, preset=quality_preset))
                    item["normalization_status"] = "ok"
                    item["normalization_input_rows"] = 1
                    item["normalization_rows"] = 1
                    item["normalization_quality_dropped_rows"] = 0
                output_path.write_text("time_btjd,flux,quality\n1,1,0\n", encoding="utf-8")

            with patch("tess_tools.fetch.download_reference", side_effect=fake_download), patch(
                "tess_tools.fetch.normalize_manifest_files", side_effect=fake_normalize
            ):
                with redirect_stdout(io.StringIO()):
                    code = fetch_main(
                        [
                            str(inventory),
                            "--out-dir",
                            str(out_dir),
                            "--normalize-csv",
                            str(normalized),
                            "--manifest-out",
                            str(manifest_out),
                            "--quality-preset",
                            "recommended",
                        ]
                    )

            self.assertEqual(code, 0)
            payload = json.loads(manifest_out.read_text(encoding="utf-8"))
            self.assertEqual(payload["outputs"]["quality_preset"], "recommended")
            self.assertNotIn("quality_mask", payload["outputs"])
            self.assertEqual(payload["files"][0]["quality_preset"], "recommended")
            self.assertEqual(payload["files"][0]["quality_policy"], "spoc-recommended")
            self.assertEqual(payload["files"][0]["quality_mask"], 21183)

    def test_download_reference_verifies_existing_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.fits"
            path.write_bytes(b"data")
            item = {"access_url": "https://example.test/file.fits", "destination": str(path), "expected_size": 4}

            download_reference(item)

            self.assertEqual(item["status"], "exists_verified")
            self.assertEqual(item["local_size"], 4)
            self.assertEqual(item["local_sha256"], hashlib.sha256(b"data").hexdigest())

    def test_download_reference_reports_existing_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.fits"
            path.write_bytes(b"bad")
            item = {"access_url": "https://example.test/file.fits", "destination": str(path), "expected_size": 4}

            with self.assertRaises(RuntimeError):
                download_reference(item)

    def test_download_reference_reports_sha256_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.fits"
            path.write_bytes(b"data")
            item = {
                "access_url": "https://example.test/file.fits",
                "destination": str(path),
                "expected_size": 4,
                "expected_sha256": hashlib.sha256(b"other").hexdigest(),
            }

            with self.assertRaises(RuntimeError):
                download_reference(item)

    def test_download_reference_resumes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.fits"
            path.write_bytes(b"ab")
            item = {"access_url": "https://example.test/file.fits", "destination": str(path), "expected_size": 4}
            seen_ranges: list[str | None] = []

            def fake_urlopen(request, timeout=60):
                seen_ranges.append(request.headers.get("Range"))
                return FakeResponse(b"cd", code=206)

            with patch("tess_tools.fetch.urlopen", side_effect=fake_urlopen):
                download_reference(item, resume=True)

            self.assertEqual(seen_ranges, ["bytes=2-"])
            self.assertEqual(path.read_bytes(), b"abcd")
            self.assertEqual(item["status"], "resumed")
            self.assertEqual(item["local_size"], 4)
            self.assertEqual(item["local_sha256"], hashlib.sha256(b"abcd").hexdigest())

    def test_download_reference_extracts_tesscut_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive_payload = io.BytesIO()
            with zipfile.ZipFile(archive_payload, "w") as archive:
                archive.writestr("cutout.fits", b"fits-data")
            item = {
                "access_url": "https://mast.stsci.edu/tesscut/api/v0.1/astrocut?ra=1&dec=2&x=5&y=5&units=px&sector=1",
                "destination": str(tmp_path / "tesscut-s0001-5x5.zip"),
                "fetch_reference_kind": "tesscut_astrocut",
                "file_name": "tesscut-s0001-5x5.zip",
            }

            with patch("tess_tools.fetch.urlopen", return_value=FakeResponse(archive_payload.getvalue())):
                download_reference(item)

            self.assertEqual(item["status"], "downloaded")
            self.assertEqual(item["n_local_files"], 1)
            self.assertTrue(Path(item["local_paths"][0]).exists())
            self.assertEqual(item["local_hashes"][0]["sha256"], hashlib.sha256(b"fits-data").hexdigest())
            self.assertFalse((tmp_path / "tesscut-s0001-5x5.zip").exists())

    def test_download_reference_rejects_unsafe_tesscut_zip_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive_payload = io.BytesIO()
            with zipfile.ZipFile(archive_payload, "w") as archive:
                archive.writestr("../bad.fits", b"bad")
            item = {
                "access_url": "https://mast.stsci.edu/tesscut/api/v0.1/astrocut?ra=1&dec=2&x=5&y=5&units=px&sector=1",
                "destination": str(tmp_path / "tesscut-s0001-5x5.zip"),
                "fetch_reference_kind": "tesscut_astrocut",
                "file_name": "tesscut-s0001-5x5.zip",
            }

            with patch("tess_tools.fetch.urlopen", return_value=FakeResponse(archive_payload.getvalue())):
                with self.assertRaises(RuntimeError):
                    download_reference(item)

    def test_cutout_aperture_helpers(self) -> None:
        self.assertEqual(circular_aperture_pixels(3, 3, 0.1), [(1, 1)])
        self.assertEqual(len(circular_aperture_pixels(5, 5, 1.5)), 9)
        self.assertEqual(median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(median([4.0, 1.0]), 2.5)

    def test_cutout_pixel_mask_parser(self) -> None:
        self.assertEqual(parse_pixel_mask("1,1; 1,2;1,1", width=3, height=3), [(1, 1), (1, 2)])
        with self.assertRaises(RuntimeError):
            parse_pixel_mask("3,1", width=3, height=3)
        with self.assertRaises(RuntimeError):
            parse_pixel_mask("", width=3, height=3)

    def test_cutout_threshold_aperture_uses_median_image(self) -> None:
        cube = [
            [[1.0, 1.0, 1.0], [1.0, 10.0, 1.0], [1.0, 1.0, 1.0]],
            [[1.0, 1.0, 1.0], [1.0, 12.0, 1.0], [1.0, 1.0, 1.0]],
        ]

        self.assertEqual(threshold_aperture_pixels(cube, width=3, height=3, sigma=1.0), [(1, 1)])
        self.assertEqual(median_flux_image(cube, width=3, height=3)[1][1], 11.0)

    def test_aperture_diagnostics_write_summary_and_mask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            summary_path = tmp_path / "summary.json"
            mask_path = tmp_path / "mask.csv"
            report_path = tmp_path / "report.html"
            diagnostic = build_aperture_diagnostic(
                tmp_path / "cutout.fits",
                {"target_label": "target", "sector": 2, "product": "cutout"},
                width=3,
                height=2,
                aperture_pixels=[(0, 1), (1, 1)],
                background_pixels=[(0, 0), (0, 2), (1, 0), (1, 2)],
                aperture_mode="pixels",
                aperture_radius=1.5,
                aperture_pixel_spec="0,1;1,1",
                threshold_sigma=3.0,
                background="median-outside",
                median_image=[[1.0, 2.0, 3.0], [None, 4.0, 5.0]],
            )

            write_aperture_diagnostics([diagnostic], summary_json=summary_path, mask_csv=mask_path, report_html=report_path)

            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], "tess-fetch.aperture-summary.v0.1")
            self.assertEqual(summary["apertures"][0]["aperture_n_pixels"], 2)
            self.assertEqual(summary["apertures"][0]["aperture_bounds"]["x_min"], 1)
            self.assertEqual(summary["apertures"][0]["median_flux_image"][0][1], 2.0)
            with mask_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 6)
            self.assertEqual(sum(row["in_aperture"] == "True" for row in rows), 2)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("TESSCut Aperture Report", report)
            self.assertIn("pixel selected", report)
            self.assertIn("Pixel color", report)
            self.assertIn("--heat: rgb", report)

    def test_extract_cutout_manifest_files_writes_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cutout_path = tmp_path / "cutout.fits"
            cutout_path.write_bytes(b"fits")
            output_path = tmp_path / "cutout_lc.csv"
            summary_path = tmp_path / "aperture_summary.json"
            mask_path = tmp_path / "aperture_mask.csv"
            report_path = tmp_path / "aperture_report.html"
            items = [
                {
                    "product": "cutout",
                    "sector": 2,
                    "file_role": "cutout",
                    "status": "downloaded",
                    "local_paths": [str(cutout_path)],
                }
            ]

            with patch(
                "tess_tools.cutout.extract_cutout_lightcurve_file",
                return_value=[
                    {
                        "tic_id": None,
                        "sector": 2,
                        "cadence_sec": None,
                        "product": "cutout",
                        "time_btjd": 1.0,
                        "flux": 42.0,
                        "flux_err": None,
                        "quality": 0,
                        "source_file": str(cutout_path),
                        "aperture_n_pixels": 9,
                    }
                ],
            ) as extract:
                extract_cutout_manifest_files(
                    items,
                    output_path,
                    aperture_mode="pixels",
                    aperture_radius=1.5,
                    aperture_pixels="1,1;1,2",
                    threshold_sigma=2.0,
                    background="median-outside",
                    quality_mask=1,
                    aperture_summary_json=summary_path,
                    aperture_mask_csv=mask_path,
                    aperture_report_html=report_path,
                )

            self.assertEqual(items[0]["cutout_lightcurve_status"], "ok")
            self.assertEqual(items[0]["cutout_lightcurve_rows"], 1)
            self.assertEqual(items[0]["cutout_lightcurve_input_rows"], 1)
            self.assertEqual(items[0]["cutout_lightcurve_quality_dropped_rows"], 0)
            self.assertEqual(items[0]["cutout_lightcurve_empty_aperture_rows"], 0)
            self.assertEqual(items[0]["quality_mask"], 1)
            extract.assert_called_once()
            with output_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["flux"], "42.0")
            self.assertEqual(rows[0]["aperture_n_pixels"], "9")
            self.assertTrue(summary_path.exists())
            self.assertTrue(mask_path.exists())
            self.assertTrue(report_path.exists())

    def test_normalize_lightcurve_rows_maps_common_columns(self) -> None:
        rows = normalize_lightcurve_rows(
            [
                {
                    "TIME": 1.0,
                    "PDCSAP_FLUX": 10.0,
                    "PDCSAP_FLUX_ERR": 0.1,
                    "SAP_FLUX": 11.0,
                    "MOM_CENTR1": 512.0,
                    "QUALITY": 0,
                }
            ],
            columns=["TIME", "PDCSAP_FLUX", "PDCSAP_FLUX_ERR", "SAP_FLUX", "MOM_CENTR1", "QUALITY"],
            metadata={"tic_id": "123", "sector": 1, "cadence_sec": 120, "product": "spoc"},
            source_file="file.fits",
        )

        self.assertEqual(rows[0]["tic_id"], "123")
        self.assertEqual(rows[0]["time_btjd"], 1.0)
        self.assertEqual(rows[0]["flux"], 10.0)
        self.assertEqual(rows[0]["flux_err"], 0.1)
        self.assertEqual(rows[0]["raw_pdcsap_flux"], 10.0)
        self.assertEqual(rows[0]["raw_sap_flux"], 11.0)
        self.assertEqual(rows[0]["raw_mom_centr1"], 512.0)

    def test_quality_mask_drops_flagged_rows(self) -> None:
        stats: dict[str, int] = {}
        rows = normalize_lightcurve_rows(
            [
                {"TIME": 1.0, "PDCSAP_FLUX": 10.0, "QUALITY": 0},
                {"TIME": 2.0, "PDCSAP_FLUX": 20.0, "QUALITY": 1},
                {"TIME": 3.0, "PDCSAP_FLUX": 30.0, "QUALITY": 2},
            ],
            columns=["TIME", "PDCSAP_FLUX", "QUALITY"],
            metadata={"tic_id": "123", "sector": 1, "cadence_sec": 120, "product": "spoc"},
            source_file="file.fits",
            quality_mask=1,
            quality_filter_stats=stats,
        )

        self.assertEqual([row["time_btjd"] for row in rows], [1.0, 3.0])
        self.assertEqual(stats["input_rows"], 3)
        self.assertEqual(stats["quality_dropped_rows"], 1)
        self.assertEqual(stats["output_rows"], 2)
        self.assertFalse(should_skip_quality(1, 0))
        self.assertTrue(should_skip_quality(3, 1))

    def test_resolve_quality_mask_presets(self) -> None:
        self.assertIsNone(resolve_quality_mask(None, None))
        self.assertIsNone(resolve_quality_mask(None, "none"))
        self.assertEqual(resolve_quality_mask(None, "bit0"), 1)
        self.assertEqual(resolve_quality_mask(None, "conservative"), 65535)
        self.assertEqual(resolve_quality_mask(None, "spoc-recommended"), 21183)
        self.assertEqual(resolve_quality_mask(None, "tess-spoc-recommended"), 21183)
        self.assertEqual(resolve_quality_mask(None, "qlp-recommended"), 7357)
        self.assertEqual(resolve_quality_mask(None, "recommended", product="spoc"), 21183)
        self.assertEqual(resolve_quality_mask(None, "recommended", product="tess-spoc"), 21183)
        self.assertEqual(resolve_quality_mask(None, "recommended", product="qlp"), 7357)
        self.assertEqual(resolve_quality_mask(None, "recommended", product="cutout"), 21183)
        self.assertEqual(resolve_quality_mask(7, None), 7)
        self.assertEqual(resolve_quality_mask(7, "none"), 7)
        with self.assertRaises(ValueError):
            resolve_quality_mask(1, "bit0")
        with self.assertRaises(ValueError):
            resolve_quality_mask(None, "recommended")
        with self.assertRaises(ValueError):
            resolve_quality_mask(None, "unknown")

    def test_quality_policy_for_item_records_contextual_recommended_policy(self) -> None:
        spoc = quality_policy_for_item({"product": "spoc"}, preset="recommended")
        qlp = quality_policy_for_item({"product": "qlp"}, preset="recommended")

        self.assertEqual(spoc, {"quality_preset": "recommended", "quality_policy": "spoc-recommended", "quality_mask": 21183})
        self.assertEqual(qlp, {"quality_preset": "recommended", "quality_policy": "qlp-recommended", "quality_mask": 7357})

    def test_write_normalized_csv_includes_dynamic_raw_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "normalized.csv"
            write_normalized_csv(
                path,
                [
                    {
                        "tic_id": "123",
                        "sector": 1,
                        "cadence_sec": 120,
                        "product": "spoc",
                        "time_btjd": 1.0,
                        "flux": 10.0,
                        "flux_err": 0.1,
                        "quality": 0,
                        "source_file": "file.fits",
                        "raw_sap_flux": 11.0,
                    }
                ],
            )

            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertIn("raw_sap_flux", rows[0])
            self.assertEqual(rows[0]["raw_sap_flux"], "11.0")

    def test_normalize_manifest_files_writes_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fits_path = tmp_path / "file.fits"
            fits_path.write_bytes(b"fits")
            output_path = tmp_path / "normalized.csv"
            items = [
                {
                    "destination": str(fits_path),
                    "file_role": "lightcurve",
                    "status": "exists_verified",
                    "tic_id": "123",
                    "sector": 1,
                    "cadence_sec": 120,
                    "product": "spoc",
                }
            ]

            with patch(
                "tess_tools.fetch.normalize_lightcurve_file",
                return_value=[
                    {
                        "tic_id": "123",
                        "sector": 1,
                        "cadence_sec": 120,
                        "product": "spoc",
                        "time_btjd": 1.0,
                        "flux": 10.0,
                        "flux_err": 0.1,
                        "quality": 0,
                        "source_file": str(fits_path),
                    }
                ],
            ):
                normalize_manifest_files(items, output_path)

            self.assertEqual(items[0]["normalization_status"], "ok")
            self.assertEqual(items[0]["normalization_input_rows"], 1)
            self.assertEqual(items[0]["normalization_rows"], 1)
            self.assertEqual(items[0]["normalization_quality_dropped_rows"], 0)
            with output_path.open("r", encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(csv_rows[0]["tic_id"], "123")

    def test_normalize_manifest_files_passes_quality_mask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fits_path = tmp_path / "file.fits"
            fits_path.write_bytes(b"fits")
            output_path = tmp_path / "normalized.csv"
            items = [
                {
                    "destination": str(fits_path),
                    "file_role": "lightcurve",
                    "status": "exists_verified",
                    "tic_id": "123",
                    "sector": 1,
                    "cadence_sec": 120,
                    "product": "spoc",
                }
            ]

            with patch("tess_tools.fetch.normalize_lightcurve_file", return_value=[]) as normalize:
                normalize_manifest_files(items, output_path, quality_mask=1)

            self.assertEqual(items[0]["quality_mask"], 1)
            self.assertEqual(items[0]["normalization_input_rows"], 0)
            self.assertEqual(items[0]["normalization_rows"], 0)
            self.assertEqual(items[0]["normalization_quality_dropped_rows"], 0)
            normalize.assert_called_once()

    def test_normalize_manifest_files_resolves_recommended_quality_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fits_path = tmp_path / "qlp.fits"
            fits_path.write_bytes(b"fits")
            output_path = tmp_path / "normalized.csv"
            items = [
                {
                    "destination": str(fits_path),
                    "file_role": "lightcurve",
                    "status": "exists_verified",
                    "tic_id": "123",
                    "sector": 26,
                    "cadence_sec": 1800,
                    "product": "qlp",
                }
            ]

            with patch("tess_tools.fetch.normalize_lightcurve_file", return_value=[]) as normalize:
                normalize_manifest_files(items, output_path, quality_preset="recommended")

            self.assertEqual(items[0]["quality_preset"], "recommended")
            self.assertEqual(items[0]["quality_policy"], "qlp-recommended")
            self.assertEqual(items[0]["quality_mask"], 7357)
            normalize.assert_called_once()


def sample_fetch_inventory() -> dict:
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "targets": [
            {
                "input": "TIC 261136679",
                "target": {"kind": "tic", "tic_id": "261136679", "ra_deg": 1.0, "dec_deg": 2.0},
                "fetch_plan": {
                    "schema_version": "tess-where.fetch-plan.v0.4",
                    "target_label": "TIC 261136679",
                    "product": "spoc",
                    "selected_product_references": [
                        {
                            "sector": 1,
                            "cadence_sec": 120,
                            "file_role": "lightcurve",
                            "file_name": "target_lc.fits",
                            "file_product_subgroup": "LC",
                            "file_size": 4,
                            "fetch_reference_kind": "mast_data_uri",
                            "fetch_reference": "mast:TESS/product/target_lc.fits",
                            "access_url": "https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:TESS%2Fproduct%2Ftarget_lc.fits",
                        },
                        {
                            "sector": 1,
                            "file_role": "dv-timeseries",
                            "file_name": "target_dvt.fits",
                            "file_product_subgroup": "DVT",
                            "fetch_reference_kind": "mast_data_uri",
                            "fetch_reference": "mast:TESS/product/target_dvt.fits",
                            "access_url": "https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:TESS%2Fproduct%2Ftarget_dvt.fits",
                        },
                    ],
                },
                "errors": [],
            }
        ],
    }


def sample_cutout_inventory() -> dict:
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "targets": [
            {
                "input": "102.7 -70.5",
                "target": {"kind": "coords", "tic_id": None, "ra_deg": 102.7, "dec_deg": -70.5},
                "fetch_plan": {
                    "schema_version": "tess-where.fetch-plan.v0.4",
                    "target_label": "102.700000 -70.500000",
                    "product": "cutout",
                    "sectors": [2, 3],
                    "sector_argument": "all",
                    "n_selected_product_references": 0,
                    "selected_product_references": [],
                },
                "errors": [],
            }
        ],
    }


class LiveValidateTests(unittest.TestCase):
    def test_target_tokens_from_entry_accepts_string_or_list(self) -> None:
        self.assertEqual(target_tokens_from_entry({"target": "TIC 123"}), ["TIC", "123"])
        self.assertEqual(target_tokens_from_entry({"target": ["102.7", "-70.5"]}), ["102.7", "-70.5"])

    def test_evaluate_inventory_passes_expected_inventory(self) -> None:
        inventory = {
            "input": "TIC 123",
            "target": {"tic_id": "123", "source": "mast.catalogs.filtered.tic.http"},
            "sector_summary": {"n_sectors": 25},
            "product_summary": {
                "families": ["TESSCut"],
                "n_products_with_file_uri": 1,
                "fetch_reference_kinds": ["mast_data_uri"],
            },
            "fetch_plan": {"product": "cutout"},
            "providers": [{"name": "TESSCut", "status": "ok"}],
            "errors": [],
        }
        expect = {
            "tic_id": "123",
            "target_source_contains": "mast.catalogs",
            "min_sectors": 20,
            "product_families_contains": ["TESSCut"],
            "product_summary_min": {"n_products_with_file_uri": 1},
            "product_summary_contains": {"fetch_reference_kinds": ["mast_data_uri"]},
            "fetch_product": "cutout",
            "provider_status": {"TESSCut": "ok"},
            "no_errors": True,
        }

        self.assertEqual(evaluate_inventory(inventory, expect), [])
        self.assertIn("sectors=25", summarize_inventory(inventory))

    def test_evaluate_inventory_checks_fetch_plan_selected_references(self) -> None:
        inventory = {
            "input": "TIC 123",
            "target": {"tic_id": "123", "source": "mast.catalogs.filtered.tic.http"},
            "sector_summary": {"n_sectors": 25},
            "product_summary": {"families": ["SPOC"]},
            "fetch_plan": {
                "product": "spoc",
                "n_selected_product_references": 1,
                "selected_product_references": [
                    {"file_role": "lightcurve", "fetch_reference_kind": "mast_data_uri"}
                ],
            },
            "providers": [{"name": "MAST observations", "status": "ok"}],
            "errors": [],
        }
        expect = {
            "fetch_product": "spoc",
            "fetch_plan_min": {"n_selected_product_references": 1},
            "selected_references_contains": {
                "file_role": ["lightcurve"],
                "fetch_reference_kind": ["mast_data_uri"],
            },
        }

        self.assertEqual(evaluate_inventory(inventory, expect), [])

    def test_evaluate_inventory_checks_crowding_summary(self) -> None:
        inventory = {
            "input": "TIC 123",
            "target": {"tic_id": "123", "source": "mast.catalogs.filtered.tic.http"},
            "sector_summary": {"n_sectors": 2},
            "product_summary": {"families": ["TESSCut"]},
            "crowding_summary": {
                "risk": "medium",
                "n_neighbors": 4,
                "n_neighbors_within_1_pixel": 1,
            },
            "fetch_plan": {"product": "cutout"},
            "providers": [{"name": "Gaia neighbors", "status": "ok"}],
            "errors": [],
        }
        expect = {
            "crowding_summary_min": {"n_neighbors": 1},
            "crowding_summary_contains": {"risk": ["low", "medium", "high"]},
            "provider_status": {"Gaia neighbors": "ok"},
        }

        self.assertEqual(evaluate_inventory(inventory, expect), [])

    def test_evaluate_inventory_checks_known_object_summary(self) -> None:
        inventory = {
            "input": "TIC 150428135",
            "target": {"tic_id": "150428135"},
            "sector_summary": {"n_sectors": 5},
            "product_summary": {},
            "known_object_summary": {
                "n_matches": 3,
                "catalogs": ["TOI", "TESS EB"],
                "toi": {"n_matches": 2, "dispositions": ["KP", "PC"]},
                "tess_eb": {"n_matches": 1},
            },
            "fetch_plan": {"product": "cutout"},
            "providers": [{"name": "TOI catalog", "status": "ok"}],
            "errors": [],
        }
        expect = {
            "known_object_summary_min": {"n_matches": 1},
            "known_object_summary_contains": {"catalogs": ["TOI", "TESS EB"]},
            "toi_summary_min": {"n_matches": 1},
            "toi_summary_contains": {"dispositions": ["KP"]},
            "tess_eb_summary_min": {"n_matches": 1},
            "provider_status": {"TOI catalog": "ok"},
        }

        self.assertEqual(evaluate_inventory(inventory, expect), [])

    def test_evaluate_inventory_reports_failures(self) -> None:
        inventory = {
            "input": "TIC 123",
            "target": {"tic_id": "456", "source": "fallback"},
            "sector_summary": {"n_sectors": 1},
            "product_summary": {"families": ["SPOC"], "n_products_with_file_uri": 0, "fetch_reference_kinds": []},
            "fetch_plan": {"product": "spoc"},
            "providers": [{"name": "TESSCut", "status": "empty"}],
            "errors": ["warning"],
        }
        expect = {
            "tic_id": "123",
            "target_source_contains": "mast",
            "min_sectors": 20,
            "product_families_contains": ["TESSCut"],
            "product_summary_min": {"n_products_with_file_uri": 1},
            "product_summary_contains": {"fetch_reference_kinds": ["mast_data_uri"]},
            "fetch_product": "cutout",
            "provider_status": {"TESSCut": "ok"},
            "no_errors": True,
        }

        failures = evaluate_inventory(inventory, expect)

        self.assertGreaterEqual(len(failures), 5)

    def test_infrastructure_error_detection_uses_inventory_errors(self) -> None:
        inventory = {
            "errors": [
                "TESSCut lookup failed: HTTPSConnectionPool(host='mast.stsci.edu'): "
                "Max retries exceeded with url: /api/v0.1/Download/file "
                "(Caused by NameResolutionError: getaddrinfo failed)"
            ]
        }

        self.assertTrue(is_infrastructure_error(inventory))
        self.assertFalse(is_infrastructure_error({"errors": ["expected TIC 123, found 456"]}))
        self.assertFalse(
            is_infrastructure_error(
                {
                    "errors": [
                        "MAST observation query failed after retries: "
                        "CAOM filtered failed: Invalid column name 'dataURI'. | "
                        "CAOM cone failed: The read operation timed out"
                    ]
                }
            )
        )

    def test_fetch_smoke_is_skipped_unless_explicitly_enabled(self) -> None:
        result = run_fetch_smoke(
            sample_fetch_inventory()["targets"][0],
            {"product": "spoc"},
            name="sample",
            include=False,
            out_dir=Path("unused"),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["failures"], [])

    def test_fetch_smoke_downloads_and_checks_normalized_csv_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            def fake_download(item, *, overwrite=False, resume=False):
                item["status"] = "exists_verified"

            def fake_normalize(items, output_path, *, quality_mask=None):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    "time_btjd,flux,quality,raw_sap_flux\n"
                    "1.0,10.0,0,11.0\n",
                    encoding="utf-8",
                )

            with patch("tess_tools.live_validate.download_reference", side_effect=fake_download):
                with patch("tess_tools.live_validate.normalize_manifest_files", side_effect=fake_normalize):
                    result = run_fetch_smoke(
                        sample_fetch_inventory()["targets"][0],
                        {
                            "product": "spoc",
                            "file_role": "lightcurve",
                            "max_files": 1,
                            "expect_min_rows": 1,
                            "expect_columns": ["time_btjd", "quality", "raw_sap_flux"],
                        },
                        name="sample",
                        include=True,
                        out_dir=tmp_path,
                    )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["n_files"], 1)
        self.assertEqual(result["row_count"], 1)

    def test_fetch_smoke_checks_cutout_lightcurve_csv_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            def fake_download(item, *, overwrite=False, resume=False):
                item["status"] = "downloaded"
                item["local_paths"] = [str(tmp_path / "cutout.fits")]

            def fake_extract(
                items,
                output_path,
                *,
                aperture_mode="circle",
                aperture_radius=1.5,
                aperture_pixels=None,
                threshold_sigma=3.0,
                background="none",
                quality_mask=None,
            ):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    "time_btjd,flux,quality,raw_aperture_flux,aperture_n_pixels\n"
                    "1.0,10.0,0,90.0,9\n",
                    encoding="utf-8",
                )
                for item in items:
                    item["cutout_lightcurve_status"] = "ok"
                    item["cutout_lightcurve_rows"] = 1

            with patch("tess_tools.live_validate.download_reference", side_effect=fake_download):
                with patch("tess_tools.live_validate.extract_cutout_manifest_files", side_effect=fake_extract):
                    result = run_fetch_smoke(
                        sample_cutout_inventory()["targets"][0],
                        {
                            "product": "cutout",
                            "file_role": "cutout",
                            "sectors": [2],
                            "max_files": 1,
                            "normalize_csv": False,
                            "cutout_lightcurve_csv": True,
                            "expect_min_rows": 1,
                            "expect_columns": ["time_btjd", "raw_aperture_flux", "aperture_n_pixels"],
                        },
                        name="cutout",
                        include=True,
                        out_dir=tmp_path,
                    )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["n_files"], 1)
        self.assertEqual(result["row_count"], 1)

    def test_validation_status_distinguishes_infrastructure_failures(self) -> None:
        self.assertEqual(validation_status({"passed": True, "infrastructure_error": False}), "PASS")
        self.assertEqual(validation_status({"passed": False, "infrastructure_error": True}), "INFRA")
        self.assertEqual(validation_status({"passed": False, "infrastructure_error": False}), "FAIL")


if __name__ == "__main__":
    unittest.main()
