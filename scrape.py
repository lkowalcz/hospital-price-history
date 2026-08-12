#!/usr/bin/env python3
"""Git-scrape hospital price transparency machine-readable files (MRFs).

For each hospital in hospitals.json:
  1. Fetch the CMS-mandated /cms-hpt.txt discovery file and find the block
     matching `location_name` to get the current MRF URL.
  2. Download the MRF (unzipping if needed) and normalize it.
  3. If the content hash changed, write it to data/<slug>/ along with meta.json.

Files larger than MAX_CONTENT_BYTES are tracked metadata-only: we still hash
and record them (so git history shows *when* they changed) but don't store
the content itself.

Stdlib only. Designed to run under GitHub Actions on a cron schedule, with
`git commit` happening in the workflow only when something changed.
"""

import hashlib
import io
import json
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MAX_CONTENT_BYTES = 80 * 1024 * 1024  # git-friendly cap; larger files -> metadata only
USER_AGENT = (
    "hospital-price-history/0.1 (git-scraping archive of public CMS price "
    "transparency files; contact: lkowalcz@gmail.com)"
)


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


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
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected 1 file in zip, found {names}")
        return Path(names[0]).name, zf.read(names[0])


def normalize_payload(name, payload):
    """Stable representation so diffs reflect real changes, not formatting."""
    if name.casefold().endswith(".json") and len(payload) <= MAX_CONTENT_BYTES:
        try:
            obj = json.loads(payload)
            return json.dumps(obj, indent=1, sort_keys=True).encode() + b"\n"
        except (ValueError, MemoryError):
            return payload
    if name.casefold().endswith((".csv", ".txt")):
        return payload.replace(b"\r\n", b"\n")
    return payload


def ext_of(name):
    suffix = Path(name).suffix.casefold()
    return suffix if suffix in (".csv", ".json", ".xml", ".txt") else ".bin"


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

    body, headers = fetch(mrf_url)
    name, payload = extract_payload(body, headers, mrf_url)
    payload = normalize_payload(name, payload)
    sha = hashlib.sha256(payload).hexdigest()

    if sha == old_meta.get("sha256") and mrf_url == old_meta.get("mrf_url"):
        return None  # nothing changed; leave the working tree untouched

    stored = len(payload) <= MAX_CONTENT_BYTES
    if stored:
        (outdir / f"standardcharges{ext_of(name)}").write_bytes(payload)

    meta = {
        "system": hospital["system"],
        "location_name": hospital["location_name"],
        "mrf_url": mrf_url,
        "source_filename": name,
        "sha256": sha,
        "size_bytes": len(payload),
        "source_last_modified": headers.get("Last-Modified"),
        "status": "stored" if stored else "metadata-only (too large for git)",
        "first_seen": old_meta.get("first_seen") or utcnow(),
        "last_changed": utcnow(),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return f"{slug}: {'updated' if old_meta else 'first snapshot'} ({len(payload):,} bytes)"


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
