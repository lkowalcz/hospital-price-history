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
                row touches only its bucket, so diffs stay meaningful.
                Written into the companion raw-data repo (RAW_REPO_DIR, or a
                sibling hospital-price-history-raw clone) to keep this repo
                small; meta.json and summary.csv stay here
  - summarized: larger files are stream-parsed (CMS v3 schema) into
                summary.csv — per code: description, gross charge, cash
                price, min/max negotiated rate, payer count
  - metadata-only: unparseable; meta.json hash/size/timing still recorded

Stdlib except `ijson` (streaming JSON parser), needed for summarizing
multi-GB JSON files and for sharding large ones within a small host's
memory; everything else degrades gracefully without it.
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

# Some MRFs carry very large quoted fields; never let the csv module's
# default 128 KB field cap abort a summarization.
csv.field_size_limit(10**9)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
# Sharded (payer-level) content lives in a companion repo so this repo stays
# clonable; see README "Where the data lives". Locally: a sibling clone.
RAW_DATA = Path(os.environ.get(
    "RAW_REPO_DIR", ROOT.parent / "hospital-price-history-raw")) / "data"
MAX_STORED_BYTES = 45 * 1024 * 1024   # above this, shard
MAX_SHARD_TOTAL = 600 * 1024 * 1024   # above this, summarize
MAX_SHARD_FILE = 90 * 1024 * 1024     # no single shard may exceed GitHub's 100 MB cap
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


def over_limit(size, limit):
    return (f"file is {size:,} bytes, over MAX_DOWNLOAD_BYTES={limit:,}; "
            "fetch from an uncapped environment")


def download_to(url, dest, impersonate=False, max_time=None, limit=0):
    """Stream url to dest; returns the response Headers. A non-zero limit
    aborts the transfer (ValueError) once more bytes than that arrive."""
    if impersonate and IMPERSONATE_BIN:
        def go():
            extra = ("--fail",) + (("--max-filesize", str(limit)) if limit else ())
            try:
                status, headers, _ = curl_fetch(url, dest=dest, extra=extra, max_time=max_time)
            except subprocess.CalledProcessError as exc:
                if exc.returncode == 63:  # CURLE_FILESIZE_EXCEEDED: not transient
                    raise ValueError(
                        f"transfer exceeded MAX_DOWNLOAD_BYTES={limit:,}; "
                        "fetch from an uncapped environment") from exc
                raise
            return headers
        return with_retries(go)

    def go():
        written = 0
        with request(url) as resp, open(dest, "wb") as f:
            for chunk in iter(lambda: resp.read(1024 * 1024), b""):
                f.write(chunk)
                written += len(chunk)
                if limit and written > limit:
                    raise ValueError(over_limit(written, limit))
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


def discover_mrf_url(hospital, impersonate):
    raw, _ = fetch_small(hospital["hpt_txt"], timeout=30,
                         impersonate=impersonate,
                         max_time=hospital.get("curl_max_time"))
    text = decode_text(raw)
    if text.lstrip()[:16].casefold().startswith(("<!doctype", "<html", "<head")):
        # A challenge/block page here is a fetch failure, not a delisting.
        raise ValueError("cms-hpt.txt returned an HTML page (bot-block?)")
    wanted = normalize_name(hospital["location_name"])
    for block in parse_hpt_txt(text):
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


def materialize(tmp, headers, url, workdir, limit=0):
    """Return (filename, path) of the actual MRF, unzipping (on disk) if
    needed. A non-zero limit refuses to unpack a zip entry larger than it."""
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
        unpacked = zf.getinfo(names[0]).file_size
        if limit and unpacked > limit:
            raise ValueError("zip unpacks to " + over_limit(unpacked, limit))
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

