# Operations

GitHub Actions runs `scrape.py` daily and deploys the site. A Raspberry Pi
on the home network runs `local_refetch.py` daily for hospitals that need
a local connection or a corrected download URL. This guide covers the
Pi's setup, monitoring, and recovery. Private deploy keys are stored only
on the Pi.

## What runs where

| Job | Host | Schedule | Responsibility |
|-----|------|----------|------|
| `scrape.yml` | GitHub Actions | daily, 06:23 UTC cron (often delayed by hours) | every hospital except the `SKIP` list in the workflow |
| `pages.yml` | GitHub Actions | on completion of `scrape.yml`, and on any push touching `data/` | the site |
| `local_refetch.py` | Pi `homectl.local` | daily, 13:00 New York, via cron | the `LOCAL_ONLY` list in `local_refetch.py`; a test checks that it matches the workflow's `SKIP` |

The Pi pushes commits with a deploy key, which triggers the site's
push-based deployment workflow.

After each run, the Pi force-pushes an annotated `pi-heartbeat` tag with
the completion time as its tagger date. This records successful runs even
when no files changed and no commit was needed. The `pi-heartbeat.yml`
workflow checks the tag daily at 18:30 UTC. A missing tag or a date more
than 72 hours old causes a workflow failure and a GitHub email. If you
receive one, follow the health check below. The next completed Pi run
updates the tag.

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
  the CMS `-1` suffix (`YALE_URL` in `local_refetch.py`).

## Known source outages

- **University of Kansas** (checked 2026-09-22): the Kansas City MRF URL
  published in both `cms-hpt.txt` and the hospital's
  [pricing page](https://www.kansashealthsystem.com/patient-visitor/financial/patient-bills/services-fees/charge-descriptions)
  returns Azure `BlobNotFound` (HTTP 404), including from the residential
  connection. Great Bend and Paola also return 404; Olathe's file is
  reachable but belongs to a different hospital.
  No replacement has been verified. CI continues checking the published
  link daily and retains the previous summary and archived original;
  do not clear `fetch_failures` until a live file is reachable. The
  hospital's discovery file lists `ManagedCareContracting@kumc.edu` as
  the contact for correcting the link.

## Health check

`ssh lkowalcz@homectl.local` from the Mac. Then:

```sh
tail -20 ~/hospital-price-refetch.log      # a good run ends "done: Local refetch: ..."
crontab -l                                 # the single line below
ssh -T github-hph; ssh -T github-hph-raw   # each must greet the matching repo
```

The crontab line:

```
0 13 * * * cd $HOME/hospital-price-history && flock -n /tmp/hph-refetch.lock .venv/bin/python local_refetch.py >> $HOME/hospital-price-refetch.log 2>&1
```

In the main repo, `git log --author=price-history-pi` shows the Pi's
commits. Use the heartbeat tag to check whether the job is running;
days without file changes do not produce commits.

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
3. In the main clone: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
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
   over ssh). The Pi picks up the new wrapper name on its second run after
   the pull, since it imports `local_refetch.py` before pulling.

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
  the venv has the `ia` client. Currently, only TriStar Centennial is large
  enough to be summarized; HCA Kendall is near the size threshold. The daily
  heartbeat workflow fails if any hospital handled by the Pi is summarized
  without an archived original, providing an email alert if credentials
  are missing or expired. To archive an existing snapshot from the Mac,
  use `ingest_local.py <slug> <file> --force` to process and upload it again.
- **Rotation**: generate new keys on the same page, replace the two secrets,
  and the next run uses them. Then `ia configure` again on the Mac and on
  the Pi (or copy the Mac's `ia.ini` over), since both hold the old keys.
- **Checking**: `grep -L cold_storage data/*/meta.json` lists hospitals
  not yet archived, and each `cold_storage.url` opens the item.

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
