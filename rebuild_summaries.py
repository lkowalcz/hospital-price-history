#!/usr/bin/env python3
"""Rebuild summary.csv for hospitals from content already stored in data/.

Used to backfill the uniform analytics layer without re-downloading;
ongoing runs regenerate summaries automatically whenever content changes.

Sharded hospitals are rebuilt from the companion raw repo (RAW_REPO_DIR, or
a sibling hospital-price-history-raw clone). That must be a FULL checkout:
under the workflow's sparse checkout the shards are not on disk, so this
script is local-only unless you `git sparse-checkout add data/<slug>` first.
"""

import json
import sys
import tempfile
from pathlib import Path

from scrape import (DATA, RAW_DATA, aggregate_items, summarize_csv,
                    summarize_json, write_summary)


def reconstruct_csv(hospdir, workdir):
    """Reassemble a sharded CSV (header rows + all shard rows) into one file."""
    out = workdir / "reassembled.csv"
    with open(out, "w") as f:
        f.write((hospdir / "_header.csv").read_text())
        for shard in sorted((hospdir / "shards").glob("*.csv")):
            f.write(shard.read_text())
    return out


def jsonl_items(hospdir):
    for shard in sorted((hospdir / "shards").glob("*.jsonl")):
        with open(shard) as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def rebuild(hospdir, workdir, force=False):
    out = hospdir / "summary.csv"
    if out.exists() and not force:
        return "exists"
    stored_csv = hospdir / "standardcharges.csv"
    stored_json = hospdir / "standardcharges.json"
    rawdir = RAW_DATA / hospdir.name
    if stored_csv.exists():
        return "ok" if summarize_csv(stored_csv, out) else "failed"
    if stored_json.exists():
        return "ok" if summarize_json(stored_json, out) else "failed"
    if (rawdir / "_header.csv").exists():
        return "ok" if summarize_csv(reconstruct_csv(rawdir, workdir), out) else "failed"
    if (rawdir / "_header.json").exists():
        return "ok" if write_summary(aggregate_items(jsonl_items(rawdir)), out) else "failed"
    return "no content"


def main():
    force = "--force" in sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        for hospdir in sorted(DATA.iterdir()):
            if hospdir.is_dir():
                print(f"{hospdir.name}: {rebuild(hospdir, Path(tmp), force)}", flush=True)


if __name__ == "__main__":
    main()
