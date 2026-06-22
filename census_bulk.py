"""
Census ACS bulk puller -> data for the search web app
=====================================================
Pulls EVERY U.S. geography the ACS 5-year API exposes - Nation, all States, all
~3,200 Counties, and all ~30,000 Places (cities/towns) - using wildcard queries
(one call per level/state instead of one per geography), then writes compact,
sharded JSON for the webapp/ search UI.

Reuses the verified tables/variables/builders from census_scraper.py.

Profiles are year-keyed so the web app can offer a year selector / trends.

Run (needs a Census key in CENSUS_API_KEY env var or census_key.txt):
    py census_bulk.py                 # default years 2013,2018,2024 - everything
    py census_bulk.py --years 2024
    py census_bulk.py --years 2019-2024
    py census_bulk.py --no-places     # skip the ~30k places (states+counties only)

Output:
    webapp/data/index.json            # [{id,name,level,state}] for search
    webapp/data/profiles/us.json      # nation + all states  -> {geoId:{year:profile}}
    webapp/data/profiles/<ss>.json    # counties + places of state <ss>
    webapp/data/meta.json             # {year, years, counts, generated}
"""

import os
import sys
import json
import time
import argparse
import census_scraper as c

DATA_DIR = os.path.join("webapp", "data")
PROF_DIR = os.path.join(DATA_DIR, "profiles")
# Snapshot years for the year selector / trends. Spans a decade but stays small
# enough to embed in the single-file build. Override with --years.
DEFAULT_BULK_YEARS = [2013, 2018, 2024]


def fetch_rows(year, get, geo_params, key):
    """One wildcard call -> list of dict rows (already keyed by var name)."""
    res = c.census_request(year, get, geo_params, key, max_retries=5, timeout=120)
    if res.status == "key_error":
        raise KeyError(res.detail)
    return res.rows if res.ok else []


def geo_id_of(level, row):
    if level == "Nation":
        return "01000US"
    if level == "State":
        return "04000US" + row["state"]
    if level == "County":
        return "05000US" + row["state"] + row["county"]
    if level == "Place":
        return "16000US" + row["state"] + row["place"]
    return row.get("NAME", "")


def merge(detail_rows, ins_rows, level):
    """Join the detail call and the B27010 call on the geo-id columns."""
    keycols = {"Nation": [], "State": ["state"],
               "County": ["state", "county"], "Place": ["state", "place"]}[level]

    def k(r):
        return tuple(r.get(c_, "") for c_ in keycols)

    ins = {k(r): r for r in ins_rows}
    out = []
    for d in detail_rows:
        row = dict(d)
        row.update(ins.get(k(d), {}))
        out.append((geo_id_of(level, d), d.get("NAME", ""), row))
    return out


def profile_for(flat, year):
    """Compact profile dict from one geo's merged variable row."""
    div = [(cat, pct) for (_, mg, cat, cnt, pct)
           in c.build_population_diversity(flat, year) if mg == "diversity"]
    pop = next((cnt for (_, mg, cat, cnt, pct)
                in c.build_population_diversity(flat, year)
                if mg == "population"), None)
    inc = [(cat, pct) for (_, mg, cat, cnt, pct)
           in c.build_income(flat, year)]
    ins_rows = c.build_insurance(flat, year)
    types = [(cat, pct) for (_, mg, cat, cnt, pct) in ins_rows
             if not cat.startswith("GROUP")]
    groups = [(cat.replace("GROUP: ", ""), pct) for (_, mg, cat, cnt, pct)
              in ins_rows if cat.startswith("GROUP")]
    if pop is None and not inc and not div:
        return None
    return {
        "pop": int(pop) if pop else None,
        "diversity": [[a, round(b, 1)] for a, b in div],
        "income": [[a, round(b, 1)] for a, b in inc],
        "insurance": {"types": [[a, round(b, 1)] for a, b in types],
                      "groups": [[a, round(b, 1)] for a, b in groups]},
    }


