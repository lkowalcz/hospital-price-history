# hospital-price-history

This project archives the machine-readable price files (MRFs) that US
hospitals publish under 45 CFR § 180.50. It uses
[git-scraping](https://simonwillison.net/2020/Oct/9/git-scraping/) to save
successive versions so you can compare a hospital's published prices over
time.

The archive records when it finds revised files, changed download URLs, or
links removed from a hospital's listing. Hospital pages include available
price summaries and a history of recorded changes.

## Coverage

The archive tracks 107 hospitals across ownership types and regions. The
full roster is in [hospitals.json](hospitals.json). It includes:

- **Academic medical centers**: MGH, Brigham and Women's (+2 MGB community
  hospitals), NYP Columbia, Stanford, Cleveland Clinic, Cedars-Sinai, NYU
  Langone, UChicago, UCSF, UCLA, Duke, MD Anderson, Northwestern, Penn,
  Emory, Rush, Michigan Medicine, UAB, MUSC, Yale New Haven, Jefferson,
  Geisinger, Banner University Phoenix
- **For-profit chains**: HCA (Florida Kendall, Medical City Dallas, TriStar
  Centennial, HCA Houston), Tenet (DMC Harper University)
- **Public safety-net hospitals**: Parkland, Grady, Jackson Memorial,
  Denver Health, Boston Medical Center
- **Large nonprofit and regional systems**: Kaiser Oakland, Providence
  Portland, AdventHealth Orlando, Sutter CPMC Van Ness, Intermountain,
  Baylor University Medical Center, Ochsner, OhioHealth Riverside, Novant
  Presbyterian, Northwell North Shore, Atrium Carolinas, Tampa General,
  Baptist Miami, Wellstar Kennestone, Corewell Butterworth, Sanford USD,
  Martha's Vineyard

For each hospital, `data/<slug>/meta.json` records the current status.
`summary.csv` lists gross charges, cash prices, minimum and maximum
negotiated rates, and payer-entry counts by billing code. Summaries use the
same columns regardless of how the source file is stored. See
[docs/DATA.md](docs/DATA.md) for field definitions and aggregation rules.

## How it works

`scrape.py` runs daily through GitHub Actions. It checks each hospital's
`/cms-hpt.txt` discovery file and follows the listed MRF URL, recording
changes to the URL or its removal from the listing.

Downloads are unzipped and normalized, including line endings and JSON key
order. The scraper writes a new snapshot when the content hash changes,
and the workflow commits when there is a diff. To see a hospital's
history, run `git log data/<slug>/`.

If a new summary has fewer than half the rows or coded rows of its
predecessor, the scraper saves it and adds a warning to `meta.json`, the
commit message ("summary shrank: ..."), and the hospital's page. This can
happen when a download is truncated or a hospital changes its file layout.

### Storage modes

The scraper chooses a storage mode based on file size and format:

