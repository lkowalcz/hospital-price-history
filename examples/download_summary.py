#!/usr/bin/env python3
"""Download a pinned summary and provenance using only Python's standard library.

python examples/download_summary.py rush-university --output rush.csv
python examples/download_summary.py rush-university --snapshot FULL_COMMIT --output rush-old.csv
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

CATALOG = "https://lukekowalczyk.com/hospital-price-history/data/catalog.json"


def request(url):
    return urlopen(Request(url, headers={"User-Agent": "HospitalPriceHistory-data-example/1.0"}), timeout=60)


def read_json(url):
    with request(url) as response:
        data = json.load(response)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported catalog schema; update this example before continuing.")
    return data


def choose_snapshot(manifest, revision=None):
    snapshots = [s for s in manifest["snapshots"] if s["summary_url"]]
    if revision:
        snapshots = [s for s in snapshots if revision in (s["revision"], s["snapshot_id"])]
    if not snapshots:
        raise ValueError("No downloadable summary matches this hospital and revision.")
    return snapshots[-1]


def download_verified(snapshot, output, provenance):
    """Verify the Git blob ID before exposing a file under the requested name."""
    sidecar = output.with_name(output.name + ".provenance.json")
    if output.exists() or sidecar.exists():
        raise FileExistsError("Choose a new output name; the summary or provenance file already exists.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hph-download-", dir=output.parent) as temp:
        downloaded = Path(temp) / "summary.csv"
        with request(snapshot["summary_url"]) as response, downloaded.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
        digest = hashlib.sha1(f"blob {downloaded.stat().st_size}\0".encode())
        with downloaded.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != snapshot["summary_git_blob"]:
            raise ValueError("Downloaded bytes do not match the recorded Git blob ID; no file saved.")
        evidence = Path(temp) / "provenance.json"
        evidence.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        # Hard links are exclusive: a concurrent process cannot be overwritten.
        os.link(evidence, sidecar)
        try:
            os.link(downloaded, output)
        except OSError:
            sidecar.unlink()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Hospital slug from the catalog")
    parser.add_argument("--snapshot", help="Full revision or snapshot_id; defaults to the latest available summary")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-url", default=CATALOG)
    args = parser.parse_args()
    catalog = read_json(args.catalog_url)
    hospital = next((h for h in catalog["hospitals"] if h["slug"] == args.slug), None)
    if hospital is None:
        parser.error("Hospital slug is not in this catalog.")
    manifest = read_json(hospital["snapshots_url"])
    if manifest["catalog_revision"] != catalog["catalog_revision"]:
        raise ValueError("The catalog changed during this download. Retry to use a consistent revision.")
    snapshot = choose_snapshot(manifest, args.snapshot)
    if hospital["issues"]:
        print("Recorded hospital issues: " + ", ".join(hospital["issues"]), file=sys.stderr)
    if snapshot["summary_version"] < 2 or snapshot.get("summary_warning"):
        print("Review this snapshot's method and warnings before analysis.", file=sys.stderr)
    download_verified(snapshot, args.output,
                      {"schema_version": 1, "catalog_revision": catalog["catalog_revision"],
                       "slug": args.slug, "current_hospital_issues": hospital["issues"], "snapshot": snapshot})
    print(f"Saved {args.output} and {args.output.name}.provenance.json")
    print(f"Snapshot: {snapshot['snapshot_id']} (summary method {snapshot['summary_version']})")


if __name__ == "__main__":
    main()
