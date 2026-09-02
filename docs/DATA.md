# Data layout and `summary.csv` semantics

Every hospital directory `data/<slug>/` holds a `meta.json` and a
`summary.csv`. Depending on file size it also holds the normalized source
file (`standardcharges.csv` or `.json`, the `stored` mode), or points at
shards in the [raw repo](https://github.com/lkowalcz/hospital-price-history-raw)
(`sharded`), or at an archive.org item holding the original bytes
(`summarized`, see `cold_storage`). The README describes the modes.

## `summary.csv`

One row per (code type, code, description) seen in the hospital's file,
sorted. It is the same shape for every hospital regardless of how the full
file is stored, and it is what the price index and the site read.

| Column | Meaning |
|--------|---------|
| `code_type` | The code system as the hospital labels it: `MS-DRG`, `CPT`, `HCPCS`, `RC`, `CDM`, `LOCAL`, ... Not normalized; `DRG` and `MS-DRG` both occur across hospitals. |
| `code` | The code as published. Leading zeros are kept. |
| `description` | The hospital's description, truncated to 200 characters. |
| `gross_charge` | Median of the distinct positive gross charges listed for this row's key (summary version 2). `0.0` when only zero or negative placeholders were listed; blank when no value was listed. |
| `discounted_cash` | Same rule, for the discounted cash price. |
| `min_negotiated` | Lowest dollar-denominated negotiated rate across all payers and plans. Percentage and algorithm-based rates are not included. |
| `max_negotiated` | Highest such rate. |
| `payer_entries` | Number of dollar-denominated negotiated rates the row aggregates. |

Rules that shape the rows:

- **Multi-code rows fan out.** A service billed under an MS-DRG and a CPT
  appears under both codes, with the same prices.
- **Hospital-internal code types** (`LOCAL`, `CDM`, `RC`) are kept only
  when they are a row's primary code. They are never fanned out from later
  code slots, since chargemaster rows often carry all three side by side.
- **Un-coded rows** (drugs, room rates) are kept with an empty code type
  and code.
- **Tall and wide CSVs and CMS v3 JSON** all reduce to the same rows. The
  golden-file tests pin the exact bytes for each format.

## `summary_version`

`meta.json` records which summarizer wrote the summary, as
`summary_version`. A missing value means version 1.

| Version | Change |
|---------|--------|
| 1 | Gross charge and cash price taken from the first row listing the code. Depended on row order. |
| 2 | Median of the distinct positive values listed for the code. Independent of row order. |

When the version changes, the price index does not chain a hospital across
the boundary: its first day on the new version is skipped rather than
compounded in as a price move. See `compute_index.py`.

## `meta.json`

| Field | Meaning |
|-------|---------|
| `mrf_url`, `source_filename` | Where the file was found, via the hospital's `cms-hpt.txt`, and what it was called. |
| `sha256`, `size_bytes` | Identity and size of the normalized snapshot (for giants, of the raw file). |
| `status` | Storage mode: `stored`, `sharded`, `summarized`, `metadata-only`, or `missing-from-hpt-txt`. |
| `first_seen`, `last_changed` | When the hospital entered the archive and when its content last changed. |
| `source_last_modified`, `source_etag`, `transfer_fingerprint` | Validators used to skip unchanged files cheaply. |
| `raw_commit` | For `sharded`: the raw-repo commit holding this snapshot's shards. |
| `cold_storage` | For `summarized`: archive.org item URL, snapshot and file sha256, compressed size, date. |
| `fetch_failures` | Present while the source is unreachable: first failure, last error, last attempt. |
| `summary_version` | See above. |

## Citing

Each hospital page carries schema.org Dataset markup. The archive is
dedicated to the public domain under CC0; the underlying files are
hospital disclosures required by 45 CFR § 180.50.
