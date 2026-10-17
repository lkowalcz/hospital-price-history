import csv
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import data_catalog as catalog
import gen_site
from examples.download_summary import choose_snapshot, download_verified


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "site"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.org")
        self.hospitals = [{"slug": "test", "location_name": "Test Hospital", "system": "Example"}]
        (self.root / "hospitals.json").write_text(json.dumps(self.hospitals))
        self.directory = self.root / "data/test"
        self.directory.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def commit(self, message="Snapshot"):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def snapshot(self, price="24.355", sha="a", version=2):
        self.meta = {"sha256": sha * 64, "status": "summarized", "summary_version": version,
                     "last_changed": "2026-09-01T00:00:00Z"}
        (self.directory / "meta.json").write_text(json.dumps(self.meta))
        self.write_rows([["HCPCS", "00123", "Example", "100", price, "", "150", "2"]])
        return self.commit()

    def write_rows(self, rows):
        with (self.directory / "summary.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(catalog.COLUMNS)
            writer.writerows(rows)

    def update_meta(self, **values):
        self.meta.update(values)
        (self.directory / "meta.json").write_text(json.dumps(self.meta))
        return self.commit("Metadata")

    def history(self):
        return catalog.collect_history(self.root, "HEAD", ["test"],
                                       "https://github.com/example/main", "https://github.com/example/raw")["test"]

    def build(self):
        return catalog.build_catalog(self.hospitals, self.root, self.output, "https://example.org",
                                     "https://github.com/example/main", "https://github.com/example/raw")

    def test_source_change_without_price_change_and_method_rebuild(self):
        first = self.snapshot()
        second = self.snapshot(sha="b")
        third = self.update_meta(summary_version=3)
        snapshots = self.history()
        self.assertEqual([s["revision"] for s in snapshots], [first, second, third])
        self.assertEqual([s["change_kind"] for s in snapshots], ["source", "source", "summary"])
        self.assertEqual(snapshots[0]["summary_git_blob"], snapshots[1]["summary_git_blob"])
        self.assertIn(first, snapshots[0]["summary_url"])

    def test_late_archive_enriches_matching_history_without_new_snapshot(self):
        first = self.snapshot()
        archived = self.update_meta(cold_storage={"sha256": "a" * 64,
                                                "url": "https://archive.org/details/test", "file_sha256": "c" * 64})
        self.update_meta(fetch_failures={"last_error": "offline"})
        snapshots = self.history()
        self.assertEqual(len(snapshots), 1)
        self.assertIn(first, snapshots[0]["metadata_url"])
        self.assertIn(archived, snapshots[0]["source"]["metadata_url"])

    def test_mismatched_archive_is_not_used(self):
        self.snapshot()
        self.update_meta(cold_storage={"sha256": "b" * 64, "url": "https://archive.org/details/wrong"})
        self.assertIsNone(self.history()[0]["source"])
        self.assertIn("archive_hash_mismatch", self.build()["hospitals"][0]["issues"])

    def test_raw_revision_is_pinned_and_missing_listing_keeps_archive(self):
        self.snapshot()
        self.update_meta(status="sharded", raw_commit="b" * 40)
        self.update_meta(status="missing-from-hpt-txt")
        entry = self.build()["hospitals"][0]
        self.assertIn("b" * 40, entry["latest_snapshot"]["source"]["url"])
        self.assertIn("listing_missing", entry["issues"])

    def test_reversion_and_summary_deletion_remain_observable(self):
        self.snapshot("100")
        self.snapshot("200")
        self.snapshot("100")
        (self.directory / "summary.csv").unlink()
        self.commit("Remove summary")
        snapshots = self.history()
        self.assertEqual(len(snapshots), 4)
        self.assertEqual(snapshots[-1]["change_kind"], "summary_removed")
        self.assertIsNone(snapshots[-1]["summary_url"])
        self.assertIn("summary_missing", self.build()["hospitals"][0]["issues"])

    def test_merge_records_mainline_result(self):
        self.snapshot()
        self.git("checkout", "-qb", "branch")
        self.snapshot("200")
        self.git("checkout", "-q", "main")
        (self.root / "note.txt").write_text("mainline")
        self.commit("Independent change")
        self.git("merge", "--no-ff", "-qm", "Merge snapshot", "branch")
        snapshots = self.history()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[-1]["revision"], self.git("rev-parse", "HEAD"))

    def test_unknown_freshness_failures_and_flat_catalog_agree(self):
        self.snapshot()
        self.update_meta(fetch_failures={"first_failed": "2026-09-05", "last_error": "offline"})
        result = self.build()
        entry = result["hospitals"][0]
        self.assertIsNone(entry["last_successful_check_at"])
        self.assertEqual(entry["summary_rows"], 1)
        self.assertEqual(result["totals"]["with_issues"], 1)
        self.assertEqual(result["totals"]["snapshots_without_archived_source"], 1)
        self.assertEqual(entry["snapshots_without_archived_source"], 1)
        self.assertIn("fetch_failed", entry["issues"])
        with (self.output / "data/catalog.csv").open(newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["summary_url"], entry["latest_snapshot"]["summary_url"])
        quality = json.loads((self.output / "data/quality.json").read_text())
        self.assertEqual(quality["totals"], result["totals"])
        self.assertEqual(quality["hospitals"][0]["summary"]["positive_prices"]["discounted_cash"], 1)

    def test_summary_validation_preserves_keys_and_distinguishes_absence(self):
        self.write_rows([["CPT", "001", "Name", "0", "", "-1", "nan", "1.2"],
                         ["CPT", "001", "Name", "10", "2", "", "", "0"],
                         ["CPT", "1", "Name", "10", "2", "", "", "0"], ["short"]])
        stats, issues = catalog.inspect_summary(self.directory / "summary.csv")
        self.assertEqual(stats["row_count"], 4)
        self.assertEqual(stats["duplicate_keys"], 1)
        self.assertEqual(stats["invalid_numbers"], 2)
        self.assertEqual(stats["malformed_rows"], 1)
        self.assertEqual(stats["positive_prices"]["discounted_cash"], 2)
        self.assertEqual(set(issues), {"summary_duplicate_keys", "summary_invalid_numbers", "summary_malformed_rows"})

    def test_uncommitted_data_cannot_claim_pinned_provenance(self):
        self.snapshot()
        self.write_rows([])
        with self.assertRaisesRegex(ValueError, "Commit data"):
            self.build()

    def test_shallow_clone_rejected(self):
        self.snapshot()
        clone = Path(self.temp.name) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone)], check=True)
        with self.assertRaisesRegex(ValueError, "full commit history"):
            catalog.build_catalog(self.hospitals, clone, self.output, "base", "repo", "raw")

    def test_retired_page_removes_stale_assets(self):
        destination = self.output / "hospitals/rush-university/compare"
        destination.mkdir(parents=True)
        (destination / "comparison.mjs").write_text("old UI")
        gen_site.retire_comparison(self.output)
        self.assertEqual([p.name for p in destination.iterdir()], ["index.html"])
        self.assertIn('content="0;url=', (destination / "index.html").read_text())


