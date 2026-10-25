import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import browser_fetch
import local_refetch
import scrape


class BrowserTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.browser = browser_fetch.BrowserFetch("https://x/", "https://x/file", self.root)
        self.browser.driver = Mock()

    def sample(self, data=b"abc", content_range="bytes 0-2/3"):
        return dict(data=base64.b64encode(data).decode(), range=content_range,
                    etag='"v1"', modified=None)

    def test_fingerprint_matches_existing_local_algorithm(self):
        path = self.root / "file.csv"
        path.write_bytes(bytes(range(256)) * 24000)
        payload = path.read_bytes()

        def ranged(script, url, offset, count, validator):
            data = payload[offset:offset + count]
            return self.sample(data, f"bytes {offset}-{offset + len(data) - 1}/{len(payload)}")

        with patch.object(self.browser, "_execute", side_effect=ranged):
            self.assertEqual(self.browser.fingerprint(), scrape.local_fingerprint(path))

    def test_bad_ranges_never_verify(self):
        for sample in (self.sample(b"ab"), self.sample(content_range="bytes 1-3/4"),
                       self.sample(content_range="invalid"), self.sample(content_range="bytes 0-2/8")):
            with self.subTest(sample=sample), patch.object(self.browser, "_execute", return_value=sample):
                with self.assertRaises(ValueError):
                    self.browser.fingerprint()

    def test_unsupported_range_requests_require_full_download(self):
        with patch.object(self.browser, "_execute", return_value=None):
            self.assertIsNone(self.browser.fingerprint())

    def test_version_change_during_sampling_cannot_match(self):
        first = self.sample(b"a" * scrape.SAMPLE_BYTES,
                            f"bytes 0-{scrape.SAMPLE_BYTES - 1}/{scrape.SAMPLE_BYTES * 8}")
        with patch.object(self.browser, "_execute", side_effect=[first, None]):
            self.assertIsNone(self.browser.fingerprint())

    def test_partial_html_and_failed_downloads_preserve_existing_file(self):
        dest = self.root / "file.csv"
        for body, length, failure in ((b"a,b\n", "100", None), (b"<html>blocked", "13", None),
                                      (b"", "0", None), (b"a,b\n", None, RuntimeError("connection reset"))):
            dest.write_bytes(b"previous snapshot")
            responses = [{"length": length}]
            if body:
                responses.append({"done": False, "data": base64.b64encode(body).decode()})
            responses.append(failure or {"done": True})
            with self.subTest(body=body, failure=failure), \
                    patch.object(self.browser, "_execute", side_effect=responses):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.browser.download(dest)
            self.assertEqual(dest.read_bytes(), b"previous snapshot")
            self.assertFalse(dest.with_suffix(".csv.part").exists())


class YaleFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meta = {"sha256": "a" * 64, "transfer_fingerprint": "known"}
        self.observation = {}
        self.browser = Mock()
        self.browser.fingerprint.return_value = "known"
        self.factory = Mock()
        self.factory.return_value.__enter__ = Mock(return_value=self.browser)
        self.factory.return_value.__exit__ = Mock(return_value=False)
        for p in (patch.object(local_refetch, "meta_of", side_effect=lambda slug: self.meta),
                  patch.object(local_refetch.scrape, "remote_fingerprint", return_value=None),
                  patch.object(local_refetch.scrape, "clear_failure_record"),
                  patch.object(local_refetch, "BrowserFetch", self.factory)):
            p.start()
            self.addCleanup(p.stop)

    def run_refetch(self, slug="yale-new-haven"):
        return local_refetch.refetch_workaround(slug, self.root, self.observation)

    def test_browser_sample_match_records_evidence_without_ingestion(self):
        with patch.object(local_refetch, "run", side_effect=subprocess.CalledProcessError(22, ["curl"])) as run:
            self.assertFalse(self.run_refetch())
        self.assertEqual(run.call_count, 1)
        self.browser.download.assert_not_called()
        self.assertEqual(self.observation["transport"], "browser")
        self.assertEqual(self.observation["method"], "sampled_fingerprint")
        self.assertEqual(self.observation["evidence"], {"fingerprint": "known"})
        self.assertEqual(self.observation["outcome"], "unchanged")
        self.assertIn("verified_at", self.observation)
        self.assertNotIn("downloaded_at", self.observation)
        self.assertEqual(self.observation["retry_errors"][0]["transport"], "plain")

    def test_changed_and_unsupported_samples_download_and_ingest_verified_url(self):
        for fp in ("different", None):
            self.meta["sha256"] = "a" * 64
            self.browser.fingerprint.return_value = fp
            self.browser.download.side_effect = lambda dest: dest.write_bytes(b"code,price\nx,2\n")

            def run(*args):
                if args[0] == "curl":
                    raise subprocess.CalledProcessError(22, ["curl"])
                self.assertEqual(args[-2:], ("--source-url", local_refetch.YALE_URL))
                self.assertEqual(Path(args[3]).read_bytes(), b"code,price\nx,2\n")
                self.meta["sha256"] = "b" * 64

            with self.subTest(fp=fp), patch.object(local_refetch, "run", side_effect=run):
                self.assertTrue(self.run_refetch())
            self.assertEqual(self.observation["method"], "full_download")
            self.assertEqual(self.observation["outcome"], "changed")
            self.assertEqual(self.observation["verified_at"], self.observation["downloaded_at"])
            self.assertEqual(self.observation["source_sha256"], "b" * 64)

    def test_identical_full_download_does_not_stage_missing_raw_shards(self):
        self.browser.fingerprint.return_value = None
        self.browser.download.side_effect = lambda dest: dest.write_bytes(b"code,price\nx,2\n")
        with patch.object(local_refetch, "run", side_effect=[subprocess.CalledProcessError(22, ["curl"]), None]):
            self.assertFalse(self.run_refetch())
        self.assertEqual(self.observation["outcome"], "unchanged")
        self.assertEqual(self.observation["method"], "full_download")

    def test_browser_failure_does_not_record_success_or_clear_failure(self):
        self.browser.fingerprint.side_effect = RuntimeError("challenge did not clear")
        with patch.object(local_refetch, "run", side_effect=subprocess.CalledProcessError(22, ["curl"])):
            with self.assertRaisesRegex(RuntimeError, "challenge"):
                self.run_refetch()
        self.assertEqual(self.observation["outcome"], "failed")
        self.assertNotIn("verified_at", self.observation)
        local_refetch.scrape.clear_failure_record.assert_not_called()

    def test_plain_success_and_other_hospitals_never_start_browser(self):
        with patch.object(local_refetch.scrape, "remote_fingerprint", return_value="known"):
            self.assertFalse(self.run_refetch())
        with patch.object(local_refetch, "workaround_url", return_value="https://x/file.csv"), \
                patch.object(local_refetch, "run", side_effect=subprocess.CalledProcessError(22, ["curl"])):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_refetch("tristar-centennial")
        self.factory.assert_not_called()


