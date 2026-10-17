"""Append-only collection evidence, independent of source/snapshot commits.

Each run owns one JSONL file. Spooling outside the worktree's tracked paths
allows a separate Git index to publish evidence even after an ingest fails.
No timestamps are inferred from metadata or commit dates.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUCCESS = {"changed", "unchanged"}
METHODS = {"headers", "sampled_fingerprint", "full_download"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Collection timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def git(root, *args, data=None, env=None):
    return subprocess.check_output(["git", "-C", str(root), *args], input=data,
                                   env=env, stderr=subprocess.PIPE)


def error_text(exc):
    # Transport errors may include expiring URL query credentials.
    return re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]+", r"\1?[redacted]", str(exc))[:500]


def complete_bytes(path):
    data = path.read_bytes()
    return data[:data.rfind(b"\n") + 1]  # a crash can leave a partial last write


def events(data):
    records = [json.loads(line) for line in data.splitlines()]
    if not records or records[0].get("event") != "run_started":
        raise ValueError("Collection log must start with run_started")
    start = records[0]
    seen, finished, ended = {}, set(), False
    for index, event in enumerate(records):
        if (event.get("schema_version") != 1 or event.get("run_id") != start["run_id"]
                or event.get("host") != start["host"] or ended):
            raise ValueError("Invalid collection event envelope/order")
        timestamp = instant(event["at"])
        if timestamp < instant(start["at"]):
            raise ValueError("Collection event predates run")
        kind = event["event"]
        if kind == "run_started":
            if index or len(set(event["planned_slugs"])) != len(event["planned_slugs"]):
                raise ValueError("Duplicate run start or planned slug")
        elif kind == "attempt_started":
            aid = event["attempt_id"]
            if aid in seen or event["slug"] not in start["planned_slugs"]:
                raise ValueError("Invalid attempt start")
            seen[aid] = event
        elif kind == "attempt_finished":
            aid = event["attempt_id"]
            attempt = seen.get(aid)
            if (not attempt or aid in finished or event["slug"] != attempt["slug"]
                    or timestamp < instant(attempt["at"])):
                raise ValueError("Unmatched collection result")
            finished.add(aid)
            if event["outcome"] not in SUCCESS | {"failed", "listing_missing", "deferred"}:
                raise ValueError("Unknown collection outcome")
            if event["outcome"] in SUCCESS:
                if event.get("method") not in METHODS or not re.fullmatch(r"[0-9a-f]{64}", event.get("source_sha256", "")):
                    raise ValueError("Successful check requires evidence and source identity")
                verified = instant(event["verified_at"])
                if not instant(attempt["at"]) <= verified <= timestamp:
                    raise ValueError("Verification time is outside the attempt")
                if event["method"] == "full_download" and event.get("downloaded_at") != event["verified_at"]:
                    raise ValueError("Full-download evidence must use download time")
            elif event.get("verified_at") or event.get("downloaded_at"):
                raise ValueError("Unsuccessful checks cannot advance verification")
        elif kind == "run_finished":
            if event["outcome"] not in {"completed", "interrupted"}:
                raise ValueError("Unknown run outcome")
            if event["outcome"] == "completed" and (
                    set(seen) != finished or {a["slug"] for a in seen.values()} != set(start["planned_slugs"])):
                raise ValueError("Completed run is missing hospital outcomes")
            ended = True
        else:
            raise ValueError("Unknown collection event")
    return records


class RunLog:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.start = events(complete_bytes(self.path))[0]

    @classmethod
    def create(cls, root, host, planned, excluded=None):
        if not re.fullmatch(r"[a-z0-9-]+", host):
            raise ValueError("Invalid collection host")
        run_id = f"{host}-{uuid.uuid4().hex}"
        timestamp = now()
        path = Path(root) / ".collection-spool" / "observations" / timestamp[:10] / f"{run_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            revision = git(root, "rev-parse", "HEAD").decode().strip()
        except subprocess.CalledProcessError:
            revision = None  # isolated tests or a non-Git local run
        start = dict(schema_version=1, event="run_started", run_id=run_id, host=host, at=timestamp,
                     code_revision=revision, planned_slugs=planned, excluded=excluded or {})
        with path.open("x") as handle:
            handle.write(json.dumps(start, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return cls(path)

    def append(self, kind, **fields):
        record = dict(schema_version=1, event=kind, run_id=self.start["run_id"],
                      host=self.start["host"], at=now(), **fields)
        # One writer per run, including when a parent hands the log to a child.
        with self.path.open("ab", buffering=0) as handle:
            handle.write((json.dumps(record, sort_keys=True) + "\n").encode())
            os.fsync(handle.fileno())

    def begin(self, slug):
        attempt_id = uuid.uuid4().hex
        self.append("attempt_started", slug=slug, attempt_id=attempt_id)
        return attempt_id

    def finish_attempt(self, slug, attempt_id, result):
        self.append("attempt_finished", slug=slug, attempt_id=attempt_id, **result)

    def finish(self, outcome="completed"):
        complete = complete_bytes(self.path)
        records = events(complete)
        if records[-1]["event"] != "run_finished":
            candidate = dict(schema_version=1, event="run_finished", run_id=self.start["run_id"],
                             host=self.start["host"], at=now(), outcome=outcome)
            events(complete + (json.dumps(candidate) + "\n").encode())
            # Cleanup may run after the writer was killed mid-record.
            with self.path.open("r+b") as handle:
                handle.truncate(len(complete))
            self.append("run_finished", outcome=outcome)
            events(complete_bytes(self.path))


def archive_result(meta):
    if meta.get("sha256") and (meta.get("cold_storage") or {}).get("sha256") == meta["sha256"]:
        return "available"
    if meta.get("sha256") and (meta.get("cold_storage_attempt") or {}).get("sha256") == meta["sha256"]:
        return "failed"
    return "not_recorded"


def publish(root, retries=4):
    """Publish only complete log records, preserving dirty data and both indexes.

    An append can extend the same run's remote log; a stale shorter copy is
    harmless. Divergent bytes are an error. A concurrent host push is retried
    against its new commit, never force-pushed.
    """
    root = Path(root).resolve()
    snapshots = {}
    for path in sorted((root / ".collection-spool" / "observations").glob("*/*.jsonl")):
        data = complete_bytes(path)
        events(data)
        snapshots[path.relative_to(root / ".collection-spool").as_posix()] = data
    if not snapshots:
        return None
    with tempfile.TemporaryDirectory(prefix="collection-index-") as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"),
                   GIT_AUTHOR_NAME="price-history-observer", GIT_COMMITTER_NAME="price-history-observer",
                   GIT_AUTHOR_EMAIL="actions@users.noreply.github.com",
                   GIT_COMMITTER_EMAIL="actions@users.noreply.github.com")
        for retry in range(retries):
            git(root, "fetch", "--quiet", "origin", "main")
            parent = git(root, "rev-parse", "FETCH_HEAD").decode().strip()
            git(root, "read-tree", parent, env=env)
            tree = git(root, "ls-tree", "-r", parent, "--", "observations").decode()
            remote = {line.split("\t", 1)[1]: line.split()[2] for line in tree.splitlines()}
            changed = False
            for path, data in snapshots.items():
                if path in remote:
                    old = git(root, "cat-file", "blob", remote[path])
                    if old.startswith(data):
                        continue
                    if not data.startswith(old):
                        raise ValueError(f"Refusing divergent observation history: {path}")
                oid = git(root, "hash-object", "-w", "--stdin", data=data).decode().strip()
                git(root, "update-index", "--add", "--cacheinfo", f"100644,{oid},{path}", env=env)
                changed = True
            if not changed:
                return parent
            tree_id = git(root, "write-tree", env=env).decode().strip()
            commit = git(root, "commit-tree", tree_id, "-p", parent,
                         data=b"Record hospital collection observations\n", env=env).decode().strip()
            try:
                git(root, "push", "--quiet", "origin", f"{commit}:refs/heads/main")
                return commit
            except subprocess.CalledProcessError:
                if retry == retries - 1:
                    raise


def policy_for(root):
    path = Path(root) / "collection_policy.json"
    # Fixture catalogs without a policy still retain honest unknown timestamps.
    return json.loads(path.read_text()) if path.exists() else {
        "schema_version": 1, "default_host": "github-actions", "check_interval_hours": 24,
        "grace_hours": 48, "overrides": {}, "hosts": {"github-actions": {
            "schedule": "Daily", "maximum_run_hours": 7}}}


def build_health(root, hospitals, evaluated_at=None):
    root = Path(root)
    evaluated_at = evaluated_at or now()
    clock = instant(evaluated_at)
    policy = policy_for(root)
    runs, attempts, monitoring = [], [], []
    for path in sorted((root / "observations").glob("*/*.jsonl")):
        records = events(path.read_bytes())
        if any(instant(e["at"]) > clock + timedelta(minutes=5) for e in records):
            raise ValueError(f"Observation clock is in the future: {path}")
        start = records[0]
        monitoring.append(start["at"])
        starts = {e["attempt_id"]: e for e in records if e["event"] == "attempt_started"}
        ends = {e["attempt_id"]: e for e in records if e["event"] == "attempt_finished"}
        terminal = records[-1] if records[-1]["event"] == "run_finished" else None
        maximum = policy["hosts"].get(start["host"], {}).get("maximum_run_hours", 30)
        interrupted = bool(terminal and terminal["outcome"] == "interrupted") or (
            not terminal and clock > instant(start["at"]) + timedelta(hours=maximum))
        run = {**start, "path": path.relative_to(root).as_posix(),
               "status": "interrupted" if interrupted else ("completed" if terminal else "running"),
               "missing_outcomes": sorted(set(start["planned_slugs"]) - {e["slug"] for e in ends.values()})}
        runs.append(run)
        for aid, attempt in starts.items():
            attempts.append({"slug": attempt["slug"], "host": start["host"], "run_id": start["run_id"],
                             "attempt_id": aid, "started_at": attempt["at"], "path": run["path"],
                             "result": ends.get(aid)})
    since = min(monitoring, key=instant) if monitoring else None
    entries = []
    for hospital in sorted(hospitals, key=lambda h: h["slug"]):
        slug = hospital["slug"]
        owner = policy["overrides"].get(slug, policy["default_host"])
        own = [a for a in attempts if a["slug"] == slug]
        own.sort(key=lambda a: (instant(a["started_at"]), a["attempt_id"]))
        scheduled = [a for a in own if a["host"] == owner]
        good = [a for a in own if a["result"] and a["result"]["outcome"] in SUCCESS]
        successful = max(good, key=lambda a: instant(a["result"]["verified_at"]), default=None)
        downloads = [a for a in good if a["result"]["method"] == "full_download"]
        full = max(downloads, key=lambda a: instant(a["result"]["downloaded_at"]), default=None)
        latest = own[-1] if own else None
        result = latest["result"] if latest else None
        last_scheduled = scheduled[-1]["started_at"] if scheduled else None
        last_success = successful["result"]["verified_at"] if successful else None
        # Only scheduled-owner evidence can change the verification cadence.
        cadence_results = [a["result"] for a in scheduled if a["result"] and
                           a["result"]["outcome"] in SUCCESS | {"deferred"} and
                           "verification_interval_hours" in a["result"]]
        interval = cadence_results[-1]["verification_interval_hours"] if cadence_results else 24
        if interval not in (24, 168):
            raise ValueError("Unknown source-verification cadence")
        def overdue(timestamp, hours):
            baseline = timestamp or since
            return bool(baseline and clock > instant(baseline) + timedelta(hours=hours + policy["grace_hours"]))
        check_overdue = overdue(last_scheduled, policy["check_interval_hours"])
        verification_overdue = overdue(last_success, interval)
        gaps = [r for r in runs if r["host"] == owner and r["status"] == "interrupted"
                and slug in r["missing_outcomes"]
                and not any(a["result"] and instant(a["started_at"]) > instant(r["at"]) for a in scheduled)]
        issues = []
        if check_overdue:
            issues.append("collection_check_overdue")
        if verification_overdue:
            issues.append("collection_verification_overdue")
        if gaps:
            issues.append("collection_interrupted")
        if result and result["outcome"] in {"failed", "listing_missing"}:
            issues.append("collection_failed")
        entries.append({"slug": slug, "scheduled_host": owner, "schedule": policy["hosts"][owner]["schedule"],
                        "check_interval_hours": policy["check_interval_hours"],
                        "verification_interval_hours": interval, "grace_hours": policy["grace_hours"],
                        "last_attempt_at": latest["started_at"] if latest else None,
                        "last_scheduled_attempt_at": last_scheduled,
                        "last_successful_check_at": last_success,
                        "last_full_download_at": full["result"]["downloaded_at"] if full else None,
                        "latest_attempt": latest, "last_successful_check": successful,
                        "state": result["outcome"] if result else ("in_progress" if latest and not gaps else "unknown"),
                        "check_overdue": check_overdue, "verification_overdue": verification_overdue,
                        "issues": issues})
    return {"schema_version": 1, "evaluated_at": evaluated_at, "monitoring_since": since,
            "policy": policy, "totals": {"hospitals": len(entries),
                "with_successful_check": sum(bool(e["last_successful_check_at"]) for e in entries),
                "without_successful_check": sum(not e["last_successful_check_at"] for e in entries),
                "check_overdue": sum(e["check_overdue"] for e in entries),
                "verification_overdue": sum(e["verification_overdue"] for e in entries),
                "issues": dict(sorted(Counter(i for e in entries for i in e["issues"]).items()))},
            "runs": runs, "hospitals": entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "finish", "publish", "check"])
    args = parser.parse_args()
    if args.command == "prepare":
        hospitals = json.loads((ROOT / "hospitals.json").read_text())
        only, skip = os.environ.get("ONLY", ""), os.environ.get("SKIP", "")
        planned = [h["slug"] for h in hospitals if (h["slug"] in only.split(",") if only
                                                   else h["slug"] not in skip.split(","))]
        excluded = {h["slug"]: "outside_requested_subset" if only else "other_collector"
                    for h in hospitals if h["slug"] not in planned}
        log = RunLog.create(ROOT, os.environ.get("COLLECTION_HOST", "manual"), planned, excluded)
        if os.environ.get("GITHUB_ENV"):
            with open(os.environ["GITHUB_ENV"], "a") as handle:
                handle.write(f"COLLECTION_LOG={log.path}\n")
        print(log.path)
    elif args.command == "finish":
        # The collector records completed itself. A workflow cleanup after a
        # killed process can only attest to interruption.
        if os.environ.get("COLLECTION_LOG"):
            RunLog(os.environ["COLLECTION_LOG"]).finish("interrupted")
    elif args.command == "publish":
        print(publish(ROOT) or "No observations to publish")
    else:
        report = build_health(ROOT, json.loads((ROOT / "hospitals.json").read_text()))
        print(json.dumps(report["totals"], indent=2))
        failing = [e for e in report["hospitals"] if e["check_overdue"] or e["verification_overdue"]
                   or "collection_interrupted" in e["issues"]]
        for entry in failing:
            print(entry["slug"] + ": " + ", ".join(entry["issues"]))
        if failing or not report["monitoring_since"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
