"""Track source outages in GitHub issues without relaxing freshness checks.

Issue creation, weekly comments, and verified recovery are the notifications.
Collector outages and failures to maintain the incident log still fail CI.
Run without --apply to preview changes without writing to GitHub.
"""

import argparse
import json
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path

import collection_health as health

LABEL = "collection-incident"
MARKER = "collection-incident-v1"
NOTICE = "<!-- collection-incident-notification-v1 -->"
SOURCE_ISSUES = {"collection_failed", "collection_verification_overdue"}
COLLECTOR_ISSUES = {"collection_check_overdue", "collection_interrupted"}


class GitHub:
    def __init__(self, repo):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError("Expected an owner/repository name")
        self.repo = repo

    def request(self, endpoint, method="GET", payload=None):
        command = ["gh", "api", f"repos/{self.repo}/{endpoint}", "--method", method]
        if payload is not None:
            command.extend(["--input", "-"])
        return json.loads(subprocess.check_output(
            command, input=json.dumps(payload) if payload is not None else None, text=True))

    def listing(self, endpoint):
        items, page = [], 1
        separator = "&" if "?" in endpoint else "?"
        while True:
            batch = self.request(f"{endpoint}{separator}per_page=100&page={page}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1


def incident_state(issue):
    match = re.search(r"<!-- " + MARKER + r" (.+?) -->", issue.get("body") or "")
    if not match:
        raise ValueError(f"Incident #{issue['number']} has no tracking marker; restore it before continuing")
    state = json.loads(match[1])
    if not re.fullmatch(r"[a-z0-9-]+", state["slug"]):
        raise ValueError("Invalid incident hospital slug")
    health.instant(state["detected_at"])
    return state


def evidence(entry, repo, revision):
    latest = entry.get("latest_attempt") or {}
    result = latest.get("result") or {}
    lines = [f"Hospital: `{entry['slug']}`",
             f"Issues: {', '.join(entry['issues']) or 'none'}",
             f"Last attempted check: {entry['last_attempt_at'] or 'unknown'}",
             f"Last successful source verification: {entry['last_successful_check_at'] or 'unknown'}"]
    if result.get("error"):
        # Error text comes from external responses. Quote it, redact URL queries,
        # and neutralize mentions rather than notifying arbitrary GitHub users.
        error = health.error_text(result["error"]).replace("@", "@\u200b")
        lines.append("Latest error:\n\n" + "\n".join("> " + line for line in error.splitlines()))
    if latest.get("path"):
        lines.append(f"[Collection evidence](https://github.com/{repo}/blob/{revision}/{latest['path']})")
    return "\n\n".join(lines)


def reconcile(report, github, revision, *, apply=False, assignee=None):
    if not report["monitoring_since"]:
        raise ValueError("No collection observations; cannot evaluate or resolve incidents")
    clock = health.instant(report["evaluated_at"])
    entries = {e["slug"]: e for e in report["hospitals"]}
    issues = {}
    for issue in github.listing(f"issues?state=open&labels={LABEL}"):
        if "pull_request" in issue:
            continue
        state = incident_state(issue)
        slug = state["slug"]
        if slug in issues:
            raise ValueError(f"Multiple open incidents for {slug}; reconcile them before continuing")
        if health.instant(state["detected_at"]) > clock:
            raise ValueError("Incident is newer than the health report; refusing a stale evaluation")
        issues[slug] = (issue, state)

    messages = []
    active = {slug for slug, e in entries.items() if SOURCE_ISSUES.intersection(e["issues"])}
    new = active - issues.keys()
    if new and apply:
        labels = github.listing("labels")
        if not any(label["name"] == LABEL for label in labels):
            github.request("labels", "POST", {"name": LABEL, "color": "d93f0b",
                "description": "Source outage tracked by collection freshness"})
    for slug in sorted(new):
        state = {"slug": slug, "detected_at": report["evaluated_at"]}
        body = (f"<!-- {MARKER} {json.dumps(state, sort_keys=True)} -->\n\n"
                "Source collection needs attention. Daily collection continues; freshness deadlines "
                "and historical snapshots remain unchanged. This issue receives a weekly reminder "
                "until a successful source check confirms recovery.\n\n" + evidence(entries[slug], github.repo, revision))
        payload = {"title": f"Collection source outage: {slug}", "body": body, "labels": [LABEL]}
        if assignee:
            payload["assignees"] = [assignee]
        if apply:
            created = github.request("issues", "POST", payload)
            messages.append(f"Opened {created['html_url']}")
        else:
            messages.append(f"Would open incident for {slug}:\n\n{body}")

    for slug, (issue, state) in sorted(issues.items()):
        entry = entries.get(slug)
        if entry is None:
            raise ValueError(f"Open incident for {slug} is outside the roster; resolve it explicitly")
        comments = github.listing(f"issues/{issue['number']}/comments")
        notices = [c for c in comments if (c.get("body") or "").startswith(NOTICE)
                   and (c.get("user", {}).get("login") == "github-actions[bot]"
                        or c.get("user", {}).get("login") == issue["user"]["login"])]
        latest = max([health.instant(issue["created_at"])] +
                     [health.instant(c["created_at"]) for c in notices])
        result = (entry.get("latest_attempt") or {}).get("result") or {}
        recovered = (not (SOURCE_ISSUES | COLLECTOR_ISSUES).intersection(entry["issues"])
                     and result.get("outcome") in health.SUCCESS
                     and health.instant(result["verified_at"]) > health.instant(state["detected_at"]))
        if recovered:
            # Retrying after a successful comment but failed close must not send
            # another notification for the same verified observation.
            recovery_marker = f"<!-- recovered-at {result['verified_at']} -->"
            if apply:
                if not any(recovery_marker in c["body"] for c in notices):
                    github.request(f"issues/{issue['number']}/comments", "POST", {"body":
                        f"{NOTICE}\n{recovery_marker}\n\nSource verification recovered.\n\n" +
                        evidence(entry, github.repo, revision)})
                github.request(f"issues/{issue['number']}", "PATCH", {"state": "closed", "state_reason": "completed"})
            messages.append(f"{'Closed' if apply else 'Would close'} recovered incident #{issue['number']} for {slug}")
        elif clock >= latest + timedelta(days=7):
            if apply:
                github.request(f"issues/{issue['number']}/comments", "POST", {"body":
                    f"{NOTICE}\n\nWeekly reminder: recovery has not been verified.\n\n" +
                    evidence(entry, github.repo, revision)})
            messages.append(f"{'Reminded' if apply else 'Would remind'} incident #{issue['number']} for {slug}")
        else:
            messages.append(f"Incident #{issue['number']} for {slug} remains open; next reminder "
                            f"{(latest + timedelta(days=7)).isoformat()}")

    failures = [e for e in entries.values() if COLLECTOR_ISSUES.intersection(e["issues"])]
    messages.extend(f"Collector needs attention: {e['slug']}: {', '.join(e['issues'])}" for e in failures)
    return messages, bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create/update GitHub incidents (default: preview)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--assignee", default=os.environ.get("INCIDENT_ASSIGNEE"))
    args = parser.parse_args()
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    report = health.build_health(health.ROOT, json.loads((health.ROOT / "hospitals.json").read_text()))
    revision = health.git(health.ROOT, "rev-parse", "HEAD").decode().strip()
    messages, failed = reconcile(report, GitHub(args.repo), revision, apply=args.apply, assignee=args.assignee)
    summary = "Collection incident evaluation: " + report["evaluated_at"] + "\n\n" + "\n\n".join(messages)
    print(json.dumps(report["totals"], indent=2))
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as handle:
            handle.write(summary + "\n")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