class VerifiedIngestTests(unittest.TestCase):
    def test_explicit_source_ingests_without_network_discovery_or_head(self):
        import ingest_local
        fixture = Path(__file__).parent / "fixtures" / "wide.csv"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hospitals.json").write_text(json.dumps([
                {"slug": "yale", "system": "Yale", "location_name": "Hospital"}]))
            with patch.object(ingest_local, "ROOT", root), \
                    patch.object(ingest_local, "DATA", root / "data"), \
                    patch.object(scrape, "DATA", root / "data"), \
                    patch.object(scrape, "REWRITTEN", []), \
                    patch.object(ingest_local, "discover_mrf_url", side_effect=AssertionError("discovery called")), \
                    patch.object(ingest_local, "head", side_effect=AssertionError("HEAD called")), \
                    patch("sys.argv", ["ingest_local.py", "yale", str(fixture), "--source-url", local_refetch.YALE_URL]):
                ingest_local.main()
            meta = json.loads((root / "data/yale/meta.json").read_text())
            self.assertEqual(meta["mrf_url"], local_refetch.YALE_URL)
            self.assertIsNone(meta["source_etag"])
            self.assertEqual(meta["transfer_fingerprint"], scrape.local_fingerprint(fixture))
            self.assertTrue((root / "data/yale/summary.csv").exists())


@unittest.skipUnless(os.environ.get("HPH_BROWSER_TESTS") == "1", "set HPH_BROWSER_TESTS=1 for Chromium integration")
class BrowserIntegrationTests(unittest.TestCase):
    """Real browser/HTTP tests, enabled on a host with the optional transport."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        cls.payload = b"code,price\n" + b"sample,123\n" * 600000
        cls.mode = "valid"

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body, status, headers = cls.payload, 200, {}
                if self.path == "/":
                    body = f'<a href="{cls.url}/file">Download</a>'.encode()
                elif cls.mode == "html":
                    body = b"<html>challenge</html>"
                elif self.headers.get("Range") and cls.mode != "ignored_ranges":
                    lo, hi = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
                    body, status = cls.payload[lo:hi + 1], 206
                    headers["Content-Range"] = f"bytes {lo}-{lo + len(body) - 1}/{len(cls.payload)}"
                    if cls.mode == "bad_range":
                        headers["Content-Range"] = "invalid"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body) + (10 if cls.mode == "truncated" else 0)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.browser = browser_fetch.BrowserFetch(cls.url + "/", cls.url + "/file", cls.temp.name)
        cls.browser.__enter__()
        cls.addClassCleanup(cls.browser.__exit__, None, None, None)

    def test_real_ranges_and_streamed_download_match_original(self):
        type(self).mode = "valid"
        path = Path(self.temp.name) / "source.csv"
        path.write_bytes(self.payload)
        self.assertEqual(self.browser.fingerprint(), scrape.local_fingerprint(path))
        dest = path.with_name("download.csv")
        self.browser.download(dest)
        self.assertEqual(dest.read_bytes(), self.payload)

    def test_ignored_range_falls_back_to_streamed_get(self):
        type(self).mode = "ignored_ranges"
        self.assertIsNone(self.browser.fingerprint())
        dest = Path(self.temp.name) / "no-range.csv"
        self.browser.download(dest)
        self.assertEqual(dest.read_bytes(), self.payload)

    def test_invalid_range_is_rejected(self):
        type(self).mode = "bad_range"
        with self.assertRaises(ValueError):
            self.browser.fingerprint()

    def test_html_and_broken_stream_cannot_replace_existing_download(self):
        for mode in ("html", "truncated"):
            type(self).mode = mode
            dest = Path(self.temp.name) / "previous.csv"
            dest.write_bytes(b"previous")
            with self.subTest(mode=mode), self.assertRaises((ValueError, RuntimeError)):
                self.browser.download(dest)
            self.assertEqual(dest.read_bytes(), b"previous")


if __name__ == "__main__":
    unittest.main()
