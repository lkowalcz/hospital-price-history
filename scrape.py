#!/usr/bin/env python3
"""Git-scrape hospital price transparency machine-readable files (MRFs).

For each hospital in hospitals.json:
  1. Fetch the CMS-mandated /cms-hpt.txt discovery file and find the block
     matching `location_name` to get the current MRF URL.
  2. Detect change cheaply: HEAD Last-Modified/ETag when the server provides
     them, else a fingerprint hashed from ~1 MB Range-request samples at five
     fixed offsets. Full downloads happen only when something moved (with a
     weekly Sunday backstop for servers that support neither).
  3. Download (streamed to disk, unzipped if needed), normalize, and — when
     the content hash changed — write to data/<slug>/.

Storage modes, chosen automatically by size:
  - stored:     single normalized file (<= 45 MB)
  - sharded:    32 hash-bucketed, sorted shard files (<= 600 MB); a changed
                row touches only its bucket, so diffs stay meaningful
  - summarized: larger files are stream-parsed (CMS v3 schema) into
                summary.csv — per code: description, gross charge, cash
                price, min/max negotiated rate, payer count
  - metadata-only: unparseable; meta.json hash/size/timing still recorded

Stdlib except `ijson` (streaming JSON parser), needed only for summarizing
multi-GB JSON files; everything else degrades gracefully without it.
"""

import csv
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MAX_STORED_BYTES = 45 * 1024 * 1024   # above this, shard
MAX_SHARD_TOTAL = 600 * 1024 * 1024   # above this, summarize
SHARD_COUNT = 32
SAMPLE_BYTES = 1024 * 1024            # per-offset sample for fingerprinting

# Several hospital CDNs (Cloudflare, Akamai) refuse non-browser User-Agents,
# despite 45 CFR 180.50 requiring these files be accessible to automated
# searches. A browser UA recovers most of them.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------- HTTP layer

