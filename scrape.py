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
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
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

# Optional path to a curl-impersonate wrapper (e.g. curl_chrome145). Hospitals
# whose CDNs block by TLS fingerprint are marked "fetch": "impersonate" in
# hospitals.json and routed through it.
IMPERSONATE_BIN = os.environ.get("CURL_IMPERSONATE_BIN")


class Headers(dict):
    """Case-insensitive header lookup; curl/HTTP2 lowercases header names."""

    def __init__(self, pairs):
        super().__init__({k.lower(): v for k, v in pairs})

    def get(self, key, default=None):
        return super().get(key.lower(), default)


def parse_header_dump(text):
    """Headers of the final response in a curl -D dump (redirects stack blocks)."""
    blocks = [b for b in text.replace("\r", "").strip().split("\n\n") if b.strip()]
    status, pairs = 0, []
    if blocks:
        lines = blocks[-1].splitlines()
        m = re.search(r"\s(\d{3})", lines[0])
        status = int(m.group(1)) if m else 0
        pairs = [line.partition(":")[::2] for line in lines[1:] if ":" in line]
    return status, Headers((k.strip(), v.strip()) for k, v in pairs)


def curl_fetch(url, dest=None, extra=(), max_time=None):
    """Fetch via curl-impersonate; returns (status, Headers, stdout_bytes)."""
    with tempfile.NamedTemporaryFile(suffix=".hdrs", delete=False) as tf:
        hdr_path = tf.name
    # Real file downloads resume across dropped connections (Mayo's 14.5 GB
    # blob resets long transfers) — curl retries internally, continuing at
    # the byte offset already on disk.
    resume = ("-C", "-", "--retry", "3", "--retry-all-errors") \
        if dest and str(dest) != os.devnull else ()
    cmd = [IMPERSONATE_BIN, "-sS", "-L", "--compressed",
           "--max-time", str(max_time or os.environ.get("CURL_MAX_TIME", "3600")),
           "-D", hdr_path,
           "-o", str(dest) if dest else "-", *resume, *extra, url]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True)
        status, headers = parse_header_dump(Path(hdr_path).read_text(errors="replace"))
        return status, headers, proc.stdout
    finally:
        Path(hdr_path).unlink(missing_ok=True)


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
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError,
                subprocess.CalledProcessError):
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def fetch_small(url, timeout=60, impersonate=False, max_time=None):
    if impersonate and IMPERSONATE_BIN:
        def go():
            status, headers, body = curl_fetch(url, extra=("--fail",), max_time=max_time)
            return body, headers
        return with_retries(go)

    def go():
        with request(url, timeout=timeout, extra={"Accept-Encoding": "gzip"}) as resp:
            body = resp.read()
            if body[:2] == b"\x1f\x8b":  # MGB gzips even identity requests
                body = gzip.decompress(body)
            return body, Headers(resp.headers.items())
    return with_retries(go)


def head(url, impersonate=False, max_time=None):
    if impersonate and IMPERSONATE_BIN:
        def go():
            status, headers, _ = curl_fetch(url, dest=os.devnull, extra=("-I",), max_time=max_time)
            return headers if 200 <= status < 300 else Headers([])
        return with_retries(go)

    def go():
        try:
            with request(url, method="HEAD", timeout=60) as resp:
                return Headers(resp.headers.items())
        except urllib.error.HTTPError:
            return Headers([])  # reachable but no HEAD support; fall through to GET
    return with_retries(go)


def download_to(url, dest, impersonate=False, max_time=None):
    if impersonate and IMPERSONATE_BIN:
        def go():
            status, headers, _ = curl_fetch(url, dest=dest, extra=("--fail",), max_time=max_time)
            return headers
        return with_retries(go)

    def go():
        with request(url) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
            return Headers(resp.headers.items())
    return with_retries(go)


def fingerprint_offsets(total):
    return sorted({0, total // 4, total // 2, 3 * total // 4,
                   max(total - SAMPLE_BYTES, 0)})


def remote_fingerprint(url, impersonate=False, max_time=None):
    """Hash of ~5 MB of Range-request samples + length; None if unsupported."""
    def ranged(off):
        if impersonate and IMPERSONATE_BIN:
            status, headers, body = curl_fetch(
                url, extra=("-r", f"{off}-{off + SAMPLE_BYTES - 1}"), max_time=max_time)
            if status != 206:
                return None, None
            return headers, body
        resp = request(url, extra={"Range": f"bytes={off}-{off + SAMPLE_BYTES - 1}"},
                       timeout=120)
        with resp:
            if resp.status != 206:
                return None, None
            return Headers(resp.headers.items()), resp.read()

    try:
        headers, body = ranged(0)
        if headers is None:
            return None
        total = int(headers.get("Content-Range").split("/")[1])
        h = hashlib.sha256(str(total).encode())
        h.update(body)
        for off in fingerprint_offsets(total)[1:]:
            headers, body = ranged(off)
            if headers is None:
                return None
            h.update(body)
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
    """Parse cms-hpt.txt into a list of {key: value} blocks.

    Records are delimited by each new location-name key rather than by blank
    lines — some hospitals (Grady) blank-line-separate every field.
    """
    blocks, current = [], {}
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().casefold()
        if not key:
            continue
        if key == "location-name" and current:
            blocks.append(current)
            current = {}
        current.setdefault(key, value.strip())
    if current:
        blocks.append(current)
    return blocks


