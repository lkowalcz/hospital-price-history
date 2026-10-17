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

## Catalog and snapshot manifests

The static site publishes these resources, regenerated at every deployment:

| Resource (relative to the site root) | Contents |
|---|---|
| `data/catalog.json` | Full hospital roster, current metadata, issues, and latest snapshot descriptors. |
| `data/catalog.csv` | Flat hospital inventory with current summary/source links. Null values are empty cells; issue codes are separated with semicolons. |
| `data/snapshots/<slug>.json` | Recorded source and summary versions for a hospital, oldest first in mainline commit order. |
| `data/quality.json` | Coverage totals, issue definitions, and structural checks of current summaries. |
| `data/collection-health.json` | Explicit check observations, collection schedules, and overdue flags. Uses `evaluated_at` for its assessment time. |
| `data/source-audit.json` | Reviewed historical source gaps, repair evidence, and all remaining gaps at build time. |

Every JSON resource has `schema_version` (currently 1) and `catalog_revision`
(the full main-repository commit used for the build). Reports also include
`generated_at` (UTC build time), except collection health, which uses `evaluated_at`.
This catalog schema version is separate from
`summary_version`, which identifies price aggregation methodology.
Consumers should reject unsupported schema versions, tolerate additional
fields, and save the manifest alongside their analysis. Breaking field or
semantic changes require a new catalog schema version.

Catalog and manifest URLs are moving discovery resources. Price summaries,
metadata, and Git-hosted sources are linked at full revisions. Check that
catalog and manifest `catalog_revision` values agree; retry if a deployment
occurred between requests. Internet Archive items are identified by snapshot
hash and also expose the archived file's hash when recorded.

### Hospital records

`catalog.json` contains `hospitals`, `totals`, and `quality_url`. Each hospital
has these fields:

| Field | Meaning |
|---|---|
| `slug`, `name`, `system` | Roster identity. Use the slug for joins; display names can change. |
| `status` | Most recently recorded storage/listing status; null before metadata exists. |
| `first_seen`, `last_changed` | Metadata observation timestamps, or null. A URL change can update `last_changed` even when bytes match. |
| `last_successful_check_at` | Latest successful source verification time from an explicit observation, or null. Header and sample matches count; inspect the method. Never inferred from `last_changed`, a commit date, or build time. |
| `last_attempt_at`, `last_full_download_at` | Latest attempt start and latest validated full download time, or null. A failed ingest does not count as a validated download. |
| `collection` | Schedule, collector, latest attempt and result, last successful evidence, pinned log URLs, and overdue/interruption flags. See collection health below. |
| `snapshot_count` | Number of source/summary states in the manifest, including rebuilds and states without summaries. Not a count of price changes. |
| `snapshots_without_archived_source` | Recorded states without a matching source link, including older versions. Summary rebuilds can share a source, so this is not necessarily a count of distinct missing files. |
| `latest_snapshot` | Last descriptor in the manifest, or null if no snapshot exists. |
| `summary_rows` | Rows in the current summary, or null if absent. Inspect issue flags before treating the file as valid. |
| `metadata_url` | Current metadata pinned to `catalog_revision`, or null. |
| `snapshots_url`, `hospital_url` | Discovery links to the full manifest and archive page. |
| `issues` | List of recorded collection problems and structural validation findings. Empty does not prove source freshness or price accuracy. |

### Snapshot descriptors

Each manifest contains `slug` and `snapshots`, in addition to the common
envelope. A new descriptor is emitted when the source hash, summary blob,
or summary method changes. Validator refreshes and failure records do not
create snapshots. A return to an earlier content state remains a separate
snapshot. Summary removal creates a descriptor with a null `summary_url`.
Deleting metadata ends that recorded state; historical descriptors remain.
At merges, the state entering the first-parent/mainline history is recorded.

