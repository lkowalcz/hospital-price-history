# Data roadmap

The project maintains a historical archive that other people can find,
reproduce, and analyze independently. Website work supports discovery,
documentation, and downloads. Consumer price-comparison interfaces and
expansion of the experimental index are outside the current scope.

## 1. Make the existing archive usable — implemented

- Publish a versioned JSON catalog and CSV inventory for the full roster.
- List historical source and summary states with pinned downloads, method
  versions, warnings, and source provenance.
- Publish current coverage and structural quality checks, including failures,
  missing summaries, duplicate entries, and absent source links.
- Provide a dependency-free example that verifies a historical summary and
  saves provenance alongside it.
- Retire the Rush comparison interface while retaining its archive page and
  redirecting old comparison links.

Initial inventory from data at repository revision `8e6f203`: 107 hospitals
and 418 recorded source/summary states.
All 107 have a current summary and a matching archived-source link; one has
an unresolved recorded fetch failure. These are build-time counts, not a
claim that every source was freshly downloaded or remotely verified.
There were 38 historical states without a matching archived-source link;
these can include multiple summary rebuilds for the same underlying file.
The [live quality report](https://lukekowalczyk.com/hospital-price-history/data/quality.json)
is the current source for these measurements.

Completion criterion: a new user can discover a hospital, download an exact
historical summary without cloning the repositories, verify its bytes, and
identify the applicable method and warnings. Automated tests exercise history
reversions, merges, metadata-only updates, late archiving, missing summaries,
quality flags, and rejected corrupt downloads.

## 2. Measure collection reliability — logging and source audit implemented

The collectors now record compact observations for every attempted hospital
check, including successful unchanged checks. Historical checks before logging
began remain unknown. The catalog exposes evidence, schedules, and overdue
checks; a daily workflow monitors deadlines independently of site builds.

- Distinguish discovery success, validator or sampled-fingerprint matches,
  full downloads, deliberately deferred checks, and failures.
- Record attempt time, last successful check, last full download, collection
  host, and the evidence used to decide a source is unchanged.
- Merge CI and Pi observations without allowing one host to overwrite newer
  evidence from the other. An interrupted run must leave an explicit gap.
- Extend existing workflow and Pi-heartbeat monitoring with per-hospital
  coverage gaps, using each hospital's actual check schedule.
- Surface archive-upload retries and source-link failures separately from
  price-file fetch failures.

Completion criterion: every attempted check produces a durable outcome, an
unchanged reachable source stays distinguishable from an unchecked source,
and fixtures verify skipped checks, interrupted runs, and overlapping hosts.
Do not backfill successful-check timestamps from historical commit dates.

The [historical source audit](SOURCE_AUDIT.md) reviewed the 38 baseline gaps:
32 were temporary checksum resets, two were relocated DMC Harper shard
snapshots, and three were archived originals whose hashes match after the
original newline normalization. Hackensack's first capture is recoverable as
an exactly verified byte prefix of its archived full file. All 38 links are
restored with evidence; the truncated capture has an explicit warning against
interpreting absent entries as price removals. All 418 existing state IDs
remain stable. These include operational metadata states, not only distinct files.

Operational follow-through: observe a complete scheduled cycle on both
collectors to verify the logging rollout covers all 107 hospitals. The initial
live probes covered GitHub Actions and the Pi; earlier checks remain unknown.

**Next: observe complete scheduled cycles on both collectors, and review the
detailed-export pilot with a downstream user.** Preserve evidence for future
questions before expanding coverage or adding interfaces.

## 3. Preserve exact originals — deployed; scheduled-cycle checks pending

The November 2026 baseline has 28 current snapshots with archived-original
links, 21 stored as normalized files, and 58 stored as sorted shards. The
normalized files and shards retain useful data but do not necessarily retain
exact downloaded bytes or original record order.

- Preserve each distinct completed download before ZIP/gzip extraction,
  normalization, or parsing, including unsupported price-file formats.
- Identify downloaded bytes, extracted payloads, and normalized snapshots
  with separate hashes. Keep formatting changes distinct from price changes.
- Retain pending originals until upload succeeds; retry those bytes even
  after the hospital replaces its live file.
- Publish receipts independently of successful parsing. An archived file
  is not necessarily valid price data. Report pending uploads separately.

Implemented by `preserve_source.py`, the scraper, and local ingestion.
Scheduled collectors enable `PRESERVE_ORIGINALS=1`; manual collectors can
opt in. The Pi retains a persistent spool. GitHub Actions restores and
checkpoints its queue in a 90-day recovery artifact and stops collection
when an upload remains pending. The artifact is temporary recovery storage.
Hard runner termination before checkpointing remains a limitation; a missing
checkpoint must be investigated rather than reset silently.

Deployed to GitHub Actions and the Pi on 2026-11-02 at code revision
`52f38d77cb90ecb64c4dfbe2dd5fd2d426bd0d46`. All 185 tests ran successfully on
both hosts (four optional browser tests skipped). A
[scoped GitHub collection](https://github.com/lkowalcz/hospital-price-history/actions/runs/41409296554)
captured McLean's exact download; a manual Pi check captured Hopkins Bayview's.
Both public Internet Archive downloads were independently retrieved and their
compressed/original SHA-256 hashes verified. A
[second GitHub run](https://github.com/lkowalcz/hospital-price-history/actions/runs/41409730731)
restored the prior recovery checkpoint and verified the unchanged source by
headers. Both queues were empty of pending uploads. These were bounded rollout
checks, not evidence that a complete scheduled cycle or live upload outage
has been exercised.

Completion criterion: demonstrate a scheduled cycle on both hosts and an
upload outage/recovery with correct public receipts. Independently verify
archived bytes after upload. Add a bounded policy for periodic full downloads
despite matching headers/samples. Capture is prospective: re-fetching today's
file does not recover an old original. Plan backfills separately with explicit
storage and bandwidth limits.

## 4. Preserve analytical context in derived exports — pilot implemented

`export_records.py` produces a versioned, partitioned JSONL export from CMS
tall CSV, wide CSV, and JSON. It retains source records and file metadata
beside convenient fields, without aggregation, code selection, price filtering,
or inferred payer identities. Existing summaries remain available. The schema
is a pilot and is not generated automatically for the full archive.

Initial verification at source-data revision
`c9258d9a468fbf4f50bc1a66891b256f6eb874ae`: all 81,889 source records were
reproduced from McLean (489), Johns Hopkins (59,395), and OhioHealth Riverside
(22,005). CSV cells/JSON objects and file metadata matched the supplied
normalized sources; partition checksums were verified. Tall/wide CSV and JSON
fixtures additionally cover missing values, multiline records, linked codes,
unknown fields, exact decimals, and unsupported/ambiguous input. This verifies
extraction fidelity, not clinical comparability or exact-original coverage.

- Preserve payer, plan, service setting, billing-code relationships, modifiers,
  units, rate type, and the unmodified description when present.
- Keep dollar rates, percentages, formulas, and absent values distinct.
- Keep hospital-reported median/10th/90th-percentile allowed amounts, their
  supporting counts (including `1 through 10`), and legacy estimated amounts
  separate from negotiated charges and statistics calculated by this project.
- Retain all source cells/parsed JSON properties, including unknown fields,
  notes, unusual values, duplicate rows, and uncoded services. Reject ambiguous
  duplicate JSON properties explicitly; keep the original for another parser.
- Include source snapshot identity, parser version, source record locator,
  and explicit missingness. Avoid inferring dimensions the source omits.
- Publish small partitioned downloads with checksums. Choose CSV/JSONL/Parquet
  based on actual source shapes and consumer queries; avoid adding several
  equivalent formats without a demonstrated use.
- Add round-trip and source-to-export fixtures, then review real examples
  across several hospitals before declaring the schema stable.

Completion criterion: a consumer can trace an exported rate to its original
record and distinguish settings, units, and payer context without consulting
undocumented parser behavior. New formats must have bounded memory/disk use
on both the CI runner and collection host.

## 5. Preserve discovery and interpretive evidence — initial capture implemented

- Save exact distinct `cms-hpt.txt` responses and pin their receipts/downloads
  in the catalog, including listings from which a hospital disappeared.
- Retain hospital identifiers, locations, schema versions, attestations,
  file-level contract provisions, and financial-aid notes as published.
  The export pilot preserves complete supplied file metadata.
- Keep collection time, the hospital's stated update date, and explicitly
  supplied effective dates separate; do not infer one from another.
- Extend provenance to final redirect URLs and references to external
  documents needed to interpret a file. Evaluate retention of those documents
  on real examples before expanding collection.
- Review identity checks against source identifiers/addresses before adding
  automated discovery or expanding the roster.

Completion criterion: a researcher can reconstruct how a source was found,
which hospitals it identifies, what its dates mean, and the source-specific
conditions attached to its rates.

## 6. Validate reuse before expanding coverage

Use a concrete downstream analysis or integration to test the documentation
and export design. Ask participating users to identify the missing fields,
access patterns, and provenance that prevent independent use.

Review task: follow one payer/plan's published rate for the same service and
setting across two snapshots, reproduce the source values, and explain every
ambiguity. Seek feedback from a domain expert or downstream project such as
The Healthcare Files before declaring the export schema stable.

Measure successful reproducible downloads, resolved collection gaps,
source/archive coverage, and independent reuse. Add hospitals or new product
features when that evidence justifies their collection and maintenance cost.

Completion criterion: a downstream user reproduces a historical observation
and can explain its limitations using the published documentation and data.