def request(url, method="GET", timeout=600, extra=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    headers.update(extra or {})
    return urllib.request.urlopen(
        urllib.request.Request(url, method=method, headers=headers), timeout=timeout
    )


def with_retries(fn, attempts=3, delay=20):
    """Retry transient network failures with linear backoff; re-raise the last."""
    for i in range(attempts):
        try:
            return fn()
        except urllib.error.HTTPError:
            raise  # a definitive server answer, not a transient fault
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def fetch_small(url, timeout=60):
    def go():
        with request(url, timeout=timeout) as resp:
            return resp.read(), dict(resp.headers)
    return with_retries(go)


def head(url):
    def go():
        try:
            with request(url, method="HEAD", timeout=60) as resp:
                return dict(resp.headers)
        except urllib.error.HTTPError:
            return {}  # reachable but no HEAD support; caller falls through to GET
    return with_retries(go)


def download_to(url, dest):
    def go():
        with request(url) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
            return dict(resp.headers)
    return with_retries(go)


def fingerprint_offsets(total):
    return sorted({0, total // 4, total // 2, 3 * total // 4,
                   max(total - SAMPLE_BYTES, 0)})


def remote_fingerprint(url):
    """Hash of ~5 MB of Range-request samples + length; None if unsupported."""
    try:
        with request(url, extra={"Range": f"bytes=0-{SAMPLE_BYTES - 1}"},
                     timeout=120) as resp:
            if resp.status != 206:
                return None
            total = int(resp.headers["Content-Range"].split("/")[1])
            h = hashlib.sha256(str(total).encode())
            h.update(resp.read())
        for off in fingerprint_offsets(total)[1:]:
            with request(url, extra={"Range": f"bytes={off}-{off + SAMPLE_BYTES - 1}"},
                         timeout=120) as resp:
                if resp.status != 206:
                    return None
                h.update(resp.read())
        return h.hexdigest()
    except Exception:
        return None


def local_fingerprint(path):
    """Same construction as remote_fingerprint, over the downloaded bytes."""
    total = path.stat().st_size
    h = hashlib.sha256(str(total).encode())
    with open(path, "rb") as f:
        for off in fingerprint_offsets(total):
            f.seek(off)
            h.update(f.read(SAMPLE_BYTES))
    return h.hexdigest()


# ------------------------------------------------------------- MRF discovery

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
    raw, _ = fetch_small(hospital["hpt_txt"], timeout=30)
    wanted = normalize_name(hospital["location_name"])
    for block in parse_hpt_txt(raw.decode("utf-8", errors="replace")):
        if normalize_name(block.get("location-name", "")) == wanted:
            return block.get("mrf-url")
    return None


# ------------------------------------------------------------ payload wrangling

def filename_from(headers, url, path):
    """Best-effort MRF filename: Content-Disposition, then URL, then sniffing."""
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m:
        return urllib.parse.unquote(m.group(1).strip())
    name = url.split("?")[0].rsplit("/", 1)[-1] or "standardcharges"
    if ext_of(name) == ".bin":  # e.g. Box's index.php download URLs
        with open(path, "rb") as f:
            start = f.read(16).lstrip(b"\xef\xbb\xbf").lstrip()
        if start[:1] in (b"{", b"["):
            return name + ".json"
        if b"," in start or start[:1] == b'"':
            return name + ".csv"
    return name


def materialize(tmp, headers, url, workdir):
    """Return (filename, path) of the actual MRF, unzipping (on disk) if needed."""
    with open(tmp, "rb") as f:
        magic = f.read(4)
    if magic != b"PK\x03\x04" and "zip" not in headers.get("Content-Type", ""):
        return filename_from(headers, url, tmp), tmp
    with zipfile.ZipFile(tmp) as zf:
        names = [
            n for n in zf.namelist()
            if not n.endswith("/")
            and not n.startswith("__MACOSX")
            and not Path(n).name.startswith("._")
        ]
        if len(names) != 1:
            raise ValueError(f"expected 1 file in zip, found {names}")
        out = workdir / "extracted"
        with zf.open(names[0]) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        return Path(names[0]).name, out


def normalize_payload(name, payload):
    """Stable representation so diffs reflect real changes, not formatting."""
    lower = name.casefold()
    if lower.endswith(".json") and len(payload) <= MAX_STORED_BYTES:
        try:
            obj = json.loads(payload.decode("utf-8-sig"))  # tolerate BOM
            return json.dumps(obj, indent=1, sort_keys=True).encode() + b"\n"
        except (ValueError, MemoryError):
            return payload
    if lower.endswith((".csv", ".txt")):
        return payload.replace(b"\r\n", b"\n")
    return payload


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ext_of(name):
    suffix = Path(name).suffix.casefold()
    return suffix if suffix in (".csv", ".json", ".xml", ".txt") else ".bin"


# ----------------------------------------------------------------- sharding

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
        for _ in range(3):
            try:
                next(reader)
            except StopIteration:
                break
        header_line_count = reader.line_num
        header_text = "\n".join(lines[:header_line_count]) + "\n"
        body = [l for l in lines[header_line_count:] if l]
        write_shards(outdir, "_header.csv", header_text, shard_lines(body), ".csv")
        return "sharded"
    if lower.endswith(".json"):
        try:
            obj = json.loads(payload.decode("utf-8-sig"))  # tolerate BOM
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


# --------------------------------------------------------------- summarizing

SUMMARY_COLUMNS = [
    "code_type", "code", "description",
    "gross_charge", "discounted_cash",
    "min_negotiated", "max_negotiated", "payer_entries",
]


class CodeAgg:
    __slots__ = ("gross", "cash", "lo", "hi", "n")

    def __init__(self):
        self.gross = self.cash = self.lo = self.hi = None
        self.n = 0

    def add_negotiated(self, v):
        self.lo = v if self.lo is None else min(self.lo, v)
        self.hi = v if self.hi is None else max(self.hi, v)
        self.n += 1


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def write_summary(agg, out_path):
    if not agg:
        return False
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SUMMARY_COLUMNS)
        for (ctype, code, desc) in sorted(agg):
            a = agg[(ctype, code, desc)]
            w.writerow([ctype, code, desc, a.gross, a.cash, a.lo, a.hi, a.n])
    return True


