#!/usr/bin/env python3
"""Ingest a pre-downloaded MRF for one hospital: ingest_local.py <slug> <file>.

For hospitals whose files must be fetched outside the scraper (oversized
for runners, IP-blocked CDNs, flaky hosts needing manual resume). Runs the
same storage/summary/meta pipeline as scrape.py's process().

Sharded content is written into the companion raw repo (a sibling
hospital-price-history-raw clone, or RAW_REPO_DIR); remember to commit and
push BOTH repos, raw first. Consider archiving the original download with
archive_snapshot.py before deleting it.
"""

import json
import sys
from pathlib import Path

import shutil

from scrape import (DATA, RAW_DATA, ROOT, MAX_STORED_BYTES, MAX_SHARD_TOTAL,
                    clear_content, discover_mrf_url, ext_of, head,
                    local_fingerprint, normalize_payload, sha256_file,
                    store_sharded, store_summarized, utcnow, wants_impersonate)


def main():
    slug, path = sys.argv[1], Path(sys.argv[2])
    hospital = next(h for h in json.loads((ROOT / "hospitals.json").read_text())
                    if h["slug"] == slug)
    outdir = DATA / slug
    outdir.mkdir(parents=True, exist_ok=True)
    meta_path = outdir / "meta.json"
    old_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    imp = wants_impersonate(hospital)
    mrf_url = discover_mrf_url(hospital, imp)
    h = head(mrf_url, impersonate=imp, max_time=hospital.get("curl_max_time"))
    name = path.name
    size = path.stat().st_size

    clear_content(outdir)
    shutil.rmtree(RAW_DATA / slug, ignore_errors=True)
    if size <= MAX_SHARD_TOTAL:
        payload = normalize_payload(name, path.read_bytes())
        import hashlib
        sha = hashlib.sha256(payload).hexdigest()
        if len(payload) <= MAX_STORED_BYTES:
            (outdir / f"standardcharges{ext_of(name)}").write_bytes(payload)
            mode = "stored"
        else:
            mode = store_sharded(RAW_DATA / slug, name, payload)
            if mode == "metadata-only":
                mode = store_summarized(outdir, name, path)
        if mode in ("stored", "sharded"):
            store_summarized(outdir, name, path)
    else:
        sha = sha256_file(path)
        mode = store_summarized(outdir, name, path)

    meta = {
        "system": hospital["system"],
        "location_name": hospital["location_name"],
        "mrf_url": mrf_url,
        "source_filename": name,
        "sha256": sha,
        "size_bytes": size,
        "source_last_modified": h.get("Last-Modified"),
        "source_etag": h.get("ETag"),
        "transfer_fingerprint": local_fingerprint(path),
        "status": mode,
        "first_seen": old_meta.get("first_seen") or utcnow(),
        "last_changed": utcnow(),
        "note": "ingested from a local fetch (see README notes)",
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{slug}: ingested ({size:,} bytes, {mode})")


if __name__ == "__main__":
    main()
