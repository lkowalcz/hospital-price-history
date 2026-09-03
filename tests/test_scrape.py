"""Golden-file and unit tests for the scrape pipeline.

Fixtures in tests/fixtures/ are small CMS-v3-shaped MRFs that bake in the
pathologies met in the wild (BOMs, UTF-16, multi-line quoted CSV records,
multi-code rows, hospital-internal code types, blank-line hpt files).
Goldens in tests/golden/ are the expected summary.csv bytes; a change there
is a deliberate methodology change and should be reviewed as one.
"""

import csv
import io
import json
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

import scrape

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"

try:
    import ijson  # noqa: F401
    HAVE_IJSON = True
except ImportError:
    HAVE_IJSON = False


class TestHptParsing(unittest.TestCase):
    def test_standard_blocks(self):
        text = ("location-name: General Hospital\n"
                "source-page-url: https://example.org/prices\n"
                "mrf-url: https://example.org/mrf.csv\n"
                "location-name: Other Campus\n"
                "mrf-url: https://example.org/other.csv\n")
        blocks = scrape.parse_hpt_txt(text)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["mrf-url"], "https://example.org/mrf.csv")
        self.assertEqual(blocks[1]["location-name"], "Other Campus")

    def test_blank_line_separated_fields(self):
        # Grady blank-line-separates every field; records must still be
        # delimited by location-name, not by blank lines.
        text = ("location-name: A\n\nmrf-url: https://a.example/mrf.json\n\n"
                "location-name: B\n\nmrf-url: https://b.example/mrf.json\n")
        blocks = scrape.parse_hpt_txt(text)
        self.assertEqual([b["location-name"] for b in blocks], ["A", "B"])
        self.assertEqual(blocks[1]["mrf-url"], "https://b.example/mrf.json")

    def test_first_value_wins_and_junk_skipped(self):
        text = ("no colon here\n"
                "location-name: A\nmrf-url: first\nmrf-url: second\n")
        blocks = scrape.parse_hpt_txt(text)
        self.assertEqual(blocks[0]["mrf-url"], "first")

    def test_decode_text_boms(self):
        self.assertEqual(scrape.decode_text("héllo".encode("utf-16")), "héllo")   # Sutter
        self.assertEqual(scrape.decode_text(b"\xfe\xff" + "héllo".encode("utf-16-be")),
                         "héllo")
        self.assertEqual(scrape.decode_text(b"\xef\xbb\xbfplain"), "plain")       # UCSF BOM
        self.assertEqual(scrape.decode_text(b"plain"), "plain")

    def test_normalize_name_curly_quotes(self):
        self.assertEqual(scrape.normalize_name("Brigham and Women’s Hospital"),
                         scrape.normalize_name("BRIGHAM AND WOMEN'S HOSPITAL  "))


class TestNormalizePayload(unittest.TestCase):
    def test_json_sorted_and_bom_tolerated(self):
        out = scrape.normalize_payload("x.json", b'\xef\xbb\xbf{"b": 1, "a": 2}')
        self.assertEqual(out, b'{\n "a": 2,\n "b": 1\n}\n')
        # idempotent: normalizing the normalized form changes nothing
        self.assertEqual(scrape.normalize_payload("x.json", out), out)

    def test_invalid_json_passes_through(self):
        self.assertEqual(scrape.normalize_payload("x.json", b"{nope"), b"{nope")

    def test_csv_crlf(self):
        self.assertEqual(scrape.normalize_payload("x.csv", b"a,b\r\n1,2\r\n"),
                         b"a,b\n1,2\n")

    def test_ext_of(self):
        self.assertEqual(scrape.ext_of("Charges.JSON"), ".json")
        self.assertEqual(scrape.ext_of("download.php"), ".bin")


class TestCsvRecords(unittest.TestCase):
    def test_single_line_passthrough(self):
        lines = ["a,b,c", 'x,"y",z']
        self.assertEqual(list(scrape.csv_records(lines)), lines)

    def test_multiline_grouped(self):
        lines = ['"first', 'part",1', "plain,2"]
        self.assertEqual(list(scrape.csv_records(lines)),
                         ['"first\npart",1', "plain,2"])

    def test_escaped_quotes_stay_single_line(self):
        lines = ['"say ""hi""",1', "plain,2"]
        self.assertEqual(list(scrape.csv_records(lines)), lines)

    def test_unbalanced_trailer_kept(self):
        self.assertEqual(list(scrape.csv_records(['ok,1', '"dangling'])),
                         ["ok,1", '"dangling'])

    def test_literal_quotes_in_unquoted_fields(self):
        # Geisinger-style: a stray inch-mark inside an unquoted field is
        # literal, not a field opener — naive parity counting would glue
        # every following row into one giant record.
        lines = ['CATHETER 5" X 2,C123,10.50',
                 'PIPE 3" ELBOW,C124,4.25',
                 '"quoted, field",C125,1.00']
        self.assertEqual(list(scrape.csv_records(lines)), lines)

    def test_quote_after_comma_still_opens_field(self):
        lines = ['a,"multi', 'line",b', 'plain,1']
        self.assertEqual(list(scrape.csv_records(lines)),
                         ['a,"multi\nline",b', "plain,1"])


