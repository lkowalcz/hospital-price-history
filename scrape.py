#!/usr/bin/env python3
"""Git-scrape hospital price transparency machine-readable files (MRFs).

For each hospital in hospitals.json:
  1. Fetch the CMS-mandated /cms-hpt.txt discovery file and find the block
     matching `location_name` to get the current MRF URL.
  2. HEAD the MRF; if Last-Modified/ETag match the stored meta, skip it.
  3. Otherwise download (unzipping if needed), normalize, and — when the
     content hash changed — write it to data/<slug>/.

Storage modes, chosen automatically by size:
  - stored:   single normalized file under data/<slug>/
  - sharded:  content split into SHARD_COUNT hash-bucketed, sorted shard
              files (CSV rows or JSONL items), each git-sized. A changed row
              touches only its bucket, so diffs stay meaningful.
  - metadata-only: unparseable giants; meta.json still records hash/size/
              timing on every change.

Stdlib only. Designed to run under GitHub Actions on a cron schedule, with
`git commit` happening in the workflow only when something changed.
"""

import csv
import hashlib
import io
import json
import shutil
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MAX_STORED_BYTES = 45 * 1024 * 1024   # above this, shard
MAX_SHARD_TOTAL = 600 * 1024 * 1024   # above this, metadata-only (repo size budget)
SHARD_COUNT = 32
# No-validator servers force a full download just to detect change; for big
# files that's too expensive daily, so refetch those only on Sundays.
WEEKLY_FETCH_BYTES = 400 * 1024 * 1024

# Several hospital CDNs (Cloudflare, Akamai) refuse non-browser User-Agents,
# despite 45 CFR 180.50 requiring these files be accessible to automated
# searches. A browser UA recovers most of them.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def request(url, method="GET", timeout=300):
    req = urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp


