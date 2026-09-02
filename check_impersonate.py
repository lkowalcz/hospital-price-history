#!/usr/bin/env python3
"""Probe every "fetch": "impersonate" hospital through a curl-impersonate
wrapper: check_impersonate.py [path/to/curl_chromeNNN]. Defaults to
$CURL_IMPERSONATE_BIN.

For refreshing curl-impersonate (docs/OPERATIONS.md): run it once with the
wrapper in use and once with the candidate. Every hospital that passes
with the old one must pass with the new one before the workflow,
local_refetch.py, and the hosts' .tools/ move over. Two light requests per
hospital: the cms-hpt.txt discovery file and a HEAD on the MRF.

Exit status is the number of failures.
"""

import json
import os
import sys
from pathlib import Path

if len(sys.argv) > 1:
    os.environ["CURL_IMPERSONATE_BIN"] = sys.argv[1]

import scrape  # noqa: E402  (reads CURL_IMPERSONATE_BIN at import)


def main():
    if not scrape.IMPERSONATE_BIN or not Path(scrape.IMPERSONATE_BIN).exists():
        sys.exit(f"no wrapper at {scrape.IMPERSONATE_BIN!r}; pass a path or set CURL_IMPERSONATE_BIN")
    print(f"wrapper: {scrape.IMPERSONATE_BIN}")
    hospitals = [h for h in json.loads((scrape.ROOT / "hospitals.json").read_text())
                 if scrape.wants_impersonate(h)]
    failures = 0
    for h in hospitals:
        try:
            url = scrape.discover_mrf_url(h, True)
            if not url:
                print(f"  {h['slug']}: cms-hpt.txt reachable, location not listed")
                continue
            size = scrape.head(url, impersonate=True, max_time=60).get("Content-Length") or "?"
            print(f"  {h['slug']}: ok (Content-Length {size})")
        except Exception as exc:
            failures += 1
            print(f"  {h['slug']}: FAIL {str(exc)[:100]}")
    print(f"{len(hospitals) - failures}/{len(hospitals)} ok")
    sys.exit(failures)


if __name__ == "__main__":
    main()
