#!/usr/bin/env python3
"""Generate the static GitHub Pages site: gen_site.py <output_dir>.

One page per hospital (price digest, change history from git, dataset
markup for Google Dataset Search) plus a homepage listing them. Deployed by the Pages
workflow; never committed to the repo.
"""

import csv
import html
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from data_catalog import ISSUES, build_catalog

ROOT = Path(__file__).resolve().parent
BASE = "https://lukekowalczyk.com/hospital-price-history"
REPO = "https://github.com/lkowalcz/hospital-price-history"
# Payer-level sharded content lives in a companion repo (see README).
RAW_REPO = "https://github.com/lkowalcz/hospital-price-history-raw"

# Well-known, comparable procedures shown on each hospital page when present.
FEATURED_DRGS = {
    "470": "Major joint replacement",
    "291": "Heart failure w/ MCC",
    "807": "Vaginal delivery",
    "788": "Cesarean section",
    "247": "Cardiac stent w/ MCC",
    "003": "ECMO/tracheostomy",
}

CSS = """
:root{--fg:#1a1a2e;--muted:#666;--line:#e0e0e8;--accent:#0b5fa5;--bg:#fff;--soft:#f6f7fa}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 -apple-system,system-ui,Segoe UI,sans-serif;color:var(--fg);background:var(--bg)}
main{max-width:60rem;margin:0 auto;padding:1.5rem}
h1{font-size:1.6rem;margin:.2rem 0}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid var(--line);padding-bottom:.3rem}
a{color:var(--accent)}.muted{color:var(--muted);font-size:.9rem}
table{border-collapse:collapse;width:100%;font-size:.9rem;margin:.8rem 0}
th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid var(--line)}
th{background:var(--soft)}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.warn{background:#fff4e5;border:1px solid #f0c988;border-radius:6px;padding:.7rem 1rem;margin:1rem 0}
.badge{display:inline-block;background:var(--soft);border:1px solid var(--line);border-radius:4px;padding:0 .45rem;font-size:.8rem}
footer{margin:3rem 0 1rem;font-size:.85rem;color:var(--muted);border-top:1px solid var(--line);padding-top:1rem}
.wrap{overflow-x:auto}code{overflow-wrap:anywhere}
"""


def money(v):
    if v in (None, "", "None"):
        return "—"
    return f"${float(v):,.0f}"


def esc(s):
    return html.escape(str(s or ""))


