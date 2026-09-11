# Data files and field definitions

Hospital data is stored under `data/<slug>/`. `meta.json` records the
source, storage mode, and download status. `summary.csv`, when available,
contains prices grouped by billing code and description.

In `stored` mode, the directory also contains the normalized source file
(`standardcharges.csv` or `.json`). In `sharded` mode, the metadata points
to files in the [raw repo](https://github.com/lkowalcz/hospital-price-history-raw).
For `summarized` files, `cold_storage` links to the original on the
Internet Archive once it has been uploaded. See the
[README](../README.md#storage-modes) for storage thresholds.

## `summary.csv`

The summary has one row per distinct (code type, code, description),
sorted by those fields. All hospitals use the same columns. The website
and price index read these summaries.

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

The summarizer handles codes and file formats as follows:

- A service with multiple billing codes appears under each code with the
  same prices. For example, a service with both an MS-DRG and a CPT code
  produces a row for each.
- `LOCAL`, `CDM`, and `RC` codes are included only when they are the
  source row's primary code. Additional code slots of these types do not
  produce separate rows, since chargemaster entries often list all three.
- Entries without codes, such as some drugs or room rates, are kept with
  empty `code_type` and `code` fields.
- Tall CSVs, wide CSVs, and CMS v3 JSON use the same output format.
  Golden-file tests compare the exact summary output for each format.

## `summary_version`

The `summary_version` field in `meta.json` identifies the summarization
method used. A missing value means version 1.

| Version | Change |
|---------|--------|
| 1 | Gross charge and cash price taken from the first row listing the code. Reordering source rows could change the summary price. This caused the two 2026-08-27 entries in `index-anomalies.csv`: HCA Florida Kendall CPT 80053 and HCA Houston CPT 72148. Each file had reordered line items with different gross charges. |
| 2 | Median of the distinct positive values listed for the code. Independent of row order. |

The price index excludes a hospital on its first day using a new summary
version. This prevents a change in the summarization method from being
counted as a price change. See `compute_index.py`.

## `meta.json`

| Field | Meaning |
|-------|---------|
| `mrf_url`, `source_filename` | Where the file was found, via the hospital's `cms-hpt.txt`, and what it was called. |
| `sha256`, `size_bytes` | SHA-256 hash and size of the normalized snapshot, or of the raw file for files too large to normalize. |
| `status` | Storage mode: `stored`, `sharded`, `summarized`, `metadata-only`, or `missing-from-hpt-txt`. |
| `first_seen`, `last_changed` | When the hospital entered the archive and when the archive last recorded a content change. |
| `source_last_modified`, `source_etag`, `transfer_fingerprint` | Saved values used to check for changes without downloading the full file. |
| `raw_commit` | For `sharded`: the raw-repo commit holding this snapshot's shards. |
| `cold_storage` | For `summarized`: archive.org item URL, snapshot and file sha256, compressed size, date. |
| `fetch_failures` | Present while the source is unreachable: first failure, last error, last attempt. |
| `summary_version` | See above. |
| `summary_warning` | Set when a new summary has fewer than half the rows or coded rows of its predecessor. Contains `at` and a `detail` such as `rows 120,000 -> 3,000`. The snapshot is saved, and the hospital is excluded from the price index until a later snapshot passes the check and clears the warning. |

## Attribution and license

Each hospital page includes schema.org Dataset metadata. The archive is
dedicated to the public domain under CC0. Its source files are the price
disclosures hospitals publish under 45 CFR § 180.50.