| Field | Meaning |
|---|---|
| `snapshot_id` | `<slug>:<full main-repo commit>`, identifying when this state entered the archive. |
| `revision`, `recorded_at` | Mainline commit and recording timestamp. These are not price effective dates. |
| `change_kind` | `source` for an initial state or changed source hash; `summary` for changed summary bytes/method; `summary_removed` when a summary disappears. |
| `source_observed_at` | `last_changed` from metadata at recording time; can predate a summary rebuild. |
| `source_sha256`, `source_size_bytes` | Snapshot identity and size from metadata. The hash covers normalized bytes for smaller files and unnormalized bytes for large files; it is not always the hash of an archived download. Null when unknown. |
| `summary_version` | Aggregation method, or null without a summary. Missing legacy metadata defaults to method 1. |
| `summary_git_blob` | Git object ID for the exact summary bytes, or null. This repository uses SHA-1 Git object IDs: hash `b"blob " + byte_length + b"\0" + file_bytes`. It is distinct from `source_sha256`. |
| `summary_url` | CSV download at the recording revision, or null. All price strings and code formatting are unchanged. |
| `metadata_url` | Metadata at the recording revision. |
| `summary_warning` | Warning recorded with that snapshot, or null. |
| `source` | Matching archived source descriptor, or null when no link is recorded. |
| `source_identity` | Optional audited identity for a retained state whose metadata checksum was deliberately cleared. Contains `sha256`, `basis`, and a pinned evidence `metadata_url`; the original null `source_sha256` remains unchanged. |
| `source_audit` | Optional reviewed status, repair method, explanation, and pinned `audit_url`. An unavailable original remains a null `source`. |
| `source_warning` | Optional historical source limitation with `code` and `detail`. `truncated_capture` means the retained capture is an incomplete prefix of the full file. |

A source descriptor has `kind`, `url`, and `metadata_url` identifying the
metadata that supports the link. Kinds are `normalized_file` (direct download),
`source_shards` (pinned Git directory), and `archived_original` (Internet
Archive item). Git sources include `revision`; archived originals include
`file_sha256` when recorded, covering the uncompressed original bytes.
Shard directories contain a header and content buckets, not one CSV download.

Originals sometimes reach cold storage after initial collection. The manifest
can enrich older descriptors using a source with the same hospital and source
hash; `source.metadata_url` pins that evidence separately. An Internet Archive
original takes preference over normalized files or shards. Corrected raw-repo
pins update source links without creating a new price or summary state. No live hospital
URL is substituted for an absent archived source. A source link's presence
does not establish that its remote host is currently reachable.

Some historical CSV hashes cover CRLF-to-LF normalization while their archived
originals retain CRLF. Audited links set `source.snapshot_hash_transform` to
`crlf_to_lf`, retain the original `file_sha256`, and provide the compressed
`download_url` and `compressed_sha256`. Decompress before applying the documented
transformation and comparing with the snapshot hash. The full audit records
both unmodified and normalized lengths and hashes.

A recovered truncated capture instead uses `snapshot_hash_transform: prefix`
with `snapshot_prefix_bytes`. Decompress the linked full original and retain
exactly that many raw bytes, without newline normalization, before checking
the snapshot hash. Its `source_warning` identifies the incomplete capture;
absent summary entries do not establish price removals.

### Historical source audit

`source-audit.json` includes the normal catalog envelope, `audited_at`, pinned
baseline main/raw revisions, searched refs, `records`, `totals`, and
`remaining_gaps`. The latter lists every currently unlinked archive state,
including future gaps outside the reviewed set. The catalog links this report
as `source_audit_url` and supplies `source_audit_totals`.

Records identify an exact hospital, main revision, metadata blob, and recorded
source hash. Methods are `metadata_hash_reset`, `relocated_shards`, `crlf_to_lf`,
`verified_prefix`, and `not_located`; statuses are `linked` or `unavailable`.
Evidence documents the matching prior metadata, Git content objects, complete
file or prefix hashes, or stores searched. Site generation rejects mismatched
identities and unexpected metadata changes rather than applying a repair to a
different source.

The audit restores links while retaining historical IDs, summaries, recorded
hashes, and metadata-transition classifications. Archive-state counts include
temporary checksum resets; they are not counts of unique source files or price
changes. See [the audit findings and repeatable verification](SOURCE_AUDIT.md).

### Quality report

`quality.json` includes `totals`, `issue_definitions` (code to explanation),
and `hospitals`. Hospital reports contain `slug`, `issues`, `summary` statistics,
`snapshots_without_archived_source`, the current `fetch_failures` record, and
`summary_warning`. `historical_source_gaps` identifies each unlinked historical
state and includes its audit explanation when reviewed.
`historical_source_warnings` lists known limitations with snapshot identity,
date, summary URL, warning code, and detail. The catalog includes the same
per-hospital list and a total count. These warnings describe historical states
separately from current collection issues.

Summary checks cover column names, row shape, duplicate exact
code/type/description keys, finite numeric prices, and nonnegative integer
payer counts. Statistics include row and coded-row counts, malformed rows,
duplicate keys, invalid numbers, and positive-value counts for each price
field. Zero, negative, and blank prices do not count as positive observations.
These checks do not establish clinical comparability, unit consistency,
correct source interpretation, or complete payer coverage.

