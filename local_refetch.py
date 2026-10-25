#!/usr/bin/env python3
"""Scheduled local refetch for hospitals CI cannot collect: local_refetch.py.

Three failure classes are covered (see docs/OPERATIONS.md):
  - Runner-IP blocks (Hopkins x4, Orlando Regional, Jackson Memorial):
    their CDNs serve a residential IP fine, so the normal scrape pipeline
    just runs here.
  - Files above CI's download cap (University of Kansas): the normal
    pipeline runs uncapped, using disk-backed scratch space instead of
    the Pi's small /tmp RAM disk.
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
import collection_health as health  # noqa: E402
from scrape import RAW_DATA  # noqa: E402
from browser_fetch import BrowserFetch  # noqa: E402

RAW_REPO = RAW_DATA.parent
IP_BLOCKED = ["johns-hopkins", "hopkins-bayview", "hopkins-all-childrens",
              "sibley-memorial", "orlando-regional", "jackson-memorial"]
OVERSIZED = ["university-of-kansas"]
SAS_DONOR = "hca-houston-medical-center"
STALE_SAS = ["hca-florida-kendall", "tristar-centennial"]
# Everything this script owns; CI's scrape.yml SKIPs exactly this set.
LOCAL_ONLY = IP_BLOCKED + OVERSIZED + STALE_SAS + ["yale-new-haven"]
YALE_URL = ("https://www.ynhh.org/-/media/Files/YNHHS/sc/"
            "06-0646652-Yale_New_Haven_Hospital_Standard_Charges011025-1.ashx")
YALE_PAGE = "https://www.ynhh.org/patients-visitors/billing-insurance/Pricing"
# Annotated tag re-pointed after every run; .github/workflows/pi-heartbeat.yml
# fails (and so emails) when its tagger date is more than three days old.
# Host completion is separate from individual hospital verification.
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


def unchanged_workaround(slug, fp, observation):
    print(f"{slug}: unchanged (fingerprint match, {observation['transport']})", flush=True)
    scrape.clear_failure_record(slug)
    meta = meta_of(slug)
    observation.update(outcome="unchanged", method="sampled_fingerprint", verified_at=health.now(),
                       source_sha256=meta["sha256"], evidence={"fingerprint": fp},
                       archive_status=health.archive_result(meta))
    return False


def validate_workaround_download(dest):
    with dest.open("rb") as handle:
        start = handle.read(512).lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if not start or start.startswith((b"<!doctype", b"<html", b"<head")):
        raise ValueError("received an empty file or HTML page instead of an MRF")


def refetch_workaround(slug, scratch, observation=None):
    """Fingerprint-check the workaround URL; ingest only on change."""
    observation = observation if observation is not None else {}
    observation.update(outcome="failed", method="discovery", discovery_status="workaround",
                       transport="plain", verification_interval_hours=24)
    url = workaround_url(slug)
    fp = scrape.remote_fingerprint(url)
    if fp and fp == meta_of(slug).get("transfer_fingerprint"):
        return unchanged_workaround(slug, fp, observation)
    name = Path(urllib.parse.urlsplit(url).path).name or f"{slug}.bin"
    if slug == "yale-new-haven":
        name = name.replace("-1.ashx", ".csv")
    dest = Path(scratch) / name
    observation["method"] = "full_download"
    old_sha = meta_of(slug).get("sha256")
    try:
        run("curl", "--fail", "-sS", "-L", "--retry", "3", "-C", "-", "--max-time", "7200",
            "-A", scrape.USER_AGENT, "-o", dest, url)
        downloaded_at = health.now()
        validate_workaround_download(dest)
    except (subprocess.CalledProcessError, ValueError) as exc:
        if slug != "yale-new-haven":
            raise
        print(f"{slug}: plain fetch failed; retrying in Chromium", flush=True)
        observation.update(transport="browser", retry_errors=[{
            "transport": "plain", "error": health.error_text(exc)}])
        with BrowserFetch(YALE_PAGE, url, scratch) as browser:
            fp = browser.fingerprint()
            if fp and fp == meta_of(slug).get("transfer_fingerprint"):
                return unchanged_workaround(slug, fp, observation)
            browser.download(dest)
            downloaded_at = health.now()
    validate_workaround_download(dest)
    source_args = ("--source-url", url) if slug == "yale-new-haven" else ()
    run(sys.executable, ROOT / "ingest_local.py", slug, dest, *source_args)
    scrape.clear_failure_record(slug)
    meta = meta_of(slug)
    observation.update(outcome="unchanged" if meta["sha256"] == old_sha else "changed",
                       verified_at=downloaded_at, downloaded_at=downloaded_at, source_sha256=meta["sha256"],
                       archive_status=health.archive_result(meta))
    # An identical ingest writes no shards. Staging it as a replacement in
    # the Pi's sparse raw checkout would delete the existing raw snapshot.
    return meta["sha256"] != old_sha


def stamp():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def refetch_hospitals(log=None):
    # /tmp is a small tmpfs on the Pi. Keep downloads, extracted files,
    # and compressed originals on its disk; subprocesses inherit TMPDIR.
    scratch_root = Path.home() / ".cache" / "hospital-price-history"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="refetch-", dir=scratch_root) as scratch:
        env = dict(os.environ, ONLY=",".join(IP_BLOCKED + OVERSIZED),
                   TMPDIR=scratch, MAX_DOWNLOAD_BYTES="0")
        if log:
            env.update(COLLECTION_LOG=str(log.path), COLLECTION_PARENT_RUN="1", COLLECTION_HOST="pi")
        run(sys.executable, ROOT / "scrape.py", env=env)
        changed = (ROOT / "changed_slugs.txt").read_text().split()

        for slug in STALE_SAS + ["yale-new-haven"]:
            attempt_id = log.begin(slug) if log else None
            observation = {}
            try:
                kwargs = {"observation": observation} if log else {}
                if refetch_workaround(slug, scratch, **kwargs):
                    changed.append(slug)
            except Exception as exc:
                scrape.record_failure(slug, exc)
                observation.update(outcome="failed", error=health.error_text(exc))
                observation.pop("verified_at", None)
                observation.pop("downloaded_at", None)
                print(f"{slug}: workaround failed: {exc}", file=sys.stderr, flush=True)
            if log:
                log.finish_attempt(slug, attempt_id, observation)
    return changed


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

    log = health.RunLog.create(ROOT, "pi", LOCAL_ONLY)
    # Publish the planned run before any long download. A hard kill then
    # leaves a durable, explicitly incomplete run rather than a silent gap.
    health.publish(ROOT)
    try:
        changed = refetch_hospitals(log)
        log.finish()
        commit_results(changed)
    finally:
        log.finish("interrupted")
        health.publish(ROOT)


def commit_results(changed):

    # Commit raw first, pin raw_commit, then commit main. The scraper's
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
        # Large downloads can overlap CI's next data commit.
        git(ROOT, "pull", "--rebase", "--quiet")
        git(ROOT, "push")
    heartbeat()
    print(f"=== {stamp()} done:", msg, flush=True)


if __name__ == "__main__":
    main()
