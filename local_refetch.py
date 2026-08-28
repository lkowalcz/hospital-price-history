#!/usr/bin/env python3
"""Weekly local refetch for hospitals CI cannot reach: local_refetch.py.

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

import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ.setdefault("CURL_IMPERSONATE_BIN", str(ROOT / ".tools" / "curl_chrome145"))

import scrape  # noqa: E402  (needs CURL_IMPERSONATE_BIN set first)
from scrape import RAW_DATA  # noqa: E402

RAW_REPO = RAW_DATA.parent
IP_BLOCKED = ["johns-hopkins", "hopkins-bayview", "hopkins-all-childrens",
              "sibley-memorial", "orlando-regional"]
SAS_DONOR = "hca-houston-medical-center"
STALE_SAS = ["hca-florida-kendall", "tristar-centennial"]
YALE_URL = ("https://www.ynhh.org/-/media/Files/YNHHS/sc/"
            "06-0646652-Yale_New_Haven_Hospital_Standard_Charges011025-1.ashx")


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
        return False
    name = Path(urllib.parse.urlsplit(url).path).name or f"{slug}.bin"
    if slug == "yale-new-haven":
        name = name.replace("-1.ashx", ".csv")
    dest = Path(scratch) / name
    run("curl", "-sS", "-L", "--retry", "3", "-C", "-", "--max-time", "7200",
        "-A", scrape.USER_AGENT, "-o", dest, url)
    run(sys.executable, ROOT / "ingest_local.py", slug, dest)
    return True


def main():
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

    # 3) Commit raw first, pin raw_commit, then commit main.
    msg = "Local refetch: " + (", ".join(changed) or "no content changes")
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
    print("done:", msg, flush=True)


if __name__ == "__main__":
    main()
