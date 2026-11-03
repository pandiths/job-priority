#!/usr/bin/env python3
"""Bay Area H-1B sponsors for software / ML roles, from the same DOL LCA file as build_sponsors.py.

Usage (python3 -I; stdlib only):
  python3 -I bay_area.py LCA_Disclosure_Data_FY2026_Q3.xlsx --out bay_area.json

Writes [{employer, key, count, median_wage, cities: {city: n}, top_titles}] for employers with
certified H-1B filings at a worksite in the nine Bay Area counties plus Santa Cruz, after the same
title and staffing filters as build_sponsors.py. Board matching is done afterwards by
`build_sponsors.py probe bay_area.json --boards ../boards.json`.
"""
import argparse, collections, json, os, re, statistics, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_sponsors import iter_rows, norm_name, annual, TITLE_INC, TITLE_EXC, DENY, STAFFING_WORDS

COUNTIES = {"alameda", "contra costa", "marin", "napa", "san francisco", "san mateo", "santa clara",
            "solano", "sonoma", "santa cruz"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--out", default="bay_area.json")
    ap.add_argument("--min", type=int, default=2, help="drop employers with fewer filings")
    args = ap.parse_args()
    H, emp = None, {}
    for row in iter_rows(args.xlsx):
        if H is None:
            H = {row[i]: i for i in row}
            continue
        g = lambda k: row.get(H[k], "")
        if g("VISA_CLASS") != "H-1B" or g("CASE_STATUS").strip().lower() != "certified":
            continue
        if g("WORKSITE_STATE").strip().upper() != "CA":
            continue
        county = re.sub(r"\s+county$", "", g("WORKSITE_COUNTY").strip().lower())
        title = g("JOB_TITLE").strip()
        if county not in COUNTIES or not TITLE_INC.search(title) or TITLE_EXC.search(title):
            continue
        name = g("EMPLOYER_NAME").strip()
        key = norm_name(name)
        if not key:
            continue
        e = emp.setdefault(key, {"names": collections.Counter(), "titles": collections.Counter(),
                                 "cities": collections.Counter(), "wages": [], "n": 0, "sec": 0})
        e["n"] += 1
        e["names"][name] += 1
        e["titles"][re.sub(r"\s+", " ", title.title())] += 1
        e["cities"][g("WORKSITE_CITY").strip().title()] += 1
        e["sec"] += g("SECONDARY_ENTITY").strip().lower().startswith("y")
        w = annual(g("WAGE_RATE_OF_PAY_FROM"), g("WAGE_UNIT_OF_PAY"))
        if w:
            e["wages"].append(w)
    out = []
    for key, e in emp.items():
        disp = e["names"].most_common(1)[0][0]
        light = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", disp.lower().replace("&", " "))).strip() + " "
        staffing = (any((" " + d.strip() + " ") in f" {key} " or (" " + d.strip() + " ") in light for d in DENY)
                    or STAFFING_WORDS.search(disp) or e["sec"] / e["n"] >= 0.05)
        if staffing or e["n"] < args.min:
            continue
        out.append({"employer": disp, "key": key, "count": e["n"],
                    "median_wage": round(statistics.median(e["wages"])) if e["wages"] else None,
                    "cities": dict(e["cities"].most_common(5)),
                    "top_titles": [t for t, _ in e["titles"].most_common(3)],
                    "name_variants": [k for k, _ in e["names"].most_common(4)]})
    out.sort(key=lambda r: -r["count"])
    json.dump(out, open(args.out, "w"), indent=1)
    print(f"{len(out)} Bay Area employers -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