class TestSharding(unittest.TestCase):
    def shard_and_reconstruct(self, payload, tmp):
        outdir = Path(tmp) / "sharded"
        self.assertEqual(scrape.store_sharded(outdir, "f.csv", payload), "sharded")
        recon = Path(tmp) / "recon.csv"
        with open(recon, "w") as f:
            f.write((outdir / "_header.csv").read_text())
            for shard in sorted((outdir / "shards").glob("*.csv")):
                f.write(shard.read_text())
        return outdir, recon

    def test_round_trip_preserves_summary(self):
        # Sharding must be lossless w.r.t. the summary: summarizing the
        # reconstructed file gives the same bytes as summarizing the original.
        payload = (FIXTURES / "wide.csv").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            _, recon = self.shard_and_reconstruct(payload, tmp)
            out = Path(tmp) / "summary.csv"
            self.assertTrue(scrape.summarize_csv(recon, out))
            self.assertEqual(out.read_bytes(),
                             (GOLDEN / "wide_summary.csv").read_bytes())

    def test_deterministic(self):
        payload = (FIXTURES / "wide.csv").read_bytes()
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            d1, _ = self.shard_and_reconstruct(payload, t1)
            d2, _ = self.shard_and_reconstruct(payload, t2)
            for shard in sorted((d1 / "shards").glob("*.csv")):
                self.assertEqual(shard.read_bytes(),
                                 (d2 / "shards" / shard.name).read_bytes())

    def test_multiline_record_not_fragmented(self):
        # The fixture's quoted multi-line record must land whole in one shard.
        payload = (FIXTURES / "wide.csv").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            outdir, _ = self.shard_and_reconstruct(payload, tmp)
            hits = [s for s in (outdir / "shards").glob("*.csv")
                    if "Multi\nline description drug" in s.read_text()]
            self.assertEqual(len(hits), 1)

    def test_oversized_bucket_refuses_to_shard(self):
        # A file whose records would put >MAX_SHARD_FILE bytes in one shard
        # must not be sharded (GitHub rejects files over 100 MB); the
        # pipeline then falls back to the summary layer.
        payload = (FIXTURES / "wide.csv").read_bytes()
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(scrape, "MAX_SHARD_FILE", 10):
            outdir = Path(tmp) / "s"
            self.assertEqual(scrape.store_sharded(outdir, "f.csv", payload),
                             "metadata-only")
            self.assertFalse(outdir.exists())  # nothing half-written

    def test_json_shards_and_unparseable(self):
        doc = {"version": "3.0.0",
               "standard_charge_information": [{"description": "X"}]}
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "s"
            mode = scrape.store_sharded(outdir, "f.json", json.dumps(doc).encode())
            self.assertEqual(mode, "sharded")
            self.assertTrue((outdir / "_header.json").exists())
            items = [json.loads(line)
                     for shard in (outdir / "shards").glob("*.jsonl")
                     for line in shard.read_text().splitlines() if line]
            self.assertEqual(items, [{"description": "X"}])
            self.assertEqual(scrape.store_sharded(Path(tmp) / "t", "f.json", b"[1]"),
                             "metadata-only")
            self.assertEqual(scrape.store_sharded(Path(tmp) / "u", "f.xml", b"<x/>"),
                             "metadata-only")

    @unittest.skipUnless(HAVE_IJSON, "ijson not installed")
    def test_json_streaming_matches_in_memory(self):
        # The streaming sharder must produce the same header bytes and the
        # same bucket contents as parsing the whole document; a divergence
        # would surface as a bogus price change in every JSON hospital.
        payload = (FIXTURES / "v3.json").read_bytes()
        self.assertEqual(scrape.shard_json_streaming(payload),
                         scrape.shard_json_in_memory(payload))
        # BOM tolerated on both paths; header keys after the item array
        # (and floats, ints, nested maps, unicode) survive the event walk.
        doc = ('{"a": [1, 2.5, {"b": null}], "standard_charge_information": '
               '[{"x": 1.0, "y": "\u00e9", "z": [true, {"k": 12345678901234}]}, '
               '{"x": 2}], "after": {"n": -0.5, "s": "t"}}')
        for prefix in (b"", b"\xef\xbb\xbf"):
            self.assertEqual(scrape.shard_json_streaming(prefix + doc.encode()),
                             scrape.shard_json_in_memory(doc.encode()))

    @unittest.skipUnless(HAVE_IJSON, "ijson not installed")
    def test_json_streaming_rejects_non_item_documents(self):
        for raw in (b"[1]", b'{"version": "3"}',
                    b'{"standard_charge_information": {"not": "a list"}}',
                    b'{"standard_charge_information": 1}'):
            self.assertIsNone(scrape.shard_json_streaming(raw), raw)
        with self.assertRaises(ValueError):
            scrape.shard_json_streaming(b'{"standard_charge_information": [1,')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(scrape.store_sharded(Path(tmp) / "v", "f.json",
                                                  b'{"standard_charge_information": [1,'),
                             "metadata-only")


