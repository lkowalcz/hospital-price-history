"""Publish versioned data discovery and quality reports without copying MRFs.

History reads only small metadata blobs. Git's raw diff supplies summary
object IDs without downloading historical summaries or source files.
"""

import csv
import json
import math
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from collection_health import build_health

SCHEMA_VERSION = 1
COLUMNS = ["code_type", "code", "description", "gross_charge", "discounted_cash",
           "min_negotiated", "max_negotiated", "payer_entries"]
ISSUES = {
    "metadata_missing": "No metadata has been recorded.",
    "fetch_failed": "The most recently recorded download failure is unresolved.",
    "listing_missing": "The hospital is missing from its discovery listing.",
    "summary_missing": "No current summary is available.",
    "summary_empty": "The current summary has no entries.",
    "summary_schema_invalid": "The summary columns differ from the documented schema.",
    "summary_malformed_rows": "Some summary rows have the wrong number of fields.",
    "summary_duplicate_keys": "Some summary entries repeat a code/type/description key.",
    "summary_invalid_numbers": "Some price or payer-count values are not valid finite numbers.",
    "summary_warning": "The scraper flagged a large reduction in summary coverage.",
    "legacy_summary_method": "The summary uses the legacy, order-dependent method.",
    "source_unavailable": "No archived source link is recorded for the current content hash.",
    "archive_hash_mismatch": "The recorded cold-storage item belongs to a different snapshot.",
    "collection_check_overdue": "The scheduled collector has not attempted a check within its interval and grace period.",
    "collection_verification_overdue": "No successful source verification was recorded within its interval and grace period.",
    "collection_interrupted": "An interrupted collection run is missing this hospital's outcome.",
    "collection_failed": "The latest observed check failed or could not find the source listing.",
    "observed_source_not_current": "The latest verified source differs from the committed snapshot; inspect its observation log.",
    "archive_upload_failed": "The latest check records an unsuccessful archive upload, separately from source verification.",
}


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True)


def raw_url(repo, revision, path):
    return f"{repo.replace('https://github.com/', 'https://raw.githubusercontent.com/', 1)}/{revision}/{path}"


def recorded_source(root, meta, revision, slug, repo, raw_repo):
    """Return a source supported by this metadata revision, never a live URL."""
    provenance = raw_url(repo, revision, f"data/{slug}/meta.json")
    archived = meta.get("cold_storage") or {}
    if meta.get("sha256") and archived.get("sha256") == meta["sha256"]:
        if archived.get("url", "").startswith("https://archive.org/details/"):
            return {"kind": "archived_original", "url": archived["url"],
                    "metadata_url": provenance, "file_sha256": archived.get("file_sha256")}
    if meta.get("status") == "sharded" and meta.get("raw_commit"):
        ref = meta["raw_commit"]
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise ValueError(f"{slug}: invalid raw_commit")
        return {"kind": "source_shards", "url": f"{raw_repo}/tree/{ref}/data/{slug}",
                "metadata_url": provenance, "revision": ref}
    if meta.get("status") in {"stored", "sharded"}:
        names = git(root, "ls-tree", "-r", "--name-only", revision, "--", f"data/{slug}/").splitlines()
        originals = [name for name in names if Path(name).name.startswith("standardcharges.")]
        if originals:
            return {"kind": "normalized_file", "url": raw_url(repo, revision, originals[0]),
                    "metadata_url": provenance, "revision": revision}
        if any("/shards/" in name for name in names):
            return {"kind": "source_shards", "url": f"{repo}/tree/{revision}/data/{slug}",
                    "metadata_url": provenance, "revision": revision}
    return None


def history_events(root, revision):
    """Walk mainline states, including the result of merges, in commit order."""
    output = git(root, "log", "--first-parent", "--reverse", "--raw", "--no-abbrev",
                 "--no-renames", "--diff-merges=first-parent", "--format=%H%x09%cI",
                 revision, "--", "data/*/meta.json", "data/*/summary.csv")
    event = None
    for line in output.splitlines():
        if not line:
            continue
        if line.startswith(":"):
            fields, path = line.split("\t", 1)
            parts = path.split("/")
            if len(parts) == 3 and parts[2] in {"meta.json", "summary.csv"}:
                oid = fields.split()[3]
                event[2].append((parts[1], parts[2], None if set(oid) == {"0"} else oid))
        else:
            if event:
                yield event
            commit, recorded = line.split("\t")
            event = (commit, recorded, [])
    if event:
        yield event