def csv_records(lines):
    """Group physical lines into CSV records.

    A quoted field can contain newlines, so a record is complete only on a
    line that leaves quoting closed; fragmenting such records across shards
    would scramble the file's row structure. Quote state follows real CSV
    semantics — a quote opens a field only at a field boundary — because
    files with stray literal quotes inside unquoted fields (Geisinger:
    sizes like 5") would otherwise glue thousands of rows into one giant
    record. Single-line records pass through byte-identical.
    """
    buf = []
    in_quotes = False
    for line in lines:
        if '"' not in line:
            if in_quotes:      # quoteless continuation inside a quoted field
                buf.append(line)
            else:
                yield line
            continue
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if in_quotes:
                if c == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        i += 1  # escaped ""
                    else:
                        in_quotes = False
            elif c == '"' and (i == 0 or line[i - 1] == ","):
                in_quotes = True  # field boundary: this quote opens a field
            i += 1
        buf.append(line)
        if not in_quotes:
            yield "\n".join(buf)
            buf = []
    if buf:  # unterminated quote (malformed CSV): keep the bytes anyway
        yield "\n".join(buf)


def bucket_of(line):
    return int.from_bytes(hashlib.sha1(line.encode()).digest()[:4], "big") % SHARD_COUNT


def shard_lines(lines):
    buckets = [[] for _ in range(SHARD_COUNT)]
    for line in lines:
        buckets[bucket_of(line)].append(line)
    for bucket in buckets:
        bucket.sort()
    return buckets


def oversized(buckets):
    """True if any bucket would exceed GitHub's per-file limit — e.g. a
    pathological file whose records mostly hash to one bucket, or a huge
    mis-grouped record. Such files must not be sharded at all."""
    return max((sum(len(l) + 1 for l in b) for b in buckets if b), default=0) \
        > MAX_SHARD_FILE


def write_shards(outdir, header_name, header_text, buckets, ext):
    shard_dir = outdir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
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
        body = [r for r in csv_records(lines[header_line_count:]) if r]
        buckets = shard_lines(body)
        if oversized(buckets):
            return "metadata-only"
        write_shards(outdir, "_header.csv", header_text, buckets, ".csv")
        return "sharded"
    if lower.endswith(".json"):
        try:
            parsed = shard_json_streaming(payload)
        except ImportError:
            parsed = shard_json_in_memory(payload)
        except (ValueError, MemoryError):
            return "metadata-only"
        if parsed is None:
            return "metadata-only"
        header_text, buckets = parsed
        if oversized(buckets):
            return "metadata-only"
        write_shards(outdir, "_header.json", header_text, buckets, ".jsonl")
        return "sharded"
    return "metadata-only"


JSON_ITEMS_KEY = "standard_charge_information"


def json_item_line(item):
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def json_header_text(obj):
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def shard_json_streaming(payload):
    """(header_text, buckets) for a CMS v3 JSON, without ever holding the
    parsed document: peak memory is the payload plus the item lines, not the
    Python object graph (which runs 5-6x the file size and OOMs small hosts).

    Two passes over the bytes: parse events for the top-level header (the
    item array is skipped event by event), then `ijson.items` for the items
    themselves, each serialized and dropped in turn. Produces byte-identical
    shards to the in-memory path. Returns None when the document is not a
    dict carrying a list under JSON_ITEMS_KEY. Raises ImportError without
    ijson, ValueError (ijson.JSONError is mapped onto it) on malformed JSON.
    """
    import ijson
    from ijson.common import ObjectBuilder
    item_prefix = JSON_ITEMS_KEY + ".item"

    def source():
        f = io.BytesIO(payload)  # shares the buffer; no copy
        if f.read(3) != b"\xef\xbb\xbf":  # tolerate BOM
            f.seek(0)
        return f

    try:
        header = ObjectBuilder()
        first, saw_items = True, False
        for prefix, event, value in ijson.parse(source(), use_float=True):
            if first:
                first = False
                if event != "start_map":
                    return None
            if prefix == JSON_ITEMS_KEY:
                if event == "start_array":
                    saw_items = True
                    continue
                if event == "end_array":
                    continue
                return None  # present, but not a list
            if prefix.startswith(item_prefix):
                continue
            if prefix == "" and event == "map_key" and value == JSON_ITEMS_KEY:
                continue
            header.event(event, value)
        if not saw_items:
            return None
        header_text = json_header_text(header.value)
        buckets = shard_lines(
            json_item_line(item)
            for item in ijson.items(source(), item_prefix, use_float=True))
    except ijson.JSONError as exc:
        raise ValueError(str(exc)) from exc
    return header_text, buckets


