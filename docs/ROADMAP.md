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

**Next: pilot richer derived exports on fixtures**, as described below.

## 3. Preserve analytical context in derived exports

Pilot a separate versioned export on existing tall CSV, wide CSV, and CMS
JSON fixtures before applying it to the full archive. Keep current summaries
available during the pilot.

- Preserve payer, plan, service setting, billing-code relationships, modifiers,
  units, rate type, and the unmodified description when present.
- Keep dollar rates, percentages, formulas, and absent values distinct.
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

## 4. Validate reuse before expanding coverage

Use a concrete downstream analysis or integration to test the documentation
and export design. Ask participating users to identify the missing fields,
access patterns, and provenance that prevent independent use.

Measure successful reproducible downloads, resolved collection gaps,
source/archive coverage, and independent reuse. Add hospitals or new product
features when that evidence justifies their collection and maintenance cost.

Completion criterion: a downstream user reproduces a historical observation
and can explain its limitations using the published documentation and data.