def shard_of(level, geo_id):
    if level in ("Nation", "State"):
        return "us"
    return geo_id.split("US", 1)[1][:2]  # state FIPS


def latest_year(key):
    for y in range(2024, 2009, -1):
        if fetch_rows(y, "NAME,B19001_001E", {"for": "us:1"}, key):
            return y
    raise SystemExit("Could not reach the Census API for any year.")


def pull_level(level, geo_params, year, key):
    """Return list of (geo_id, name, level, profile)."""
    detail = fetch_rows(year, ",".join(c.DETAIL_VARS), geo_params, key)
    ins = fetch_rows(year, "group(B27010)", geo_params, key)
    out = []
    for gid, name, flat in merge(detail, ins, level):
        prof = profile_for(flat, year)
        if prof:
            out.append((gid, name, level, prof))
    return out


def parse_years(spec):
    if not spec:
        return DEFAULT_BULK_YEARS
    years = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            years.update(range(int(a), int(b) + 1))
        elif part:
            years.add(int(part))
    return sorted(years)


def main():
    ap = argparse.ArgumentParser(description="Bulk-pull all Census geographies.")
    ap.add_argument("--years", help="e.g. 2013,2018,2024 or 2013-2024 "
                                    "(default: 2013,2018,2024)")
    ap.add_argument("--no-places", action="store_true",
                    help="skip ~30k places (states+counties only, much faster)")
    args = ap.parse_args()

    key = c.load_api_key()
    if not key:
        print(c.KEY_HELP)
        return

    os.makedirs(PROF_DIR, exist_ok=True)
    years = parse_years(args.years)
    latest = max(years)
    print(f"Census bulk pull - ACS 5-year {years}")

    index_map = {}          # geo_id -> index entry (from the latest year)
    # shard name -> {geo_id: {year_str: profile}}
    shards = {}

    def add(records, year, is_latest):
        for gid, name, level, prof in records:
            shards.setdefault(shard_of(level, gid), {}) \
                  .setdefault(gid, {})[str(year)] = prof
            if is_latest:
                index_map[gid] = {
                    "id": gid, "name": name, "level": level,
                    "state": gid.split("US", 1)[1][:2]
                    if level in ("County", "Place", "State") else ""}

    t0 = time.time()
    fipses = sorted(c.STATE_FIPS.values())
    for year in years:
        is_latest = (year == latest)
        print(f"\n  year {year}:", flush=True)
        print("    nation/states/counties ...", flush=True)
        add(pull_level("Nation", {"for": "us:1"}, year, key), year, is_latest)
        add(pull_level("State", {"for": "state:*"}, year, key), year, is_latest)
        add(pull_level("County", {"for": "county:*"}, year, key), year, is_latest)
        if not args.no_places:
            for i, ss in enumerate(fipses, 1):
                recs = pull_level("Place", {"for": "place:*",
                                            "in": f"state:{ss}"}, year, key)
                add(recs, year, is_latest)
                print(f"\r    places {i}/{len(fipses)} (state {ss})        ",
                      end="", flush=True)
            print()

    index = sorted(index_map.values(), key=lambda r: (r["name"] or "").lower())
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    for shard, profiles in shards.items():
        with open(os.path.join(PROF_DIR, f"{shard}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))
    counts = {}
    for r in index:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    with open(os.path.join(DATA_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"year": latest, "years": years, "counts": counts,
                   "total": len(index),
                   "generated": time.strftime("%Y-%m-%d %H:%M")}, f)

    size = sum(os.path.getsize(os.path.join(PROF_DIR, x))
               for x in os.listdir(PROF_DIR)) / 1e6
    print(f"\nDone in {time.time()-t0:.0f}s")
    print(f"  geographies: {len(index):,}  years: {years}  {counts}")
    print(f"  data: webapp/data/  ({size:.1f} MB profiles + index)")
    print("  start the app:  py serve.py")


if __name__ == "__main__":
    main()