def collect_history(root, revision, slugs, repo, raw_repo):
    states, histories, sources, previous, metadata = {}, {s: [] for s in slugs}, {}, {}, {}
    for commit, recorded, changes in history_events(root, revision):
        affected = set()
        for slug, name, oid in changes:
            if slug in histories:
                states.setdefault(slug, {})[name] = oid
                affected.add(slug)
        for slug in sorted(affected):
            state = states[slug]
            oid = state.get("meta.json")
            if not oid:
                previous.pop(slug, None)
                continue
            if oid not in metadata:
                metadata[oid] = json.loads(git(root, "cat-file", "blob", oid))
            meta = metadata[oid]
            sha, summary = meta.get("sha256"), state.get("summary.csv")
            version = meta.get("summary_version", 1) if summary else None
            identity = (sha, summary, version)
            if not sha and not summary:
                continue
            source = recorded_source(root, meta, commit, slug, repo, raw_repo)
            if source and sha:
                known = sources.get((slug, sha))
                # Keep the first evidence for an unchanged link, but accept
                # corrected raw-repo pins (for example after a history graft).
                # A recorded original takes precedence over normalized copies.
                changed_link = known is None or any(source.get(key) != known.get(key)
                                                     for key in ("kind", "url", "revision", "file_sha256"))
                preferred = known is None or known["kind"] != "archived_original" or source["kind"] == "archived_original"
                if changed_link and preferred:
                    sources[(slug, sha)] = source
            if previous.get(slug) == identity:
                continue
            prior = previous.get(slug)
            kind = "source" if not prior or prior[0] != sha else "summary"
            if prior and prior[1] and not summary:
                kind = "summary_removed"
            histories[slug].append({
                "snapshot_id": f"{slug}:{commit}", "revision": commit,
                "recorded_at": recorded, "change_kind": kind,
                "source_observed_at": meta.get("last_changed"),
                "source_sha256": sha, "source_size_bytes": meta.get("size_bytes"),
                "summary_version": version, "summary_git_blob": summary,
                "summary_url": raw_url(repo, commit, f"data/{slug}/summary.csv") if summary else None,
                "metadata_url": raw_url(repo, commit, f"data/{slug}/meta.json"),
                "summary_warning": meta.get("summary_warning"),
            })
            previous[slug] = identity
    # Archiving may occur after collection. Enrich earlier snapshots only with
    # a source whose content identity matches, and pin that evidence separately.
    for slug, snapshots in histories.items():
        for snapshot in snapshots:
            snapshot["source"] = sources.get((slug, snapshot["source_sha256"]))
    return histories


