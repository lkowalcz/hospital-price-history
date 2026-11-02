"""Plan bounded extra downloads from committed capture and collection evidence.

Run-start reservations survive failed/interrupted attempts and same-day reruns.
They budget two non-retrying transfers per hospital (the transport fallback).
Normal change-triggered collection and HTTP overhead are outside this budget.
"""

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from collection_health import complete_bytes, events, instant, now, policy_for


def observation_records(root, *, include_spool=True):
    # A local log can extend its published prefix. Never count it twice.
    logs = {}
    folders = [Path(root) / "observations"]
    if include_spool:
        folders.append(Path(root) / ".collection-spool/observations")
    for folder in folders:
        for path in sorted(folder.glob("*/*.jsonl")):
            key = path.relative_to(folder).as_posix()
            data = complete_bytes(path)
            old = logs.get(key, b"")
            if not (data.startswith(old) or old.startswith(data)):
                raise ValueError(f"Divergent collection evidence: {key}")
            logs[key] = data if len(data) > len(old) else old
    return [events(data) for data in logs.values()]


def report(root, hospitals, host, *, at=None, logs=None):
    root = Path(root)
    at = at or now()
    clock = instant(at)
    policy = policy_for(root)
    config = policy.get("original_capture", {})
    limits = config.get("hosts", {}).get(host)
    if not limits:
        return {"host": host, "evaluated_at": at, "selected": {}, "hospitals": []}
    for key in ("max_files_per_day", "max_bytes_per_day", "max_download_bytes"):
        if type(limits.get(key)) is not int or limits[key] <= 0:
            raise ValueError(f"Invalid original-capture limit: {key}")
    interval = timedelta(days=config["full_download_interval_days"])
    retry = timedelta(hours=config["retry_interval_hours"])
    logs = observation_records(root) if logs is None else logs
    reserved_bytes = reserved_files = 0
    last_full, last_reserved = {}, {}
    for records in logs:
        start = records[0]
        for slug, reservation in start.get("original_capture_plan", {}).items():
            when = instant(start["at"])
            last_reserved[slug] = max(last_reserved.get(slug, when), when)
            if start["host"] == host and when.date() == clock.date():
                reserved_files += 1
                reserved_bytes += reservation["reserved_bytes"]
        for event in records:
            if (event["event"] == "attempt_finished" and event.get("outcome") in {"changed", "unchanged"}
                    and event.get("method") == "full_download"):
                when = instant(event["downloaded_at"])
                slug = event["slug"]
                last_full[slug] = max(last_full.get(slug, when), when)

    candidates, entries = [], []
    for hospital in hospitals:
        slug = hospital["slug"]
        if policy["overrides"].get(slug, policy["default_host"]) != host:
            continue
        path = root / "data" / slug
        meta = json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else {}
        sha = meta.get("sha256")
        receipts = [json.loads(p.read_text()) for p in (path / "originals").glob("*.json")]
        matches = [r for r in receipts if sha and r.get("snapshot_sha256") == sha
                   and r.get("status") == "archived" and r.get("artifact_kind") == "download"]
        legacy = bool(sha and (meta.get("cold_storage") or {}).get("sha256") == sha)
        coverage = "exact_download" if matches else "legacy_original" if legacy else "missing"
        dates = [instant(r["captured_at"]) for r in matches]
        if slug in last_full:
            dates.append(last_full[slug])
        latest = max(dates, default=None)
        reason = "missing_original" if coverage == "missing" else "periodic_full_download"
        # Leave room for modest source growth; the enforced cap, not this
        # estimate, is reserved. Unknown sizes receive the host's full cap.
        size = max([meta.get("size_bytes", 0)] + [r.get("download_bytes", 0) for r in receipts])
        cap = min(limits["max_download_bytes"], max(10_000_000, (size * 5 + 3) // 4)) if size else limits["max_download_bytes"]
        reservation = 2 * (cap + 1)  # each streaming transport may read one excess byte
        state = "due"
        if not sha or meta.get("size_bytes") == 0:
            state = "source_unusable"
        elif any(r.get("status") == "pending" for r in receipts):
            state = "upload_pending"
        elif coverage != "missing" and latest and clock < latest + interval:
            state = "current"
        elif slug in last_reserved and clock < last_reserved[slug] + retry:
            state = "retry_wait"
        elif size > limits["max_download_bytes"] or reservation > limits["max_bytes_per_day"]:
            state = "over_budget"
        entry = {"slug": slug, "coverage": coverage, "last_full_download_at": latest.isoformat() if latest else None,
                 "reason": reason, "state": state, "download_limit_bytes": cap, "reserved_bytes": reservation}
        entries.append(entry)
        if state == "due":
            # Missing originals first; rotate failures behind never-tried files.
            candidates.append((coverage != "missing", last_reserved.get(slug, instant("1970-01-01T00:00:00Z")),
                               latest or instant("1970-01-01T00:00:00Z"), slug, entry))
    selected = {}
    for *_, entry in sorted(candidates):
        if (reserved_files >= limits["max_files_per_day"]
                or reserved_bytes + entry["reserved_bytes"] > limits["max_bytes_per_day"]):
            entry["state"] = "daily_budget"
            continue
        selected[entry["slug"]] = {k: entry[k] for k in ("reason", "download_limit_bytes", "reserved_bytes")}
        entry["state"] = "selected"
        reserved_bytes += entry["reserved_bytes"]
        reserved_files += 1
    return {"host": host, "evaluated_at": at, "limits": limits,
            "reserved_files": reserved_files, "reserved_bytes": reserved_bytes,
            "selected": selected, "hospitals": entries}


def plan(root, host, slugs):
    if os.environ.get("ORIGINAL_CAPTURE_ROTATION") != "1" or os.environ.get("PRESERVE_ORIGINALS") != "1":
        return {}
    hospitals = [{"slug": s} for s in slugs]
    return report(root, hospitals, host)["selected"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["github-actions", "pi"], required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    print(json.dumps(report(root, json.loads((root / "hospitals.json").read_text()), args.host), indent=2))


if __name__ == "__main__":
    main()
