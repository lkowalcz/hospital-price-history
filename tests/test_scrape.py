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


if __name__ == "__main__":
    unittest.main()
