"""Unit tests for the price-index chain math in compute_index.py."""

import json
import math
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import compute_index as ci


class TestCanon(unittest.TestCase):
    def test_canon_type(self):
        self.assertEqual(ci.canon_type("MS-DRG"), "MSDRG")
        self.assertEqual(ci.canon_type("ms drg"), "MSDRG")
        self.assertEqual(ci.canon_type(None), "")

    def test_canon_code_leading_zeros(self):
        self.assertEqual(ci.canon_code("003"), "3")
        self.assertEqual(ci.canon_code(" 0470 "), "470")
        self.assertEqual(ci.canon_code("000"), "0")

    def test_to_float_positive_only(self):
        # The index rejects zero/negative prices; the summarizer keeps them.
        self.assertIsNone(ci.to_float("0"))
        self.assertIsNone(ci.to_float("-5"))
        self.assertIsNone(ci.to_float("n/a"))
        self.assertEqual(ci.to_float("12.5"), 12.5)

    def test_median(self):
        self.assertEqual(ci.median([3, 1, 2]), 2)
        self.assertEqual(ci.median([4, 1, 2, 3]), 2.5)

    def test_geomean(self):
        self.assertAlmostEqual(ci.geomean([2, 8]), 4.0)
        self.assertIsNone(ci.geomean([]))


class TestBasketPrices(unittest.TestCase):
    """basket_prices reads data/<slug>/summary.csv under compute_index.ROOT."""

    ITEMS = {"DRG|470": ("DRG", "470"), "DRG|003": ("DRG", "3"),
             "CPT|70450": ("CPT", "70450")}

    def setUp(self):
        self._root = ci.ROOT
        self._tmp = tempfile.TemporaryDirectory()
        ci.ROOT = Path(self._tmp.name)

    def tearDown(self):
        ci.ROOT = self._root
        self._tmp.cleanup()

    def write_summary(self, slug, rows):
        d = ci.ROOT / "data" / slug
        d.mkdir(parents=True)
        header = ("code_type,code,description,gross_charge,discounted_cash,"
                  "min_negotiated,max_negotiated,payer_entries\n")
        (d / "summary.csv").write_text(
            header + "".join(",".join(map(str, r)) + "\n" for r in rows))

    def test_matching_and_medians(self):
        self.write_summary("h1", [
            ("MS-DRG", "470", "Joint A", 100, 50, "", "", 0),
            ("DRG", "0470", "Joint B", 300, 70, "", "", 0),    # DRG label + zeros
            ("MS-DRG", "003", "ECMO", 900, 800, "", "", 0),    # basket code 3
            ("HCPCS", "70450", "CT head", 200, 90, "", "", 0), # HCPCS matches CPT item
            ("CPT", "70450", "CT head dup", 400, 0, "", "", 0),  # 0 cash filtered
            ("LOCAL", "470", "chargemaster row", 999, 999, "", "", 0),  # wrong type
        ])
        prices = ci.basket_prices("h1", self.ITEMS)
        self.assertEqual(prices["DRG|470"], {"cash": 60, "gross": 200})  # medians
        self.assertEqual(prices["DRG|003"], {"cash": 800, "gross": 900})
        self.assertEqual(prices["CPT|70450"], {"cash": 90, "gross": 300})

    def test_missing_summary(self):
        self.assertEqual(ci.basket_prices("nope", self.ITEMS), {})


