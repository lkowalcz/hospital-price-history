# hospital-price-history

A [git-scraping](https://simonwillison.net/2020/Oct/9/git-scraping/) archive of
hospital price transparency machine-readable files (MRFs), which US hospitals
are required to publish under 45 CFR § 180.50.

Aggregations of these files exist. **A public record of how they change over
time does not.** Every commit here is a timestamped snapshot: when a hospital
revises a negotiated rate, republishes its file, or quietly removes it, the
git history shows exactly when.

## Coverage

21 hospitals across 18 major systems, weighted toward large academic medical
centers: Mass General Brigham (MGH, Brigham and Women's, Faulkner, Martha's
Vineyard), NewYork-Presbyterian Columbia, Stanford, Cleveland Clinic main
campus, Cedars-Sinai, NYU Langone Tisch, University of Chicago, UCSF
Parnassus, Ronald Reagan UCLA, Duke University Hospital, MD Anderson,
Northwestern Memorial, Hospital of the University of Pennsylvania, Northwell
North Shore, Emory University Hospital, Kaiser Oakland, Atrium Carolinas
Medical Center, and Boston Medical Center.

See `hospitals.json` for the exact list; each entry's current state lives in
`data/<slug>/meta.json`.

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
| `sharded` | ≤ 600 MB, parseable | 32 hash-bucketed, sorted shard files (CSV rows or JSONL items) plus a `_header` file — every shard is git-sized and a changed row touches only its bucket |
| `metadata-only` | larger or unparseable | `meta.json` hash/size/timing only, so change *timing* is still captured. Several systems (Northwestern, Atrium, Penn, Cleveland Clinic, Duke, Cedars-Sinai, Northwell) publish 0.7–5 GB files that exceed any sane git budget |

Servers that provide no `Last-Modified`/`ETag` force a full download just to
detect change; files over 400 MB from such servers are refetched weekly
(Sundays) instead of daily.

## Running locally

```sh
python3 scrape.py
```

No dependencies. Writes into `data/` and `commit_message.txt`.

## Notes

- Hospital CDNs behind Cloudflare/Akamai often refuse non-browser
  User-Agents, despite the CMS requirement that these files be accessible to
  automated searches; the scraper sends a browser UA. Johns Hopkins, UW
  Medicine, Mayo Clinic, Mount Sinai, Houston Methodist, and Vanderbilt block
  at the TLS-fingerprint level and would need a real browser fetch — future
  work.
- The scraper re-discovers each MRF URL from `cms-hpt.txt` on every run, so a
  URL change or a delisting is itself recorded in `meta.json`.