class TestSummarizeGolden(unittest.TestCase):
    def check(self, fixture, golden, fn):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "summary.csv"
            self.assertTrue(fn(FIXTURES / fixture, out))
            self.assertEqual(out.read_bytes(), (GOLDEN / golden).read_bytes())

    def test_wide_csv(self):
        # Covers: multi-code fan-out (MS-DRG + CPT), internal types kept as
        # primary but not fanned out (CDM/RC), un-coded fallback row,
        # duplicate-slot dedupe, junk/zero prices, multi-line description.
        self.check("wide.csv", "wide_summary.csv", scrape.summarize_csv)

    def test_tall_csv(self):
        self.check("tall.csv", "tall_summary.csv", scrape.summarize_csv)

    @unittest.skipUnless(HAVE_IJSON, "ijson not installed")
    def test_v3_json_with_bom(self):
        self.check("v3.json", "v3_summary.csv", scrape.summarize_json)

    def test_aggregate_items_matches_json_golden(self):
        # The shard-rebuild path (aggregate_items over items) must agree
        # with the streaming path (summarize_json) on the same content.
        raw = (FIXTURES / "v3.json").read_bytes()
        doc = json.loads(raw.decode("utf-8-sig"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "summary.csv"
            self.assertTrue(scrape.write_summary(
                scrape.aggregate_items(doc["standard_charge_information"]), out))
            self.assertEqual(out.read_bytes(),
                             (GOLDEN / "v3_summary.csv").read_bytes())

    def test_missing_required_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "bad.csv"
            src.write_text("a,b\n1,2\ndescription,code|1\nX,470\n")  # no negotiated cols
            self.assertFalse(scrape.summarize_csv(src, Path(tmp) / "out.csv"))

    def test_row_order_does_not_matter(self):
        # v2: gross/cash are the median of the distinct values listed, so a
        # hospital reordering its rows cannot move the summary (or the
        # index). Records are regrouped so the multi-line row stays whole.
        lines = (FIXTURES / "wide.csv").read_text().split("\n")
        body = [r for r in scrape.csv_records(lines[3:]) if r]
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "reversed.csv"
            src.write_text("\n".join(lines[:3] + body[::-1]) + "\n")
            out = Path(tmp) / "summary.csv"
            self.assertTrue(scrape.summarize_csv(src, out))
            self.assertEqual(out.read_bytes(), (GOLDEN / "wide_summary.csv").read_bytes())

    def test_code_agg_pick(self):
        a = scrape.CodeAgg()
        self.assertIsNone(a.pick(a.gross))
        for v in (0.0, 200.0, 100.0, 200.0, -5.0):
            a.add(a.gross, v)
        self.assertEqual(a.pick(a.gross), 150.0)  # median of distinct positives
        z = scrape.CodeAgg()
        z.add(z.cash, 0.0)
        z.add(z.cash, None)
        self.assertEqual(z.pick(z.cash), 0.0)  # placeholders only

    def test_internal_type_matching(self):
        self.assertTrue(scrape.internal_type("local"))
        self.assertTrue(scrape.internal_type("R.C."))
        self.assertFalse(scrape.internal_type("HCPCS"))
        self.assertFalse(scrape.internal_type("MS-DRG"))


class TestDiscoverMrfUrl(unittest.TestCase):
    def fake_fetch(self, body):
        return lambda url, timeout=60, impersonate=False, max_time=None: (body, {})

    def test_scheme_less_url_fixed(self):
        # Rush publishes its mrf-url without a scheme.
        body = b"location-name: Rush\nmrf-url: example.org/standardcharges.csv\n"
        with unittest.mock.patch.object(scrape, "fetch_small", self.fake_fetch(body)):
            h = {"hpt_txt": "https://x/cms-hpt.txt", "location_name": "Rush"}
            self.assertEqual(scrape.discover_mrf_url(h, False),
                             "https://example.org/standardcharges.csv")

    def test_missing_location_returns_none(self):
        body = b"location-name: Other\nmrf-url: https://x/mrf.csv\n"
        with unittest.mock.patch.object(scrape, "fetch_small", self.fake_fetch(body)):
            h = {"hpt_txt": "https://x/cms-hpt.txt", "location_name": "Rush"}
            self.assertIsNone(scrape.discover_mrf_url(h, False))

    def test_html_block_page_raises(self):
        # A challenge page must surface as a fetch failure (so the fallback
        # ladder retries it), never as a "missing-from-hpt-txt" delisting.
        body = b"<!DOCTYPE html><html><body>Checking your browser</body></html>"
        with unittest.mock.patch.object(scrape, "fetch_small", self.fake_fetch(body)):
            h = {"hpt_txt": "https://x/cms-hpt.txt", "location_name": "Rush"}
            with self.assertRaises(ValueError):
                scrape.discover_mrf_url(h, False)


class TestEscalation(unittest.TestCase):
    def setUp(self):
        self._data = scrape.DATA
        self._tmp = tempfile.TemporaryDirectory()
        scrape.DATA = Path(self._tmp.name)

    def tearDown(self):
        scrape.DATA = self._data
        self._tmp.cleanup()

    def test_mark_and_read_round_trip(self):
        self.assertFalse(scrape.is_escalated("h1"))
        scrape.mark_escalated("h1")
        self.assertTrue(scrape.is_escalated("h1"))
        meta = json.loads((scrape.DATA / "h1" / "meta.json").read_text())
        self.assertIn("fetch_escalated", meta)

    def test_mark_preserves_existing_meta_and_timestamp(self):
        d = scrape.DATA / "h1"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(
            {"sha256": "abc", "fetch_escalated": "2026-01-01T00:00:00Z"}) + "\n")
        scrape.mark_escalated("h1")  # already marked: must not overwrite
        meta = json.loads((d / "meta.json").read_text())
        self.assertEqual(meta["fetch_escalated"], "2026-01-01T00:00:00Z")
        self.assertEqual(meta["sha256"], "abc")


class TestFilenameFrom(unittest.TestCase):
    def test_content_disposition(self):
        h = scrape.Headers([("Content-Disposition", 'attachment; filename="a b.csv"')])
        self.assertEqual(scrape.filename_from(h, "https://x/y", None), "a b.csv")
        h = scrape.Headers([("content-disposition", "attachment; filename*=UTF-8''pl%C3%A1n.json")])
        self.assertEqual(scrape.filename_from(h, "https://x/y", None), "plán.json")

    def test_url_fallback_and_sniffing(self):
        h = scrape.Headers([])
        self.assertEqual(scrape.filename_from(h, "https://x/charges.csv?sig=1", None),
                         "charges.csv")
        with tempfile.NamedTemporaryFile(suffix=".tmp") as f:
            f.write(b"\xef\xbb\xbf {\"a\": 1}")
            f.flush()
            self.assertEqual(
                scrape.filename_from(h, "https://x/index.php", Path(f.name)),
                "index.php.json")


class TestSkipEnv(unittest.TestCase):
    """The daily workflow SKIPs the hospitals local_refetch.py owns; the two
    lists are maintained by hand, so pin them to each other here."""

    def test_workflow_skip_matches_local_refetch(self):
        import re
        import local_refetch
        yml = (Path(__file__).parent.parent / ".github" / "workflows" / "scrape.yml").read_text()
        m = re.search(r"^\s*SKIP: (.+)$", yml, re.M)
        self.assertIsNotNone(m, "scrape.yml no longer sets SKIP")
        self.assertEqual(sorted(m.group(1).split(",")), sorted(local_refetch.LOCAL_ONLY))

    def test_workflow_wrapper_matches_local_refetch(self):
        import re
        import local_refetch
        yml = (Path(__file__).parent.parent / ".github" / "workflows" / "scrape.yml").read_text()
        m = re.search(r"^\s*CI_WRAPPER: (\S+)$", yml, re.M)
        self.assertIsNotNone(m, "scrape.yml no longer sets CI_WRAPPER")
        self.assertEqual(m.group(1), local_refetch.IMPERSONATE_WRAPPER)

    def test_skip_filters_unless_only(self):
        hospitals = [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}]
        slugs = lambda only, skip: [h["slug"] for h in scrape.select_hospitals(hospitals, only, skip)]
        self.assertEqual(slugs(None, "b,c"), ["a"])
        self.assertEqual(slugs("b", "b,c"), ["b"])  # ONLY wins over SKIP
        self.assertEqual(slugs(None, None), ["a", "b", "c"])


