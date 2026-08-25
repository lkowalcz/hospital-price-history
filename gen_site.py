#!/usr/bin/env python3
"""Generate the static GitHub Pages site: gen_site.py <output_dir>.

One page per hospital (price digest, change history from git, dataset
markup for Google Dataset Search) plus an index. Deployed by the Pages
workflow; never committed to the repo.
"""

import csv
import html
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://lkowalcz.github.io/hospital-price-history"
REPO = "https://github.com/lkowalcz/hospital-price-history"

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
th{background:var(--soft)}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.warn{background:#fff4e5;border:1px solid #f0c988;border-radius:6px;padding:.7rem 1rem;margin:1rem 0}
.badge{display:inline-block;background:var(--soft);border:1px solid var(--line);border-radius:4px;padding:0 .45rem;font-size:.8rem}
footer{margin:3rem 0 1rem;font-size:.85rem;color:var(--muted);border-top:1px solid var(--line);padding-top:1rem}
.wrap{overflow-x:auto}
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
<script type="application/ld+json">{json.dumps(jsonld)}</script>
<style>{CSS}</style></head><body><main>
{body}
<footer>Data: hospital machine-readable files published under 45 CFR § 180.50,
archived daily. <a href="{REPO}">Method &amp; raw history on GitHub</a>.
Generated {date.today().isoformat()}.</footer>
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


def featured_rows(rows):
    out = []
    for r in rows or []:
        if r["code_type"].upper().startswith("MS-DRG") and r["code"].lstrip("0") in \
                {k.lstrip("0") for k in FEATURED_DRGS}:
            out.append(r)
    if not out and rows:
        out = sorted(rows, key=lambda r: -int(r["payer_entries"] or 0))[:6]
    return out[:8]


def hospital_page(h, meta, outdir):
    slug = h["slug"]
    name = h["location_name"] if len(h["location_name"]) < 60 else slug.replace("-", " ").title()
    rows = load_summary(slug)
    events = git_history(slug)
    canonical = f"{BASE}/hospitals/{slug}/"

    parts = [f'<p class="muted"><a href="{BASE}/">&larr; All hospitals</a></p>',
             f"<h1>{esc(name)}</h1>",
             f'<p class="muted">{esc(h["system"])} &middot; '
             f'<span class="badge">{esc(meta.get("status", "no data"))}</span></p>']

    parts.append("<h2>Tracking status</h2><table>")
    for label, val in [
            ("Monitored since", (meta.get("first_seen") or "")[:10]),
            ("File last changed", (meta.get("last_changed") or "")[:10]),
            ("File size", f'{meta.get("size_bytes", 0):,} bytes' if meta.get("size_bytes") else "—"),
            ("Billing codes in summary", f"{len(rows):,}" if rows else "—"),
            ("Source file", f'<a href="{esc(meta.get("mrf_url", ""))}" rel="nofollow">machine-readable file</a>'
             if meta.get("mrf_url") else "—")]:
        parts.append(f"<tr><th>{label}</th><td>{val}</td></tr>")
    parts.append("</table>")

    ff = meta.get("fetch_failures")
    if ff:
        parts.append(f'<div class="warn">⚠ This hospital&rsquo;s price file has been '
                     f'unreachable since {esc(ff["first_failed"][:10])} '
                     f'(<code>{esc(ff["last_error"][:90])}</code>). The failure streak '
                     f'is recorded in the archive.</div>')

    if rows:
        feat = featured_rows(rows)
        if feat:
            parts.append("<h2>Example prices</h2><div class=\"wrap\"><table>"
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
                         f'<p class="muted">Full digest of all {len(rows):,} codes: '
                         f'<a href="https://raw.githubusercontent.com/lkowalcz/hospital-price-history/main/data/{slug}/summary.csv">summary.csv</a></p>')

    parts.append("<h2>Change history</h2>")
    if events:
        parts.append("<table><tr><th>Date</th><th>Event</th></tr>")
        for d, commit_hash, s in events[:30]:
            parts.append(f'<tr><td>{d}</td><td><a href="{REPO}/commit/{commit_hash}">{esc(s)}</a></td></tr>')
        parts.append("</table>")
    else:
        parts.append('<p class="muted">No recorded events yet.</p>')

    desc = (f"Price transparency history for {name} ({h['system']}): standard charges, "
            f"cash prices and negotiated rates, archived daily with full change history.")
    jsonld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": f"{name} — hospital price history",
        "description": desc, "url": canonical,
        "creator": {"@type": "Person", "name": "Lucas Kowalczyk"},
        "isBasedOn": meta.get("mrf_url"),
        "dateModified": (meta.get("last_changed") or "")[:10],
        "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv",
                          "contentUrl": f"https://raw.githubusercontent.com/lkowalcz/hospital-price-history/main/data/{slug}/summary.csv"}],
        "keywords": ["hospital prices", "price transparency", h["system"], name],
    }
    d = outdir / "hospitals" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(
        page(f"{name} price history", desc, "\n".join(parts), canonical, jsonld))
    return canonical