def fetch(url, timeout=300):
    with request(url, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def head(url):
    try:
        with request(url, method="HEAD", timeout=60) as resp:
            return dict(resp.headers)
    except Exception:
        return {}  # no HEAD support; fall through to GET


def normalize_name(s):
    return s.replace("’", "'").replace("‘", "'").strip().casefold()


def parse_hpt_txt(text):
    """Parse cms-hpt.txt into a list of {key: value} blocks."""
    blocks, current = [], {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current:
                blocks.append(current)
                current = {}
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current[key.strip().casefold()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def discover_mrf_url(hospital):
    raw, _ = fetch(hospital["hpt_txt"], timeout=30)
    wanted = normalize_name(hospital["location_name"])
    for block in parse_hpt_txt(raw.decode("utf-8", errors="replace")):
        if normalize_name(block.get("location-name", "")) == wanted:
            return block.get("mrf-url")
    return None


def extract_payload(body, headers, url):
    """Return (filename, bytes) of the actual MRF, unzipping if necessary."""
    content_type = headers.get("Content-Type", "")
    is_zip = body[:4] == b"PK\x03\x04" or "zip" in content_type
    if not is_zip:
        name = url.split("?")[0].rsplit("/", 1)[-1] or "standardcharges"
        return name, body
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = [
            n for n in zf.namelist()
            if not n.endswith("/")
            and not n.startswith("__MACOSX")
            and not Path(n).name.startswith("._")
        ]
        if len(names) != 1:
            raise ValueError(f"expected 1 file in zip, found {names}")
        return Path(names[0]).name, zf.read(names[0])


def normalize_payload(name, payload):
    """Stable representation so diffs reflect real changes, not formatting."""
    lower = name.casefold()
    if lower.endswith(".json") and len(payload) <= MAX_STORED_BYTES:
        try:
            obj = json.loads(payload)
            return json.dumps(obj, indent=1, sort_keys=True).encode() + b"\n"
        except (ValueError, MemoryError):
            return payload
    if lower.endswith((".csv", ".txt")):
        return payload.replace(b"\r\n", b"\n")
    return payload


def ext_of(name):
    suffix = Path(name).suffix.casefold()
    return suffix if suffix in (".csv", ".json", ".xml", ".txt") else ".bin"


def bucket_of(line):
    return int.from_bytes(hashlib.sha1(line.encode()).digest()[:4], "big") % SHARD_COUNT


def shard_lines(lines):
    buckets = [[] for _ in range(SHARD_COUNT)]
    for line in lines:
        buckets[bucket_of(line)].append(line)
    for bucket in buckets:
        bucket.sort()
    return buckets


def write_shards(outdir, header_name, header_text, buckets, ext):
    shard_dir = outdir / "shards"
    shard_dir.mkdir(exist_ok=True)
    (outdir / header_name).write_text(header_text)
    for i, bucket in enumerate(buckets):
        (shard_dir / f"{i:02d}{ext}").write_text(
            "\n".join(bucket) + ("\n" if bucket else "")
        )


def store_sharded(outdir, name, payload):
    """Split an oversized MRF into hash-bucketed sorted shards. Returns mode."""
    lower = name.casefold()
    if lower.endswith(".csv"):
        text = payload.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # CMS v3 CSV: rows 1-2 are attestation metadata, row 3 is the column
        # header; keep them whole so shard rows share a documented schema.
        reader = csv.reader(io.StringIO(text))
        header_rows = []
        for _ in range(3):
            try:
                header_rows.append(next(reader))
            except StopIteration:
                break
        header_line_count = reader.line_num
        header_text = "\n".join(lines[:header_line_count]) + "\n"
        body = [l for l in lines[header_line_count:] if l]
        write_shards(outdir, "_header.csv", header_text, shard_lines(body), ".csv")
        return "sharded"
    if lower.endswith(".json"):
        try:
            obj = json.loads(payload)
        except (ValueError, MemoryError):
            return "metadata-only"
        if not isinstance(obj, dict):
            return "metadata-only"
        items = obj.pop("standard_charge_information", None)
        if not isinstance(items, list):
            return "metadata-only"
        header_text = json.dumps(obj, indent=1, sort_keys=True) + "\n"
        lines = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in items]
        write_shards(outdir, "_header.json", header_text, shard_lines(lines), ".jsonl")
        return "sharded"
    return "metadata-only"


def clear_content(outdir):
    for path in outdir.iterdir():
        if path.name == "meta.json":
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process(hospital):
    slug = hospital["slug"]
    outdir = DATA / slug
    outdir.mkdir(parents=True, exist_ok=True)
    meta_path = outdir / "meta.json"
    old_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    mrf_url = discover_mrf_url(hospital)
    if not mrf_url:
        # Losing the listing is itself signal; record it without erasing data.
        if old_meta.get("status") != "missing-from-hpt-txt":
            old_meta["status"] = "missing-from-hpt-txt"
            old_meta["status_changed"] = utcnow()
            meta_path.write_text(json.dumps(old_meta, indent=2) + "\n")
            return f"{slug}: MRF no longer listed in cms-hpt.txt"
        return None

    # Cheap skip: if the server's validators match what we stored, don't
    # re-download (matters when tracking dozens of multi-hundred-MB files).
    if mrf_url == old_meta.get("mrf_url") and old_meta.get("sha256"):
        h = head(mrf_url)
        validators = (h.get("Last-Modified"), h.get("ETag"))
        stored = (old_meta.get("source_last_modified"), old_meta.get("source_etag"))
        if any(validators) and validators == stored:
            return None
        if (
            not any(stored)
            and old_meta.get("size_bytes", 0) > WEEKLY_FETCH_BYTES
            and datetime.now(timezone.utc).weekday() != 6
        ):
            return None  # big no-validator file: full refetch Sundays only

    body, headers = fetch(mrf_url)
    name, payload = extract_payload(body, headers, mrf_url)
    payload = normalize_payload(name, payload)
    sha = hashlib.sha256(payload).hexdigest()

    changed = sha != old_meta.get("sha256") or mrf_url != old_meta.get("mrf_url")
    if not changed:
        # Content identical but validators rotated; refresh them quietly so
        # the HEAD short-circuit works next run.
        old_meta["source_last_modified"] = headers.get("Last-Modified")
        old_meta["source_etag"] = headers.get("ETag")
        meta_path.write_text(json.dumps(old_meta, indent=2) + "\n")
        return None

    clear_content(outdir)
    if len(payload) <= MAX_STORED_BYTES:
        (outdir / f"standardcharges{ext_of(name)}").write_bytes(payload)
        mode = "stored"
    elif len(payload) <= MAX_SHARD_TOTAL:
        mode = store_sharded(outdir, name, payload)
    else:
        mode = "metadata-only"

    meta = {
        "system": hospital["system"],
        "location_name": hospital["location_name"],
        "mrf_url": mrf_url,
        "source_filename": name,
        "sha256": sha,
        "size_bytes": len(payload),
        "source_last_modified": headers.get("Last-Modified"),
        "source_etag": headers.get("ETag"),
        "status": mode,
        "first_seen": old_meta.get("first_seen") or utcnow(),
        "last_changed": utcnow(),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    verb = "updated" if old_meta.get("sha256") else "first snapshot"
    return f"{slug}: {verb} ({len(payload):,} bytes, {mode})"


def main():
    hospitals = json.loads((ROOT / "hospitals.json").read_text())
    changes, failures = [], []
    for hospital in hospitals:
        try:
            result = process(hospital)
            if result:
                changes.append(result)
                print(result)
            else:
                print(f"{hospital['slug']}: unchanged")
        except Exception as exc:  # one bad hospital shouldn't sink the run
            failures.append(f"{hospital['slug']}: {exc}")
            print(f"{hospital['slug']}: ERROR {exc}", file=sys.stderr)

    (ROOT / "commit_message.txt").write_text(
        "Update price files: " + "; ".join(c.split(":")[0] for c in changes) + "\n"
        if changes
        else "No changes\n"
    )
    if failures and not changes:
        sys.exit(1)


if __name__ == "__main__":
    main()
