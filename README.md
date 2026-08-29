# hospital-price-history

A [git-scraping](https://simonwillison.net/2020/Oct/9/git-scraping/) archive of
hospital price transparency machine-readable files (MRFs), which US hospitals
are required to publish under 45 CFR § 180.50.

Aggregations of these files exist. **A public record of how they change over
time does not.** Every commit here is a timestamped snapshot: when a hospital
revises a negotiated rate, republishes its file, or quietly removes it, the
git history shows exactly when.

## Coverage

107 hospitals chosen for breadth of ownership type and geography (see
`hospitals.json` for the full roster). The founding cohort, one flagship
per system:

- **Academic medical centers**: MGH, Brigham and Women's (+2 MGB community
  hospitals), NYP Columbia, Stanford, Cleveland Clinic, Cedars-Sinai, NYU
  Langone, UChicago, UCSF, UCLA, Duke, MD Anderson, Northwestern, Penn,
  Emory, Rush, Michigan Medicine, UAB, MUSC, Yale New Haven, Jefferson,
  Geisinger, Banner University Phoenix
- **For-profit chains**: HCA (Florida Kendall, Medical City Dallas, TriStar
  Centennial, HCA Houston), Tenet (DMC Harper University)
- **Public safety-net hospitals**: Parkland, Grady, Jackson Memorial,
  Denver Health, Boston Medical Center
- **Large nonprofit/regional systems**: Kaiser Oakland, Providence Portland,
  AdventHealth Orlando, Sutter CPMC Van Ness, Intermountain, Baylor
  University Medical Center, Ochsner, OhioHealth Riverside, Novant
  Presbyterian, Northwell North Shore, Atrium Carolinas, Tampa General,
  Baptist Miami, Wellstar Kennestone, Corewell Butterworth, Sanford USD,
  Martha's Vineyard

See `hospitals.json` for the exact list; each entry's current state lives in
`data/<slug>/meta.json`, and every hospital gets a `summary.csv` — a uniform
per-code digest (gross charge, cash price, min/max negotiated, payer count)
regardless of how the full file is stored.

## How it works

- `scrape.py` (stdlib-only Python) runs daily via GitHub Actions. For each
  hospital it fetches the CMS-standard `/cms-hpt.txt` discovery file and
  follows the listed MRF URL — so URL changes and delistings are themselves
  recorded.
- A `HEAD` request short-circuits the download when the server's
  `Last-Modified`/`ETag` still match the stored copy, keeping daily runs
  cheap even with hundreds of MB under watch.
- Payloads are unzipped and normalized (line endings, sorted JSON keys), and
  written only when the content hash changed; the workflow commits only when
  there is a diff. `git log data/<slug>/` is a clean changelog per hospital.

### Storage modes (automatic, by size)