def shard_json_in_memory(payload):
    """Fallback for hosts without ijson: same output, whole document parsed."""
    obj = json.loads(payload.decode("utf-8-sig"))  # tolerate BOM
    if not isinstance(obj, dict):
        return None
    items = obj.pop(JSON_ITEMS_KEY, None)
    if not isinstance(items, list):
        return None
    return json_header_text(obj), shard_lines(json_item_line(i) for i in items)


# --------------------------------------------------------------- summarizing

SUMMARY_COLUMNS = [
    "code_type", "code", "description",
    "gross_charge", "discounted_cash",
    "min_negotiated", "max_negotiated", "payer_entries",
]

# Hospital-internal code systems (chargemaster and revenue-center codes).
# They are kept when they are a row's primary (first) code, but prices are
# not fanned out to them from later slots: chargemaster rows often carry
# LOCAL + CDM + RC side by side, and duplicating every row across all three
# multiplies the summary without adding cross-hospital signal.
INTERNAL_CODE_TYPES = {"LOCAL", "CDM", "RC"}
_internal_cache = {}


def internal_type(t):
    r = _internal_cache.get(t)
    if r is None:
        r = re.sub(r"[^A-Z0-9]", "", t.upper()) in INTERNAL_CODE_TYPES
        _internal_cache[t] = r
    return r


# Bump whenever summarize_csv/aggregate_items change what a summary.csv row
# means (which codes a row fans out to, how gross/cash are picked, ...).
# It is stamped into meta.json as summary_version, and compute_index.py
# refuses to chain a hospital across two versions: a methodology change
# would otherwise compound into the index as a price move.
SUMMARY_VERSION = 1


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
        # All code|N slots: a row often carries several codes for one service
        # (MS-DRG in slot 1, CPT in slot 2, ...); its prices count toward
        # every code it is billed under, not just the first.
        code_cols = []
        for name, i in idx.items():
            m = re.fullmatch(r"code\|(\d+)", name)
            if m:
                code_cols.append((int(m.group(1)), i, idx.get(f"code|{m.group(1)}|type")))
        code_cols.sort()
        gross_i = idx.get("standard_charge|gross")
        cash_i = idx.get("standard_charge|discounted_cash")
        neg = [i for name, i in idx.items() if "negotiated_dollar" in name]
        if desc_i is None or not code_cols or not neg:
            return False

        def cell(row, i):
            return row[i].strip() if i is not None and i < len(row) else ""

        agg = {}
        for row in reader:
            desc = cell(row, desc_i)[:200]
            keys = []
            for _, ci, ti in code_cols:
                code = cell(row, ci)
                if code:
                    ctype = cell(row, ti)
                    if keys and internal_type(ctype):
                        continue
                    k = (ctype, code, desc)
                    if k not in keys:
                        keys.append(k)
            if not keys:  # un-coded row (drugs, room rates): keep, under a blank code
                keys = [("", "", desc)]
            aggs = [agg.setdefault(k, CodeAgg()) for k in keys]
            gross, cash = to_float(cell(row, gross_i)), to_float(cell(row, cash_i))
            for a in aggs:
                a.gross = a.gross or gross
                a.cash = a.cash or cash
            for i in neg:
                v = to_float(cell(row, i))
                if v is not None:
                    for a in aggs:
                        a.add_negotiated(v)
    return write_summary(agg, out_path)


