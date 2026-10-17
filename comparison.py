"""Export immutable hospital summary snapshots for the comparison page.

Only summaries and metadata are read from history; historical MRF blobs
can be gigabytes and are never needed to build the page.
"""

import csv
import html
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "site_assets"
COMPARISON_SLUGS = {"rush-university"}
COLUMNS = ["code_type", "code", "description", "gross_charge", "discounted_cash",
           "min_negotiated", "max_negotiated", "payer_entries"]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def source_link(meta, revision, slug, repo, raw_repo):
    archived = meta.get("cold_storage") or {}
    if archived.get("sha256") and archived.get("sha256") == meta.get("sha256"):
        url = archived.get("url", "")
        if url.startswith("https://archive.org/"):
            return {"url": url, "label": "Archived original"}
    raw_commit = meta.get("raw_commit")
    if meta.get("status") == "sharded" and raw_commit:
        if not re.fullmatch(r"[0-9a-f]{40}", raw_commit):
            raise ValueError("Invalid raw snapshot revision")
        return {"url": f"{raw_repo}/tree/{raw_commit}/data/{slug}",
                "label": "Archived source shards"}
    if meta.get("status") in {"stored", "sharded"}:
        return {"url": f"{repo}/tree/{revision}/data/{slug}",
                "label": "Archived source files"}
    return None


def export_history(root, slug, destination, repo, raw_repo):
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValueError("Invalid hospital slug")
    path = f"data/{slug}/summary.csv"
    # Include method changes recorded only in metadata, but skip ordinary
    # validator/failure updates with the unchanged (summary blob, method).
    history = git(root, "log", "--reverse", "--format=%H%x09%cI", "--",
                  path, f"data/{slug}/meta.json")
    snapshots = []
    previous = None
    destination.mkdir(parents=True, exist_ok=True)
    for line in history.splitlines():
        revision, recorded = line.split("\t")
        # Git log can include a deletion; it is not a usable snapshot.
        exists = subprocess.run(["git", "cat-file", "-e", f"{revision}:{path}"],
                                cwd=root, capture_output=True)
        if exists.returncode:
            continue
        meta = json.loads(git(root, "show", f"{revision}:data/{slug}/meta.json"))
        version = meta.get("summary_version", 1)
        blob = git(root, "rev-parse", f"{revision}:{path}").strip()
        if (blob, version) == previous:
            continue
        previous = (blob, version)
        reader = csv.DictReader(io.StringIO(git(root, "show", f"{revision}:{path}")))
        if reader.fieldnames != COLUMNS:
            raise ValueError(f"{slug} at {revision}: unexpected summary columns")
        rows, keys = [], set()
        for row in reader:
            if None in row or any(row[column] is None for column in COLUMNS):
                raise ValueError(f"{slug} at {revision}: malformed summary row")
            key = tuple(row[c] for c in COLUMNS[:3])
            if key in keys:
                raise ValueError(f"{slug} at {revision}: duplicate summary key")
            keys.add(key)
            rows.append([row[c] for c in COLUMNS])
        if not rows:
            continue
        snapshot = {
            "id": revision, "recorded_at": recorded,
            "source_observed_at": meta.get("last_changed"),
            "summary_version": version, "row_count": len(rows),
            "warning": (meta.get("summary_warning") or {}).get("detail"),
            "sha256": meta.get("sha256"),
            "summary_url": f"{repo.replace('https://github.com/', 'https://raw.githubusercontent.com/', 1)}/{revision}/{path}",
            "metadata_url": f"{repo}/blob/{revision}/data/{slug}/meta.json",
            "source": source_link(meta, revision, slug, repo, raw_repo),
            "data_url": f"snapshots/{revision}.json",
        }
        (destination / f"{revision}.json").write_text(
            json.dumps({"id": revision, "columns": COLUMNS, "rows": rows},
                       ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        snapshots.append(snapshot)
    return snapshots


def build_comparison(hospital, outdir, root, base, repo, raw_repo):
    slug = hospital["slug"]
    if slug not in COMPARISON_SLUGS:
        return None
    destination = outdir / "hospitals" / slug / "compare"
    snapshots = export_history(root, slug, destination / "snapshots", repo, raw_repo)
    if len(snapshots) < 2:
        return None
    for name in ("comparison.css", "comparison.mjs", "comparison-model.mjs"):
        shutil.copyfile(ASSETS / name, destination / name)
    canonical = f"{base}/hospitals/{slug}/compare/"
    manifest = {
        "hospital": hospital["location_name"], "slug": slug,
        "snapshots": snapshots,
        "methodology_url": f"{repo}/blob/main/docs/DATA.md",
    }
    # JSON script blocks must not be terminable by source text.
    encoded = json.dumps(manifest, ensure_ascii=False).replace("<", "\\u003c")
    replacements = {
        "__HOSPITAL__": html.escape(hospital["location_name"]),
        "__SYSTEM__": html.escape(hospital["system"]),
        "__BASE__": html.escape(base, quote=True),
        "__HOSPITAL_URL__": html.escape(f"{base}/hospitals/{slug}/", quote=True),
        "__CANONICAL__": html.escape(canonical, quote=True),
        "__MANIFEST__": encoded,
    }
    template = (ASSETS / "comparison.html").read_text()
    # Substitute in a single pass so source strings cannot become placeholders.
    rendered = re.sub("|".join(map(re.escape, replacements)),
                      lambda match: replacements[match.group()], template)
    (destination / "index.html").write_text(rendered, encoding="utf-8")
    return canonical
