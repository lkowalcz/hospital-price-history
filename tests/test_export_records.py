import csv
import gzip
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import export_records as exporter

FIXTURES = Path(__file__).parent / "fixtures"


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "export"

    def read_export(self):
        manifest = json.loads((self.output / "manifest.json").read_text())
        records = []
        for part in manifest["partitions"]:
            path = self.output / part["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), part["sha256"])
            with gzip.open(path, "rt") as handle:
                rows = [json.loads(line, parse_float=Decimal) for line in handle]
            self.assertEqual(len(rows), part["records"])
            records.extend(rows)
        return manifest, records

    def test_csv_roundtrip_retains_all_cells_codes_and_multiline_locators(self):
        source = FIXTURES / "wide.csv"
        exporter.export(source, self.output, input_kind="original", partition_rows=2)
        manifest, records = self.read_export()
        with source.open(newline="") as handle:
            rows = list(csv.reader(handle))
        metadata = json.loads((self.output / "metadata.json").read_text())
        self.assertEqual(metadata["metadata_rows"], rows[:2])
        self.assertEqual(metadata["columns"], rows[2])
        self.assertEqual([r["source_record"]["cells"] for r in records], rows[3:])
        self.assertEqual(manifest["records"], len(rows) - 3)
        self.assertEqual([c["code"] for c in records[0]["codes"]], ["470", "27447", ""])
        multiline = next(r for r in records if "\n" in r["description"])
        self.assertGreater(multiline["locator"]["line_end"], multiline["locator"]["line_start"])
        self.assertEqual(records[0]["charge_groups"][0]["rates"][0]["dollar"], "25000")
        self.assertEqual(records[0]["charge_groups"][0]["rates"][0]["payer_name"], "Acme Health")

    def test_tall_retains_percentage_only_plan_and_blank_dollar(self):
        exporter.export(FIXTURES / "tall.csv", self.output)
        _, rows = self.read_export()
        rate = rows[-1]["charge_groups"][0]["rates"][0]
        self.assertEqual(rate["payer_name"], "Blue Sky")
        self.assertEqual(rate["plan_name"], "HMO")
        self.assertEqual(rate["dollar"], "")
        self.assertEqual(rate["methodology"], "percent of charge")
        self.assertIsNone(rate["percentage"])  # absent column differs from blank cell

    def test_json_roundtrip_and_multiple_charge_groups(self):
        source = FIXTURES / "v3.json"
        exporter.export(source, self.output)
        _, records = self.read_export()
        original = json.loads(source.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        self.assertEqual([r["source_record"] for r in records], original.pop("standard_charge_information"))
        metadata = json.loads((self.output / "metadata.json").read_text())
        self.assertEqual(metadata["fields"], original)
        self.assertEqual(len(records[-1]["charge_groups"]), 2)
        self.assertEqual(records[1]["charge_groups"][0]["rates"][0]["percentage"], 80)
        self.assertIsNone(records[1]["charge_groups"][0]["rates"][0]["dollar"])

    def test_unknown_properties_and_precise_numbers_survive(self):
        source = self.root / "precise.json"
        source.write_text('''{"hospital_name":"Example","standard_charge_information":[
          {"description":"Example", "extra":{"number":0.12345678901234567890123456789},
           "code_information":[{"code":"0000123","type":"LOCAL"}],
           "standard_charges":[{"setting":"outpatient","payers_information":[
             {"payer_name":"A","plan_name":"B","standard_charge_algorithm":"x * y",
              "median_amount":12345678901234567890.123456789,"count":"1 through 10"}]}]}],
          "general_contract_provisions":[{"provisions":"lesser of billed charges"}]}''')
        exporter.export(source, self.output)
        _, records = self.read_export()
        original = json.loads(source.read_text(), parse_float=Decimal)
        self.assertEqual(records[0]["source_record"], original["standard_charge_information"][0])
        rate = records[0]["charge_groups"][0]["rates"][0]
        self.assertEqual(rate["median_allowed_amount"], Decimal("12345678901234567890.123456789"))
        self.assertEqual(rate["allowed_amount_count"], "1 through 10")
        self.assertEqual(records[0]["codes"][0]["code"], "0000123")
        metadata = json.loads((self.output / "metadata.json").read_text())
        self.assertEqual(metadata["fields"]["general_contract_provisions"], original["general_contract_provisions"])

    def test_duplicate_columns_and_short_rows_remain_explicit(self):
        source = self.root / "odd.csv"
        source.write_text('hospital_name\nExample\ndescription,code|1,code|1|type,custom,custom\nx,001,LOCAL,a,b\ny\n')
        exporter.export(source, self.output)
        _, records = self.read_export()
        self.assertEqual(records[0]["source_record"]["cells"][-2:], ["a", "b"])
        self.assertIn("duplicate_columns", records[0]["warnings"])
        self.assertIn("row_width_mismatch", records[1]["warnings"])
        self.assertIsNone(records[1]["codes"][0]["code"])

    def test_malformed_or_ambiguous_json_never_publishes_partial_export(self):
        for text in ['{"standard_charge_information":[{"description":"x"},broken]}',
                     '{"standard_charge_information":[{"description":"x","description":"y"}]}']:
            source = self.root / "broken.json"
            source.write_text(text)
            with self.assertRaises(Exception):
                exporter.export(source, self.output)
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.root.glob(".export-*")))

    def test_export_is_deterministic_and_will_not_overwrite_existing_directory(self):
        source = FIXTURES / "tall.csv"
        exporter.export(source, self.output, partition_rows=2)
        other = self.root / "other"
        exporter.export(source, other, partition_rows=2)
        for path in self.output.iterdir():
            self.assertEqual(path.read_bytes(), (other / path.name).read_bytes())
        with self.assertRaisesRegex(ValueError, "already exists"):
            exporter.export(source, self.output)


if __name__ == "__main__":
    unittest.main()
