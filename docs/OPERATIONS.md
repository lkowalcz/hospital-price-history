# Operations

The archive has two schedulers. GitHub Actions runs `scrape.py` daily and
deploys the site; a Raspberry Pi on the home network runs `local_refetch.py`
daily for the hospitals CI cannot reach. This page is the runbook for the
Pi half, which nothing else documents. Nothing here is secret: the private
deploy keys exist only on the Pi.

## What runs where

| Job | Host | Schedule | Owns |
|-----|------|----------|------|
| `scrape.yml` | GitHub Actions | daily, 06:23 UTC cron (often delayed by hours) | every hospital except the `SKIP` list in the workflow |
| `pages.yml` | GitHub Actions | on completion of `scrape.yml`, and on any push touching `data/` | the site |
| `local_refetch.py` | Pi `homectl.local` | daily, 13:00 New York, via cron | the `LOCAL_ONLY` list in `local_refetch.py`; a test pins it to the workflow's `SKIP` |

The Pi's commits push with a deploy key, so they trigger the push-based site
deploy on their own.

**Heartbeat.** Quiet days produce no commit, so after every run the Pi
force-pushes an annotated tag `pi-heartbeat` whose tagger date is the
completion time. The `pi-heartbeat.yml` workflow runs daily at 18:30 UTC and
fails, which GitHub emails, when that date is more than 72 hours old or the
tag is missing. A failure email means: run the health check below. Once the
Pi is back, the next completed run clears it on its own.

## Why the Pi exists

- **Runner-IP blocks**: the Johns Hopkins hospitals (four slugs) and Orlando
  Regional serve a residential IP but refuse GitHub's runners.
- **Broken published links**: HCA Florida Kendall and TriStar Centennial
  publish stale Azure SAS tokens; the container-scoped token from HCA
  Houston's `mrf_url` (the donor) reads the same container. If Houston's
  URL scheme changes, those two start failing with 403. Yale New Haven's
  `cms-hpt.txt` points at a deleted file; the live upload carries the CMS
  `-1` suffix (`YALE_URL` in `local_refetch.py`).

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

From anywhere, `git log --author=price-history-pi` on the main repo shows
what the Pi has pushed. A gap of more than a couple of days means the job is
stuck or the Pi is down.

## Rebuild the Pi from scratch

Pi OS 64-bit (Debian trixie) with `git` and `python3-venv` installed.

1. Clone the product repo without history blobs:
   `git clone --filter=blob:none https://github.com/lkowalcz/hospital-price-history`
2. Clone the raw repo sparse, so no shards ever land on disk:
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
8. Run `.venv/bin/python local_refetch.py` once by hand, check the log, then
   install the crontab line above.

## Revoke or rotate keys

Each repo, Settings, Deploy keys, `homectl-pi-refetch`; or
`gh repo deploy-key list -R lkowalcz/<repo>` and `gh repo deploy-key delete`.
Regenerate with steps 5 through 7 above.

## Fall back to the Mac

If the Pi is down for more than a few days, the Mac can run the same job.
Its full clones and `.tools/` are still in place. Create
`~/Library/LaunchAgents/com.hospital-price-history.local-refetch.plist`
running `/opt/homebrew/bin/python3 <repo>/local_refetch.py` with
`WorkingDirectory` set to the repo, `PATH` of
`/opt/homebrew/bin:/usr/bin:/bin`, a `StartCalendarInterval` of hour 13,
and stdout and stderr to `~/Library/Logs/hospital-price-refetch.log`. Then
`launchctl load` it, and unload it again once the Pi is back so the two do
not race.

## Refreshing curl-impersonate

Fifteen hospitals are fetched through curl-impersonate because their CDNs
block by TLS fingerprint. The fingerprint mimics a specific Chrome
release, and CDN rules eventually start treating old releases as bots, so
the build is refreshed about twice a year, or as soon as impersonated
hospitals start failing together.

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
3. Switch everything together: `CI_VERSION` and `CI_WRAPPER` in
   `scrape.yml`, `IMPERSONATE_WRAPPER` in `local_refetch.py` (a test pins
   the two names to each other), the "Last checked" date in `scrape.yml`,
   this Mac's `.tools/`, and the Pi's `.tools/` (step 3 of the rebuild,
   over ssh). The Pi picks up the new wrapper name on its second run after
   the pull, since it imports `local_refetch.py` before pulling.

Last refreshed 2026-09-02: v2.2.2, `curl_chrome150`, 15/15 hospitals.

## Internet Archive cold storage

The 29 `summarized` hospitals keep only a digest in the repo; their
original files are preserved as items on archive.org named
`hospital-price-history-<slug>-<sha256 prefix>`.

- **Credentials**: an archive.org account's S3-style keys, from
  https://archive.org/account/s3.php. In this repo they are the secrets
  `IA_ACCESS_KEY_ID` and `IA_SECRET_ACCESS_KEY` (Settings, Secrets and
  variables, Actions). While either is missing, archiving is simply off and
  the rest of the scrape is unaffected.
- **What the daily run does**: uploads each new summarized snapshot right
  after summarizing it, and re-downloads up to `BACKFILL_PER_RUN` (2)
  not-yet-archived hospitals per run until every one within the download
  cap has a `cold_storage` record. Progress is visible in commit messages
  as "archived originals: ..." and failures as "cold storage failed: ...";
  a failure is retried after seven days.
- **Files over the CI cap** (Mayo Rochester, Florida, Arizona; 10 GB and
  up): fetch locally, ingest with `ingest_local.py`, then run
  `archive_snapshot.py <slug> <file>` with `ia configure` done on that
  machine. Keep the download until the item shows up.
- **Rotation**: generate new keys on the same page, replace the two secrets,
  and the next run uses them. Locally, `ia configure` again.
- **Checking**: `grep -L cold_storage data/*/meta.json` lists hospitals
  not yet archived, and each `cold_storage.url` opens the item.

## When a commit says "summary shrank"

The scraper flags a new summary that has less than half the rows, or half
the coded rows, of the one it replaced. Nothing is blocked: the snapshot
is committed as published, the hospital's page shows a warning, and the
price index leaves the hospital out until a later file passes.

- Open the hospital's `summary.csv` diff in that commit. A file that stops
  mid-row, or whose rows now all lack codes, is a truncated download or a
  restructured layout; a file that simply lists fewer services is a real
  change.
- Truncated or restructured: nothing to repair. The previous snapshot is
  in git, and the hospital republishing a good file clears the flag on its
  own. If the layout changed for good and the summarizer no longer reads
  it, that is a summarizer fix, then `SUMMARY_VERSION` bump.
- A real reduction that should count: delete `summary_warning` from the
  hospital's `meta.json` and commit; the index picks it up the next day.

## Recovering from a bad run

- **A hospital directory is empty or missing `summary.csv`**: snapshots are
  built under `data/.staging/` and swapped in whole, so this should no
  longer happen. If it does, `rebuild_summaries.py` regenerates summaries
  from stored content, and deleting the hospital's `sha256` from
  `meta.json` forces a fresh download on the next run.
- **Leftover `data/.staging/` directories** in either clone are safe to
  delete; they are ignored or never staged.
- **The raw repo push succeeded but the main push failed**: the next run
  re-fetches and re-pins `raw_commit`; nothing needs to be done.
- **Every hospital fails on a full CI run**: the majority-failure guard
  exits before committing. Check the runner's network first, not the
  hospitals.