Issue codes are `metadata_missing`, `fetch_failed`, `listing_missing`,
`summary_missing`, `summary_empty`, `summary_schema_invalid`,
`summary_malformed_rows`, `summary_duplicate_keys`, `summary_invalid_numbers`,
`summary_warning`, `legacy_summary_method`, `source_unavailable`, and
`archive_hash_mismatch`. The report supplies their human-readable definitions.
Failure records can be persisted only weekly, so their `last_attempt` field
is the latest *recorded* failure attempt, not necessarily the latest attempt.
The observation log records every attempt independently of that legacy field.

### Collection health

`collection-health.json` includes `schema_version: 1`, `catalog_revision`,
`evaluated_at`, `monitoring_since`, `policy`, `totals`, `runs`, and `hospitals`.
The catalog links it as `collection_health_url`; `collection_totals` counts
known checks, unknown checks, overdue attempts, and overdue verifications.
These are build-time assessments. The daily Collection freshness workflow
recomputes deadlines even when no snapshots or observations arrive.

Each hospital's `collection` includes:

- `scheduled_host`, human-readable `schedule`, `check_interval_hours`,
  `verification_interval_hours`, and `grace_hours`.
- `last_attempt_at`, `last_scheduled_attempt_at`, `last_successful_check_at`,
  and `last_full_download_at`. All remain null until observed. A manual check
  can establish source reachability but cannot reset the scheduled collector's clock.
- `latest_attempt` and `last_successful_check`: attempt/run identity, host,
  start time, `result`, and an `observation_url` pinned to the catalog revision.
  A started attempt without a finished event has a null result.
- `state`, `check_overdue`, `verification_overdue`, and `issues`.
  Unknown checks during the initial grace period are still unknown, not healthy.

Results distinguish `changed`, `unchanged`, `failed`, `listing_missing`, and
`deferred`. Successful methods are `full_download`, `headers`, or
`sampled_fingerprint`. Header evidence includes Last-Modified/ETag; samples
include the fingerprint. These cheap checks cannot prove every byte unchanged.
`verified_at` records when the evidence was obtained; full downloads also have
`downloaded_at`, before parsing or upload finishes. Failed or deferred results
have neither timestamp. Discovery status, transport retries, errors, source
hash, and archive status are separate fields. `archive_status: available`
means a matching original archive is recorded, not that it was downloaded
again for verification. `not_recorded` can include sources retained in Git.

Daily checks have a 24-hour interval and 48-hour grace. Large files without
reliable cheap verification use the scraper's Sunday full-download fallback:
168 hours plus 48 hours of grace. Daily discovery-only deferrals do not reset
that verification clock. Run gaps are flagged after 7 hours on GitHub Actions
or 30 hours on the Pi, or immediately following explicit interruption.
Later completed attempts by the responsible collector resolve those gaps.
Historical interrupted runs remain in the report for provenance.

Additional issue codes are `collection_check_overdue`,
`collection_verification_overdue`, `collection_interrupted`,
`collection_failed`, `observed_source_not_current`, and `archive_upload_failed`.
The latter two distinguish an observed source not yet in the committed archive
and a failed archive upload from a failed source check.

Raw evidence lives under `observations/YYYY-MM-DD/<host>-<run-id>.jsonl`.
Events are `run_started`, `attempt_started`, `attempt_finished`, and
`run_finished`, each with schema version, run/host identity, and UTC time.
Run starts include planned slugs and reasons for excluded slugs. Exclusions
are not attempts. Each host owns separate append-only logs; aggregation uses
attempt/evidence times, never commit order. Interrupted logs can have missing
outcomes; no successful timestamp is invented to fill those gaps. Observations
do not create source/summary snapshots.

Totals count hospitals, recorded states, hospitals with a current summary,
with a source link for their latest snapshot, historical states without a
source link, hospitals with issues, and occurrences of each issue code.
A hospital can have several issues. Historical gaps are counted separately
from current issue flags so recovered hospitals retain an accurate history.

### Reproducing a download

The standard-library [example](../examples/download_summary.py) discovers a
hospital through the catalog, selects a summary, verifies its Git blob ID,
and saves the file plus a provenance sidecar. It refuses to overwrite files
or save a download whose hash differs. It preserves leading zeros and price
precision because it does not parse or rewrite the CSV. When loading it in
pandas or a spreadsheet, explicitly read billing codes as strings.

For analyses over time, match code type, code, and description deliberately;
renames are not automatically equivalent services. One service can appear
under multiple codes. Compare like summary methods, and use only finite
positive prices as percentage-change denominators. Review extreme differences
against original files. Payer names, plans, settings, modifiers, and units are
not separate dimensions in summaries; original files are needed for that context.

## Attribution and license

Each hospital page includes schema.org Dataset metadata. The archive is
dedicated to the public domain under CC0. Its source files are the price
disclosures hospitals publish under 45 CFR § 180.50.
