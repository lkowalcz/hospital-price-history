#!/usr/bin/env python3
"""Archive a raw MRF snapshot to the Internet Archive: archive_snapshot.py <slug> <file>.

For the GB-class files whose repo representation is a lossy summary.csv
(status "summarized"), this preserves the original bytes: zstd-compress the
download, upload it to archive.org, and record the item URL + sha256 in
data/<slug>/meta.json under "cold_storage".

Requires the `zstd` binary and the internetarchive CLI (`pip install
internetarchive; ia configure`). Run right after ingest_local.py, before
deleting the local download.
"""

import json
import subprocess
import sys
from pathlib import Path

from scrape import DATA, sha256_file, utcnow


def main():
    slug, path = sys.argv[1], Path(sys.argv[2])
    meta_path = DATA / slug / "meta.json"
    meta = json.loads(meta_path.read_text())

    sha = sha256_file(path)
    if meta.get("cold_storage", {}).get("sha256") == sha:
        print(f"{slug}: this snapshot is already archived")
        return

    zst = path.with_name(path.name + ".zst")
    if not zst.exists():
        subprocess.run(["zstd", "-T0", "-12", "--long=27", "-o", str(zst),
                        str(path)], check=True)

    # One item per (hospital, content hash): re-archiving a new snapshot
    # creates a new item, so old snapshots stay retrievable.
    item = f"hospital-price-history-{slug}-{sha[:12]}"
    subprocess.run(
        ["ia", "upload", item, str(zst),
         "--metadata", "collection:opensource",
         "--metadata", f"title:Hospital price MRF snapshot: {slug} ({utcnow()[:10]})",
         "--metadata", "subject:hospital price transparency",
         "--metadata",
         f"description:Raw machine-readable standard-charges file for {slug}, "
         f"archived by https://github.com/lkowalcz/hospital-price-history. "
         f"sha256 of the uncompressed file: {sha}"],
        check=True)

    meta["cold_storage"] = {
        "url": f"https://archive.org/details/{item}",
        "sha256": sha,
        "compressed_bytes": zst.stat().st_size,
        "archived": utcnow(),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{slug}: archived as {item} "
          f"({zst.stat().st_size:,} bytes compressed); meta.json updated")


if __name__ == "__main__":
    main()