class DownloadExampleTest(unittest.TestCase):
    def test_verified_bytes_and_provenance_preserve_precision(self):
        content = b'code,price\n00123,24.355\n'
        snapshot = {"summary_url": "https://example.org/summary.csv",
                    "summary_git_blob": hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "summary.csv"
            with patch("examples.download_summary.request", return_value=io.BytesIO(content)):
                download_verified(snapshot, output, {"snapshot": snapshot})
            self.assertEqual(output.read_bytes(), content)
            self.assertEqual(json.loads(output.with_name("summary.csv.provenance.json").read_text())["snapshot"], snapshot)
            with self.assertRaises(FileExistsError):
                download_verified(snapshot, output, {})

    def test_corrupt_download_does_not_leave_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "summary.csv"
            with patch("examples.download_summary.request", return_value=io.BytesIO(b"wrong")):
                with self.assertRaisesRegex(ValueError, "do not match"):
                    download_verified({"summary_url": "https://example.org", "summary_git_blob": "a" * 40}, output, {})
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_select_exact_revision_and_skip_missing_summary(self):
        manifest = {"snapshots": [{"revision": "abc", "snapshot_id": "test:abc", "summary_url": "url"},
                                   {"revision": "def", "snapshot_id": "test:def", "summary_url": None}]}
        self.assertEqual(choose_snapshot(manifest)["revision"], "abc")
        self.assertEqual(choose_snapshot(manifest, "test:abc")["revision"], "abc")
        with self.assertRaises(ValueError):
            choose_snapshot(manifest, "def")
