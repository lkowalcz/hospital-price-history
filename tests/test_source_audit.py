import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import data_catalog
import source_audit as audit


class SourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main"
        self.root.mkdir()
        self.init(self.root)
        self.path = self.root / "data/test"
        self.path.mkdir(parents=True)
        self.meta = {"sha256": "a" * 64, "size_bytes": 4, "status": "stored",
                     "mrf_url": "https://example.org/prices.csv", "last_changed": "2026-01-01T00:00:00Z",
                     "source_etag": "v1"}
        (self.path / "standardcharges.csv").write_text("a,b\n")
        (self.path / "summary.csv").write_text(",".join(data_catalog.COLUMNS) + "\nCPT,001,Example,1,2,3,4,1\n")
        self.save_meta()
        self.first = self.commit()

    def init(self, root):
        for args in [("init", "-q", "-b", "main"), ("config", "user.name", "Test"),
                     ("config", "user.email", "test@example.org")]:
            audit.git(root, *args)

    def save_meta(self):
        (self.path / "meta.json").write_text(json.dumps(self.meta))

    def commit(self, root=None):
        root = root or self.root
        audit.git(root, "add", ".")
        audit.git(root, "commit", "-qm", "Fixture")
        return audit.git(root, "rev-parse", "HEAD")

    def record(self, revision, method, evidence, status="linked"):
        meta = json.loads(audit.git(self.root, "show", f"{revision}:data/test/meta.json"))
        return dict(slug="test", revision=revision, method=method, status=status, evidence=evidence,
                    metadata_blob=audit.git(self.root, "rev-parse", f"{revision}:data/test/meta.json"),
                    recorded_source_sha256=meta.get("sha256"), explanation="Reviewed fixture")

    def save_audit(self, records):
        (self.root / audit.AUDIT_PATH).write_text(json.dumps({"schema_version": 1, "records": records}))
        self.commit()

    def history(self):
        return data_catalog.collect_history(self.root, "HEAD", ["test"],
                                           "https://github.com/example/main", "https://github.com/example/raw")["test"]

    def reset(self, changed_url=False):
        self.meta.pop("sha256")
        self.meta.pop("source_etag")
        if changed_url:
            self.meta["mrf_url"] = "https://example.org/different.csv"
        self.save_meta()
        revision = self.commit()
        evidence = dict(prior_revision=self.first, source_sha256="a" * 64,
                        prior_metadata_blob=audit.git(self.root, "rev-parse", f"{self.first}:data/test/meta.json"),
                        changed_fields=["sha256", "source_etag"])
        return self.record(revision, "metadata_hash_reset", evidence)

    def test_checksum_reset_links_retained_source_without_rewriting_history(self):
        record = self.reset()
        before = self.history()
        self.assertIsNone(before[-1]["source"])
        self.save_audit([record])
        after = self.history()
        self.assertEqual([s["snapshot_id"] for s in before], [s["snapshot_id"] for s in after])
        self.assertEqual([s["summary_git_blob"] for s in before], [s["summary_git_blob"] for s in after])
        self.assertIsNone(after[-1]["source_sha256"])
        self.assertEqual(after[-1]["source_identity"]["sha256"], "a" * 64)
        self.assertIn(self.first, after[-1]["source_identity"]["metadata_url"])
        self.assertEqual(after[-1]["source"], after[0]["source"])
        self.assertEqual(after[-1]["source_audit"]["method"], "metadata_hash_reset")

    def test_different_source_details_cannot_be_treated_as_checksum_reset(self):
        self.save_audit([self.reset(changed_url=True)])
        with self.assertRaisesRegex(ValueError, "source details changed"):
            self.history()

    def test_wrong_previous_identity_is_rejected(self):
        record = self.reset()
        record["evidence"]["source_sha256"] = "b" * 64
        self.save_audit([record])
        with self.assertRaisesRegex(ValueError, "prior identity mismatch"):
            self.history()

    def test_wrong_metadata_blob_is_rejected(self):
        record = self.reset()
        record["metadata_blob"] = "0" * 40
        self.save_audit([record])
        with self.assertRaisesRegex(ValueError, "metadata blob mismatch"):
            self.history()

    def test_duplicate_audit_cannot_override_sources(self):
        record = self.reset()
        self.save_audit([record, record])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.history()

    def test_relocated_shards_match_original_content_objects(self):
        self.meta.update(status="sharded", sha256="b" * 64)
        (self.path / "standardcharges.csv").unlink()
        (self.path / "_header.json").write_text('{"name":"test"}')
        (self.path / "shards").mkdir()
        (self.path / "shards/00.jsonl").write_text('{"price":2}\n')
        self.save_meta()
        original = self.commit()
        raw = Path(self.temp.name) / "raw"
        raw.mkdir()
        self.init(raw)
        destination = raw / "data/test"
        (destination / "shards").mkdir(parents=True)
        for name in ["_header.json", "shards/00.jsonl"]:
            (destination / name).write_bytes((self.path / name).read_bytes())
        raw_rev = self.commit(raw)
        audit.git(raw, "update-ref", "refs/remotes/origin/main", raw_rev)
        record = self.record(original, "relocated_shards", dict(
            original_main_revision=original, original_metadata_blob=audit.git(self.root, "rev-parse", f"{original}:data/test/meta.json"),
            raw_revision=raw_rev, header_blob=audit.git(raw, "rev-parse", f"{raw_rev}:data/test/_header.json"),
            shards_tree=audit.git(raw, "rev-parse", f"{raw_rev}:data/test/shards")))
        # The retained original must have the exact same metadata as its
        # corresponding rewritten snapshot, not merely the same hospital.
        audit.verify_raw(record, raw, self.root)
        bad = copy.deepcopy(record)
        bad["evidence"]["shards_tree"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "raw tree mismatch"):
            audit.verify_raw(bad, raw, self.root)
        self.save_audit([record])
        self.assertIn(raw_rev, self.history()[-1]["source"]["url"])

    def normalized_record(self):
        body = b"a\r\nb\r\n"
        normalized = body.replace(b"\r\n", b"\n")
        self.meta.update(sha256=hashlib.sha256(normalized).hexdigest(), size_bytes=len(normalized), status="metadata-only")
        (self.path / "standardcharges.csv").unlink()
        self.save_meta()
        target = self.commit()
        raw_sha = hashlib.sha256(body).hexdigest()
        item = "hospital-price-history-test-" + raw_sha[:12]
        self.meta.update(sha256=raw_sha, size_bytes=len(body), status="summarized",
                         cold_storage={"url": "https://archive.org/details/" + item,
                                       "sha256": raw_sha, "file_sha256": raw_sha})
        self.save_meta()
        archive_commit = self.commit()
        evidence = dict(item=item, file="original.csv.zst", file_sha256=raw_sha, compressed_sha256="f" * 64,
                        normalized_sha256=hashlib.sha256(normalized).hexdigest(), normalized_size_bytes=len(normalized),
                        archive_metadata_revision=archive_commit)
        evidence["download_url"] = audit.archive_download_url(evidence)
        return self.record(target, "crlf_to_lf", evidence)

    def test_normalized_identity_retains_original_file_hash(self):
        record = self.normalized_record()
        self.save_audit([record])
        snapshot = next(s for s in self.history() if s["revision"] == record["revision"])
        self.assertEqual(snapshot["source_sha256"], record["evidence"]["normalized_sha256"])
        self.assertEqual(snapshot["source"]["file_sha256"], record["evidence"]["file_sha256"])
        self.assertNotEqual(snapshot["source_sha256"], snapshot["source"]["file_sha256"])
        self.assertEqual(snapshot["source"]["snapshot_hash_transform"], "crlf_to_lf")

    def test_wrong_normalized_hash_cannot_substitute_an_original(self):
        record = self.normalized_record()
        record["evidence"]["normalized_sha256"] = "0" * 64
        self.save_audit([record])
        with self.assertRaisesRegex(ValueError, "normalized identity mismatch"):
            self.history()

    def test_archive_pointer_must_agree_with_committed_provenance(self):
        record = self.normalized_record()
        record["evidence"]["item"] = "hospital-price-history-wrong"
        self.save_audit([record])
        with self.assertRaisesRegex(ValueError, "archive provenance mismatch"):
            self.history()

    def test_unavailable_original_keeps_summary_and_explicit_reason(self):
        self.meta.update(status="metadata-only", sha256="b" * 64)
        (self.path / "standardcharges.csv").unlink()
        self.save_meta()
        revision = self.commit()
        record = self.record(revision, "not_located", {}, "unavailable")
        self.save_audit([record])
        snapshot = self.history()[-1]
        self.assertIsNone(snapshot["source"])
        self.assertIsNotNone(snapshot["summary_url"])
        self.assertEqual(snapshot["source_audit"]["status"], "unavailable")

        hospitals = [{"slug": "test", "location_name": "Test", "system": "Example"}]
        (self.root / "hospitals.json").write_text(json.dumps(hospitals))
        self.commit()
        out = Path(self.temp.name) / "site"
        catalog = data_catalog.build_catalog(hospitals, self.root, out, "https://example.org",
                                             "https://github.com/example/main", "https://github.com/example/raw")
        report = json.loads((out / "data/source-audit.json").read_text())
        self.assertEqual(catalog["source_audit_totals"]["unavailable_states"], 1)
        self.assertEqual(len(report["remaining_gaps"]), 1)
        self.assertEqual(report["catalog_revision"], catalog["catalog_revision"])
        self.assertEqual(report["remaining_gaps"][0]["source_audit"]["status"], "unavailable")

    def test_normalization_across_every_block_boundary_and_lone_cr(self):
        body = b"a\r\nb\rX\nc\r\n\r"
        expected = body.replace(b"\r\n", b"\n")
        for size in range(1, len(body) + 2):
            result = audit.hash_stream(io.BytesIO(body), size)
            self.assertEqual(result["file_sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(result["normalized_sha256"], hashlib.sha256(expected).hexdigest())
            self.assertEqual(result["normalized_size_bytes"], len(expected))
        self.assertEqual(audit.hash_stream(io.BytesIO(b""))["file_size_bytes"], 0)

    def test_prefix_hash_uses_exact_byte_count_and_rejects_short_file(self):
        body = b"a\r\nb\r\nc\r\n"
        for count in range(len(body) + 1):
            result = audit.hash_stream(io.BytesIO(body), block_size=2, prefix_bytes=count)
            self.assertEqual(result["prefix_sha256"], hashlib.sha256(body[:count]).hexdigest())
            self.assertEqual(result["file_sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(result["prefix_bytes"], count)
        with self.assertRaisesRegex(ValueError, "ends before"):
            audit.hash_stream(io.BytesIO(body), prefix_bytes=len(body) + 1)

    def test_reconstructed_prefix_preserves_incomplete_capture_warning(self):
        body = b"a\r\nb\r\n"
        prefix = body[:4]
        self.meta.update(sha256=hashlib.sha256(prefix).hexdigest(), size_bytes=len(prefix), status="summarized")
        (self.path / "standardcharges.csv").unlink()
        self.save_meta()
        target = self.commit()
        raw_sha = hashlib.sha256(body).hexdigest()
        item = "hospital-price-history-test-" + raw_sha[:12]
        self.meta.update(sha256=raw_sha, size_bytes=len(body), cold_storage={
            "url": "https://archive.org/details/" + item, "sha256": raw_sha, "file_sha256": raw_sha})
        self.save_meta()
        archive_commit = self.commit()
        evidence = dict(item=item, file="original.csv.zst", file_sha256=raw_sha, file_size_bytes=len(body),
                        compressed_sha256="f" * 64, prefix_bytes=len(prefix), prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                        archive_metadata_revision=archive_commit)
        evidence["download_url"] = audit.archive_download_url(evidence)
        record = self.record(target, "verified_prefix", evidence)
        self.save_audit([record])
        snapshot = next(s for s in self.history() if s["revision"] == target)
        self.assertEqual(snapshot["source"]["snapshot_hash_transform"], "prefix")
        self.assertEqual(snapshot["source"]["snapshot_prefix_bytes"], len(prefix))
        self.assertEqual(snapshot["source_warning"]["code"], "truncated_capture")
        bad = copy.deepcopy(record)
        bad["evidence"]["prefix_bytes"] += 1
        self.save_audit([bad])
        with self.assertRaisesRegex(ValueError, "prefix identity mismatch"):
            self.history()


if __name__ == "__main__":
    unittest.main()