def inspect_summary(path):
    stats = {"row_count": 0, "coded_rows": 0, "duplicate_keys": 0,
             "malformed_rows": 0, "invalid_numbers": 0,
             "positive_prices": {column: 0 for column in COLUMNS[3:7]}}
    if not path.exists():
        return None, ["summary_missing"]
    keys, issues = set(), []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != COLUMNS:
            return stats, ["summary_schema_invalid"]
        for row in reader:
            stats["row_count"] += 1
            if None in row or any(value is None for value in row.values()):
                stats["malformed_rows"] += 1
                continue
            key = tuple(row[column] for column in COLUMNS[:3])
            if key in keys:
                stats["duplicate_keys"] += 1
            keys.add(key)
            stats["coded_rows"] += bool(row["code"])
            for column in COLUMNS[3:]:
                value = row[column].strip()
                if not value:
                    continue
                try:
                    number = float(value)
                    if not math.isfinite(number) or (column == "payer_entries" and
                                                     (number < 0 or not number.is_integer())):
                        raise ValueError
                    if column != "payer_entries" and number > 0:
                        stats["positive_prices"][column] += 1
                except ValueError:
                    stats["invalid_numbers"] += 1
    if not stats["row_count"]:
        issues.append("summary_empty")
    for key in ("duplicate_keys", "malformed_rows", "invalid_numbers"):
        if stats[key]:
            issues.append("summary_" + key)
    return stats, issues


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def build_catalog(hospitals, root, outdir, base, repo, raw_repo):
    if git(root, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise ValueError("The catalog requires full commit history (fetch-depth: 0).")
    # Links must describe committed bytes, including when building locally.
    if git(root, "status", "--porcelain", "--untracked-files=all", "--", "data", "hospitals.json",
           "observations", "collection_policy.json").strip():
        raise ValueError("Commit data, roster, policy, and observation changes before publishing the catalog.")
    revision = git(root, "rev-parse", "HEAD").strip()
    envelope = {"schema_version": SCHEMA_VERSION, "catalog_revision": revision,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    slugs = [h["slug"] for h in hospitals]
    if len(set(slugs)) != len(slugs) or any(not re.fullmatch(r"[a-z0-9-]+", s) for s in slugs):
        raise ValueError("Hospital slugs must be unique and contain only lowercase letters, digits, and hyphens.")
    histories = collect_history(root, revision, slugs, repo, raw_repo)
    health = build_health(root, hospitals, envelope["generated_at"])
    health["catalog_revision"] = revision
    for run in health["runs"]:
        run["observation_url"] = raw_url(repo, revision, run["path"])
    for checked in health["hospitals"]:
        for key in ("latest_attempt", "last_successful_check"):
            if checked[key]:
                checked[key]["observation_url"] = raw_url(repo, revision, checked[key]["path"])
    checks = {checked["slug"]: checked for checked in health["hospitals"]}
    entries, reports = [], []
    for hospital in sorted(hospitals, key=lambda h: h["slug"]):
        slug = hospital["slug"]
        path = root / "data" / slug
        meta = json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else {}
        snapshots = histories[slug]
        latest = snapshots[-1] if snapshots else None
        source_gaps = sum(snapshot["source"] is None for snapshot in snapshots)
        stats, issues = inspect_summary(path / "summary.csv")
        if not meta:
            issues.append("metadata_missing")
        for field, issue in (("fetch_failures", "fetch_failed"), ("summary_warning", "summary_warning")):
            if meta.get(field):
                issues.append(issue)
        if meta.get("status") == "missing-from-hpt-txt":
            issues.append("listing_missing")
        if stats is not None and meta.get("summary_version", 1) < 2:
            issues.append("legacy_summary_method")
        if not latest or not latest["source"]:
            issues.append("source_unavailable")
        if meta.get("cold_storage") and meta["cold_storage"].get("sha256") != meta.get("sha256"):
            issues.append("archive_hash_mismatch")
        collection = checks[slug]
        issues.extend(collection["issues"])
        verified = (collection["last_successful_check"] or {}).get("result") or {}
        observed = (collection["latest_attempt"] or {}).get("result") or {}
        if verified.get("source_sha256") and verified["source_sha256"] != meta.get("sha256"):
            issues.append("observed_source_not_current")
        if observed.get("archive_status") == "failed":
            issues.append("archive_upload_failed")
        entry = {"slug": slug, "name": hospital["location_name"], "system": hospital["system"],
                 "status": meta.get("status"), "first_seen": meta.get("first_seen"),
                 "last_changed": meta.get("last_changed"),
                 "last_successful_check_at": collection["last_successful_check_at"],
                 "last_attempt_at": collection["last_attempt_at"],
                 "last_full_download_at": collection["last_full_download_at"], "collection": collection,
                 "snapshot_count": len(snapshots), "snapshots_without_archived_source": source_gaps,
                 "latest_snapshot": latest,
                 "summary_rows": stats["row_count"] if stats is not None else None,
                 "metadata_url": raw_url(repo, revision, f"data/{slug}/meta.json") if meta else None,
                 "snapshots_url": f"{base}/data/snapshots/{slug}.json",
                 "hospital_url": f"{base}/hospitals/{slug}/", "issues": sorted(issues)}
        entries.append(entry)
        reports.append({"slug": slug, "issues": sorted(issues), "summary": stats,
                        "snapshots_without_archived_source": source_gaps,
                        "fetch_failures": meta.get("fetch_failures"),
                        "summary_warning": meta.get("summary_warning")})
        write_json(outdir / "data" / "snapshots" / f"{slug}.json",
                   {**envelope, "slug": slug, "snapshots": snapshots})
    totals = {"hospitals": len(entries), "snapshots": sum(e["snapshot_count"] for e in entries),
              "snapshots_without_archived_source": sum(e["snapshots_without_archived_source"] for e in entries),
              "with_summary": sum(e["summary_rows"] is not None for e in entries),
              "with_archived_source": sum(bool(e["latest_snapshot"] and e["latest_snapshot"]["source"]) for e in entries),
              "with_issues": sum(bool(e["issues"]) for e in entries),
              "issues": dict(sorted(Counter(issue for e in entries for issue in e["issues"]).items()))}
    catalog = {**envelope, "quality_url": f"{base}/data/quality.json",
               "collection_health_url": f"{base}/data/collection-health.json",
               "collection_totals": health["totals"], "totals": totals, "hospitals": entries}
    write_json(outdir / "data" / "catalog.json", catalog)
    write_json(outdir / "data" / "collection-health.json", health)
    write_json(outdir / "data" / "quality.json",
               {**envelope, "totals": totals, "issue_definitions": ISSUES, "hospitals": reports})
    with (outdir / "data" / "catalog.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["slug", "name", "system", "status", "first_seen", "last_changed", "last_successful_check_at",
                  "last_attempt_at", "last_full_download_at", "scheduled_host", "check_state", "check_method",
                  "snapshot_count", "snapshots_without_archived_source", "summary_rows", "summary_url", "summary_version", "source_url",
                  "metadata_url", "snapshots_url", "issues", "catalog_revision"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for entry in entries:
            snapshot = entry["latest_snapshot"] or {}
            row = {key: entry.get(key) for key in fields}
            row.update(summary_url=snapshot.get("summary_url"), summary_version=snapshot.get("summary_version"),
                       scheduled_host=entry["collection"]["scheduled_host"], check_state=entry["collection"]["state"],
                       check_method=((entry["collection"]["latest_attempt"] or {}).get("result") or {}).get("method"),
                       source_url=(snapshot.get("source") or {}).get("url"), issues=";".join(entry["issues"]),
                       catalog_revision=revision)
            writer.writerow(row)
    return catalog