class TestMainSmoke(unittest.TestCase):
    """Run main() end to end with every hospital SKIPped: exercises the
    bookkeeping tail (commit_message.txt, changed_slugs.txt, the
    majority-failure guard) without any network."""

    def test_main_runs_with_everything_skipped(self):
        slugs = [h["slug"] for h in json.loads((Path(__file__).parent.parent / "hospitals.json").read_text())]
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.object(scrape, "ROOT", Path(d)), \
             unittest.mock.patch.dict("os.environ", {"SKIP": ",".join(slugs), "ONLY": ""}):
            (Path(d) / "hospitals.json").write_text(json.dumps([{"slug": s} for s in slugs]))
            scrape.main()
            self.assertEqual((Path(d) / "commit_message.txt").read_text(), "No changes\n")
            self.assertEqual((Path(d) / "changed_slugs.txt").read_text(), "")


class ProcessHarness(unittest.TestCase):
    """process() end to end with the network mocked out and data/ in a
    temp dir. Archiving is off unless a test turns it on."""

    HOSPITAL = {"slug": "h1", "system": "Sys", "location_name": "H1",
                "hpt_txt": "https://x/cms-hpt.txt"}
    URL = "https://x/charges.csv"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.scratch = root / "scratch"
        self.scratch.mkdir()
        self.patches = [
            unittest.mock.patch.object(scrape, "DATA", root / "data"),
            unittest.mock.patch.object(scrape, "RAW_DATA", root / "raw" / "data"),
            unittest.mock.patch.object(scrape, "REWRITTEN", []),
            unittest.mock.patch.object(scrape, "REFRESHED", []),
            unittest.mock.patch.object(scrape, "ARCHIVED", []),
            unittest.mock.patch.object(scrape, "ARCHIVE_FAILED", []),
            unittest.mock.patch.object(scrape, "BACKFILLED", []),
            unittest.mock.patch.object(scrape, "SUMMARY_WARNINGS", []),
            unittest.mock.patch.object(scrape, "discover_mrf_url", lambda h, imp: self.URL),
            unittest.mock.patch.object(scrape, "remote_fingerprint",
                                       lambda url, impersonate=False, max_time=None: None),
            unittest.mock.patch.dict("os.environ", {"MAX_DOWNLOAD_BYTES": "",
                                                    "IA_ARCHIVE": "0",
                                                    "BACKFILL_PER_RUN": ""}),
        ]
        for p in self.patches:
            p.start()
        self.body = (FIXTURES / "wide.csv").read_bytes()
        self.validators = {"Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT", "ETag": '"v1"'}
        self.downloads = 0

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def run_process(self, validators=None):
        hdrs = scrape.Headers(list((validators or self.validators).items()))

        def fake_download(url, dest, impersonate=False, max_time=None, limit=0):
            self.downloads += 1
            Path(dest).write_bytes(self.body)
            return hdrs
        with unittest.mock.patch.object(scrape, "head",
                                        lambda url, impersonate=False, max_time=None: hdrs), \
                unittest.mock.patch.object(scrape, "download_to", fake_download):
            return scrape.process(self.HOSPITAL, self.scratch)

    def meta(self):
        return json.loads((scrape.DATA / "h1" / "meta.json").read_text())

    def write_meta(self, meta):
        (scrape.DATA / "h1" / "meta.json").write_text(json.dumps(meta) + "\n")

    def summarized(self):
        # Route the fixture through the giant path: no payload in memory,
        # summary.csv only.
        return unittest.mock.patch.object(scrape, "MAX_SHARD_TOTAL", 10)


