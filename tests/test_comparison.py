import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import comparison


class TestHistoricalExport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.output = self.root / "site"
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.org")
        self.directory = self.repo / "data" / "rush-university"
        self.directory.mkdir(parents=True)
        self.hospital = {"slug": "rush-university", "location_name": "Rush", "system": "Rush"}

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def commit_snapshot(self, price, version=2, raw_commit="a" * 40, duplicate=False):
        meta = {"summary_version": version, "status": "sharded", "raw_commit": raw_commit,
                "sha256": "b" * 64, "last_changed": "2026-09-01T00:00:00Z"}
        (self.directory / "meta.json").write_text(json.dumps(meta))
        with (self.directory / "summary.csv").open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(comparison.COLUMNS)
            row = ["HCPCS", "00123", "Example", price, "24.355", "", "150", "2"]
            writer.writerow(row)
            if duplicate:
                writer.writerow(row)
        self.git("add", "data")
        self.git("commit", "-qm", "Snapshot")
        return self.git("rev-parse", "HEAD")

    def export(self):
        return comparison.export_history(self.repo, "rush-university", self.output,
                                         "https://github.com/example/main",
                                         "https://github.com/example/raw")

    def test_export_uses_exact_historical_metadata_and_source_values(self):
        first = self.commit_snapshot("100", raw_commit="a" * 40)
        second = self.commit_snapshot("200", raw_commit="c" * 40)
        # A later metadata-only commit is not a new price snapshot.
        meta = json.loads((self.directory / "meta.json").read_text())
        meta["fetch_failures"] = {"last_error": "offline"}
        (self.directory / "meta.json").write_text(json.dumps(meta))
        self.git("add", "data")
        self.git("commit", "-qm", "Metadata only")
        snapshots = self.export()
        self.assertEqual([s["id"] for s in snapshots], [first, second])
        self.assertIn("a" * 40, snapshots[0]["source"]["url"])
        self.assertIn("c" * 40, snapshots[1]["source"]["url"])
        self.assertIn(first, snapshots[0]["summary_url"])
        self.assertIn(second, snapshots[1]["metadata_url"])
        data = json.loads((self.output / f"{first}.json").read_text())
        self.assertEqual(data["rows"][0][1], "00123")
        self.assertEqual(data["rows"][0][3:6], ["100", "24.355", ""])

    def test_method_versions_preserved_and_embedded_json_cannot_end_script(self):
        self.commit_snapshot("100", version=1)
        self.commit_snapshot("200", version=2)
        self.hospital["location_name"] = 'Rush </script><script>alert("x")</script>'
        url = comparison.build_comparison(self.hospital, self.output, self.repo,
                                          "https://example.org", "https://github.com/example/main",
                                          "https://github.com/example/raw")
        self.assertEqual(url, "https://example.org/hospitals/rush-university/compare/")
        output = self.output / "hospitals/rush-university/compare"
        html = (output / "index.html").read_text()
        self.assertNotIn('<script>alert("x")', html)
        self.assertIn("\\u003c/script>", html)
        self.assertIn('"summary_version": 1', html)
        self.assertIn('"summary_version": 2', html)
        self.assertTrue((output / "comparison-model.mjs").is_file())

    def test_duplicate_keys_fail_instead_of_silently_losing_entries(self):
        self.commit_snapshot("100", duplicate=True)
        with self.assertRaisesRegex(ValueError, "duplicate summary key"):
            self.export()

    def test_return_to_an_older_price_remains_a_new_snapshot(self):
        first = self.commit_snapshot("100")
        second = self.commit_snapshot("200")
        third = self.commit_snapshot("100")
        self.assertEqual([s["id"] for s in self.export()], [first, second, third])

    def test_method_change_with_identical_summary_is_preserved(self):
        first = self.commit_snapshot("100", version=1)
        second = self.commit_snapshot("100", version=2)
        snapshots = self.export()
        self.assertEqual([s["id"] for s in snapshots], [first, second])
        self.assertEqual([s["summary_version"] for s in snapshots], [1, 2])

    def test_deleted_summary_does_not_create_a_snapshot(self):
        first = self.commit_snapshot("100")
        self.git("rm", "data/rush-university/summary.csv")
        self.git("commit", "-qm", "Remove summary")
        self.assertEqual([s["id"] for s in self.export()], [first])

    def test_archived_original_must_match_snapshot(self):
        meta = {"status": "summarized", "sha256": "current",
                "cold_storage": {"sha256": "previous", "url": "https://archive.org/details/old"}}
        self.assertIsNone(comparison.source_link(meta, "a" * 40, "rush", "main", "raw"))
        meta["cold_storage"] = {"sha256": "current", "url": "https://archive.org/details/current"}
        self.assertEqual(comparison.source_link(meta, "a" * 40, "rush", "main", "raw")["url"],
                         "https://archive.org/details/current")
