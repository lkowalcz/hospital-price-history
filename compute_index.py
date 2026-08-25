#!/usr/bin/env python3
"""Hospital List Price Index: chain-linked Jevons index over a fixed basket.

Method:
  - basket.json defines ~30 standardized codes (MS-DRGs + common CPT/HCPCS).
  - For each hospital, each basket item's cash price and gross charge are read
    from data/<slug>/summary.csv (median across matching rows).
  - Each run computes price relatives against the previous run's prices
    (stored in index-state.json), takes the geometric mean of relatives per
    hospital, then the geometric mean across hospitals, and compounds it onto
    the running index (base 100 at first run). Chain-linking means hospitals
    and codes can enter or leave the panel without breaking the series.
  - Appends one row per day to index-history.csv (skips if today already
    recorded). Cash and gross series are computed independently.

Run daily by the scrape workflow after the scrape step.
"""

import csv
import json
import math
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "index-state.json"
HISTORY = ROOT / "index-history.csv"

DRG_TYPES = {"MSDRG", "DRG"}
CPT_TYPES = {"CPT", "HCPCS", "CPTHCPCS"}


def canon_type(t):
    return re.sub(r"[^A-Z]", "", (t or "").upper())


def canon_code(c):
    c = (c or "").strip().upper()
    return c.lstrip("0") or "0"


def to_float(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def median(vals):
    vals = sorted(vals)
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def basket_prices(slug, items):
    """{item_key: {"cash": x, "gross": y}} for one hospital's summary."""
    p = ROOT / "data" / slug / "summary.csv"
    if not p.exists():
        return {}
    hits = {}  # key -> {"cash": [..], "gross": [..]}
    with open(p) as f:
        for row in csv.DictReader(f):
            ct, cc = canon_type(row["code_type"]), canon_code(row["code"])
            for key, (btype, bcode) in items.items():
                type_ok = ct in (DRG_TYPES if btype == "DRG" else CPT_TYPES)
                if type_ok and cc == bcode:
                    h = hits.setdefault(key, {"cash": [], "gross": []})
                    for field, col in (("cash", "discounted_cash"), ("gross", "gross_charge")):
                        v = to_float(row[col])
                        if v is not None:
                            h[field].append(v)
    return {
        key: {f: median(v[f]) for f in ("cash", "gross") if v[f]}
        for key, v in hits.items()
    }


def geomean(xs):
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def series_factor(prev_prices, cur_prices, field):
    """Chain factor: geomean over hospitals of geomean over codes of relatives."""
    hospital_relatives = []
    pairs = 0
    for slug, cur in cur_prices.items():
        prev = prev_prices.get(slug, {})
        rels = []
        for key, vals in cur.items():
            a, b = prev.get(key, {}).get(field), vals.get(field)
            if a and b:
                rels.append(b / a)
        if rels:
            hospital_relatives.append(geomean(rels))
            pairs += len(rels)
    return (geomean(hospital_relatives) or 1.0), pairs, len(hospital_relatives)


def main():
    basket = json.loads((ROOT / "basket.json").read_text())
    items = {f'{i["type"]}|{i["code"]}': (i["type"], canon_code(i["code"]))
             for i in basket["items"]}
    hospitals = json.loads((ROOT / "hospitals.json").read_text())
    cur_prices = {}
    for h in hospitals:
        prices = basket_prices(h["slug"], items)
        if prices:
            cur_prices[h["slug"]] = prices

    today = date.today().isoformat()
    if HISTORY.exists() and any(line.startswith(today) for line in
                                HISTORY.read_text().splitlines()):
        print(f"index: {today} already recorded")
        return

    if STATE.exists():
        state = json.loads(STATE.read_text())
        factor_cash, pairs_c, hosp_c = series_factor(state["prices"], cur_prices, "cash")
        factor_gross, pairs_g, hosp_g = series_factor(state["prices"], cur_prices, "gross")
        idx_cash = state["index_cash"] * factor_cash
        idx_gross = state["index_gross"] * factor_gross
    else:
        idx_cash = idx_gross = 100.0
        pairs_c = sum(1 for p in cur_prices.values() for v in p.values() if v.get("cash"))
        pairs_g = sum(1 for p in cur_prices.values() for v in p.values() if v.get("gross"))
        hosp_c = hosp_g = len(cur_prices)

    if not HISTORY.exists():
        HISTORY.write_text("date,index_cash,index_gross,pairs_cash,pairs_gross,"
                           "hospitals_cash,hospitals_gross\n")
    with open(HISTORY, "a") as f:
        f.write(f"{today},{idx_cash:.4f},{idx_gross:.4f},{pairs_c},{pairs_g},"
                f"{hosp_c},{hosp_g}\n")
    STATE.write_text(json.dumps({
        "date": today, "index_cash": idx_cash, "index_gross": idx_gross,
        "basket_version": basket["version"], "prices": cur_prices,
    }, indent=1) + "\n")
    print(f"index: {today} cash={idx_cash:.2f} gross={idx_gross:.2f} "
          f"({hosp_c} hospitals, {pairs_c} cash pairs)")


if __name__ == "__main__":
    main()
