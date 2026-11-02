import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import archive_recovery
import preserve_source as originals
import scrape
from test_scrape import ProcessHarness

REAL_DISCOVER = scrape.discover_mrf_url


def fake_upload(queue, record):
    return {"url": "https://archive.org/details/test-original",
            "download_url": "https://archive.org/download/test-original/original.zst",
            "compressed_sha256": "d" * 64, "compressed_bytes": 123,
            "compression": "zstd", "archived_at": "2026-11-02T12:00:00Z"}


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "download.csv"
        self.body = b'col1,col2\r\n"multi\r\nline",0001\r\n'
        self.source.write_bytes(self.body)
        self.env = patch.dict(os.environ, {"ORIGINAL_SPOOL_DIR": str(self.root / "spool")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def capture(self, **kwargs):
        return originals.capture(self.source, "example", "https://hospital/file.csv", "file.csv",
                                 root=self.root, **kwargs)

    def test_failed_upload_survives_source_deletion_and_retries_without_download(self):
        def unavailable(*args):
            raise OSError("archive offline")
        record = self.capture(uploader=unavailable)
        originals.bind(record, "a" * 64, record["download_sha256"], root=self.root)
        self.source.unlink()
        self.assertEqual(len(originals.pending(self.root)), 1)

        def verify_bytes(queue, receipt):
            self.assertEqual((queue / "original").read_bytes(), self.body)
            return fake_upload(queue, receipt)
        self.assertEqual(originals.retry(root=self.root, uploader=verify_bytes), [])
        saved = json.loads(originals.receipt_path(self.root / "data", record).read_text())
        self.assertEqual(saved["status"], "archived")
        self.assertEqual(saved["snapshot_sha256"], "a" * 64)
        self.assertFalse(list((self.root / "spool").glob("*/*/original")))

    def test_interrupt_leaves_bytes_and_receipt(self):
        with self.assertRaises(KeyboardInterrupt):
            self.capture(uploader=lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.source.unlink()
        self.assertEqual(len(originals.pending(self.root)), 1)
        self.assertEqual(next((self.root / "spool").glob("*/*/original")).read_bytes(), self.body)

    def test_identical_bytes_do_not_upload_twice(self):
        with patch.object(originals, "upload", side_effect=fake_upload) as uploader:
            first = self.capture()
            second = self.capture()
        self.assertEqual(uploader.call_count, 1)
        self.assertEqual(first["captured_at"], second["captured_at"])

    def test_formatting_changes_have_distinct_original_identities(self):
        first = self.capture(uploader=fake_upload)
        self.source.write_bytes(self.body.replace(b"\r\n", b"\n"))
        second = self.capture(uploader=fake_upload)
        self.assertNotEqual(first["download_sha256"], second["download_sha256"])
        self.assertEqual(len(list((self.root / "data/example/originals").glob("*.json"))), 2)

    def test_zip_is_preserved_without_claiming_it_is_the_unpacked_mrf(self):
        with zipfile.ZipFile(self.source, "w") as zipped:
            zipped.writestr("file.csv", self.body)
        record = self.capture(uploader=fake_upload)
        originals.bind(record, "a" * 64, hashlib.sha256(self.body).hexdigest(), root=self.root)
        meta = {"sha256": "a" * 64}
        originals.apply_to_meta(meta, record)
        self.assertEqual(record["container"], "zip")
        self.assertNotIn("cold_storage", meta)
        self.assertEqual(meta["original_download"]["status"], "archived")

    def test_retry_does_not_replace_newer_snapshot_metadata(self):
        record = self.capture(upload_now=False)
        originals.bind(record, "a" * 64, record["download_sha256"], root=self.root)
        mp = self.root / "data/example/meta.json"
        newer = {"sha256": "b" * 64, "original_download": {"download_sha256": "c" * 64}}
        mp.write_text(json.dumps(newer))
        originals.retry(root=self.root, uploader=fake_upload)
        self.assertEqual(json.loads(mp.read_text()), newer)

    def test_local_input_is_not_changed_when_operator_edits_the_source(self):
        record = self.capture(upload_now=False, artifact_kind="local_input")
        self.source.write_bytes(b"a replacement download")
        raw = self.root / "spool/example" / record["download_sha256"] / "original"
        self.assertEqual(raw.read_bytes(), self.body)

    @unittest.skipUnless(shutil.which("zstd"), "zstd unavailable")
    def test_real_compression_and_failed_transport_keep_verified_retry_bytes(self):
        real_run = subprocess.run
        sent = []

        def transport(cmd, **kwargs):
            if cmd[0] == "test-ia":
                compressed = Path(cmd[3])
                sha = hashlib.sha256(compressed.read_bytes()).hexdigest()
                self.assertEqual(compressed.name, f"original-{sha}.zst")
                self.assertEqual(subprocess.check_output(["zstd", "-q", "-d", "-c", str(compressed)]), self.body)
                sent.append(sha)
                if len(sent) == 1:
                    raise subprocess.CalledProcessError(1, cmd)
                return subprocess.CompletedProcess(cmd, 0)
            return real_run(cmd, **kwargs)

        with patch.dict(os.environ, {"IA_BIN": "test-ia"}), patch.object(subprocess, "run", side_effect=transport):
            record = self.capture()
            self.assertEqual(record["status"], "pending")
            queue = self.root / "spool/example" / record["download_sha256"]
            self.assertFalse((queue / "original").exists())
            self.assertTrue((queue / "original.zst").exists())
            self.source.unlink()
            self.assertEqual(originals.retry(root=self.root), [])
            self.assertFalse((queue / "original.zst").exists())
        self.assertEqual(sent[0], sent[1])

    def test_corrupt_original_is_not_sent_to_archive(self):
        record = self.capture(upload_now=False)
        queue = self.root / "spool/example" / record["download_sha256"]
        (queue / "original").write_bytes(b"corrupt")
        with patch.object(subprocess, "run") as transport:
            self.assertEqual(len(originals.retry(root=self.root)), 1)
        transport.assert_not_called()
        self.assertIn("checksum mismatch", originals.pending(self.root)[0]["last_error"])

    def test_incomplete_archive_receipt_cannot_discard_queued_bytes(self):
        record = self.capture(upload_now=False)
        queue = self.root / "spool/example" / record["download_sha256"]
        record["status"] = "archived"
        (queue / "record.json").write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "lacks archive"):
            originals.retry(root=self.root)
        self.assertEqual((queue / "original").read_bytes(), self.body)

    def test_ci_bootstrap_ignores_another_collectors_pending_receipt(self):
        record = self.capture(upload_now=False)
        # The restored CI spool is distinct from this manual collector's queue.
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/repo",
                        "ORIGINAL_SPOOL_DIR": str(self.root / "ci-spool")}), \
                patch.object(archive_recovery, "latest_checkpoint", return_value=None):
            archive_recovery.restore(self.root)
            self.assertTrue((self.root / "ci-spool/checkpoint.json").exists())
        self.assertEqual(record["collector"], "manual")

    def test_ci_missing_pending_checkpoint_stops_restore(self):
        with patch.dict(os.environ, {"COLLECTION_HOST": "github-actions"}):
            self.capture(upload_now=False)
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/repo",
                        "ORIGINAL_SPOOL_DIR": str(self.root / "ci-spool")}), \
                patch.object(archive_recovery, "latest_checkpoint", return_value=None):
            with self.assertRaisesRegex(ValueError, "missing from"):
                archive_recovery.restore(self.root)
            self.assertFalse((self.root / "ci-spool").exists())

    def test_latest_checkpoint_does_not_fall_back_past_an_expired_one(self):
        artifact = {"id": 2, "name": "original-queue-10-2", "expired": True,
                    "workflow_run": {"id": 10, "head_branch": "main", "repository_id": 1, "head_repository_id": 1}}
        with patch.object(subprocess, "check_output", side_effect=[
                json.dumps({"total_count": 1, "artifacts": [artifact]}).encode(),
                b'{"path":".github/workflows/scrape.yml"}']):
            with self.assertRaisesRegex(ValueError, "expired"):
                archive_recovery.latest_checkpoint("example/repo")

    def test_recovery_rejects_traversal_and_missing_original(self):
        for name, body in [("../escape", "bad"), ("checkpoint.json", '{"schema_version":1}')]:
            archive = self.root / "checkpoint.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr(name, body)
                if name == "checkpoint.json":
                    zipped.writestr("example/" + "a" * 64 + "/record.json", json.dumps({
                        "schema_version": 1, "slug": "example", "download_sha256": "a" * 64,
                        "status": "pending"}))
            with self.assertRaises(ValueError):
                archive_recovery.extract(archive, self.root / "restored")

    def test_recovered_queue_keeps_pending_bytes(self):
        record = self.capture(upload_now=False)
        queue = self.root / "spool"
        originals.write_json(queue / "checkpoint.json", {"schema_version": 1})
        archive = self.root / "checkpoint.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            for p in queue.rglob("*"):
                if p.is_file():
                    zipped.write(p, p.relative_to(queue))
        restored = self.root / "restored"
        archive_recovery.extract(archive, restored)
        self.assertEqual((restored / "example" / record["download_sha256"] / "original").read_bytes(), self.body)