class TestProcess(ProcessHarness):
    """First snapshot, quiet validator refresh, crash safety of the swap."""

    def test_first_snapshot_stored_with_summary(self):
        msg = self.run_process()
        self.assertIn("first snapshot", msg)
        outdir = scrape.DATA / "h1"
        self.assertTrue((outdir / "standardcharges.csv").exists())
        self.assertEqual((outdir / "summary.csv").read_bytes(),
                         (GOLDEN / "wide_summary.csv").read_bytes())
        m = self.meta()
        self.assertEqual(m["status"], "stored")
        self.assertEqual(m["summary_version"], scrape.SUMMARY_VERSION)
        self.assertEqual(m["source_etag"], '"v1"')
        self.assertEqual(scrape.REWRITTEN, ["h1"])
        self.assertFalse((scrape.DATA / scrape.STAGING / "h1").exists())

    def test_validator_refresh_is_reported_not_silent(self):
        self.run_process()
        first = self.meta()
        # Same bytes, rotated validators: no content change, but meta.json
        # is rewritten and the run must say so.
        result = self.run_process({"Last-Modified": "Tue, 02 Jan 2026 00:00:00 GMT",
                                   "ETag": '"v2"'})
        self.assertIsNone(result)
        self.assertEqual(scrape.REFRESHED, ["h1"])
        self.assertEqual(scrape.REWRITTEN, ["h1"])  # not rewritten twice
        m = self.meta()
        self.assertEqual(m["source_etag"], '"v2"')
        self.assertEqual(m["last_changed"], first["last_changed"])
        self.assertEqual(m["sha256"], first["sha256"])

    def test_unchanged_validators_skip_download(self):
        self.run_process()
        with unittest.mock.patch.object(scrape, "download_to",
                                        side_effect=AssertionError("must not download")):
            hdrs = scrape.Headers(list(self.validators.items()))
            with unittest.mock.patch.object(scrape, "head",
                                            lambda url, impersonate=False, max_time=None: hdrs):
                self.assertIsNone(scrape.process(self.HOSPITAL, self.scratch))
        self.assertEqual(scrape.REFRESHED, [])

    def test_crash_mid_write_keeps_previous_snapshot(self):
        self.run_process()
        outdir = scrape.DATA / "h1"
        before = {p.name: p.read_bytes() for p in outdir.iterdir()}
        # New content routed to the sharded path, where the sharder dies
        # (disk full, OOM): the old snapshot must survive untouched.
        self.body = self.body.replace(b"470", b"471")
        with unittest.mock.patch.object(scrape, "MAX_STORED_BYTES", 10), \
                unittest.mock.patch.object(scrape, "store_sharded",
                                           side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_process({"ETag": '"v2"'})
        after = {p.name: p.read_bytes() for p in outdir.iterdir()}
        self.assertEqual(after, before)
        self.assertEqual(scrape.REWRITTEN, ["h1"])
        self.assertFalse((scrape.DATA / scrape.STAGING / "h1").exists())
        self.assertFalse((scrape.RAW_DATA / scrape.STAGING / "h1").exists())

    def test_sharded_snapshot_lands_in_raw_repo(self):
        with unittest.mock.patch.object(scrape, "MAX_STORED_BYTES", 10):
            msg = self.run_process()
        self.assertIn("sharded", msg)
        rawdir = scrape.RAW_DATA / "h1"
        self.assertTrue((rawdir / "_header.csv").exists())
        self.assertEqual(len(list((rawdir / "shards").glob("*.csv"))), scrape.SHARD_COUNT)
        self.assertFalse((scrape.DATA / "h1" / "standardcharges.csv").exists())
        self.assertTrue((scrape.DATA / "h1" / "summary.csv").exists())
        self.assertEqual(self.meta()["status"], "sharded")
        self.assertFalse((scrape.RAW_DATA / scrape.STAGING).exists() and
                         any((scrape.RAW_DATA / scrape.STAGING).iterdir()))


class TestColdStorage(ProcessHarness):
    """Summarized originals go to the Internet Archive: on capture, by
    backfill for hospitals captured earlier, with weekly retry on failure."""

    RECORD = {"url": "https://archive.org/details/x", "sha256": None,
              "compressed_bytes": 5, "archived": "2026-01-01T00:00:00Z"}

    def fake_cold_store(self, calls):
        def go(slug, path, sha, name=None):
            self.assertEqual(name, "charges.csv")  # the hospital's filename, not "download"
            calls.append((slug, path.read_bytes(), sha))
            return dict(self.RECORD, sha256=sha)
        return go

    def test_new_summarized_snapshot_is_archived(self):
        calls = []
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1"}), \
                unittest.mock.patch.object(scrape, "cold_store", self.fake_cold_store(calls)):
            msg = self.run_process()
        self.assertIn("summarized", msg)
        m = self.meta()
        self.assertEqual(calls, [("h1", self.body, m["sha256"])])  # the original bytes
        self.assertEqual(m["cold_storage"]["sha256"], m["sha256"])
        self.assertEqual(scrape.ARCHIVED, ["h1"])
        self.assertEqual(scrape.BACKFILLED, [])

    def test_archiving_off_leaves_meta_alone(self):
        with self.summarized():
            self.run_process()
        self.assertNotIn("cold_storage", self.meta())

    def test_backfill_downloads_only_to_archive(self):
        with self.summarized():
            self.run_process()  # captured before archiving existed
        before = self.meta()
        calls = []
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1"}), \
                unittest.mock.patch.object(scrape, "cold_store", self.fake_cold_store(calls)):
            self.assertIsNone(self.run_process())  # same validators: would normally skip
        self.assertEqual(self.downloads, 2)
        self.assertEqual(len(calls), 1)
        m = self.meta()
        self.assertEqual(m["cold_storage"]["sha256"], before["sha256"])
        self.assertEqual(m["last_changed"], before["last_changed"])
        self.assertEqual((scrape.REWRITTEN, scrape.REFRESHED, scrape.ARCHIVED, scrape.BACKFILLED),
                         (["h1"], [], ["h1"], ["h1"]))
        # Archived now: the next run is back to the cheap skip.
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1"}):
            self.assertIsNone(self.run_process())
        self.assertEqual(self.downloads, 2)

    def test_backfill_respects_cap_and_per_run_budget(self):
        with self.summarized():
            self.run_process()
        size = self.meta()["size_bytes"]
        with self.summarized(), unittest.mock.patch.dict(
                "os.environ", {"IA_ARCHIVE": "1", "MAX_DOWNLOAD_BYTES": str(size - 1)}):
            self.assertIsNone(self.run_process())  # over the cap: never fetched
        with self.summarized(), unittest.mock.patch.dict(
                "os.environ", {"IA_ARCHIVE": "1", "BACKFILL_PER_RUN": "0"}):
            self.assertIsNone(self.run_process())  # budget spent
        self.assertEqual(self.downloads, 1)
        self.assertNotIn("cold_storage", self.meta())

    def test_failed_upload_recorded_and_retried_weekly(self):
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1"}), \
                unittest.mock.patch.object(scrape, "cold_store",
                                           side_effect=RuntimeError("503 slow down")):
            msg = self.run_process()
        self.assertIn("summarized", msg)  # the snapshot itself is fine
        m = self.meta()
        self.assertNotIn("cold_storage", m)
        self.assertEqual(m["cold_storage_attempt"]["sha256"], m["sha256"])
        self.assertIn("slow down", m["cold_storage_attempt"]["error"])
        self.assertEqual(scrape.ARCHIVE_FAILED, ["h1"])
        # Recent failure: no re-download.
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1"}):
            self.assertIsNone(self.run_process())
        self.assertEqual(self.downloads, 1)
        # A week later: retried, and success clears the attempt record.
        m["cold_storage_attempt"]["at"] = "2026-01-01T00:00:00Z"
        self.write_meta(m)
        calls = []
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1"}), \
                unittest.mock.patch.object(scrape, "cold_store", self.fake_cold_store(calls)):
            self.assertIsNone(self.run_process())
        self.assertEqual((self.downloads, len(calls)), (2, 1))
        m = self.meta()
        self.assertIn("cold_storage", m)
        self.assertNotIn("cold_storage_attempt", m)

    def test_record_carried_when_only_url_moves(self):
        calls = []
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1"}), \
                unittest.mock.patch.object(scrape, "cold_store", self.fake_cold_store(calls)):
            self.run_process()
            with unittest.mock.patch.object(scrape, "discover_mrf_url",
                                            lambda h, imp: "https://x/moved.csv"):
                self.assertIn("updated", self.run_process())
        self.assertEqual(len(calls), 1)  # same bytes: not uploaded twice
        self.assertEqual(self.meta()["cold_storage"]["url"], self.RECORD["url"])

    def test_cold_store_commands(self):
        ran = []

        def fake_run(cmd, check=True, **kw):
            ran.append(cmd)
            if cmd[0] == "zstd":
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"zst!")
            return unittest.mock.Mock(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(scrape.subprocess, "run", fake_run):
            src = Path(tmp) / "download"
            src.write_bytes(b"a,b\n1,2\n")
            rec = scrape.cold_store("h1", src, "abcdef123456789", "Hospital_Charges.csv")
        zstd, ia = ran
        self.assertEqual((zstd[0], zstd[-1]), ("zstd", str(src)))
        self.assertEqual(ia[:4], [scrape.IA_BIN, "upload", "hospital-price-history-h1-abcdef123456",
                                  str(Path(tmp) / "Hospital_Charges.csv.zst")])
        self.assertIn("--checksum", ia)
        self.assertIn("--no-derive", ia)
        self.assertEqual(rec["sha256"], "abcdef123456789")
        self.assertEqual(rec["file_sha256"], scrape.hashlib.sha256(b"a,b\n1,2\n").hexdigest())
        self.assertEqual(rec["compressed_bytes"], 4)
        self.assertTrue(rec["url"].endswith("/hospital-price-history-h1-abcdef123456"))
        self.assertTrue(any(rec["file_sha256"] in a for a in ia))

    def test_archiving_enabled_switches(self):
        with unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "", "IA_ACCESS_KEY_ID": "",
                                                     "IA_SECRET_ACCESS_KEY": ""}):
            self.assertFalse(scrape.archiving_enabled())
        with unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "", "IA_ACCESS_KEY_ID": "k",
                                                     "IA_SECRET_ACCESS_KEY": "s"}):
            self.assertTrue(scrape.archiving_enabled())
        with unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "0", "IA_ACCESS_KEY_ID": "k",
                                                     "IA_SECRET_ACCESS_KEY": "s"}):
            self.assertFalse(scrape.archiving_enabled())
        with unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": "1", "IA_ACCESS_KEY_ID": "",
                                                     "IA_SECRET_ACCESS_KEY": ""}):
            self.assertTrue(scrape.archiving_enabled())


