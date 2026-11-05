# Operations

GitHub Actions runs `scrape.py` daily and deploys the site. A Raspberry Pi
on the home network runs `local_refetch.py` daily for hospitals that need
a local connection, a larger download allowance, or a corrected URL. This
guide covers the Pi's setup, monitoring, and recovery. Private deploy keys
are stored only on the Pi.

## What runs where

| Job | Host | Schedule | Responsibility |
|-----|------|----------|------|
| `scrape.yml` | GitHub Actions | daily, 06:23 UTC cron (often delayed by hours) | every hospital except the `SKIP` list in the workflow |
| `pages.yml` | GitHub Actions | on completion of `scrape.yml`, and on relevant data/generator pushes | the site, data catalog, manifests, and quality report |
| `local_refetch.py` | Pi `homectl.local` | daily, 13:00 New York, via cron | the `LOCAL_ONLY` list in `local_refetch.py`; a test checks that it matches the workflow's `SKIP` |
| `collection-health.yml` | GitHub Actions | daily, 19:31 UTC | overdue hospital checks, overdue source verification, and interrupted runs |

The Pi pushes commits with a deploy key, which triggers the site's
push-based deployment workflow.

The generated [data catalog](https://lukekowalczyk.com/hospital-price-history/data/)
reports check outcomes, verification methods, schedules, and structural summary
checks for the full roster. The [collection health report](https://lukekowalczyk.com/hospital-price-history/data/collection-health.json)
uses explicit observations, with unknown timestamps before logging began.
Keep workflow results and the Pi heartbeat as independent execution checks.

Collectors spool append-only logs in `.collection-spool/observations/`. They
publish the planned run before fetching and publish outcomes afterward,
including after failures. Publication uses a separate Git index and only adds
log records, so dirty or rejected source changes cannot leak into that commit.
Each run has a unique path; simultaneous pushes retry against the latest main
commit. Existing logs can only grow. A stale publisher cannot shorten a log.

GitHub Actions also retains a 30-day log artifact. To retry a failed publication,
restore the artifact beneath `.collection-spool/observations/` in an authorized
checkout, then run `python collection_health.py publish`. On the Pi the spool
survives process failures and is republished on subsequent runs. Do not delete
unpublished logs. Hard termination can prevent final publication; the previously
published plan then exposes missing outcomes. `python collection_health.py check`
recomputes deadlines locally without downloading any price files.

`collection_policy.json` defines scheduled ownership, intervals, grace, and run
limits. Daily attempts allow 48 hours of grace beyond the 24-hour interval.
Sunday-only full verifications allow 48 hours beyond 168 hours; daily deferrals
do not refresh them. Incomplete runs exceed their limit after 7 hours on CI or
30 hours on Pi. Monitoring starts with the first explicit run log; it never uses
historical snapshot dates to fill gaps. See [the schema guide](DATA.md#collection-health).

After each run, the Pi force-pushes an annotated `pi-heartbeat` tag with
the completion time as its tagger date. This records successful runs even
when no price files changed. The `pi-heartbeat.yml`
workflow checks the tag daily at 18:30 UTC. A missing tag or a date more
than 72 hours old causes a workflow failure and a GitHub email. If you
receive one, follow the health check below. The next completed Pi run
updates the tag.

## Original-file preservation

The scheduled workflow sets `PRESERVE_ORIGINALS=1`. The Pi enables it when
archive.org credentials are configured. Manual runs can set it explicitly
alongside the existing archive credentials. This adds exact-original capture
for every completed download, including formats the parser cannot read; it
does not force a download of every unchanged historical source. Plain HTML
challenge pages are rejected before capture.

`preserve_source.py` copies or hard-links each completed download into
`.archive-spool/<slug>/<download-sha256>/` before processing it. The spool is
outside temporary download directories; override it with `ORIGINAL_SPOOL_DIR`
only to a persistent disk. It uses zstd compression and uploads to a
content-addressed Internet Archive item. Compression is verified against the
original SHA-256 before upload. Upload success records the public receipt
before removing local bytes. Receipts remain in the spool to recover an
interrupted data commit. Do not delete the spool while uploads are pending.

```sh
python preserve_source.py retry   # uses retained bytes, never the live hospital URL
python preserve_source.py check   # nonzero when pending originals remain
```

For a bounded rollout or archive backfill check, dispatch the scrape workflow
with explicit `only` hospital slugs and `full_download: true`. Locally use
`ONLY=<slug> FORCE_FULL_DOWNLOAD=1`; an unscoped forced run is rejected. This
bypasses validators/sampling but retains normal content hashes and history,
so downloading identical bytes does not create a new price snapshot. Scheduled
collection additionally uses the bounded rotation described below.

On CI, `archive_recovery.py` restores the most recent eligible `original-queue`
artifact from this workflow on main. Checkpoint names include the run ID and
attempt, so reruns cannot overwrite the previous recovery copy. A failed
restore stops collection.
The queue is checkpointed before data commits, even after a collection error;
an empty-queue marker supersedes older checkpoints. Pending uploads stop CI
from collecting more files and fail the backlog check. Artifacts have 90-day
retention: they are a recovery buffer, not permanent storage. Repeated daily
checkpoints carry unresolved bytes forward, but an extended outage or a hard
runner termination before checkpointing can still lose an unuploaded capture.
Investigate failures promptly. Never clear an unavailable checkpoint just to
make the workflow green. The Pi's queue survives process failures on its disk;
its run reports failure after publishing results if originals remain pending.

Original receipts are committed under `data/<slug>/originals/`. Their status
is independent of parser success. The catalog lists originals even when no
normalized snapshot could be built, and flags `original_upload_pending`.
Distinct discovery listings are retained under `data/<slug>/discovery/`.
For local ingestion, `artifact_kind: local_input` explicitly identifies bytes
supplied by the operator; a previously extracted local file must not be
described as the original ZIP download.

The 2026-11-02 rollout passed scoped live checks on GitHub Actions and the Pi,
public-download hash verification for both captured originals, and a second
GitHub run restoring the previous checkpoint. The [roadmap](ROADMAP.md#3-preserve-exact-originals--deployed-scheduled-cycle-checks-pending)
records the run links and remaining full-cycle verification. Offline tests cover upload failure, scratch cleanup,
interruption, retry after source deletion, ZIP retention, and receipt recovery.
They do not establish live Internet Archive availability or artifact delivery.
Periodic independent archive verification remains roadmap work.

### Bounded original capture

`capture_rotation.py` prioritizes current snapshots without archived originals,
then files without a recorded full download in 30 days. Matching headers and
sampled fingerprints cannot skip a selected capture. Legacy cold-storage links
count separately from the new exact-download receipts; local inputs alone do
not establish exact-download coverage. Missing historical bytes cannot be
recreated by fetching a current file.

`collection_policy.json` sets the daily UTC budgets. GitHub may select three
extra captures per day, reserving at most 20 GB with a 10 GB per-transfer cap.
The Pi may select one, reserving at most 100 GB with a 50 GB per-transfer cap
(Kansas's existing source is about 37 GB). These are ceilings, not targets:
each file reserves two transfers at 125% of its last known size, subject to the
host cap, with a 10 MB minimum. Two slots allow the existing transport fallback;
internal transfer retries are disabled for these selected captures. HTTP
overhead, cheap discovery/sampling, archive uploads, and ordinary downloads
triggered by source changes are outside this extra-capture budget.

Run-start observations contain the allocations and are published before work.
Failed/interrupted runs and same-day reruns consume their reservations. A
reserved hospital waits seven days before another forced attempt, while normal
source checks continue daily. Pending uploads prevent another scheduled capture
for that hospital. Over-budget files remain visible in the catalog's
`original_capture` report. The three Mayo sources exceed GitHub's 10 GB cap;
they retain their existing archives and need a separately bounded larger-host
capture before periodic coverage can include them.

Preview without reserving or downloading:

```sh
python capture_rotation.py --host github-actions
python capture_rotation.py --host pi
```

Scheduled GitHub runs enable `ORIGINAL_CAPTURE_ROTATION=1`; a manual workflow
can opt in with `capture_rotation: true`, optionally restricted by `only`.
The Pi enables it alongside preservation. Set `ORIGINAL_CAPTURE_ROTATION=0`
to disable extra captures while retaining ordinary checks and preservation.
The catalog report previews the next selection using committed observations;
the collector additionally includes unpublished local reservations.

### Source incidents investigated on 2026-11-02

- AdventHealth Orlando's `cms-hpt.txt` returned HTTP 403 on both local hosts.
  Its previously published HospitalPriceDisclosure URL still returned a
  36,565,578-byte JSON file identifying AdventHealth Orlando (Florida license
  4369). The roster explicitly permits using that recorded URL after a discovery
  error. Each such observation records `cached_after_error`, the error, and a
  separate `collection_discovery_failed` issue. A valid listing that removes the
  hospital never triggers this fallback. Other hospitals do not inherit it.
- Maine Medical Center's published Azure blob returned Content-Length 0 and
  the MD5 of empty bytes, last modified 2026-10-30. This confirms the recorded
  empty source rather than a parser-only loss. New empty responses are preserved
  when archiving is enabled but rejected before replacing price data. Existing
  historical empty snapshots remain in history; the catalog's
  `latest_summary_snapshot` and hospital page link to the last available summary
  without representing it as the current source.

### Freshness alert on 2026-11-05 (UTC)

The [failed freshness run](https://github.com/lkowalcz/hospital-price-history/actions/runs/41941655954)
reported overdue verification for Maine Medical Center and TriStar Centennial.
Both scheduled collectors were still attempting their assigned hospitals.

- TriStar renamed its discovery listing from `CENTENNIAL MEDICAL CENTER` to
  `TRISTAR CENTENNIAL MEDICAL CENTER`. The roster now follows that exact name.
  The local workaround also reports a missing listing explicitly instead of
  passing a missing URL into URL parsing (`Cannot mix str and non-str arguments`).
  A live check of the corrected URL matched the stored five-range fingerprint;
  the file itself remains available. Deploy the roster and collector change to
  the Pi and collect a successful observation to clear its overdue status.
- Maine's currently published URL still returns HTTP 200 with Content-Length 0
  and the MD5 of empty bytes. It remains a source failure. A successful TriStar
  check alone will not clear the workflow: Maine needs a usable source file
  before its verification can advance. Keep the existing freshness deadlines
  and empty-file rejection in place.

## Hospitals handled locally

- **Runner IP blocks**: the Johns Hopkins hospitals (four slugs), Orlando
  Regional, and Jackson Memorial accept downloads from a residential IP
  but refuse GitHub's runners. Jackson's discovery file began returning
  HTTP 403 on runners on 2026-09-20; its discovery file and MRF were
  verified from the Pi on 2026-09-22.
- **Broken published links**: HCA Florida Kendall and TriStar Centennial
  publish stale Azure SAS tokens; the container-scoped token from HCA
  Houston's `mrf_url` can access the same container. If Houston's
  URL scheme changes, those two may start returning 403 errors. Yale New
  Haven's `cms-hpt.txt` points at a deleted file; the live upload carries
  the CMS `-1` suffix (`YALE_URL` in `local_refetch.py`). Since 2026-10-23,
  Yale also challenges command-line requests. If its normal download fails,
  the Pi opens the official pricing page in headless Chromium, waits for
  the download link, and retries within that browser session. Matching
  five-range fingerprints record an unchanged check; otherwise the browser
  streams a full download to disk for normal ingestion. Observation logs
  record `transport: browser` and the failed plain request. A challenge,
  invalid range, HTML response, or incomplete download cannot refresh
  verification. The freshness policy is unchanged.
- **Files above CI's 10 GB cap**: University of Kansas now publishes a
  36.8 GB CSV (verified 2026-10-16). Its previous 404 outage has ended.
  The Pi runs the normal scraper without a download cap and archives the
  original after summarizing it. Unchanged files use the usual validators
  and fingerprint checks rather than downloading again.

Local downloads use `~/.cache/hospital-price-history/refetch-*/`, including
the scraper subprocess's temporary files. The Pi's `/tmp` is a 1.9 GB RAM
disk and cannot hold Kansas's file. Scratch directories are removed after
each completed run; allow space for the download, extracted files, and
compressed originals. An initial Kansas fetch may take several hours.
Kansas's impersonated-download fallback allows up to 12 hours per attempt.

### Yale browser fallback setup

On the Pi, install matching Chromium and ChromeDriver packages, then the
optional Python dependencies in the collector's existing venv:

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends chromium chromium-driver
.venv/bin/pip install -r requirements-local.txt
```

Keep Chromium and ChromeDriver updated together. Other hosts can set
`BROWSER_BINARY` and `CHROMEDRIVER` to matching installed executables.
Each session has a disposable profile under the disk-backed scratch directory;
no cookies or browser profiles are committed. Only Yale uses this fallback.

Run the browser transport's local HTTP fixture tests with:

```sh
HPH_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_browser_fetch.py -v
```

These cover real range requests, streaming downloads, unsupported ranges,
malformed ranges, HTML challenges, and broken transfers. They do not contact
hospitals. The ordinary suite also tests Yale's fallback and observation paths.
The verified browser download is ingested with `--source-url`, which records
the URL actually fetched and skips blocked command-line discovery and HEAD
requests. Only use that option for a source whose URL has been verified.

## Discovery listing changes

The scraper matches each roster entry's `location_name` against the
hospital's `cms-hpt.txt`. UAMS renamed its listing to `UAMS Medical Center`
in October 2026; the roster now uses that name. A returning listing forces
a download and rebuild so `missing-from-hpt-txt` clears only after a valid
snapshot is collected, even if the file itself has not changed.

## Health check

`ssh lkowalcz@homectl.local` from the Mac. Then:

```sh
tail -20 ~/hospital-price-refetch.log      # a good run ends "done: Local refetch: ..."
crontab -l                                 # the single line below
ssh -T github-hph; ssh -T github-hph-raw   # each must greet the matching repo
```

The crontab line:

```
0 13 * * * cd $HOME/hospital-price-history && flock -n /tmp/hph-refetch.lock sh -c 'git pull --rebase --quiet && exec .venv/bin/python local_refetch.py' >> $HOME/hospital-price-refetch.log 2>&1
```

In the main repo, `git log --author=price-history-pi` shows the Pi's
commits. Use the heartbeat tag to check completed host runs and collection
logs for individual checks. The cron launcher pulls under the same lock before
starting Python, so a new version is imported on the next run. Never update or
restart an active multi-gigabyte download just to roll out collector code.

## Rebuild the Pi from scratch

Pi OS 64-bit (Debian trixie) with `git` and `python3-venv` installed.

1. Clone the main repo without downloading historical file contents:
   `git clone --filter=blob:none https://github.com/lkowalcz/hospital-price-history`
2. Use a sparse checkout for the raw repo to avoid downloading existing shards:
   ```sh
   git clone --filter=blob:none --no-checkout https://github.com/lkowalcz/hospital-price-history-raw
   cd hospital-price-history-raw
   git sparse-checkout init --no-cone && git sparse-checkout set '/*' '!/data/' && git checkout main
   ```
3. In the main clone: `python3 -m venv .venv && .venv/bin/pip install -r requirements-local.txt`.
   Install Chromium and ChromeDriver as described above for Yale's fallback,
   then install curl-impersonate where `local_refetch.py` expects it (use
   the release the workflow's `CI_VERSION` names; v2.2.2 as of 2026-09-02):
   ```sh
   mkdir .tools && cd .tools && curl -sL https://github.com/lexiforest/curl-impersonate/releases/download/v2.2.2/curl-impersonate-v2.2.2.aarch64-linux-gnu.tar.gz | tar xz
   ```
4. In both clones: `git config user.name price-history-pi` and
   `git config user.email lkowalcz@gmail.com`.
5. One deploy key per repo (GitHub refuses one key on two repos):
   `ssh-keygen -t ed25519 -N '' -f ~/.ssh/hph` and again for `~/.ssh/hph-raw`.
   In `~/.ssh/config` add `Host github-hph` and `Host github-hph-raw`, each
   with `HostName github.com`, `User git`, the matching `IdentityFile`, and
   `IdentitiesOnly yes`. Then `ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts`.
6. From the Mac, register each public key with write access:
   `gh repo deploy-key add <pub> -R lkowalcz/<repo> --allow-write -t homectl-pi-refetch`.
7. Point only the push URLs at the keyed hosts; fetch stays HTTPS:
   `git remote set-url --push origin git@github-hph:lkowalcz/hospital-price-history.git`
   and the `github-hph-raw` equivalent in the raw clone.
8. Configure Archive.org credentials so the Pi can upload originals for
   summarized hospitals. Run `.venv/bin/ia configure` on the Pi with the
   lkowalcz@gmail.com account, or copy `~/.config/internetarchive/ia.ini`
   from the Mac (`chmod 600`). The job prints "cold storage: on" at the
   start of each run when it found them.
9. Run `.venv/bin/python local_refetch.py` once by hand, check the log, then
   install the crontab line above.

## Revoke or rotate keys

In each repo, open Settings → Deploy keys → `homectl-pi-refetch` to
remove the key. You can also use `gh repo deploy-key list -R lkowalcz/<repo>`
and `gh repo deploy-key delete`. To create replacements, follow steps 5
through 7 above.

## Fall back to the Mac

If the Pi is down for more than a few days, the Mac can run the same job.
Its full clones and `.tools/` are still in place. Create
`~/Library/LaunchAgents/com.hospital-price-history.local-refetch.plist`
running `/opt/homebrew/bin/python3 <repo>/local_refetch.py` with
`WorkingDirectory` set to the repo, `PATH` of
`/opt/homebrew/bin:/usr/bin:/bin`, a `StartCalendarInterval` of hour 13,
and stdout and stderr to `~/Library/Logs/hospital-price-refetch.log`. Then
`launchctl load` it. Unload it once the Pi is back to prevent both jobs
from writing to the repos at the same time.

## Refreshing curl-impersonate

Fifteen hospitals use curl-impersonate because their CDNs block downloads
by TLS fingerprint. The tool mimics a specific Chrome release, which can
stop working as CDN rules change. Refresh the build about twice a year,
or when several hospitals using impersonation start failing together.

1. Find the latest release at
   https://github.com/lexiforest/curl-impersonate/releases and unpack the
   macOS tarball somewhere temporary.
2. Probe with the wrapper in use, then with the candidate (the newest
   `curl_chromeNNN` in the new tarball):
   ```sh
   python3 check_impersonate.py .tools/curl_chrome150
   python3 check_impersonate.py /tmp/new/curl_chromeNNN
   ```
   Every hospital that passes with the old wrapper must pass with the new.
3. Update `CI_VERSION` and `CI_WRAPPER` in
   `scrape.yml`, `IMPERSONATE_WRAPPER` in `local_refetch.py` (a test checks
   that the wrapper names match), the "Last checked" date in `scrape.yml`,
   this Mac's `.tools/`, and the Pi's `.tools/` (step 3 of the rebuild,
   over ssh). The cron launcher pulls before Python starts, so the next run
   imports the new wrapper name.

Last refreshed 2026-09-02: v2.2.2, `curl_chrome150`, 15/15 hospitals.

## Internet Archive cold storage

For the 29 `summarized` hospitals, the repo stores price summaries and
metadata. Original files are uploaded to archive.org as items named
`hospital-price-history-<slug>-<sha256 prefix>`.

- **Credentials**: an archive.org account's S3-style keys, from
  https://archive.org/account/s3.php. In this repo they are the secrets
  `IA_ACCESS_KEY_ID` and `IA_SECRET_ACCESS_KEY` (Settings, Secrets and
  variables, Actions). If either is missing, the scraper runs without
  uploading originals.
- **What the daily run does**: uploads each new summarized snapshot right
  after summarizing it, and re-downloads up to `BACKFILL_PER_RUN` (2)
  hospitals without archived originals per run until every one within
  the download cap has a `cold_storage` record. Progress is visible in commit messages
  as "archived originals: ..." and failures as "cold storage failed: ...";
  a failure is retried after seven days.
- **Files over the CI cap** (Mayo Rochester, Florida, Arizona; 13 to 22
  GB): archived from the Mac. After running `ia configure` there, use a
  local scrape to re-download and archive them:
  ```sh
  IA_ARCHIVE=1 IA_BIN=~/Library/Python/3.10/bin/ia ONLY=mayo-rochester,mayo-florida,mayo-arizona \
    BACKFILL_PER_RUN=3 CURL_IMPERSONATE_BIN=.tools/curl_chrome150 CURL_MAX_TIME=14400 python3 scrape.py
  ```
  Mayo serves a few MB/s, so allow a couple of hours; then commit the
  three `meta.json` and `summary.csv` files. An already-downloaded file
  can instead go through `archive_snapshot.py <slug> <file>`.
- **Hospitals handled by the Pi** (the workflow's `SKIP` list) are
  archived locally. `local_refetch.py` turns cold storage on whenever
  `~/.config/internetarchive/ia.ini` exists there and
  the venv has the `ia` client. Kansas requires this for its oversized CSV;
  other hospitals may cross the summary size threshold as files change. The daily
  heartbeat workflow fails if any hospital handled by the Pi is summarized
  without an archived original, providing an email alert if credentials
  are missing or expired. To archive an existing snapshot from the Mac,
  use `ingest_local.py <slug> <file> --force` to process and upload it again.
- **Rotation**: generate new keys on the same page, replace the two secrets,
  and the next run uses them. Then `ia configure` again on the Mac and on
  the Pi (or copy the Mac's `ia.ini` over), since both hold the old keys.
- **Checking**: `grep -L cold_storage data/*/meta.json` lists hospitals
  not yet archived, and each `cold_storage.url` opens the item.

## Historical source repairs

Historical source-link repairs live in `source_audit.json`, separate from the
collector's current metadata. See [the source audit](SOURCE_AUDIT.md) for
evidence, the remaining unavailable original, and commands to repeat full
archive verification. A site build validates committed identity evidence;
it does not re-download archived originals. Changes to the audit trigger
tests and site deployment.

## When a commit says "summary shrank"

The scraper flags a new summary with fewer than half the rows or coded
rows of its predecessor. It commits the snapshot and shows a warning on
the hospital's page. The price index excludes that hospital until a later
file passes the check.

- Review the hospital's `summary.csv` diff and source file. A file that
  stops mid-row or whose rows all lack codes may indicate a truncated
  download or a changed layout. Check whether the hospital has actually
  reduced its service list.
- For a truncated or restructured file, the previous snapshot remains in
  git. A later file that passes the check clears the warning. If a lasting
  layout change prevents the summarizer from reading the file, update the
  summarizer and bump `SUMMARY_VERSION`.
- If you confirm a reduction that should count in the index, delete
  `summary_warning` from the hospital's `meta.json` and commit. The index
  includes the hospital again the next day.

## Recovering from a bad run

- **A hospital directory is empty or missing an expected `summary.csv`**:
  snapshots are built under `data/.staging/` and moved into place when
  complete. If a summary is missing, `rebuild_summaries.py` regenerates it
  from stored content, and deleting the hospital's `sha256` from
  `meta.json` forces a fresh download on the next run.
- **Leftover `data/.staging/` directories** in either clone are safe to
  delete; they are ignored or never staged.
- **The raw repo push succeeded but the main push failed**: the next run
  re-fetches and re-pins `raw_commit`; nothing needs to be done.
- **Every hospital fails on a full CI run**: the majority-failure guard
  exits before committing. Start by checking the runner's network.
