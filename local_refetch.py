#!/usr/bin/env python3
"""Scheduled local refetch for hospitals CI cannot reach: local_refetch.py.

Two failure classes are covered (see README "Notes"):
  - Runner-IP blocks (Hopkins x4, Orlando Regional): their CDNs serve a
    residential IP fine, so the normal scrape pipeline just runs here.
  - Broken published links, fetched via documented workarounds:
      * HCA Kendall / TriStar Centennial: their cms-hpt.txt carries stale
        Azure SAS tokens; the container-scoped token HCA publishes for a
        sibling hospital (the donor) reads the same container.
      * Yale New Haven: cms-hpt.txt points to a deleted file; the live
        re-uploaded variant carries the CMS's "-1" suffix.
    Each is fingerprint-checked first and re-ingested only on change.

Commits and pushes both repos (raw first, pinning raw_commit), mirroring
the CI workflow. Run from a machine with the raw repo as a sibling clone;
designed for launchd/cron (see README "Running locally").

The raw clone may be sparse (`data/` excluded) and blob-less, as in CI: the
scraper only ever writes a slug's directory from scratch, so each rewritten
slug is staged as an exact replacement of its index entries rather than via
`git add -A`, which would not see files outside the sparse cone. The same
commands are a no-op difference on a full checkout.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
# Same wrapper the CI workflow uses (CI_WRAPPER in scrape.yml; a test pins
# them). .tools/ holds the curl-impersonate release for this host.
IMPERSONATE_WRAPPER = "curl_chrome150"
os.environ.setdefault("CURL_IMPERSONATE_BIN", str(ROOT / ".tools" / IMPERSONATE_WRAPPER))

# Cold storage. This host archives summarized originals itself when it
# has archive.org credentials (`ia configure`, which writes IA_CONFIG);
# CI's backfill never reaches the hospitals this script owns. The ia CLI
# comes with `pip install -r requirements.txt` into the venv.
IA_CONFIG = Path.home() / ".config" / "internetarchive" / "ia.ini"


def find_ia_bin():
    import shutil
    import sysconfig
    for candidate in (Path(sys.executable).parent / "ia",
                      Path(sysconfig.get_path("scripts", "posix_user")) / "ia",
                      shutil.which("ia")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


if (ia_bin := find_ia_bin()):
    os.environ.setdefault("IA_BIN", ia_bin)  # scrape reads it at import

import scrape  # noqa: E402  (needs CURL_IMPERSONATE_BIN / IA_BIN set first)
from scrape import RAW_DATA  # noqa: E402

RAW_REPO = RAW_DATA.parent
IP_BLOCKED = ["johns-hopkins", "hopkins-bayview", "hopkins-all-childrens",
              "sibley-memorial", "orlando-regional"]
SAS_DONOR = "hca-houston-medical-center"
STALE_SAS = ["hca-florida-kendall", "tristar-centennial"]
# Everything this script owns; CI's scrape.yml SKIPs exactly this set.
LOCAL_ONLY = IP_BLOCKED + STALE_SAS + ["yale-new-haven"]
YALE_URL = ("https://www.ynhh.org/-/media/Files/YNHHS/sc/"
            "06-0646652-Yale_New_Haven_Hospital_Standard_Charges011025-1.ashx")
# Annotated tag re-pointed after every run; .github/workflows/pi-heartbeat.yml
# fails (and so emails) when its tagger date is more than three days old.
# A tag rather than a commit: quiet days produce no commit to look for.
HEARTBEAT_TAG = "pi-heartbeat"


def run(*cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(list(map(str, cmd)), check=True, **kw)


def git(repo, *args, capture=False):
    r = subprocess.run(["git", "-C", str(repo), *map(str, args)], check=True,
                       capture_output=capture, text=True)
    return r.stdout.strip() if capture else None


def stage_raw_slug(repo, slug):
    """Stage data/<slug> in the raw repo as an exact replacement: drop its
    index entries (old shards may be sparse — not on disk), then add whatever
    the scraper wrote. Net effect includes deletion when a hospital leaves
    sharded mode. Mirrors the "Commit raw repo" step in scrape.yml."""
    git(repo, "rm", "-r", "-q", "--cached", "--sparse", "--ignore-unmatch",
        f"data/{slug}")
    if (Path(repo) / "data" / slug).is_dir():
        git(repo, "add", "--sparse", f"data/{slug}")


def staged_changes(repo):
    return subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]
                          ).returncode != 0


def meta_of(slug):
    p = ROOT / "data" / slug / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def workaround_url(slug):
    if slug == "yale-new-haven":
        return YALE_URL
    donor = meta_of(SAS_DONOR).get("mrf_url", "")
    published = scrape.discover_mrf_url(
        next(h for h in json.loads((ROOT / "hospitals.json").read_text())
             if h["slug"] == slug), False)
    s = urllib.parse.urlsplit(published)
    return urllib.parse.urlunsplit(
        (s.scheme, s.netloc, s.path, urllib.parse.urlsplit(donor).query, ""))


def refetch_workaround(slug, scratch):
    """Fingerprint-check the workaround URL; ingest only on change."""
    url = workaround_url(slug)
    fp = scrape.remote_fingerprint(url)
    if fp and fp == meta_of(slug).get("transfer_fingerprint"):
        print(f"{slug}: unchanged (fingerprint match)", flush=True)
        # A stale streak from CI trying the dead published link would keep
        # the site badge on "unreachable"; the source is reachable from here.
        scrape.clear_failure_record(slug)
        return False
    name = Path(urllib.parse.urlsplit(url).path).name or f"{slug}.bin"
    if slug == "yale-new-haven":
        name = name.replace("-1.ashx", ".csv")
    dest = Path(scratch) / name
    run("curl", "-sS", "-L", "--retry", "3", "-C", "-", "--max-time", "7200",
        "-A", scrape.USER_AGENT, "-o", dest, url)
    run(sys.executable, ROOT / "ingest_local.py", slug, dest)
    scrape.clear_failure_record(slug)
    return True


def stamp():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def heartbeat():
    """Tell CI this run completed: re-point HEARTBEAT_TAG at HEAD with a
    fresh tagger date and force-push just that ref. Never fatal — the data
    commits above already went out."""
    try:
        git(ROOT, "tag", "-f", "-a", HEARTBEAT_TAG, "-m",
            f"local_refetch completed {stamp()}")
        git(ROOT, "push", "-f", "--quiet", "origin", f"refs/tags/{HEARTBEAT_TAG}")
    except subprocess.CalledProcessError as exc:
        print(f"heartbeat tag push failed: {exc}", file=sys.stderr, flush=True)


def main():
    # The cron log is append-only with no timestamps of its own.
    print(f"=== {stamp()} local_refetch start", flush=True)
    if os.environ.get("IA_ARCHIVE") is None and IA_CONFIG.exists() and os.environ.get("IA_BIN"):
        os.environ["IA_ARCHIVE"] = "1"  # inherited by the scrape/ingest subprocesses
        print("cold storage: on (archive.org credentials found)", flush=True)
    else:
        print(f"cold storage: {'on' if scrape.archiving_enabled() else 'off'}", flush=True)
    for repo in (ROOT, RAW_REPO):
        git(repo, "pull", "--rebase", "--autostash", "--quiet")

    # 1) IP-blocked hospitals: the normal pipeline, from this IP.
    env = dict(os.environ, ONLY=",".join(IP_BLOCKED))
    run(sys.executable, ROOT / "scrape.py", env=env)
    changed = (ROOT / "changed_slugs.txt").read_text().split()

    # 2) Broken-link hospitals, via their documented workarounds.
    with tempfile.TemporaryDirectory() as scratch:
        for slug in STALE_SAS + ["yale-new-haven"]:
            try:
                if refetch_workaround(slug, scratch):
                    changed.append(slug)
            except Exception as exc:
                print(f"{slug}: workaround failed: {exc}", file=sys.stderr, flush=True)

    # 3) Commit raw first, pin raw_commit, then commit main. The scraper's
    # own message carries the bookkeeping (fetch errors, recoveries,
    # validator refreshes) that explains a meta.json-only commit.
    msg = "Local refetch: " + (", ".join(changed) or "no content changes")
    scrape_msg = (ROOT / "commit_message.txt").read_text().strip()
    extras = [p for p in scrape_msg.split("; ")
              if p and p != "No changes" and not p.startswith("Update price files")]
    if extras:
        msg += "; " + "; ".join(extras)
    for slug in changed:
        stage_raw_slug(RAW_REPO, slug)
    if staged_changes(RAW_REPO):
        git(RAW_REPO, "commit", "-m", msg)
        git(RAW_REPO, "pull", "--rebase", "--quiet")
        git(RAW_REPO, "push")
    raw_sha = git(RAW_REPO, "rev-parse", "HEAD", capture=True)
    for slug in changed:
        mp = ROOT / "data" / slug / "meta.json"
        if not mp.exists():
            continue
        meta = json.loads(mp.read_text())
        if (RAW_DATA / slug).is_dir():
            meta["raw_commit"] = raw_sha
        else:
            meta.pop("raw_commit", None)
        mp.write_text(json.dumps(meta, indent=2) + "\n")
    if git(ROOT, "status", "--porcelain", "--", "data", capture=True):
        git(ROOT, "add", "data/")
        git(ROOT, "commit", "-m", msg)
        git(ROOT, "push")
    heartbeat()
    print(f"=== {stamp()} done:", msg, flush=True)


if __name__ == "__main__":
    main()