def page(title, description, body, canonical, jsonld):
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical}">
<script type="application/ld+json">{json.dumps(jsonld).replace('<', chr(92) + 'u003c')}</script>
<style>{CSS}</style></head><body><main>
{body}
<footer>Source: hospital price files published under 45 CFR § 180.50.
<a href="{REPO}">About the archive and its data</a>.
Page generated {date.today().isoformat()}.</footer>
</main></body></html>"""


def git_history(slug):
    out = subprocess.run(
        ["git", "log", "--date=short", "--format=%ad\t%h\t%s", "--", f"data/{slug}/"],
        capture_output=True, text=True, cwd=ROOT).stdout
    events = []
    for line in out.splitlines():
        d, h, s = line.split("\t", 2)
        s = s.split("Co-Authored-By")[0].strip()
        events.append((d, h, s[:110]))
    return events


def load_summary(slug):
    p = ROOT / "data" / slug / "summary.csv"
    if not p.exists():
        return None
    with open(p) as f:
        return list(csv.DictReader(f))


def is_drg(code_type):
    # Same labels compute_index.py accepts: "MS-DRG", "MSDRG", "DRG", ...
    return re.sub(r"[^A-Z]", "", code_type.upper()) in {"MSDRG", "DRG"}


def featured_rows(rows):
    out = []
    wanted = {k.lstrip("0") for k in FEATURED_DRGS}
    for r in rows or []:
        if is_drg(r["code_type"]) and r["code"].lstrip("0") in wanted:
            out.append(r)
    if not out and rows:
        out = sorted(rows, key=lambda r: -int(r["payer_entries"] or 0))[:6]
    return out[:8]


def hospital_page(h, meta, outdir, catalog_entry):
    slug = h["slug"]
    name = h["location_name"] if len(h["location_name"]) < 60 else slug.replace("-", " ").title()
    rows = load_summary(slug)
    events = git_history(slug)
    canonical = f"{BASE}/hospitals/{slug}/"

    parts = [f'<p class="muted"><a href="{BASE}/">&larr; All hospitals</a></p>',
             f"<h1>{esc(name)}</h1>",
             f'<p class="muted">{esc(h["system"])} &middot; '
             f'<span class="badge">{esc(meta.get("status", "no data"))}</span></p>']

    snapshot = catalog_entry["latest_snapshot"] or {}
    parts.append('<h2>Get the data</h2><ul>')
    if snapshot.get("summary_url"):
        parts.append(f'<li><a href="{esc(snapshot["summary_url"])}">Download latest summary CSV</a> '
                     f'(method {esc(snapshot["summary_version"])})</li>')
    parts.append(f'<li><a href="{esc(catalog_entry["snapshots_url"])}">Snapshot manifest (JSON)</a> '
                 f'— {catalog_entry["snapshot_count"]} recorded archive states</li>')
    if catalog_entry["metadata_url"]:
        parts.append(f'<li><a href="{esc(catalog_entry["metadata_url"])}">Collection metadata (JSON)</a></li>')
    if snapshot.get("source"):
        parts.append(f'<li><a href="{esc(snapshot["source"]["url"])}">Archived source for the latest snapshot</a></li>')
    parts.append(f'</ul><p class="muted">Downloads link to recorded versions. '
                 f'<a href="{BASE}/data/">Data catalog, coverage report, and usage guide</a>.</p>')
    if catalog_entry["snapshots_without_archived_source"]:
        parts.append(f'<p class="warn">{catalog_entry["snapshots_without_archived_source"]} historical archive '
                     f'state(s) have no located source file. '
                     f'<a href="{BASE}/data/source-audit.json">Read the source-gap audit</a>.</p>')
    parts.append("<h2>Tracking status</h2><table>")
    status_rows = [
        ("Monitored since", (meta.get("first_seen") or "")[:10]),
        ("Last change recorded", (meta.get("last_changed") or "")[:10]),
        ("File size", f'{meta.get("size_bytes", 0):,} bytes' if meta.get("size_bytes") else "—"),
        ("Rows in summary", f"{len(rows):,}" if rows else "—"),
        ("Source file", f'<a href="{esc(meta.get("mrf_url", ""))}" rel="nofollow">machine-readable file</a>'
         if meta.get("mrf_url") else "—")]
    collection = catalog_entry["collection"]
    verification = (collection["last_successful_check"] or {}).get("result") or {}
    status_rows.extend([
        ("Last attempted check", esc(collection["last_attempt_at"] or "Not yet recorded")),
        ("Last successful source check", esc(collection["last_successful_check_at"] or "Not yet recorded")),
        ("Verification method", esc(verification.get("method", "—").replace("_", " "))),
        ("Last validated full download", esc(collection["last_full_download_at"] or "Not yet recorded")),
        ("Collection schedule", esc(collection["schedule"]))])
    if meta.get("status") == "sharded":
        ref = meta.get("raw_commit") or "main"
        status_rows.append((
            "Payer-level rates",
            f'<a href="{RAW_REPO}/tree/{esc(ref)}/data/{slug}">full data</a> &middot; '
            f'<a href="{RAW_REPO}/commits/main/data/{slug}">change history</a>'))
    cs = meta.get("cold_storage")
    if cs and cs.get("sha256") == meta.get("sha256"):
        status_rows.append((
            "Original file",
            f'<a href="{esc(cs["url"])}">copy on the Internet Archive</a> '
            f'(zstd, {cs.get("compressed_bytes", 0):,} bytes)'))
    for label, val in status_rows:
        parts.append(f"<tr><th>{label}</th><td>{val}</td></tr>")
    parts.append("</table>")
    parts.append('<p class="muted">Header and sampled-fingerprint checks provide limited evidence '
                 'of an unchanged source; they do not verify every byte. Dates before collection '
                 'logging began remain unknown.</p>')
    if collection["latest_attempt"]:
        parts.append(f'<p>Latest check: {esc(collection["state"].replace("_", " "))}. '
                     f'<a href="{esc(collection["latest_attempt"]["observation_url"])}">Collection log</a>.</p>')
    for issue in catalog_entry["issues"]:
        if issue.startswith("collection_") or issue in {"observed_source_not_current", "archive_upload_failed"}:
            parts.append(f'<p class="warn">{esc(ISSUES[issue])}</p>')

    sw = meta.get("summary_warning")
    if sw:
        parts.append(f'<div class="warn">⚠ The summary recorded on '
                     f'{esc((meta.get("last_changed") or sw["at"])[:10])} has fewer than half '
                     f'the rows or coded rows of the previous summary ({esc(sw["detail"])}). '
                     f'The source file may be incomplete or its layout may have changed. '
                     f'You can find the previous snapshot in the change history below.</div>')

    ff = meta.get("fetch_failures")
    if ff:
        parts.append(f'<div class="warn">⚠ The archive has been unable to download '
                     f'this hospital&rsquo;s price file since {esc(ff["first_failed"][:10])}. '
                     f'Latest error: <code>{esc(ff["last_error"][:90])}</code>.</div>')

    if rows:
        feat = featured_rows(rows)
        if feat:
            parts.append('<h2>Example prices</h2><div class="wrap" tabindex="0" role="region" aria-label="Example price summary"><table>'
                         "<tr><th>Code</th><th>Description</th><th class=num>Gross charge</th>"
                         "<th class=num>Cash price</th><th class=num>Negotiated (min–max)</th>"
                         "<th class=num>Payer entries</th></tr>")
            for r in feat:
                neg = "—" if not r["min_negotiated"] else \
                    f'{money(r["min_negotiated"])}–{money(r["max_negotiated"])}'
                parts.append(f'<tr><td>{esc(r["code_type"])} {esc(r["code"])}</td>'
                             f'<td>{esc(r["description"][:70])}</td>'
                             f'<td class=num>{money(r["gross_charge"])}</td>'
                             f'<td class=num>{money(r["discounted_cash"])}</td>'
                             f'<td class=num>{neg}</td>'
                             f'<td class=num>{r["payer_entries"]}</td></tr>')
            parts.append("</table></div>"
                         f'<p class="muted">Download the full summary ({len(rows):,} rows): '
                         f'<a href="{esc(snapshot.get("summary_url", ""))}">summary.csv</a></p>')

    parts.append("<h2>Change history</h2>")
    if events:
        parts.append("<table><tr><th>Date</th><th>Event</th></tr>")
        for d, commit_hash, s in events[:30]:
            parts.append(f'<tr><td>{d}</td><td><a href="{REPO}/commit/{commit_hash}">{esc(s)}</a></td></tr>')
        parts.append("</table>")
    else:
        parts.append('<p class="muted">No recorded events yet.</p>')

    desc = (f"Archived price files for {name} ({h['system']}), with summaries of gross "
            f"charges, cash prices and negotiated rates, and a history of recorded changes.")
    jsonld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": f"{name} — hospital price history",
        "description": desc, "url": canonical,
        "creator": {"@type": "Person", "name": "Lucas Kowalczyk"},
        "isBasedOn": meta.get("mrf_url"),
        "dateModified": (meta.get("last_changed") or "")[:10],
        "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv",
                          "contentUrl": snapshot["summary_url"]}] if snapshot.get("summary_url") else [],
        "keywords": ["hospital prices", "price transparency", h["system"], name],
    }
    d = outdir / "hospitals" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(
        page(f"{name} price history", desc, "\n".join(parts), canonical, jsonld))
    return canonical


def index_page(hospitals, metas, outdir):
    body = ["<h1>Hospital Price History</h1>",
            '<p>This archive collects the machine-readable price files US hospitals '
            'publish under federal price transparency rules (45 CFR § 180.50). '
            'Scheduled checks save new versions as hospitals update their files. '
            'Choose a hospital below to download its data '
            'or browse its recorded changes.</p>'
            f'<p>The <a href="{REPO}">GitHub repository</a> contains the archive data '
            'and documentation. Larger files stored in shards, including their '
            f'payer-level rates, are in a <a href="{RAW_REPO}">separate repository</a>.</p>',
            f'<p><a href="{BASE}/data/"><strong>Use the data →</strong></a> '
            'Browse the machine-readable catalog, historical snapshot manifests, '
            'and coverage report for the full roster.</p>',
            '<div class="wrap" tabindex="0" role="region" aria-label="Hospital inventory"><table><tr><th>Hospital</th><th>System</th>'
            "<th>Last change recorded</th><th>Status</th></tr>"]
    for h in sorted(hospitals, key=lambda x: x["location_name"].casefold()):
        m = metas[h["slug"]]
        name = h["location_name"] if len(h["location_name"]) < 60 else h["slug"].replace("-", " ").title()
        status = "⚠ unreachable" if m.get("fetch_failures") else m.get("status", "—")
        body.append(f'<tr><td><a href="{BASE}/hospitals/{h["slug"]}/">{esc(name)}</a></td>'
                    f'<td>{esc(h["system"])}</td>'
                    f'<td>{(m.get("last_changed") or "—")[:10]}</td>'
                    f'<td>{esc(status)}</td></tr>')
    body.append("</table></div>")
    desc = (f"Archived price transparency files from {len(hospitals)} US hospitals, "
            "with price summaries, downloadable data and a history of recorded changes.")
    jsonld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "Hospital Price History",
        "description": desc, "url": f"{BASE}/",
        "creator": {"@type": "Person", "name": "Lucas Kowalczyk"},
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
        "distribution": [{"@type": "DataDownload", "encodingFormat": "application/zip",
                          "contentUrl": f"{REPO}/archive/refs/heads/main.zip"},
                         {"@type": "DataDownload", "encodingFormat": "application/zip",
                          "contentUrl": f"{RAW_REPO}/archive/refs/heads/main.zip"}],
        "keywords": ["hospital prices", "price transparency", "healthcare costs",
                     "negotiated rates", "machine-readable files"],
    }
    (outdir / "index.html").write_text(
        page("Hospital Price History", desc, "\n".join(body), f"{BASE}/", jsonld))


def data_page(catalog, outdir):
    totals = catalog["totals"]
    canonical = f"{BASE}/data/"
    body = [f'<p><a href="{BASE}/">← Hospital archive</a></p>',
            '<h1>Use the data</h1>',
            '<p>A historical archive for researchers and developers. Download recorded '
            'summaries, locate archived sources, and inspect collection limitations.</p>',
            '<h2>Downloads</h2><ul>',
            '<li><a href="catalog.json">Hospital catalog (JSON)</a> — current coverage and links for every hospital</li>',
            '<li><a href="catalog.csv">Hospital catalog (CSV)</a> — the same inventory as a flat table</li>',
            '<li><a href="quality.json">Quality report (JSON)</a> — recorded problems and summary checks</li>',
            '<li><a href="collection-health.json">Collection health (JSON)</a> — check outcomes, evidence, schedules, and gaps</li>',
            '<li><a href="source-audit.json">Historical source audit (JSON)</a> — repaired links, verification evidence, and remaining gaps</li>',
            f'<li><a href="{REPO}/blob/main/docs/DATA.md#catalog-and-snapshot-manifests">Schema and interpretation guide</a></li>',
            f'<li><a href="{REPO}/blob/main/examples/download_summary.py">Python example: download and verify a historical summary</a></li>',
            f'<li><a href="{REPO}/blob/main/docs/ROADMAP.md">Data roadmap</a></li></ul>',
            '<h2>Coverage at this build</h2><table>',
            f'<tr><th>Hospitals tracked</th><td>{totals["hospitals"]:,}</td></tr>',
            f'<tr><th>With a current summary</th><td>{totals["with_summary"]:,}</td></tr>',
            f'<tr><th>With an archived source link for the latest snapshot</th><td>{totals["with_archived_source"]:,}</td></tr>',
            f'<tr><th>Recorded archive states</th><td>{totals["snapshots"]:,}</td></tr>',
            f'<tr><th>Historical states without an archived source link</th><td>{totals["snapshots_without_archived_source"]:,}</td></tr>',
            f'<tr><th>Hospitals with recorded issues</th><td>{totals["with_issues"]:,}</td></tr></table>',
            '<p>Archived-source coverage means a matching link is recorded; this build does not '
            'download and verify every original. Historical gaps count recorded states, including '
            'summary rebuilds and temporary metadata resets; they do not necessarily represent distinct missing files. '
            'The catalog lists the gaps per hospital, and manifests identify each affected snapshot.</p>',
            f'<p>The historical audit reviewed {catalog["source_audit_totals"]["reviewed_states"]} states and '
            f'restored source links for {catalog["source_audit_totals"]["linked_states"]}. '
            f'<a href="{REPO}/blob/main/docs/SOURCE_AUDIT.md">Read the findings and remaining limitations</a>.</p>',
            '<h2>Collection freshness</h2>',
            f'<p>{catalog["collection_totals"]["with_successful_check"]} hospitals have a recorded successful source check; '
            f'{catalog["collection_totals"]["without_successful_check"]} have no successful check recorded yet. '
            f'{catalog["collection_totals"]["check_overdue"]} are overdue for a scheduled attempt and '
            f'{catalog["collection_totals"]["verification_overdue"]} are overdue for source verification.</p>',
            '<p>These counts are evaluated at build time. Daily monitoring also checks the recorded schedules. '
            'The catalog distinguishes full downloads, header checks, sampled fingerprints, failures, and '
            'deliberately deferred downloads. “Last changed” describes a snapshot, not its freshness. '
            'Checks before logging began remain unknown; header and sample matches do not verify every byte.</p>',
            '<h2>Recorded issues</h2>']
    if totals["with_issues"]:
        body.append('<table><tr><th>Hospital</th><th>Issue</th></tr>')
        for entry in catalog["hospitals"]:
            if entry["issues"]:
                body.append(f'<tr><td><a href="{esc(entry["hospital_url"])}">{esc(entry["name"])}</a></td>'
                            f'<td>{"<br>".join(esc(ISSUES[issue]) for issue in entry["issues"])}</td></tr>')
        body.append('</table>')
    else:
        body.append('<p>No issues were found by the implemented checks.</p>')
    body.extend(['<h2>Working with snapshots</h2><ol>',
                 '<li>Find a hospital in the catalog and open its <code>snapshots_url</code>.</li>',
                 '<li>Select a snapshot with a <code>summary_url</code>. Save its '
                 '<code>snapshot_id</code>, method version, and warnings alongside your analysis.</li>',
                 '<li>Download the summary from its pinned Git revision. The example script '
                 'verifies its Git object ID before saving.</li></ol>',
                 '<p>Snapshot dates record observations, not price effective dates. Summary rows group '
                 'codes and descriptions; they do not retain separate payer, plan, setting, modifier, or '
                 'unit dimensions. Consult original files for that context. Do not treat a method '
                 'change, a renamed entry, or a missing price as a price change.</p>',
                 f'<p class="muted">Catalog schema {catalog["schema_version"]}; built from '
                 f'<a href="{REPO}/commit/{catalog["catalog_revision"]}">{catalog["catalog_revision"][:12]}</a>. '
                 f'Generated {esc(catalog["generated_at"])}.</p>'])
    (outdir / "data" / "index.html").write_text(page(
        "Use the data · Hospital Price History", "Download the hospital data catalog and historical snapshot manifests.",
        "\n".join(body), canonical,
        {"@context": "https://schema.org", "@type": "DataCatalog", "name": "Hospital Price History data catalog",
         "url": canonical, "dataset": {"@type": "Dataset", "url": canonical + "catalog.json"}}))
    return canonical


def retire_comparison(outdir):
    # Preserve existing bookmarks while removing generated scripts and data
    # when a local output directory is reused.
    destination = outdir / "hospitals" / "rush-university" / "compare"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    target = f"{BASE}/hospitals/rush-university/"
    (destination / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="refresh" content="0;url={target}"><link rel="canonical" href="{target}">'
        '<meta name="robots" content="noindex"><title>Rush hospital archive</title></head>'
        f'<body><p>The comparison has been retired. <a href="{target}">Open the Rush hospital archive</a> '
        'for summaries and historical snapshots.</p></body></html>')


def main():
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    hospitals = json.loads((ROOT / "hospitals.json").read_text())
    metas, urls = {}, [f"{BASE}/"]
    for h in hospitals:
        mp = ROOT / "data" / h["slug"] / "meta.json"
        metas[h["slug"]] = json.loads(mp.read_text()) if mp.exists() else {}
    catalog = build_catalog(hospitals, ROOT, outdir, BASE, REPO, RAW_REPO)
    entries = {entry["slug"]: entry for entry in catalog["hospitals"]}
    for h in hospitals:
        urls.append(hospital_page(h, metas[h["slug"]], outdir, entries[h["slug"]]))
    index_page(hospitals, metas, outdir)
    urls.append(data_page(catalog, outdir))
    retire_comparison(outdir)
    (outdir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(f"<url><loc>{u}</loc></url>" for u in urls) + "\n</urlset>\n")
    (outdir / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    print(f"generated {len(urls)} pages in {outdir}")


if __name__ == "__main__":
    main()
