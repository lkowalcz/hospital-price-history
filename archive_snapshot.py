#!/usr/bin/env python3
"""Archive a raw MRF snapshot to the Internet Archive: archive_snapshot.py <slug> <file>.

The daily pipeline does this automatically for `summarized` hospitals when
IA credentials are present (see docs/OPERATIONS.md). This is the manual
path for files that never pass through a runner — the Mayo giants over the
CI download cap, or a download kept from a local ingest. Run it right
after ingest_local.py, before deleting the download; it zstd-compresses
the file, uploads it, and records the item under "cold_storage" in
data/<slug>/meta.json.

Requires the `zstd` binary and the internetarchive CLI (`pip install
internetarchive; ia configure`).
"""

import json
import sys
from pathlib import Path

from scrape import DATA, MAX_SHARD_TOTAL, cold_store, sha256_file


def main():
    slug, path = sys.argv[1], Path(sys.argv[2])
    meta_path = DATA / slug / "meta.json"
    meta = json.loads(meta_path.read_text())
    sha = meta["sha256"]
    if (meta.get("cold_storage") or {}).get("sha256") == sha:
        print(f"{slug}: this snapshot is already archived at {meta['cold_storage']['url']}")
        return
    if meta.get("size_bytes", 0) > MAX_SHARD_TOTAL and sha256_file(path) != sha:
        sys.exit(f"{slug}: {path} is not the snapshot meta.json describes "
                 f"(sha256 differs); ingest it first with ingest_local.py")

    meta["cold_storage"] = cold_store(slug, path, sha)
    meta.pop("cold_storage_attempt", None)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    cs = meta["cold_storage"]
    print(f"{slug}: archived at {cs['url']} ({cs['compressed_bytes']:,} bytes "
          "compressed); meta.json updated")


if __name__ == "__main__":
    main()