class TestChainMath(unittest.TestCase):
    def test_single_hospital_relatives(self):
        prev = {"h1": {"a": {"cash": 100}, "b": {"cash": 200}}}
        cur = {"h1": {"a": {"cash": 110}, "b": {"cash": 180}}}
        factor, pairs, hospitals, anomalies = ci.series_factor(prev, cur, "cash")
        self.assertAlmostEqual(factor, math.sqrt(1.1 * 0.9))
        self.assertEqual((pairs, hospitals, anomalies), (2, 1, []))

    def test_hospitals_weighted_equally(self):
        # geomean over hospitals of geomean over codes: h2's single 2x code
        # counts as much as h1's two flat codes.
        prev = {"h1": {"a": {"cash": 100}, "b": {"cash": 100}},
                "h2": {"c": {"cash": 50}}}
        cur = {"h1": {"a": {"cash": 100}, "b": {"cash": 100}},
               "h2": {"c": {"cash": 100}}}
        factor, pairs, hospitals, _ = ci.series_factor(prev, cur, "cash")
        self.assertAlmostEqual(factor, math.sqrt(2.0))
        self.assertEqual((pairs, hospitals), (3, 2))

    def test_entry_and_exit_ignored(self):
        # A hospital or code present on only one side contributes nothing:
        # chain-linking lets the panel change without breaking the series.
        prev = {"h1": {"a": {"cash": 100}}, "gone": {"a": {"cash": 5}}}
        cur = {"h1": {"a": {"cash": 100}, "new_code": {"cash": 7}},
               "new_hospital": {"a": {"cash": 9}}}
        factor, pairs, hospitals, _ = ci.series_factor(prev, cur, "cash")
        self.assertEqual((factor, pairs, hospitals), (1.0, 1, 1))

    def test_empty_overlap(self):
        factor, pairs, hospitals, _ = ci.series_factor({}, {"h": {"a": {"cash": 1}}}, "cash")
        self.assertEqual((factor, pairs, hospitals), (1.0, 0, 0))

    def test_missing_field_skipped(self):
        prev = {"h1": {"a": {"gross": 100}}}
        cur = {"h1": {"a": {"gross": 100, "cash": 50}}}
        self.assertEqual(ci.series_factor(prev, cur, "cash")[1:3], (0, 0))
        self.assertEqual(ci.series_factor(prev, cur, "gross")[1:3], (1, 1))

    def test_extreme_relative_excluded_and_logged(self):
        # A one-day 5x move is a suspected artifact: it must not compound
        # into the chain, and it must be surfaced as an anomaly.
        prev = {"h1": {"a": {"cash": 100}, "b": {"cash": 100}}}
        cur = {"h1": {"a": {"cash": 500}, "b": {"cash": 110}}}
        factor, pairs, hospitals, anomalies = ci.series_factor(prev, cur, "cash")
        self.assertAlmostEqual(factor, 1.1)
        self.assertEqual((pairs, hospitals), (1, 1))
        self.assertEqual(anomalies, [("h1", "a", 100, 500)])

    def test_extreme_drop_excluded_symmetrically(self):
        prev = {"h1": {"a": {"cash": 100}}}
        cur = {"h1": {"a": {"cash": 10}}}
        factor, pairs, hospitals, anomalies = ci.series_factor(prev, cur, "cash")
        self.assertEqual((factor, pairs, hospitals), (1.0, 0, 0))
        self.assertEqual(anomalies, [("h1", "a", 100, 10)])

    def test_limit_boundary_included(self):
        # Exactly RELATIVE_LIMIT is still a (barely) admissible move.
        prev = {"h1": {"a": {"cash": 100}}}
        cur = {"h1": {"a": {"cash": 100 * ci.RELATIVE_LIMIT}}}
        factor, pairs, hospitals, anomalies = ci.series_factor(prev, cur, "cash")
        self.assertAlmostEqual(factor, ci.RELATIVE_LIMIT)
        self.assertEqual((pairs, anomalies), (1, []))

    def test_all_anomalous_hospital_contributes_nothing(self):
        # A wholesale corrupt file (every item 10x) must leave the index flat.
        prev = {"h1": {k: {"cash": 100} for k in "abc"},
                "h2": {"z": {"cash": 100}}}
        cur = {"h1": {k: {"cash": 1000} for k in "abc"},
               "h2": {"z": {"cash": 105}}}
        factor, pairs, hospitals, anomalies = ci.series_factor(prev, cur, "cash")
        self.assertAlmostEqual(factor, 1.05)
        self.assertEqual((pairs, hospitals, len(anomalies)), (1, 1, 3))


class TestMethodChange(unittest.TestCase):
    """A summarizer version bump must not compound into the chain."""

    PREV = {"h1": {"a": {"cash": 100}}, "h2": {"a": {"cash": 100}}}
    CUR = {"h1": {"a": {"cash": 150}}, "h2": {"a": {"cash": 100}},
           "new": {"a": {"cash": 7}}}

    def test_changed_version_dropped_for_the_day(self):
        kept, dropped = ci.drop_method_changes(
            self.PREV, self.CUR, {"h1": 1, "h2": 1}, {"h1": 2, "h2": 1, "new": 2})
        self.assertEqual(dropped, ["h1"])
        self.assertEqual(set(kept), {"h2", "new"})  # newcomers chain from tomorrow
        factor, pairs, hospitals, _ = ci.series_factor(self.PREV, kept, "cash")
        self.assertEqual((factor, pairs, hospitals), (1.0, 1, 1))

    def test_unstamped_summaries_are_version_one(self):
        # State written before versions were recorded, meta.json without a
        # stamp: both mean version 1, so nothing is dropped spuriously.
        kept, dropped = ci.drop_method_changes(self.PREV, self.CUR, {}, {"h1": 1})
        self.assertEqual(dropped, [])
        self.assertEqual(kept, self.CUR)
        kept, dropped = ci.drop_method_changes(self.PREV, self.CUR, {}, {"h1": 2})
        self.assertEqual(dropped, ["h1"])

    def test_summary_version_reads_meta(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(ci, "ROOT", Path(tmp)):
            d = Path(tmp) / "data" / "h1"
            d.mkdir(parents=True)
            self.assertEqual(ci.summary_version("h1"), 1)
            (d / "meta.json").write_text(json.dumps({"summary_version": 3}))
            self.assertEqual(ci.summary_version("h1"), 3)


if __name__ == "__main__":
    unittest.main()