def index_page(hospitals, metas, outdir):
    body = ["<h1>Hospital Price History</h1>",
            '<p>A public archive tracking the machine-readable price files US hospitals '
            'must publish under federal price transparency rules (45 CFR § 180.50). '
            'Files are checked daily; every change — a revised rate, a republished file, '
            'a file quietly taken down — is recorded in '
            f'<a href="{REPO}">version-controlled history</a>.</p>',
            "<div class=\"wrap\"><table><tr><th>Hospital</th><th>System</th>"
            "<th>Last changed</th><th>Status</th></tr>"]
    for h in sorted(hospitals, key=lambda x: x["location_name"].casefold()):
        m = metas[h["slug"]]
        name = h["location_name"] if len(h["location_name"]) < 60 else h["slug"].replace("-", " ").title()
        status = "⚠ unreachable" if m.get("fetch_failures") else m.get("status", "—")
        body.append(f'<tr><td><a href="{BASE}/hospitals/{h["slug"]}/">{esc(name)}</a></td>'
                    f'<td>{esc(h["system"])}</td>'
                    f'<td>{(m.get("last_changed") or "—")[:10]}</td>'
                    f'<td>{esc(status)}</td></tr>')
    body.append("</table></div>")
    desc = (f"Daily archive of price transparency files from {len(hospitals)} major US "
            "hospitals — standard charges, cash prices, negotiated rates — with full "
            "change history.")
    jsonld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "Hospital Price History",
        "description": desc, "url": f"{BASE}/",
        "creator": {"@type": "Person", "name": "Lucas Kowalczyk"},
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
        "distribution": [{"@type": "DataDownload", "encodingFormat": "application/zip",
                          "contentUrl": f"{REPO}/archive/refs/heads/main.zip"}],
        "keywords": ["hospital prices", "price transparency", "healthcare costs",
                     "negotiated rates", "machine-readable files"],
    }
    (outdir / "index.html").write_text(
        page("Hospital Price History", desc, "\n".join(body), f"{BASE}/", jsonld))


def main():
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    hospitals = json.loads((ROOT / "hospitals.json").read_text())
    metas, urls = {}, [f"{BASE}/"]
    for h in hospitals:
        mp = ROOT / "data" / h["slug"] / "meta.json"
        metas[h["slug"]] = json.loads(mp.read_text()) if mp.exists() else {}
    for h in hospitals:
        urls.append(hospital_page(h, metas[h["slug"]], outdir))
    index_page(hospitals, metas, outdir)
    (outdir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(f"<url><loc>{u}</loc></url>" for u in urls) + "\n</urlset>\n")
    (outdir / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    print(f"generated {len(urls)} pages in {outdir}")


if __name__ == "__main__":
    main()
