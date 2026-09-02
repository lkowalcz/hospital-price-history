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

import hashlib
import json
import sys
from pathlib import Path

from scrape import (DATA, ROOT, MAX_SHARD_TOTAL, SUMMARY_VERSION,
                    archiving_enabled, discover_mrf_url, head,
                    local_fingerprint, normalize_payload, sha256_file,
                    store_snapshot, try_cold_store, utcnow, wants_impersonate)


def main():
    slug, path = sys.argv[1], Path(sys.argv[2])
    hospital = next(h for h in json.loads((ROOT / "hospitals.json").read_text())
                    if h["slug"] == slug)
    outdir = DATA / slug
    outdir.mkdir(parents=True, exist_ok=True)
    meta_path = outdir / "meta.json"
    old_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    imp = wants_impersonate(hospital) or bool(old_meta.get("fetch_escalated"))
    mrf_url = discover_mrf_url(hospital, imp)
    h = head(mrf_url, impersonate=imp, max_time=hospital.get("curl_max_time"))
    name = path.name
    size = path.stat().st_size

    if size <= MAX_SHARD_TOTAL:
        payload = normalize_payload(name, path.read_bytes())
        sha = hashlib.sha256(payload).hexdigest()
    else:
        payload = None
        sha = sha256_file(path)
    if sha == old_meta.get("sha256"):
        print(f"{slug}: content identical to the stored snapshot; nothing to do")
        return

    mode = store_snapshot(slug, name, payload, path)

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
    if (outdir / "summary.csv").exists():
        meta["summary_version"] = SUMMARY_VERSION
    if old_meta.get("fetch_escalated"):
        meta["fetch_escalated"] = old_meta["fetch_escalated"]
    if mode == "summarized" and archiving_enabled():
        try_cold_store(slug, path, sha, meta)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{slug}: ingested ({size:,} bytes, {mode})")


if __name__ == "__main__":
    main()
