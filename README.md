# hospital-price-history

A [git-scraping](https://simonwillison.net/2020/Oct/9/git-scraping/) archive of
hospital price transparency machine-readable files (MRFs), which US hospitals
are required to publish under 45 CFR § 180.50.

Aggregations of these files exist. **A public record of how they change over
time does not.** Every commit here is a timestamped snapshot: when a hospital
revises a negotiated rate, republishes its file, or quietly removes it, the
git history shows exactly when.

## How it works

- `hospitals.json` lists the pilot hospitals.
- `scrape.py` (stdlib-only Python) runs daily via GitHub Actions. For each
  hospital it fetches the CMS-standard `/cms-hpt.txt` discovery file, follows
  the listed MRF URL, unzips/normalizes the payload, and writes it to
  `data/<slug>/` — but only when the content hash actually changed.
- The workflow commits only when there is a diff, so `git log data/<slug>/`
  is a clean changelog of that hospital's pricing file.

Files too large for git (e.g. NewYork-Presbyterian's 227 MB JSON) are tracked
**metadata-only**: `meta.json` records the SHA-256, size, and Last-Modified on
every change, so the *timing* of changes is still captured even though the
content isn't stored.

## Pilot hospitals

| Slug | Hospital | System | Mode |
|------|----------|--------|------|
| `mgh` | Massachusetts General Hospital | Mass General Brigham | full CSV |
| `brigham-and-womens` | Brigham and Women's Hospital | Mass General Brigham | full CSV |
| `bwh-faulkner` | Brigham and Women's Faulkner Hospital | Mass General Brigham | full CSV |
| `marthas-vineyard` | Martha's Vineyard Hospital | Mass General Brigham | full CSV |
| `nyp-columbia` | NewYork-Presbyterian Columbia | NewYork-Presbyterian | metadata-only |

## Running locally

```sh
python3 scrape.py
```

No dependencies. Writes into `data/` and `commit_message.txt`.

## Notes

- Some hospital sites (Cleveland Clinic, Johns Hopkins, UW Medicine, Mayo)
  block non-browser requests to `cms-hpt.txt`; adding them will need a
  different fetch strategy.
- The scraper re-discovers each MRF URL from `cms-hpt.txt` on every run, so a
  URL change or a delisting is itself recorded in `meta.json`.