def decode_text(raw):
    """Decode site text tolerating BOMs; some hospitals serve UTF-16 (Sutter)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def wants_impersonate(hospital):
    return hospital.get("fetch") == "impersonate"


def discover_mrf_url(hospital):
    raw, _ = fetch_small(hospital["hpt_txt"], timeout=30,
                         impersonate=wants_impersonate(hospital),
                         max_time=hospital.get("curl_max_time"))
    wanted = normalize_name(hospital["location_name"])
    for block in parse_hpt_txt(decode_text(raw)):
        if normalize_name(block.get("location-name", "")) == wanted:
            url = block.get("mrf-url")
            if url and not re.match(r"^https?://", url):
                url = "https://" + url  # Rush lists a scheme-less mrf-url
            return url
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
    if magic[:2] == b"\x1f\x8b":  # transfer-gzipped despite our requests
        plain = workdir / "gunzipped"
        with gzip.open(tmp, "rb") as src_f, open(plain, "wb") as dst:
            shutil.copyfileobj(src_f, dst, 1024 * 1024)
        tmp = plain
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


def aggregate_items(items):
    """Fold CMS v3 standard_charge_information items into per-code aggregates."""
    agg = {}
    for item in items:
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
    return agg


def summarize_json(path, out_path):
    """Stream a CMS v3 JSON into a per-code price digest (needs ijson)."""
    try:
        import ijson
    except ImportError:
        return False
    with open(path, "rb") as f:
        if f.read(3) != b"\xef\xbb\xbf":  # skip BOM if present
            f.seek(0)
        agg = aggregate_items(ijson.items(f, "standard_charge_information.item"))
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

    imp = wants_impersonate(hospital)
    mt = hospital.get("curl_max_time")
    if not upgrade_needed and mrf_url == old_meta.get("mrf_url") and old_meta.get("sha256"):
        h = head(mrf_url, impersonate=imp, max_time=mt)
        validators = (h.get("Last-Modified"), h.get("ETag"))
        stored = (old_meta.get("source_last_modified"), old_meta.get("source_etag"))
        if any(validators) and validators == stored:
            return None
        # Validators absent — or rotating on every request (Kaiser): fall
        # back to a ~5 MB sampled Range fingerprint before a full download.
        fp = remote_fingerprint(mrf_url, impersonate=imp, max_time=mt)
        if fp and fp == old_meta.get("transfer_fingerprint"):
            return None
        if fp is None and not any(stored) \
                and old_meta.get("size_bytes", 0) > MAX_SHARD_TOTAL \
                and datetime.now(timezone.utc).weekday() != 6:
            return None  # no Range support either: full refetch Sundays only

    # Runners have limited disk; oversized files (Mayo's 14.5 GB CSV) must be
    # fetched from an environment without the cap (a local run).
    limit = int(os.environ.get("MAX_DOWNLOAD_BYTES", 0))
    if limit:
        cl = int(head(mrf_url, impersonate=imp, max_time=mt).get("Content-Length") or 0)
        if cl > limit:
            raise ValueError(
                f"file is {cl:,} bytes, over MAX_DOWNLOAD_BYTES={limit:,}; "
                "fetch from an uncapped environment")

    workdir = Path(tempfile.mkdtemp(dir=scratch))
    try:
        tmp = workdir / "download"
        headers = download_to(mrf_url, tmp, impersonate=imp, max_time=mt)
        transfer_fp = local_fingerprint(tmp)
        name, payload_path = materialize(tmp, headers, mrf_url, workdir)
        size = payload_path.stat().st_size

        with open(payload_path, "rb") as f:
            start = f.read(512).lstrip(b"\xef\xbb\xbf").lstrip().lower()
        if start.startswith((b"<!doctype", b"<html", b"<head")):
            # Never archive a bot-block/captcha/error page as price data.
            raise ValueError("received an HTML page instead of an MRF (bot-block?)")

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
        if mode in ("stored", "sharded"):
            # Companion summary so summary.csv is a uniform analytics layer
            # across every hospital regardless of storage mode.
            store_summarized(outdir, name, payload_path)

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
    """Persist an outage streak in meta.json — unreachability is signal too.

    Re-persists at most weekly so a permanently broken source (dead link,
    bot-block) doesn't generate a noise commit every day; the streak length
    is derived from first_failed, not from write frequency.
    """
    meta_path = DATA / slug / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    prior = meta.get("fetch_failures", {})
    first = prior.get("first_failed") or utcnow()
    days = (datetime.now(timezone.utc)
            - datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ).days + 1
    last = prior.get("last_attempt")
    stale = (
        not last
        or (datetime.now(timezone.utc)
            - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ).days >= 7
    )
    if stale:
        meta["fetch_failures"] = {
            "first_failed": first,
            "last_error": str(exc)[:200],
            "last_attempt": utcnow(),
        }
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return days


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
    only = os.environ.get("ONLY")
    if only:
        hospitals = [h for h in hospitals if h["slug"] in only.split(",")]
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
