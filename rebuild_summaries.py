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

from scrape import (DATA, RAW_DATA, SUMMARY_VERSION, aggregate_items,
                    summarize_csv, summarize_json, write_summary)


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


def stamp_version(hospdir):
    """Record which summarizer produced summary.csv, so compute_index.py can
    tell a methodology change from a price change."""
    mp = hospdir / "meta.json"
    if not mp.exists():
        return
    meta = json.loads(mp.read_text())
    if meta.get("summary_version") != SUMMARY_VERSION:
        meta["summary_version"] = SUMMARY_VERSION
        mp.write_text(json.dumps(meta, indent=2) + "\n")


def rebuild(hospdir, workdir, force=False):
    out = hospdir / "summary.csv"
    if out.exists() and not force:
        return "exists"
    stored_csv = hospdir / "standardcharges.csv"
    stored_json = hospdir / "standardcharges.json"
    rawdir = RAW_DATA / hospdir.name
    if stored_csv.exists():
        ok = summarize_csv(stored_csv, out)
    elif stored_json.exists():
        ok = summarize_json(stored_json, out)
    elif (rawdir / "_header.csv").exists():
        ok = summarize_csv(reconstruct_csv(rawdir, workdir), out)
    elif (rawdir / "_header.json").exists():
        ok = write_summary(aggregate_items(jsonl_items(rawdir)), out)
    else:
        return "no content"
    if ok:
        stamp_version(hospdir)
    return "ok" if ok else "failed"


def main():
    force = "--force" in sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        for hospdir in sorted(DATA.iterdir()):
            if hospdir.is_dir():
                print(f"{hospdir.name}: {rebuild(hospdir, Path(tmp), force)}", flush=True)


if __name__ == "__main__":
    main()