| Mode | When | What's stored |
|------|------|---------------|
| `stored` | ≤ 45 MB | single normalized file |
| `sharded` | ≤ 600 MB, parseable | 32 hash-bucketed, sorted shard files (CSV rows or JSONL items) plus a `_header` file — every shard is git-sized and a changed row touches only its bucket. Lives in the [companion raw repo](https://github.com/lkowalcz/hospital-price-history-raw) (see below) |
| `summarized` | larger, CMS v3 | `summary.csv`: per code — description, gross charge, discounted cash price, min/max negotiated rate, payer-entry count. Collapses the 0.7–5 GB files (Northwestern, Atrium, Penn, Cleveland Clinic, Duke, Cedars-Sinai, Northwell) into a few diffable MB |
| `metadata-only` | unparseable | `meta.json` hash/size/timing only, so change *timing* is still captured |

### Where the data lives

This repo is the product layer — `hospitals.json`, per-hospital `meta.json`
and `summary.csv`, small (`stored`) payloads, the price index, and the site.
Payer-level sharded content lives in a companion repo,
[hospital-price-history-raw](https://github.com/lkowalcz/hospital-price-history-raw),
under the same `data/<slug>/` layout, so this repo stays a ~1 GB clone
while the raw record grows freely.

A daily snapshot is therefore a **pair of commits**: the raw repo is
committed first, and each sharded hospital's `meta.json` in the main commit
pins the exact raw commit (`raw_commit`) holding its shards. The raw repo
carries the complete shard history from the archive's first day — `git log
data/<slug>/` there is the full payer-level changelog per hospital.

For `summarized` giants, whose repo representation is lossy, the original
multi-GB download can be preserved via `archive_snapshot.py`, which
zstd-compresses it, uploads it to the Internet Archive, and records the
item URL + sha256 in `meta.json` under `cold_storage`.

### Cheap change detection

Downloads are skipped when nothing changed, checked in escalating order of
cost: `HEAD` `Last-Modified`/`ETag` when the server provides validators;
otherwise a fingerprint hashed from ~1 MB Range-request samples at five
fixed offsets (+ content length) — ~5 MB to check a 5 GB file. Servers
supporting neither (Cedars-Sinai, Cleveland Clinic, Duke) get a full
refetch weekly (Sundays) instead of daily.

## Running locally

```sh
git clone https://github.com/lkowalcz/hospital-price-history-raw ../hospital-price-history-raw
python3 scrape.py
```

Writes into `data/`, `commit_message.txt`, and (for sharded hospitals) the
sibling raw clone — pull both repos first (the daily workflow also commits
to them), then commit and push both, raw first. `RAW_REPO_DIR`
overrides the raw clone location. `ONLY=slug1,slug2` limits the run;
`SKIP=slug1,slug2` excludes slugs (ignored when `ONLY` is set). The daily
workflow SKIPs the hospitals `local_refetch.py` owns, so CI never records a
failure streak that the local run would clear a few hours later.

Neither clone needs history or old shards on disk — the scraper reads only
`meta.json` and rewrites a hospital's raw directory from scratch — so on a
small host (a Raspberry Pi running `local_refetch.py`) clone both with
`--filter=blob:none` and make the raw clone sparse, as the workflow does:

```sh
git clone --filter=blob:none --no-checkout git@github.com:lkowalcz/hospital-price-history-raw ../hospital-price-history-raw
cd ../hospital-price-history-raw
git sparse-checkout init --no-cone && git sparse-checkout set '/*' '!/data/' && git checkout main
```

That is ~2 GB on disk instead of ~17. `local_refetch.py` stages each
rewritten slug as an exact replacement of its index entries, so the sparse
and full layouts commit identically.

## Tests

```sh
python3 -m unittest discover -s tests
```

Golden-file tests for the parsers, summarizers, sharding round-trip, and
index chain math, run by CI on every code push. The fixtures bake in the
real-world pathologies listed under Notes; `tests/golden/` holds the
expected `summary.csv` bytes — a diff there is a methodology change and
should be reviewed as one.

## Notes

- Hospital CDNs behind Cloudflare/Akamai often refuse non-browser
  User-Agents, despite the CMS requirement that these files be accessible to
  automated searches; the scraper sends a browser UA, and hospitals whose
  CDNs block by TLS fingerprint are fetched via `curl-impersonate`
  (`"fetch": "impersonate"` in `hospitals.json`). When either transport
  fails, the scraper automatically retries once with the other; a plain
  fetch rescued by impersonation is remembered (`fetch_escalated` in
  `meta.json`) so later runs go straight to what works.
- UPMC, Houston Methodist, and Memorial Hermann serve a `cms-hpt.txt`
  containing prose instructions to click through their website instead of
  the machine-readable `mrf-url` blocks the CMS format specifies.
- Yale New Haven's `cms-hpt.txt` points to a dead URL (404); the live
  re-uploaded file is fetched by `local_refetch.py` instead. Geisinger's
  MRF link resolves to a Radware CAPTCHA page, tracked as an ongoing
  `fetch_failures` streak in its `meta.json`. UAB's CDN blocks GitHub's
  runner IPs (its snapshot was fetched locally). Rush publishes its
  `mrf-url` without an `https://` scheme; the scraper compensates.
- Sutter Health serves its `cms-hpt.txt` as UTF-16; UCSF's JSON leads with
  a UTF-8 BOM; several zips contain `__MACOSX` junk. The scraper tolerates
  all of these.
- The scraper re-discovers each MRF URL from `cms-hpt.txt` on every run, so a
  URL change or a delisting is itself recorded in `meta.json`.