def aggregate_items(items):
    """Fold CMS v3 standard_charge_information items into per-code aggregates.

    An item's prices count toward every entry in its code_information list
    (e.g. both the MS-DRG and the CPT it is billed under), not just the
    first — except hospital-internal types (see INTERNAL_CODE_TYPES), which
    only count when they are the item's primary code.
    """
    agg = {}
    for item in items:
        desc = str(item.get("description", ""))[:200]
        keys = []
        for c in item.get("code_information") or [{}]:
            ctype = str(c.get("type", ""))
            if keys and internal_type(ctype):
                continue
            k = (ctype, str(c.get("code", "")), desc)
            if k not in keys:
                keys.append(k)
        aggs = [agg.setdefault(k, CodeAgg()) for k in keys]
        for sc in item.get("standard_charges") or []:
            gross = to_float(sc.get("gross_charge"))
            cash = to_float(sc.get("discounted_cash"))
            for a in aggs:
                a.gross = a.gross or gross
                a.cash = a.cash or cash
            for p in sc.get("payers_information") or []:
                v = to_float(p.get("standard_charge_dollar"))
                if v is not None:
                    for a in aggs:
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

# ------------------------------------------------------------- cold storage

# `summarized` hospitals keep only a lossy digest in the repo, so the
# original bytes are preserved on the Internet Archive: one item per
# (hospital, snapshot sha), zstd-compressed, recorded in meta.json under
# cold_storage. Needs the `zstd` binary and the `ia` CLI (pip install
# internetarchive) with credentials in IA_ACCESS_KEY_ID/IA_SECRET_ACCESS_KEY
# (CI secrets) or from `ia configure` locally (then IA_ARCHIVE=1).
IA_BIN = os.environ.get("IA_BIN", "ia")


def archiving_enabled():
    if os.environ.get("IA_ARCHIVE") == "0":
        return False
    return os.environ.get("IA_ARCHIVE") == "1" or bool(
        os.environ.get("IA_ACCESS_KEY_ID") and os.environ.get("IA_SECRET_ACCESS_KEY"))


def cold_store(slug, path, sha, name=None):
    """Compress path and upload it to archive.org; returns the cold_storage
    record. `sha` is the snapshot's identity from meta.json (for giants the
    file's own sha256; for a fallback-summarized smaller file, that of its
    normalized payload), so the record can be matched against meta.json;
    file_sha256 is always the hash of the bytes actually archived. `name`
    is the hospital's own filename for the archived file — path is usually
    a scratch file called "download" or "extracted"."""
    zst = path.with_name((name or path.name) + ".zst")
    if not zst.exists():
        subprocess.run(["zstd", "-q", "-T0", "-12", "--long=27", "-o", str(zst), str(path)],
                       check=True)
    file_sha = sha256_file(path)
    # One item per (hospital, content hash): re-archiving a new snapshot
    # creates a new item, so old snapshots stay retrievable.
    item = f"hospital-price-history-{slug}-{sha[:12]}"
    subprocess.run(
        [IA_BIN, "upload", item, str(zst),
         "--no-derive", "--checksum", "--retries", "10", "--sleep", "60",
         "--size-hint", str(zst.stat().st_size),
         # A dataset, not a text: archive.org files it with community data
         # and skips the book-style processing it would try on "texts".
         "--metadata", "mediatype:data",
         "--metadata", f"title:Hospital price MRF snapshot: {slug} ({utcnow()[:10]})",
         "--metadata", "subject:hospital price transparency",
         "--metadata",
         f"description:Raw machine-readable standard-charges file for {slug}, "
         f"archived by https://github.com/lkowalcz/hospital-price-history. "
         f"sha256 of the uncompressed file: {file_sha}"],
        check=True)
    return {
        "url": f"https://archive.org/details/{item}",
        "sha256": sha,
        "file_sha256": file_sha,
        "compressed_bytes": zst.stat().st_size,
        "archived": utcnow(),
    }