class CollectionPreservationTests(ProcessHarness):
    def setUp(self):
        super().setUp()
        for p in [patch.dict(os.environ, {"PRESERVE_ORIGINALS": "1", "ORIGINAL_SPOOL_DIR": str(self.scratch.parent / "spool")}),
                  patch.object(originals, "upload", side_effect=fake_upload)]:
            self.patches.append(p)
            p.start()

    def test_stored_sharded_and_metadata_only_all_preserve_originals(self):
        for small, large, body, mode in [(10**8, 10**8, self.body, "stored"),
                                         (100, 10**8, self.body, "sharded"),
                                         (1, 1, b"unsupported content", "metadata-only")]:
            self.body = body
            # Force each independent storage decision, even for identical content.
            (scrape.DATA / "h1/meta.json").unlink(missing_ok=True)
            with patch.object(scrape, "MAX_STORED_BYTES", small), patch.object(scrape, "MAX_SHARD_TOTAL", large):
                self.run_process({"ETag": mode})
            meta = self.meta()
            self.assertEqual(meta["status"], mode)
            self.assertEqual(meta["original_download"]["status"], "archived")
            self.assertEqual(meta["original_download"]["download_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(self.meta()["status"], "metadata-only")

    def test_parser_failure_still_has_original_receipt(self):
        # A multiple-member ZIP currently cannot be materialized by scrape.py.
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as zipped:
            zipped.writestr("one.csv", self.body)
            zipped.writestr("two.csv", self.body)
        self.body = content.getvalue()
        with self.assertRaisesRegex(ValueError, "expected 1 file"):
            self.run_process()
        receipt = json.loads(next((scrape.DATA / "h1/originals").glob("*.json")).read_text())
        self.assertEqual(receipt["status"], "archived")
        self.assertNotIn("snapshot_sha256", receipt)

    def test_failed_upload_keeps_original_after_scraper_scratch_cleanup(self):
        with patch.object(originals, "upload", side_effect=OSError("offline")):
            self.run_process()
        self.assertEqual(self.meta()["original_download"]["status"], "pending")
        queued = next((self.scratch.parent / "spool").glob("*/*/original"))
        self.assertEqual(queued.read_bytes(), self.body)
        originals.retry(data=scrape.DATA, root=scrape.DATA.parent, uploader=fake_upload)
        self.assertEqual(self.meta()["original_download"]["status"], "archived")

    def test_discovery_keeps_exact_utf16_bytes(self):
        raw = ("location-name: H1\nmrf-url: https://x/file.csv\n").encode("utf-16")
        with patch.object(scrape, "fetch_small", return_value=(raw, scrape.Headers([]))), \
                patch.object(scrape, "discover_mrf_url", REAL_DISCOVER):
            self.assertEqual(scrape.discover_mrf_url(self.HOSPITAL, False), "https://x/file.csv")
        self.assertEqual(next((scrape.DATA / "h1/discovery").glob("*.bin")).read_bytes(), raw)

    def test_local_ingestion_uses_the_same_preservation_path(self):
        import ingest_local
        root = self.scratch.parent
        (root / "hospitals.json").write_text(json.dumps([self.HOSPITAL]))
        path = self.scratch / "local.csv"
        path.write_bytes(self.body)
        with patch.object(ingest_local, "ROOT", root), patch.object(ingest_local, "DATA", scrape.DATA), \
                patch("sys.argv", ["ingest_local.py", "h1", str(path), "--source-url", self.URL]):
            ingest_local.main()
        self.assertEqual(self.meta()["original_download"]["artifact_kind"], "local_input")
        self.assertEqual(self.meta()["original_download"]["status"], "archived")


if __name__ == "__main__":
    unittest.main()