| Mode | When | What's stored |
|------|------|---------------|
| `stored` | ≤ 45 MB | A single normalized file in this repo. |
| `sharded` | ≤ 600 MB, parseable | 32 sorted shard files (CSV rows or JSONL items) and a `_header` file in the [raw repo](https://github.com/lkowalcz/hospital-price-history-raw). |
| `summarized` | Larger, CMS v3 | A `summary.csv` with descriptions, gross charges, cash prices, minimum and maximum negotiated rates, and payer-entry counts by code. Files of 0.7–5 GB can produce summaries of a few MB. |
| `metadata-only` | Unparseable | Hash, size, and timestamps in `meta.json`, which still allow file changes to be tracked. |

Sharded files are split into buckets by a hash of each row's contents. A
changed row appears in the diff as a deletion and an insertion, keeping
diffs readable and individual files small enough for git.

### Where the data lives

This repo contains the hospital roster, metadata, summaries, small source
files, and website generator. Larger files stored as shards are in
[hospital-price-history-raw](https://github.com/lkowalcz/hospital-price-history-raw),
under the same `data/<slug>/` layout. Keeping them separate limits this
repo to roughly a 1 GB clone.

The raw repo is committed first. Each sharded hospital's `meta.json` then
records the exact raw commit containing its files in `raw_commit`. The
raw repo includes shard history from the archive's first day; run
`git log data/<slug>/` there to see changes to payer-level data.

For `summarized` hospitals, the original files are compressed with zstd
and uploaded to the Internet Archive as separate items. The
`cold_storage` field in `meta.json` records the item URL, SHA-256 hash, and
compressed size. Each hospital's page links to its archived original.

The daily run uploads originals when `IA_ACCESS_KEY_ID` and
`IA_SECRET_ACCESS_KEY` are set as repository secrets. Locally, run
`ia configure` and set `IA_ARCHIVE=1`. Files collected before archiving
was enabled are re-downloaded at a rate of `BACKFILL_PER_RUN` per run
(default 2); failed uploads are retried after a week. Files above the CI
download cap, such as Mayo's, can be archived manually with
`archive_snapshot.py <slug> <file>` after a local ingest.

### Checking for changes

The scraper avoids full downloads where possible. It first sends a `HEAD`
request and compares `Last-Modified` or `ETag` with the saved values. If
those are unavailable, it compares a fingerprint made from roughly 1 MB
Range-request samples at five fixed offsets, plus the content length.
This uses about 5 MB to check a 5 GB file.

Servers that support neither method, including Cedars-Sinai, Cleveland
Clinic, and Duke, are downloaded in full on Sundays instead of daily.

## Running locally

```sh
git clone https://github.com/lkowalcz/hospital-price-history-raw ../hospital-price-history-raw
python3 scrape.py
```

The scraper writes to `data/`, `commit_message.txt`, and the sibling raw
clone. Pull both repos before running, since the daily workflow also
commits to them. Commit and push the raw repo first, then the main repo.

Settings for local runs:

- `RAW_REPO_DIR` overrides the raw clone location.
- `ONLY=slug1,slug2` limits the run to specific hospitals.
- `SKIP=slug1,slug2` excludes hospitals and is ignored when `ONLY` is set.
  The daily workflow skips hospitals handled by `local_refetch.py` so CI
  does not record download failures for hospitals fetched locally.
- `MAX_DOWNLOAD_BYTES` caps download and unpacked size. The workflow sets
  it to 10 GB; it is unset locally.
- `CURL_MAX_TIME` sets the default timeout for impersonated fetches
  (one hour). A hospital's `"curl_max_time": <seconds>` in `hospitals.json`
  overrides it.

Snapshots are assembled under `data/.staging/` in each clone and moved
into `data/<slug>/` only when complete. An interrupted run therefore
leaves the previous snapshot in place. Staging directories are ignored
by git or excluded from staging; leftovers after a crash can be deleted.

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for the scheduled local run,
including the Raspberry Pi cron job, deploy keys, and setup instructions.

### Running with limited disk space

The scraper reads `meta.json` and rebuilds each hospital's raw directory
from scratch. It does not need old file contents on disk. On a small host,
clone both repos with `--filter=blob:none` and use a sparse checkout for
the raw repo, as the workflow does:

```sh
git clone --filter=blob:none --no-checkout git@github.com:lkowalcz/hospital-price-history-raw ../hospital-price-history-raw
cd ../hospital-price-history-raw
git sparse-checkout init --no-cone && git sparse-checkout set '/*' '!/data/' && git checkout main
```

This uses roughly 2 GB on disk instead of 17 GB. `local_refetch.py`
replaces the index entries for each updated hospital, producing the same
commits with a sparse or full checkout.

## Tests

```sh
python3 -m unittest discover -s tests
```

CI runs tests for parsing, summarization, sharding round-trips, and price
index calculations on every code push. Fixtures cover the file-format
issues described below. `tests/golden/` contains the expected
`summary.csv` output; changes to those files should be reviewed as
changes to the summarization method.

When the summarization method changes, bump `SUMMARY_VERSION` in
`scrape.py`. Each `meta.json` records that version. `compute_index.py`
excludes a hospital from the day's calculation when its summary version
differs from the previous run's, preventing a method change from being
counted as a price change.

Run `rebuild_summaries.py --force` from a machine with a full raw checkout
to update all `stored` and `sharded` hospitals at once. For `summarized`
hospitals, the daily run re-downloads files a few at a time until their
summaries are current. This uses the same `BACKFILL_PER_RUN` allowance as
cold-storage backfills.

### Experimental price index

`compute_index.py` runs after each daily scrape and appends to
`index-history.csv`. It calculates a chain-linked Jevons index of cash
prices and gross charges for the fixed basket in `basket.json`.
Day-over-day changes beyond 4× are logged to `index-anomalies.csv` and
excluded from the calculation.

The index is not published on the site. Hospitals tend to revise these
files roughly once a year, so the series changes infrequently. So far,
the anomaly log has mainly helped detect summarizer changes that would
otherwise appear as price changes. The index can be recalculated from
the committed `summary.csv` history.

## Source file issues

- Hospital CDNs behind Cloudflare or Akamai often reject non-browser
  User-Agents. The scraper sends a browser User-Agent and uses
  `curl-impersonate` for hospitals that block by TLS fingerprint
  (`"fetch": "impersonate"` in `hospitals.json`). If either transport fails,
  it retries once with the other. When impersonation succeeds after a
  plain fetch fails, `fetch_escalated` in `meta.json` tells later runs to
  use impersonation first.
- UPMC, Houston Methodist, and Memorial Hermann publish `cms-hpt.txt`
  files with instructions for browsing their websites instead of the
  machine-readable `mrf-url` blocks specified by the CMS format.
- Yale New Haven's listed URL returns a 404; `local_refetch.py` fetches
  the replacement file. Geisinger's link leads to a Radware CAPTCHA page,
  recorded under `fetch_failures` in its `meta.json`. UAB's CDN blocks
  GitHub runner IPs, so its snapshot was fetched locally. Rush omits the
  `https://` scheme from its `mrf-url`; the scraper adds it.
- Jackson Memorial's discovery file blocks GitHub runners; the daily Pi
  job handles it from a residential connection. University of Kansas's
  published file returns 404 as of 2026-09-22, with no verified replacement;
  its previous snapshot remains available while daily checks continue.
  See [known source outages](docs/OPERATIONS.md#known-source-outages).
- The parser handles Sutter Health's UTF-16 discovery file, the UTF-8
  byte order mark in UCSF's JSON, and ZIP files containing `__MACOSX`
  metadata.
