"""Golden-file and unit tests for the scrape pipeline.

Fixtures in tests/fixtures/ are small CMS-v3-shaped MRFs that bake in the
pathologies met in the wild (BOMs, UTF-16, multi-line quoted CSV records,
multi-code rows, hospital-internal code types, blank-line hpt files).
Goldens in tests/golden/ are the expected summary.csv bytes; a change there
is a deliberate methodology change and should be reviewed as one.
"""

import csv
import json
import tempfile
import unittest
import unittest.mock
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


if __name__ == "__main__":
    unittest.main()
