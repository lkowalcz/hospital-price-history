import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capture_rotation as rotation
import collection_health as health


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = "2026-11-02T10:00:00Z"
        self.policy = {"default_host": "github-actions", "overrides": {"pi-file": "pi"},
                       "original_capture": {"full_download_interval_days": 30, "retry_interval_hours": 168,
                           "hosts": {"github-actions": {"max_files_per_day": 2,
                               "max_bytes_per_day": 40_000_004, "max_download_bytes": 20_000_000}}}}
        self.write_policy()
        self.hospitals = [{"slug": s} for s in ["a", "b", "c", "pi-file"]]
        for h in self.hospitals:
            path = self.root / "data" / h["slug"] / "meta.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"sha256": "a" * 64, "size_bytes": 1000}))

    def write_policy(self):
        (self.root / "collection_policy.json").write_text(json.dumps(self.policy))

    def report(self, **kwargs):
        return rotation.report(self.root, self.hospitals, "github-actions", at=self.at, **kwargs)

    def receipt(self, slug, **overrides):
        r = {"snapshot_sha256": "a" * 64, "download_bytes": 1000, "artifact_kind": "download",
             "status": "archived", "captured_at": "2026-11-01T00:00:00Z"}
        r.update(overrides)
        path = self.root / "data" / slug / "originals" / "receipt.json"
        path.parent.mkdir()
        path.write_text(json.dumps(r))

    def test_missing_first_with_host_and_byte_limits(self):
        self.receipt("a", captured_at="2026-09-01T00:00:00Z")
        report = self.report()
        self.assertEqual(list(report["selected"]), ["b", "c"])
        self.assertEqual(report["reserved_bytes"], 40_000_004)
        self.assertNotIn("pi-file", [e["slug"] for e in report["hospitals"]])

    def test_current_original_requires_matching_hash_and_download_provenance(self):
        self.receipt("a")
        self.receipt("b", snapshot_sha256="b" * 64)
        self.receipt("c", artifact_kind="local_input")
        report = self.report()
        self.assertEqual(set(report["selected"]), {"b", "c"})
        self.assertEqual(report["hospitals"][0]["state"], "current")

    def test_pending_and_oversized_files_are_explicitly_deferred(self):
        self.receipt("a", status="pending")
        path = self.root / "data/b/meta.json"
        path.write_text(json.dumps({"sha256": "b" * 64, "size_bytes": 30_000_000}))
        report = self.report()
        states = {e["slug"]: e["state"] for e in report["hospitals"]}
        self.assertEqual(states["a"], "upload_pending")
        self.assertEqual(states["b"], "over_budget")
        self.assertEqual(set(report["selected"]), {"c"})

    def test_reservation_survives_interruption_and_same_day_rerun(self):
        with patch.dict(os.environ, {"PRESERVE_ORIGINALS": "1", "ORIGINAL_CAPTURE_ROTATION": "1"}), \
                patch.object(health, "now", return_value=self.at), patch.object(rotation, "now", return_value=self.at):
            log = health.RunLog.create(self.root, "github-actions", ["a", "b", "c"])
            self.assertEqual(set(log.start["original_capture_plan"]), {"a", "b"})
            # Published prefix and local interrupted log count only once.
            published = self.root / "observations" / self.at[:10] / log.path.name
            published.parent.mkdir(parents=True)
            published.write_bytes(log.path.read_bytes())
            log.begin("a")
            log.finish("interrupted")
            later = health.RunLog.create(self.root, "github-actions", ["a", "b", "c"])
            self.assertEqual(later.start["original_capture_plan"], {})
        report = self.report()
        self.assertEqual(report["reserved_files"], 2)
        self.assertEqual(report["reserved_bytes"], 40_000_004)

    def test_failed_reservation_cools_down_and_does_not_starve_other_files(self):
        logs = [[{"event": "run_started", "host": "github-actions", "at": "2026-11-01T10:00:00Z",
                  "original_capture_plan": {"a": {"reserved_bytes": 20_000_002}}}]]
        report = self.report(logs=logs)
        self.assertEqual(set(report["selected"]), {"b", "c"})
        self.assertEqual(report["hospitals"][0]["state"], "retry_wait")
        self.at = "2026-11-10T10:00:00Z"
        self.assertEqual(list(self.report(logs=logs)["selected"]), ["b", "c"])

    def test_recent_actual_download_postpones_legacy_recheck(self):
        path = self.root / "data/a/meta.json"
        path.write_text(json.dumps({"sha256": "a" * 64, "size_bytes": 1000,
                                    "cold_storage": {"sha256": "a" * 64}}))
        logs = [[{"event": "run_started", "host": "manual", "at": self.at},
                 {"event": "attempt_finished", "slug": "a", "outcome": "unchanged",
                  "method": "full_download", "downloaded_at": self.at}]]
        self.assertEqual(self.report(logs=logs)["hospitals"][0]["state"], "current")

    def test_rotation_requires_explicit_enablement_and_preservation(self):
        for preservation, rotation_enabled in [("0", "1"), ("1", "0")]:
            with patch.dict(os.environ, {"PRESERVE_ORIGINALS": preservation,
                                        "ORIGINAL_CAPTURE_ROTATION": rotation_enabled}):
                self.assertEqual(rotation.plan(self.root, "github-actions", ["a"]), {})

    def test_empty_source_never_treated_as_preserved_price_data(self):
        path = self.root / "data/a/meta.json"
        path.write_text(json.dumps({"sha256": "a" * 64, "size_bytes": 0}))
        self.assertEqual(self.report()["hospitals"][0]["state"], "source_unusable")