class TestSummaryUpgrade(ProcessHarness):
    """A summarized hospital's content exists nowhere but the source, so a
    SUMMARY_VERSION bump is honored by re-downloading it within the same
    per-run backfill budget cold storage uses."""

    def test_stale_summary_rebuilt_by_backfill(self):
        with self.summarized():
            self.run_process()
        m = self.meta()
        m["summary_version"] = scrape.SUMMARY_VERSION - 1
        self.write_meta(m)
        (scrape.DATA / "h1" / "summary.csv").write_text("stale\n")
        with self.summarized():
            msg = self.run_process()  # same validators: would normally skip
        self.assertIn("backfilled summary", msg)
        self.assertEqual(self.downloads, 2)
        self.assertEqual(scrape.BACKFILLED, ["h1"])
        self.assertEqual((scrape.DATA / "h1" / "summary.csv").read_bytes(),
                         (GOLDEN / "wide_summary.csv").read_bytes())
        m2 = self.meta()
        self.assertEqual(m2["summary_version"], scrape.SUMMARY_VERSION)
        self.assertEqual(m2["last_changed"], m["last_changed"])  # not a content change

    def test_current_summary_not_refetched(self):
        with self.summarized():
            self.run_process()
            self.assertIsNone(self.run_process())
        self.assertEqual(self.downloads, 1)

    def test_upgrade_shares_the_backfill_budget(self):
        with self.summarized():
            self.run_process()
        m = self.meta()
        m["summary_version"] = scrape.SUMMARY_VERSION - 1
        self.write_meta(m)
        with self.summarized(), unittest.mock.patch.dict("os.environ", {"BACKFILL_PER_RUN": "0"}):
            self.assertIsNone(self.run_process())
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.meta()["summary_version"], scrape.SUMMARY_VERSION - 1)