def summarize_csv(path, out_path):
    """Stream a CMS v3 CSV (tall or wide) into a per-code price digest."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        try:
            next(reader), next(reader)  # attestation metadata rows
            header = next(reader)
        except StopIteration:
            return False
        idx = {name.strip().casefold(): i for i, name in enumerate(header)}
        desc_i = idx.get("description")
        code_i = idx.get("code|1")
        ctype_i = idx.get("code|1|type")
        gross_i = idx.get("standard_charge|gross")
        cash_i = idx.get("standard_charge|discounted_cash")
        neg = [i for name, i in idx.items() if "negotiated_dollar" in name]
        if desc_i is None or code_i is None or not neg:
            return False

        def cell(row, i):
            return row[i].strip() if i is not None and i < len(row) else ""

        agg = {}
        for row in reader:
            key = (cell(row, ctype_i), cell(row, code_i), cell(row, desc_i)[:200])
            a = agg.setdefault(key, CodeAgg())
            a.gross = a.gross or to_float(cell(row, gross_i))
            a.cash = a.cash or to_float(cell(row, cash_i))
            for i in neg:
                v = to_float(cell(row, i))
                if v is not None:
                    a.add_negotiated(v)
    return write_summary(agg, out_path)


def summarize_json(path, out_path):
    """Stream a CMS v3 JSON into a per-code price digest (needs ijson)."""
    try:
        import ijson
    except ImportError:
        return False
    agg = {}
    with open(path, "rb") as f:
        if f.read(3) != b"\xef\xbb\xbf":  # skip BOM if present
            f.seek(0)
        for item in ijson.items(f, "standard_charge_information.item"):
            desc = str(item.get("description", ""))[:200]
            codes = item.get("code_information") or [{}]
            ctype = str(codes[0].get("type", ""))
            code = str(codes[0].get("code", ""))
            a = agg.setdefault((ctype, code, desc), CodeAgg())
            for sc in item.get("standard_charges") or []:
                a.gross = a.gross or to_float(sc.get("gross_charge"))
                a.cash = a.cash or to_float(sc.get("discounted_cash"))
                for p in sc.get("payers_information") or []:
                    v = to_float(p.get("standard_charge_dollar"))
                    if v is not None:
                        a.add_negotiated(v)
    return write_summary(agg, out_path)


def store_summarized(outdir, name, path):
    lower = name.casefold()
    try:
        if lower.endswith(".csv") and summarize_csv(path, outdir / "summary.csv"):
            return "summarized"
        if lower.endswith(".json") and summarize_json(path, outdir / "summary.csv"):
            return "summarized"
    except Exception as exc:
        print(f"  summarize failed for {name}: {exc}", file=sys.stderr)
    return "metadata-only"


# ------------------------------------------------------------------ pipeline

def clear_content(outdir):
    for path in outdir.iterdir():
        if path.name == "meta.json":
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process(hospital, scratch):
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

    # A metadata-only giant that predates the summarizer gets one forced
    # download to backfill summary.csv, bypassing the cheap-skip paths.
    upgrade_needed = (
        old_meta.get("status", "").startswith("metadata-only")
        and old_meta.get("size_bytes", 0) > MAX_SHARD_TOTAL
        and not (outdir / "summary.csv").exists()
        # Don't re-download daily for a summary that keeps failing; retry
        # only when the file itself changes.
        and old_meta.get("summary_attempted_sha") != old_meta.get("sha256")
    )

    if not upgrade_needed and mrf_url == old_meta.get("mrf_url") and old_meta.get("sha256"):
        h = head(mrf_url)
        validators = (h.get("Last-Modified"), h.get("ETag"))
        stored = (old_meta.get("source_last_modified"), old_meta.get("source_etag"))
        if any(validators) and validators == stored:
            return None
        if not any(stored):
            # Server offers no validators: try a ~5 MB sampled fingerprint.
            fp = remote_fingerprint(mrf_url)
            if fp and fp == old_meta.get("transfer_fingerprint"):
                return None
            if fp is None and old_meta.get("size_bytes", 0) > MAX_SHARD_TOTAL \
                    and datetime.now(timezone.utc).weekday() != 6:
                return None  # no Range support either: full refetch Sundays only

    workdir = Path(tempfile.mkdtemp(dir=scratch))
    try:
        tmp = workdir / "download"
        headers = download_to(mrf_url, tmp)
        transfer_fp = local_fingerprint(tmp)
        name, payload_path = materialize(tmp, headers, mrf_url, workdir)
        size = payload_path.stat().st_size

        if size <= MAX_SHARD_TOTAL:
            payload = normalize_payload(name, payload_path.read_bytes())
            sha = hashlib.sha256(payload).hexdigest()
        else:
            payload = None
            sha = sha256_file(payload_path)

        changed = sha != old_meta.get("sha256") or mrf_url != old_meta.get("mrf_url")
        if not changed and not upgrade_needed:
            # Content identical but validators rotated; refresh them quietly
            # so the cheap-skip paths work next run.
            old_meta["source_last_modified"] = headers.get("Last-Modified")
            old_meta["source_etag"] = headers.get("ETag")
            old_meta["transfer_fingerprint"] = transfer_fp
            meta_path.write_text(json.dumps(old_meta, indent=2) + "\n")
            return None

        clear_content(outdir)
        if payload is not None and len(payload) <= MAX_STORED_BYTES:
            (outdir / f"standardcharges{ext_of(name)}").write_bytes(payload)
            mode = "stored"
        elif payload is not None:
            mode = store_sharded(outdir, name, payload)
        else:
            mode = store_summarized(outdir, name, payload_path)

        meta = {
            "system": hospital["system"],
            "location_name": hospital["location_name"],
            "mrf_url": mrf_url,
            "source_filename": name,
            "sha256": sha,
            "size_bytes": size if payload is None else len(payload),
            "source_last_modified": headers.get("Last-Modified"),
            "source_etag": headers.get("ETag"),
            "transfer_fingerprint": transfer_fp,
            "status": mode,
            "first_seen": old_meta.get("first_seen") or utcnow(),
            "last_changed": utcnow() if changed else old_meta.get("last_changed"),
        }
        if mode == "metadata-only" and size > MAX_SHARD_TOTAL:
            meta["summary_attempted_sha"] = sha
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
        verb = ("updated" if old_meta.get("sha256") else "first snapshot") \
            if changed else "backfilled summary"
        return f"{slug}: {verb} ({size:,} bytes, {mode})"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def record_failure(slug, exc):
    """Persist an outage streak in meta.json — unreachability is signal too."""
    meta_path = DATA / slug / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    prior = meta.get("fetch_failures", {})
    meta["fetch_failures"] = {
        "count": prior.get("count", 0) + 1,
        "first_failed": prior.get("first_failed") or utcnow(),
        "last_error": str(exc)[:200],
        "last_attempt": utcnow(),
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return meta["fetch_failures"]["count"]


def clear_failure_record(slug):
    meta_path = DATA / slug / "meta.json"
    if not meta_path.exists():
        return False
    meta = json.loads(meta_path.read_text())
    if meta.pop("fetch_failures", None) is None:
        return False
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return True


def main():
    scratch = Path(tempfile.mkdtemp(prefix="mrf-scrape-"))
    hospitals = json.loads((ROOT / "hospitals.json").read_text())
    changes, failures, recovered = [], [], []
    try:
        for hospital in hospitals:
            slug = hospital["slug"]
            try:
                result = process(hospital, scratch)
                if clear_failure_record(slug):
                    recovered.append(slug)
                if result:
                    changes.append(result)
                    print(result, flush=True)
                else:
                    print(f"{slug}: unchanged", flush=True)
            except Exception as exc:  # one bad hospital shouldn't sink the run
                streak = record_failure(slug, exc)
                failures.append(f"{slug} (day {streak})")
                print(f"{slug}: ERROR {exc}", file=sys.stderr, flush=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    parts = []
    if changes:
        parts.append("Update price files: " + "; ".join(c.split(":")[0] for c in changes))
    if failures:
        parts.append("fetch errors: " + ", ".join(failures))
    if recovered:
        parts.append("recovered: " + ", ".join(recovered))
    (ROOT / "commit_message.txt").write_text(("; ".join(parts) or "No changes") + "\n")
    if len(failures) > len(hospitals) / 2:
        sys.exit(1)  # majority failing means the problem is ours, not theirs


if __name__ == "__main__":
    main()
