# Historical source audit

The October 17, 2026 audit resolved **all 38 source-link gaps** in the
catalog at main revision `64704381037323a9ec23cbe17322d0ef4dae997e`.
One recovered source is a truncated capture, explicitly flagged as incomplete.
All 418 existing archive-state IDs and their summary downloads are retained;
the audit does not manufacture historical checks or new source captures.

| Finding | States | Resolution |
|---|---:|---|
| Source checksum cleared to force a refresh | 32 | Link the retained source using the immediately preceding metadata identity, with explicit evidence. |
| Pre-split DMC Harper shards omitted from catalog links | 2 | Link the matching published raw-repository revisions. |
| Early CSV hashes normalized CRLF to LF | 3 | Link archived originals whose fully decompressed bytes reproduce the old hashes under that exact transformation. |
| Hackensack's first capture was truncated | 1 | Reproduce its exact bytes from a verified prefix of the archived full file; flag the incomplete capture. |

The reviewed records and exact checksums are in [source_audit.json](../source_audit.json).
The [published audit](https://lukekowalczyk.com/hospital-price-history/data/source-audit.json)
also lists all remaining catalog gaps at build time, including future gaps
that have not yet been reviewed. The audit is scoped to the 38 baseline gaps;
it is not a claim that every archived source was downloaded again.

## Metadata resets

Four maintenance commits on August 26 cleared change-detection fields to
force refetches or summary rebuilds. The catalog represented these intermediate
states with a null `source_sha256` and consequently lost the source link.

For each affected state, the audit checks the exact metadata blob and its
immediate parent's blob. The recorded URL, size, source filename, timestamps,
and other source details must remain unchanged. Only the reviewed checksum,
validator, fingerprint, and repository-pointer fields may differ. The
retained identity must also have an independently recorded source link.

The manifest preserves its original null `source_sha256` and adds
`source_identity` with the recovered hash and a pinned link to the prior
metadata. `source_audit.method: metadata_hash_reset` identifies the operational
reset. It does not assert a new file or price change. Existing `change_kind`
values retain their original classification from recorded metadata transitions.

## Relocated DMC Harper files

The August 14 and August 22 sources survived the repository split and history
rewrite. For each, the retained pre-rewrite main metadata blob is identical
to the published main metadata blob. The complete shard-tree object ID and
header-blob object ID also match the corresponding published raw revision:

| Main snapshot | Published raw revision |
|---|---|
| `c7f341aaba…` (August 14) | `0563fe573fce3b267cb891b57397514c5f6482e2` |
| `e0f2fb334c…` (August 22) | `4ac91e80175f40ee3f71ac0658c21ff3f9d9d791` |

These are exact Git content-object comparisons. No current source was
substituted for the historical shards. Both revisions are ancestors of the
published raw main branch. The other 86 raw-repository source links in the
baseline catalog also reference commits reachable from that published branch.

## Newline normalization

The first scraper normalized CSV bytes with `payload.replace(b"\r\n", b"\n")`.
Later giant-file captures hashed their unmodified bytes. Three apparent
missing files therefore had different hashes from their archived originals,
despite matching exactly after the original transformation.

The audit downloaded approximately 200 MB of compressed originals and streamed
their decompression, checking the archive's compressed SHA-1, recording a
compressed SHA-256, and verifying both the unmodified and normalized SHA-256
and byte counts. The uncompressed files total approximately 6.65 GB.

| Hospital | Historical normalized SHA-256 prefix | Archived original SHA-256 prefix |
|---|---|---|
| Cleveland Clinic | `dabba0312d52` | `9f966fd58f5c` |
| Penn HUP | `25eb29300f50` | `84201ae3ef0e` |
| Atrium Carolinas | `1c45d66064e0` | `9d77955855b5` |

Full hashes, lengths, compressed filenames, direct downloads, and supporting
metadata revisions are recorded in the audit. The manifest keeps both hashes
distinct and sets `source.snapshot_hash_transform: crlf_to_lf`. Downloading
the original yields its original bytes, not a silently modified file.

## Truncated capture: Hackensack University Medical Center

The August 25 state at `a359d32e52…` records a 1,082,699,953-byte file with
SHA-256 `53ba10171f58a97857d93c26fd79dc84aa8ef63a86d2c28f7b40a86d8575ce7e`.
Its summary and metadata remain downloadable. The first 1,082,699,953 raw
bytes of the later archived `94eadf5d55bc…` original match that SHA-256 exactly.
The full original is 2,295,205,053 bytes. The audit verified the entire
compressed and decompressed file as well as the exact historical prefix.

To reproduce the old capture, decompress the linked original, retain exactly
the first 1,082,699,953 bytes without newline normalization, and check the
historical hash. The source descriptor records `snapshot_hash_transform: prefix`
and `snapshot_prefix_bytes`; the audit records the full-file and prefix hashes
and lengths separately. The linked download is the full archived original.

This recovery also establishes a limitation: the old capture was incomplete.
Its manifest carries `source_warning.code: truncated_capture`, and the quality
report and hospital page surface the warning. Its summary cannot establish
that absent entries were removed from the hospital's published prices. This
historical warning does not describe the current full-file capture.

## Repeat the checks

Use full-history clones of both repositories, and fetch their current refs:

```sh
python3 source_audit.py --raw-repo ../hospital-price-history-raw
python3 source_audit.py --raw-repo ../hospital-price-history-raw --verify-downloads
```

The first command validates committed metadata evidence and the raw Git
objects. It also compares the original pre-rewrite objects when that retained
local history is available. Fresh clones can verify the published raw object
IDs against the committed audit evidence.

The second command repeats the four complete archive checks and requires
`zstd`. Downloads use a temporary directory; decompressed files are streamed
with bounded memory and are not written to disk. Add `--cache /path/to/cache`
to retain and reuse the approximately 261 MB of compressed downloads (about
9 GB decompressed). Cached bytes are still checked before use. Ordinary site builds only read the
committed audit and do not repeat those downloads.

Future repairs should add reviewed records with exact snapshot identity and
verification evidence. Keep an unresolved source null until matching content
is located. Do not substitute a later source, reuse another hospital's hash,
or infer identity solely from a similar summary, filename, or timestamp.