class TestSummaryCheck(ProcessHarness):
    """A new summary far smaller than the one it replaces is recorded but
    flagged; the flag persists while the file is unchanged and clears on
    the next snapshot that passes."""

    def test_stats_and_regression_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.csv"
            self.assertIsNone(scrape.summary_stats(p))
            scrape.summarize_csv(FIXTURES / "wide.csv", p)
            self.assertEqual(scrape.summary_stats(p), (9, 8))  # the aspirin row is un-coded
        reg = scrape.summary_regression
        self.assertIsNone(reg(None, (5, 5)))                      # first snapshot
        self.assertIsNone(reg((1000, 900), (600, 500)))           # 60%: a real change
        self.assertEqual(reg((1000, 900), (400, 300)), "rows 1,000 -> 400")
        self.assertEqual(reg((1000, 900), (950, 100)), "coded rows 900 -> 100")
        self.assertIsNone(reg((50, 50), (1, 1)))                  # too small to judge
        self.assertEqual(reg((1000, 900), None), "summary lost (was 1,000 rows)")

    def test_shrunken_file_flagged_then_cleared(self):
        full = self.body
        self.run_process()
        lines = full.decode().split("\n")
        body = [r for r in scrape.csv_records(lines[3:]) if r]
        self.body = ("\n".join(lines[:3] + body[:1]) + "\n").encode()  # one record survives
        with unittest.mock.patch.object(scrape, "SUMMARY_CHECK_MIN_ROWS", 1):
            msg = self.run_process({"ETag": '"v2"'})
        self.assertIn("updated", msg)  # recorded as published, not refused
        m = self.meta()
        self.assertEqual(m["summary_warning"]["detail"], "rows 9 -> 2")
        self.assertEqual(scrape.SUMMARY_WARNINGS, ["h1 (rows 9 -> 2)"])
        # Same file again: the cheap skip leaves the flag standing.
        self.assertIsNone(self.run_process({"ETag": '"v2"'}))
        self.assertIn("summary_warning", self.meta())
        # A full file again clears it without anyone touching meta.json.
        self.body = full
        with unittest.mock.patch.object(scrape, "SUMMARY_CHECK_MIN_ROWS", 1):
            self.run_process({"ETag": '"v3"'})
        self.assertNotIn("summary_warning", self.meta())
        self.assertEqual(scrape.SUMMARY_WARNINGS, ["h1 (rows 9 -> 2)"])  # nothing new

    def test_first_snapshot_never_flagged(self):
        with unittest.mock.patch.object(scrape, "SUMMARY_CHECK_MIN_ROWS", 1):
            self.run_process()
        self.assertNotIn("summary_warning", self.meta())
        self.assertEqual(scrape.SUMMARY_WARNINGS, [])


