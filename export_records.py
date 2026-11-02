"""Pilot detailed exports from CMS tall/wide CSV or JSON MRFs.

One output record per input service record, with linked codes and charge groups;
no aggregation, code selection, price filtering, or inferred payer identities.
Input cells/objects and file metadata remain alongside the convenient fields.
"""

import argparse
import csv
import gzip
import json
import os
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from preserve_source import digest, write_json

SCHEMA_VERSION = 1
PARSER_VERSION = "1.0.0-pilot"
RATE_FIELDS = {"dollar": ("negotiated_dollar", "standard_charge_dollar"),
               "percentage": ("negotiated_percentage", "standard_charge_percentage"),
               "algorithm": ("negotiated_algorithm", "standard_charge_algorithm"),
               "methodology": ("methodology", "methodology"),
               "median_allowed_amount": ("median_amount", "median_amount"),
               "allowed_amount_10th_percentile": ("10th_percentile", "10th_percentile"),
               "allowed_amount_90th_percentile": ("90th_percentile", "90th_percentile"),
               "allowed_amount_count": ("count", "count"),
               "estimated_amount": ("estimated_amount", "estimated_amount")}


def encode(value):
    """JSON with exact Decimal values; never round a source number through float."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite JSON number")
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + encode(v)
                              for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(encode, value)) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class UniqueKeys(dict):
    def __setitem__(self, key, value):
        if key in self:
            raise ValueError(f"Duplicate JSON property: {key!r}; use the preserved original")
        super().__setitem__(key, value)


def json_stream(path):
    handle = Path(path).open("rb")
    if handle.read(3) != b"\xef\xbb\xbf":
        handle.seek(0)
    return handle


def json_metadata(path):
    import ijson
    from ijson.common import ObjectBuilder
    builder = ObjectBuilder(map_type=UniqueKeys)
    found = False
    skip_next, skipping = False, 0
    with json_stream(path) as handle:
        for prefix, event, value in ijson.parse(handle):
            if skip_next:
                if event != "start_array":
                    raise ValueError("standard_charge_information must be an array")
                skip_next, skipping = False, 1
                continue
            if skipping:
                if event in ("start_array", "start_map"):
                    skipping += 1
                elif event in ("end_array", "end_map"):
                    skipping -= 1
                continue
            if prefix == "" and event == "map_key" and value == "standard_charge_information":
                if found:
                    raise ValueError("Duplicate standard_charge_information array")
                found = skip_next = True
                continue
            builder.event(event, value)
    if not found or not isinstance(builder.value, dict):
        raise ValueError("Expected CMS JSON with standard_charge_information")
    return {"format": "cms_json", "fields": builder.value}


def json_records(path):
    import ijson
    with json_stream(path) as handle:
        for index, item in enumerate(ijson.items(handle, "standard_charge_information.item", map_type=UniqueKeys)):
            if not isinstance(item, dict):
                raise ValueError(f"Service record {index} is not an object")
            codes = item.get("code_information", [])
            charges = []
            for position, charge in enumerate(item.get("standard_charges", [])):
                rates = []
                for payer in charge.get("payers_information", []):
                    rates.append({"payer_name": payer.get("payer_name"), "plan_name": payer.get("plan_name"),
                                  **{name: payer.get(keys[1]) for name, keys in RATE_FIELDS.items()},
                                  "notes": payer.get("additional_payer_notes")})
                charges.append({"source_charge_index": position, "setting": charge.get("setting"),
                                "billing_class": charge.get("billing_class", item.get("billing_class")),
                                "gross_charge": charge.get("gross_charge"),
                                "discounted_cash": charge.get("discounted_cash"),
                                "minimum": charge.get("minimum"), "maximum": charge.get("maximum"),
                                "rates": rates})
            yield {"locator": {"kind": "json_pointer", "pointer": f"/standard_charge_information/{index}"},
                   "description": item.get("description"), "codes": codes,
                   "modifiers": item.get("modifiers"), "drug_information": item.get("drug_information"),
                   "notes": item.get("additional_generic_notes"), "charge_groups": charges,
                   "source_record": item, "warnings": []}


def csv_metadata(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        rows = [next(reader) for _ in range(3)]
    columns = rows[2]
    if not any(re.fullmatch(r"code\|\d+", c.strip().lower()) for c in columns):
        raise ValueError("Expected CMS CSV metadata rows and code|N columns")
    return {"format": "cms_csv", "metadata_rows": rows[:2], "columns": columns}


def csv_records(path, metadata):
    columns = metadata["columns"]
    positions = {}
    for index, column in enumerate(columns):
        positions.setdefault(column.strip().lower(), []).append(index)
    duplicate = any(len(p) > 1 for p in positions.values())
    slots = sorted({int(m.group(1)) for c in positions if (m := re.fullmatch(r"code\|(\d+)", c))})
    payers = []
    for column in columns:
        parts = column.split("|")
        if len(parts) == 4 and parts[0].strip().lower() == "standard_charge":
            pair = (parts[1], parts[2])
            if pair not in payers:
                payers.append(pair)

    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        for _ in range(3):
            next(reader)
        last_line = reader.line_num
        for ordinal, cells in enumerate(reader, 4):
            def get(name):
                indices = positions.get(name.lower(), [])
                # Missing/ambiguous/truncated cells are never invented. source_record
                # keeps every cell; duplicate columns receive an explicit warning.
                return cells[indices[0]] if len(indices) == 1 and indices[0] < len(cells) else None

            rates = []
            if "payer_name" in positions or "plan_name" in positions:
                rates.append({"payer_name": get("payer_name"), "plan_name": get("plan_name"),
                              **{name: get(("standard_charge|" if keys[0].startswith("negotiated_")
                                            or keys[0] == "methodology" else "") + keys[0])
                                 for name, keys in RATE_FIELDS.items()},
                              "notes": get("additional_payer_notes")})
            else:
                for payer, plan in payers:
                    prefix = f"standard_charge|{payer}|{plan}|"
                    rates.append({"payer_name": payer, "plan_name": plan,
                                  **{name: get(prefix + keys[0] if keys[0].startswith("negotiated_")
                                               or keys[0] == "methodology" else f"{keys[0]}|{payer}|{plan}")
                                     for name, keys in RATE_FIELDS.items()},
                                  "notes": get(f"additional_payer_notes|{payer}|{plan}")})
            record = {"locator": {"kind": "csv_record", "record": ordinal,
                                   "line_start": last_line + 1, "line_end": reader.line_num},
                      "description": get("description"),
                      "codes": [{"source_slot": slot, "code": get(f"code|{slot}"),
                                 "type": get(f"code|{slot}|type")} for slot in slots],
                      "modifiers": get("modifiers"),
                      "drug_information": {"unit": get("drug_unit_of_measurement"),
                                           "type": get("drug_type_of_measurement")},
                      "notes": get("additional_generic_notes"),
                      "charge_groups": [{"setting": get("setting"), "billing_class": get("billing_class"),
                                         "gross_charge": get("standard_charge|gross"),
                                         "discounted_cash": get("standard_charge|discounted_cash"),
                                         "minimum": get("standard_charge|min"), "maximum": get("standard_charge|max"),
                                         "rates": rates}],
                      "source_record": {"cells": cells},
                      "warnings": (["duplicate_columns"] if duplicate else []) +
                                  (["row_width_mismatch"] if len(cells) != len(columns) else [])}
            last_line = reader.line_num
            yield record


def export(path, output, *, source_url=None, snapshot=None, input_kind="unspecified", partition_rows=5000):
    """Publish only complete exports; memory is bounded by the largest record."""
    path, output = Path(path), Path(output)
    if partition_rows < 1:
        raise ValueError("partition_rows must be positive")
    if output.exists():
        raise ValueError("Output already exists; choose a new directory")
    csv.field_size_limit(10**9)
    sha = digest(path)
    with path.open("rb") as handle:
        is_json = handle.read(4096).lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"{")
    metadata = json_metadata(path) if is_json else csv_metadata(path)
    records = json_records(path) if is_json else csv_records(path, metadata)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".export-", dir=output.parent))
    parts, count, warnings = [], 0, 0
    raw = compressed = None
    try:
        (stage / "metadata.json").write_text(encode(metadata) + "\n", encoding="utf-8")
        for record in records:
            if count % partition_rows == 0:
                if compressed:
                    compressed.close()
                    raw.close()
                filename = f"records-{len(parts):05d}.jsonl.gz"
                raw = (stage / filename).open("wb")
                compressed = gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0)
                parts.append({"path": filename, "records": 0})
            record.update(schema_version=SCHEMA_VERSION, parser_version=PARSER_VERSION,
                          source_sha256=sha, record_id=f"{sha}:{count}")
            compressed.write((encode(record) + "\n").encode("utf-8"))
            count += 1
            warnings += bool(record["warnings"])
            parts[-1]["records"] += 1
        if compressed:
            compressed.close()
            raw.close()
            compressed = raw = None
        if digest(path) != sha:
            raise ValueError("Input changed during export")
        for part in parts:
            file = stage / part["path"]
            part.update(sha256=digest(file), bytes=file.stat().st_size)
        manifest = {"schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
                    "stability": "pilot", "source_sha256": sha, "source_bytes": path.stat().st_size,
                    "source_filename": path.name, "source_url": source_url, "snapshot_id": snapshot,
                    "input_kind": input_kind, "format": metadata["format"],
                    "records": count, "records_with_warnings": warnings,
                    "metadata": {"path": "metadata.json", "sha256": digest(stage / "metadata.json")},
                    "partitions": parts}
        write_json(stage / "manifest.json", manifest)
        os.replace(stage, output)
        return manifest
    finally:
        if compressed:
            compressed.close()
        if raw:
            raw.close()
        shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-url")
    parser.add_argument("--snapshot")
    parser.add_argument("--input-kind", choices=["original", "normalized", "reconstructed", "unspecified"], default="unspecified")
    parser.add_argument("--partition-rows", type=int, default=5000)
    args = parser.parse_args()
    manifest = export(args.source, args.output, source_url=args.source_url, snapshot=args.snapshot,
                      input_kind=args.input_kind, partition_rows=args.partition_rows)
    print(f"Exported {manifest['records']} records in {len(manifest['partitions'])} partitions to {args.output}")


if __name__ == "__main__":
    main()
