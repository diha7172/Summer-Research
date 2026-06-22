"""
PharmShed data pull from the Data USA (Tesseract) API.

Pulls four datasets across three geography levels (State, County, Place/city)
for every year each cube has, then saves CSVs into the current folder.

Handles the API's intermittent 500 errors and silent-empty large requests
by retrying with backoff and falling back to year-by-year pulls.

Run:  py pull_pharmshed.py
"""

import time
import requests
import pandas as pd

BASE = "https://api.datausa.io/tesseract/data.jsonrecords"
GEO_LEVELS = ["State", "County", "Place"]   # Place = city
YEARS = range(2009, 2025)                   # generous ACS 5-year bounds

CUBES = {
    "population": ("acs_yg_total_population_5", "Population", ""),
    "ethnicity":  ("acs_ygr_race_with_hispanic_5", "Hispanic Population", "Ethnicity"),
    "insurance":  ("acs_health_coverage_s_5", "Number Covered", "Health Coverage"),
    "income":     ("acs_yg_household_income_5", "Household Income", "Household Income Bucket"),
}

COVERAGE_GROUP = {
    "Employer": "Private",
    "Direct Purchase": "Private",
    "Medicare": "Public",
    "Medicaid": "Public",
    "Veterans Affairs": "Public",
    "Military Health Insurance": "Public",
    "Uninsured": "Uninsured",
}


def fetch(cube, measure, drilldowns, year=None, retries=4):
    """Single API call with retries on 500s / timeouts."""
    params = {"cube": cube, "drilldowns": drilldowns, "measures": measure}
    if year is not None:
        params["include"] = f"Year:{year}"
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(BASE, params=params, timeout=180)
            resp.raise_for_status()
            return pd.DataFrame(resp.json().get("data", []))
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))   # 2s, 4s, 6s, 8s
    raise last_err


def pull(cube, measure, geo_level, extra=""):
    """Try all years at once; if that fails or is empty, go year-by-year."""
    drilldowns = f"{geo_level},Year"
    if extra:
        drilldowns += f",{extra}"

    # First attempt: everything in one call
    try:
        df = fetch(cube, measure, drilldowns)
        if len(df):
            span = f"{df['Year'].min()}-{df['Year'].max()}"
            print(f"  {geo_level}: {len(df)} rows (years {span})")
            return df
    except Exception as e:
        print(f"  {geo_level}: one-shot failed ({type(e).__name__}), going year-by-year...")

    # Fallback: one year at a time, each with its own retries
    parts = []
    for yr in YEARS:
        try:
            d = fetch(cube, measure, drilldowns, year=yr)
            if len(d):
                parts.append(d)
                print(f"      {yr}: {len(d)} rows")
        except Exception as e:
            print(f"      {yr}: failed after retries ({type(e).__name__})")
        time.sleep(0.4)

    if parts:
        df = pd.concat(parts, ignore_index=True)
        span = f"{df['Year'].min()}-{df['Year'].max()}"
        print(f"  {geo_level}: {len(df)} rows total (years {span})")
        return df
    print(f"  {geo_level}: no data")
    return pd.DataFrame()


def main():
    for name, (cube, measure, extra) in CUBES.items():
        print(f"\n{name}:")
        frames = []
        for level in GEO_LEVELS:
            df = pull(cube, measure, level, extra)
            if len(df):
                df["Geo Level"] = level
                frames.append(df)
            time.sleep(0.5)

        if not frames:
            print(f"  -> no data for {name}, skipping")
            continue

        combined = pd.concat(frames, ignore_index=True)
        if name == "insurance":
            combined["Coverage Group"] = combined["Health Coverage"].map(COVERAGE_GROUP)

        out = f"{name}.csv"
        combined.to_csv(out, index=False)
        print(f"  -> saved {out} ({len(combined)} rows)")


if __name__ == "__main__":
    main()