def cold_attempt_recent(meta, days=7):
    """True if archiving this exact snapshot failed within the last week:
    the retry is a multi-GB re-download, so don't do it daily."""
    attempt = meta.get("cold_storage_attempt") or {}
    if attempt.get("sha256") != meta.get("sha256") or not attempt.get("at"):
        return False
    at = datetime.strptime(attempt["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).days < days


def try_cold_store(slug, path, sha, meta, name=None):
    """Archive and record in meta (in place); a failure is recorded as an
    attempt so process() retries it next week, and never fails the run:
    the snapshot itself is already stored."""
    try:
        meta["cold_storage"] = cold_store(slug, path, sha, name)
        meta.pop("cold_storage_attempt", None)
        ARCHIVED.append(slug)
        print(f"{slug}: archived original at {meta['cold_storage']['url']}", flush=True)
        return True
    except Exception as exc:
        meta["cold_storage_attempt"] = {"sha256": sha, "at": utcnow(),
                                        "error": str(exc)[:200]}
        ARCHIVE_FAILED.append(slug)
        print(f"{slug}: cold storage failed: {exc}", file=sys.stderr, flush=True)
        return False


# ------------------------------------------------------------------ pipeline

# Slugs whose content was rewritten this run. The workflow reads
# changed_slugs.txt to sync exactly these paths in the raw repo (whose old
# shards are not on disk under its sparse checkout).
REWRITTEN = []
# Slugs whose meta.json validators were refreshed without a content change.
REFRESHED = []
# Cold-storage bookkeeping for the commit message.
ARCHIVED, ARCHIVE_FAILED = [], []
# Summarized hospitals re-downloaded this run only to archive their bytes.
BACKFILLED = []
# Snapshots are assembled under data/<STAGING>/<slug> (and the raw-repo
# equivalent) and swapped into place only once complete. Git-ignored in
# this repo; the raw repo's commit steps add data/<slug> paths only.
STAGING = ".staging"


def clear_content(outdir):
    for path in outdir.iterdir():
        if path.name == "meta.json":
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()


def store_snapshot(slug, name, payload, payload_path):
    """Write one hospital's content — a stored payload or raw-repo shards,
    plus the companion summary.csv — and return the storage mode.

    Everything is built in staging directories beside the destinations and
    swapped in only when complete. Without that, a crash mid-way (disk
    full, the OOM killer on a small host) left an emptied data/<slug> next
    to an unchanged meta.json: the next run's validators matched, nothing
    was re-fetched, and the workflow committed the emptiness as a deletion.
    """
    outdir = DATA / slug
    stage = DATA / STAGING / slug
    raw_stage = RAW_DATA / STAGING / slug
    for d in (stage, raw_stage):
        shutil.rmtree(d, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        if payload is not None and len(payload) <= MAX_STORED_BYTES:
            (stage / f"standardcharges{ext_of(name)}").write_bytes(payload)
            mode = "stored"
        elif payload is not None:
            mode = store_sharded(raw_stage, name, payload)
            if mode == "metadata-only":
                # unshardable (unparseable, or a bucket over the per-file
                # cap): fall back to the summary layer rather than lose
                # the price data entirely
                mode = store_summarized(stage, name, payload_path)
        else:
            mode = store_summarized(stage, name, payload_path)
        if mode in ("stored", "sharded"):
            # Companion summary so summary.csv is a uniform analytics layer
            # across every hospital regardless of storage mode.
            store_summarized(stage, name, payload_path)

        # Swap. From here the old snapshot is gone and the new one complete;
        # renames within a directory tree are atomic per path.
        outdir.mkdir(parents=True, exist_ok=True)
        clear_content(outdir)
        for p in stage.iterdir():
            p.rename(outdir / p.name)
        # Raw-repo cleanup for local full checkouts; under the workflow's
        # sparse checkout old shards aren't on disk, so the commit step
        # re-syncs each rewritten slug's raw path from changed_slugs.txt.
        shutil.rmtree(RAW_DATA / slug, ignore_errors=True)
        if raw_stage.is_dir():
            raw_stage.rename(RAW_DATA / slug)
        REWRITTEN.append(slug)
        return mode
    finally:
        for d in (stage, raw_stage):
            shutil.rmtree(d, ignore_errors=True)


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process(hospital, scratch, impersonate=None):
    """Scrape one hospital. impersonate: None = per-config (hospitals.json
    `fetch` plus a learned fetch_escalated flag in meta.json); True/False
    forces the transport — the fallback ladder in main() retries a failed
    hospital once with the opposite one."""
    slug = hospital["slug"]
    outdir = DATA / slug
    outdir.mkdir(parents=True, exist_ok=True)
    meta_path = outdir / "meta.json"
    old_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    imp = impersonate
    if imp is None:
        imp = wants_impersonate(hospital) or bool(old_meta.get("fetch_escalated"))
    mrf_url = discover_mrf_url(hospital, imp)
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

    # Runners have limited disk; oversized files (Mayo's 14.5 GB CSV) must be
    # fetched from an environment without the cap (a local run). Content-
    # Length is the cheap early exit below; the cap is also enforced on the
    # bytes actually received and on the unpacked size of a zip, since
    # servers without HEAD support report nothing and a zip's payload is
    # what lands on disk.
    limit = int(os.environ.get("MAX_DOWNLOAD_BYTES") or 0)

    # A summarized hospital whose original bytes are not yet in cold storage
    # gets one forced download to archive them — this backfills the giants
    # captured before archiving existed — a few per run so the daily job
    # stays bounded. A failed attempt is retried weekly, not daily.
    archive_needed = (
        not upgrade_needed
        and archiving_enabled()
        and old_meta.get("status") == "summarized"
        and bool(old_meta.get("sha256"))
        and (old_meta.get("cold_storage") or {}).get("sha256") != old_meta.get("sha256")
        and not cold_attempt_recent(old_meta)
        and (not limit or old_meta.get("size_bytes", 0) <= limit)
        and len(BACKFILLED) < int(os.environ.get("IA_BACKFILL_PER_RUN") or 2)
    )
    if archive_needed:
        BACKFILLED.append(slug)

    mt = hospital.get("curl_max_time")
    if not upgrade_needed and not archive_needed \
            and mrf_url == old_meta.get("mrf_url") and old_meta.get("sha256"):
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

    if limit:
        cl = int(head(mrf_url, impersonate=imp, max_time=mt).get("Content-Length") or 0)
        if cl > limit:
            raise ValueError(over_limit(cl, limit))

    workdir = Path(tempfile.mkdtemp(dir=scratch))
    try:
        tmp = workdir / "download"
        headers = download_to(mrf_url, tmp, impersonate=imp, max_time=mt, limit=limit)
        transfer_fp = local_fingerprint(tmp)
        name, payload_path = materialize(tmp, headers, mrf_url, workdir, limit=limit)
        if payload_path != tmp:
            tmp.unlink()  # the zip has served its purpose; free runner disk
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
            # Content identical but validators rotated; refresh them so the
            # cheap-skip paths work next run. The meta.json edit still lands
            # in a commit, so main() names it rather than calling the run
            # "No changes".
            old_meta["source_last_modified"] = headers.get("Last-Modified")
            old_meta["source_etag"] = headers.get("ETag")
            old_meta["transfer_fingerprint"] = transfer_fp
            if archive_needed:  # downloaded for cold storage only
                try_cold_store(slug, payload_path, sha, old_meta, name)
            else:
                REFRESHED.append(slug)
            meta_path.write_text(json.dumps(old_meta, indent=2) + "\n")
            return None

        mode = store_snapshot(slug, name, payload, payload_path)

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
        if (outdir / "summary.csv").exists():
            meta["summary_version"] = SUMMARY_VERSION
        if mode == "metadata-only" and size > MAX_SHARD_TOTAL:
            meta["summary_attempted_sha"] = sha
        if old_meta.get("fetch_escalated"):
            meta["fetch_escalated"] = old_meta["fetch_escalated"]
        for key in ("cold_storage", "cold_storage_attempt"):
            if (old_meta.get(key) or {}).get("sha256") == sha:
                meta[key] = old_meta[key]  # same bytes (e.g. the URL moved)
        if mode == "summarized" and "cold_storage" not in meta and archiving_enabled():
            try_cold_store(slug, payload_path, sha, meta, name)
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


def is_escalated(slug):
    meta_path = DATA / slug / "meta.json"
    if not meta_path.exists():
        return False
    return bool(json.loads(meta_path.read_text()).get("fetch_escalated"))


def mark_escalated(slug):
    """Remember that this hospital needed the impersonate fallback, so
    future runs go straight to the transport that works."""
    meta_path = DATA / slug / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if not meta.get("fetch_escalated"):
        meta["fetch_escalated"] = utcnow()
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")


def clear_failure_record(slug):
    meta_path = DATA / slug / "meta.json"
    if not meta_path.exists():
        return False
    meta = json.loads(meta_path.read_text())
    if meta.pop("fetch_failures", None) is None:
        return False
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return True


def select_hospitals(hospitals, only, skip):
    """ONLY=a,b restricts the run; otherwise SKIP=c,d excludes slugs.

    SKIP is for hospitals another host owns (see local_refetch.py): CI
    leaves them alone so it neither records a failure streak the other
    host will clear hours later nor flags them "unreachable" meanwhile.
    """
    if only:
        return [h for h in hospitals if h["slug"] in only.split(",")]
    if skip:
        drop = set(skip.split(","))
        return [h for h in hospitals if h["slug"] not in drop]
    return hospitals


def main():
    scratch = Path(tempfile.mkdtemp(prefix="mrf-scrape-"))
    hospitals = json.loads((ROOT / "hospitals.json").read_text())
    only = os.environ.get("ONLY")
    hospitals = select_hospitals(hospitals, only, os.environ.get("SKIP"))
    changes, failures, recovered, escalated = [], [], [], []
    try:
        for hospital in hospitals:
            slug = hospital["slug"]
            try:
                try:
                    result = process(hospital, scratch)
                except Exception as exc:
                    # Fallback ladder: retry once with the opposite transport
                    # (plain <-> curl-impersonate). CDNs block one or the
                    # other, and which one changes over time.
                    if not IMPERSONATE_BIN:
                        raise
                    default_imp = wants_impersonate(hospital) or is_escalated(slug)
                    print(f"{slug}: {exc}; retrying via "
                          f"{'plain fetch' if default_imp else 'impersonate'}",
                          file=sys.stderr, flush=True)
                    result = process(hospital, scratch, impersonate=not default_imp)
                    if not default_imp:
                        mark_escalated(slug)
                        escalated.append(slug)
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
    if escalated:
        parts.append("escalated to impersonate: " + ", ".join(escalated))
    if ARCHIVED:
        parts.append("archived originals: " + ", ".join(ARCHIVED))
    if ARCHIVE_FAILED:
        parts.append("cold storage failed: " + ", ".join(ARCHIVE_FAILED))
    if REFRESHED:
        # meta.json edits that would otherwise be committed as "No changes"
        # and show up as unexplained events in a hospital's change history.
        parts.append("validators refreshed: " + ", ".join(REFRESHED))
    (ROOT / "commit_message.txt").write_text(("; ".join(parts) or "No changes") + "\n")
    (ROOT / "changed_slugs.txt").write_text(
        "".join(s + "\n" for s in REWRITTEN))
    # Majority failing on a FULL run means the problem is ours (runner IP
    # block, network) — bail before committing spurious failure streaks.
    # An ONLY run is often deliberately scoped to failing hospitals, where
    # the heuristic is meaningless and would discard whatever recovered.
    if not only and len(failures) > len(hospitals) / 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