class TestHeartbeat(unittest.TestCase):
    def test_workflow_checks_the_tag_local_refetch_pushes(self):
        import local_refetch
        yml = (Path(__file__).parent.parent / ".github" / "workflows" / "pi-heartbeat.yml").read_text()
        self.assertIn(f"git/ref/tags/{local_refetch.HEARTBEAT_TAG}", yml)

    def test_importing_local_refetch_does_not_switch_archiving_on(self):
        # Detection of credentials happens in main(), never at import: the
        # tests import the module and must not start uploading.
        with unittest.mock.patch.dict("os.environ", {"IA_ARCHIVE": ""}):
            import importlib
            import local_refetch
            importlib.reload(local_refetch)
            self.assertEqual(__import__("os").environ.get("IA_ARCHIVE"), "")
            self.assertTrue(local_refetch.find_ia_bin() is None
                            or Path(local_refetch.find_ia_bin()).exists())


class TestDownloadCap(unittest.TestCase):
    def test_plain_download_aborts_past_limit(self):
        class Resp(io.BytesIO):
            headers = {"Content-Type": "text/csv"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(scrape, "request",
                                           lambda url, **kw: Resp(b"x" * 3000)):
            dest = Path(tmp) / "dl"
            with self.assertRaises(ValueError):
                scrape.download_to("https://x/f", dest, limit=1000)
            self.assertEqual(scrape.download_to("https://x/f", dest, limit=0)
                             .get("Content-Type"), "text/csv")
            self.assertEqual(dest.stat().st_size, 3000)

    def test_zip_unpacked_size_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "f.zip"
            with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("charges.csv", "a,b\n" * 1000)  # 4000 B, tiny zipped
            hdrs = scrape.Headers([("Content-Type", "application/zip")])
            with self.assertRaises(ValueError):
                scrape.materialize(z, hdrs, "https://x/f.zip", Path(tmp), limit=1000)
            name, path = scrape.materialize(z, hdrs, "https://x/f.zip", Path(tmp), limit=10000)
            self.assertEqual((name, path.stat().st_size), ("charges.csv", 4000))


if __name__ == "__main__":
    unittest.main()
