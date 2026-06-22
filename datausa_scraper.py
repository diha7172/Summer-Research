"""
DataUSA Data Fetcher  (combined build, v3 - resilient against origin outages)
=============================================================================
Pulls household income (by bracket), health-insurance coverage, and
race/ethnicity from the DataUSA Tesseract API for any U.S. geography, at
Nation, State, County, or Place (city) level.

WHAT WAS ACTUALLY CAUSING THE STATE-LEVEL "500s" (root cause, verified)
-----------------------------------------------------------------------
The state-level failures were NOT caused by the query being too large, and
NOT by anything state-specific in the cube or parameters. They were caused by
the DataUSA Tesseract ORIGIN server being intermittently unreachable behind
Cloudflare.

Evidence gathered by hitting the live API (see the diagnosis note in the PR /
chat for full numbers):
  * The lightest possible state query (one state, latest year, NO bucket
    drilldown -> 16 rows) failed exactly as often as the heaviest one
    (all years + bucket). 0/15 vs 0/15. Row count is irrelevant, so the
    "the big query overloads the backend" hypothesis is false.
  * The income cube failed at EVERY level - Nation, State, County, and Place -
    not just State. So it is not state-specific.
  * The errors were Cloudflare edge errors against the origin, not application
    errors from the query: HTTP 525 (SSL handshake to origin failed), 502
    (bad gateway), 500 (origin 500), and read timeouts - cycling, never the
    same code "every time".
  * Meanwhile the exact "known-working" County race URL returned 200 on every
    single attempt for several minutes... and then, the instant its Cloudflare
    cache entry expired, it ALSO started returning 525. That is the tell: it
    was being served from Cloudflare's edge cache the whole time, never
    touching the sick origin.

So the user's real-world pattern ("State 500s, County/Place work") was
Cloudflare cache behavior: County/Place URLs that had already been pulled were
cache HITs (200, served from the edge); fresh State URLs were cache MISSes that
had to reach the unhealthy origin and came back 5xx. Nothing about the State
query itself is wrong.

HOW THIS VERSION FIXES IT
-------------------------
You cannot "fix" someone else's flaky origin, but you can stop it from
corrupting your data and stop it from masquerading as "no data":

  1. RETRY every transient transport failure, not just classic 500s:
     all 5xx INCLUDING Cloudflare's 520-527 family, plus 408/429, timeouts and
     connection/SSL errors. Patient, capped exponential backoff with jitter.

  2. TRI-STATE result for every request, so we never confuse the three
     fundamentally different outcomes:
        - "ok"           : HTTP 200 with rows  -> real data
        - "empty"        : HTTP 200 with 0 rows -> genuinely no data here
        - "server_error" : exhausted retries on 5xx/timeout -> origin problem
        - "client_error" : 4xx (e.g. bad cube name) -> our request is wrong
     A server_error is NEVER recorded as "not available" or as an empty CSV.
     This is the specific bug that made fixable 500s look like missing data:
     the old availability probe turned any failed request into
     "NOT available" and skipped the cube entirely.

  3. The availability probe no longer gates on server errors. It only skips a
     cube when the API positively answers 200-with-zero-rows (true no-data) or
     a 4xx. A server_error during the probe means "attempt the real pull
     anyway".

  4. Year-by-year fallback is used as a RECOVERY path for server_errors
     (smaller per-year requests get their own retry budget and are far more
     likely to be individually cacheable / to slip through during a brief
     origin up-window), not as a response to genuine emptiness.

  5. Built for unattended batch runs:
        - one geography or one cube failing never crashes the run,
        - every per-cube CSV is written the moment it is fetched (crash-safe),
        - the MASTER long file is appended after each geography,
        - --resume reloads already-saved per-cube CSVs instead of re-fetching,
        - a failures log (_failures.csv) and a run log record exactly which
          geo / cube / year failed and why (server_error vs no-data).

Cube names / measures / dimensions are unchanged from v2 (verified live):
  income    : acs_yg_household_income_5      measure "Household Income"
              extra drilldown "Household Income Bucket"   (B19001 brackets)
  insurance : acs_health_coverage_s_5        measure "Number Covered"
              extra drilldowns "Health Coverage","Age Group"
  race      : acs_ygr_race_with_hispanic_5   measure "Hispanic Population"
              extra drilldowns "Race","Ethnicity"        (B03002 counts)

Year filtering uses include=Year:<yr> (the time= param only accepts
.latest/.oldest/.trailing, never a literal year).

Usage (Windows PowerShell: use `python` or `py`):
    pip install requests
    python datausa_scraper.py --geo 04000US08 --name "Colorado"
    python datausa_scraper.py --examples
    python datausa_scraper.py --geo 05000US08013 --name "Boulder County, CO" --by-year
    python datausa_scraper.py --geos-file geos.txt --resume
    python datausa_scraper.py --geo 04000US08,04000US54 --name "CO+WV"
"""

import os
import csv
import time
import random
import argparse
import logging
import requests


BASE_URL = "https://api.datausa.io/tesseract/data.jsonrecords"
OUT_DIR = "datausa_output"

# Long-format schema. Fixed order so the combined + MASTER files have a stable
# schema across every geography and every (resumed) run.
LONG_FIELDS = [
    "geo_id", "geo_name", "geo_level", "year",
    "measure_group", "category", "measure", "value",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://datausa.io/",
    "Origin": "https://datausa.io",
    "Accept": "application/json, text/plain, */*",
})

log = logging.getLogger("datausa")


GEO_LEVEL_MAP = {
    "16000US": "Place",
    "05000US": "County",
    "04000US": "State",
    "01000US": "Nation",
}


def detect_geo_level(geo_id: str) -> str:
    for prefix, level in GEO_LEVEL_MAP.items():
        if geo_id.startswith(prefix):
            return level
    # Bare "01000US" is the whole nation; fall back to Place for unknowns.
    if geo_id.startswith("01000US"):
        return "Nation"
    return "Place"


# Each measure group. extra_dd are drilldowns beyond geo+year (e.g. Race).
CUBES = {
    "income": {
        "cube": "acs_yg_household_income_5",
        "measures": ["Household Income"],
        "extra_dd": ["Household Income Bucket"],
        "label": "Household Income (by bracket)",
    },
    "insurance": {
        "cube": "acs_health_coverage_s_5",
        "measures": ["Number Covered"],
        "extra_dd": ["Health Coverage", "Age Group"],
        "label": "Health Insurance Coverage",
    },
    "race": {
        "cube": "acs_ygr_race_with_hispanic_5",
        "measures": ["Hispanic Population"],
        "extra_dd": ["Race", "Ethnicity"],
        "label": "Race / Ethnicity",
    },
}


# --- tri-state fetch result ------------------------------------------------

class FetchResult:
    """One of four outcomes for an API call, so callers never confuse
    'no data' with 'the server failed'.

      status == "ok"           -> HTTP 200, rows present
      status == "empty"        -> HTTP 200, zero rows (genuinely no data)
      status == "server_error" -> retries exhausted on 5xx / timeout / conn
      status == "client_error" -> 4xx that is not retryable (e.g. bad cube)
    """

    def __init__(self, status, rows=None, http=None, detail=""):
        self.status = status
        self.rows = rows or []
        self.http = http
        self.detail = detail

    @property
    def ok(self):           # request succeeded at the HTTP layer
        return self.status in ("ok", "empty")

    @property
    def has_data(self):
        return self.status == "ok" and len(self.rows) > 0

    @property
    def is_server_error(self):
        return self.status == "server_error"


# Cloudflare-specific 5xx codes (origin unreachable / TLS / timeout) on top of
# the normal 500-504. All are transient transport problems -> retry.
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504,
                  520, 521, 522, 523, 524, 525, 526, 527, 530}


def request_with_retry(params: dict, max_retries: int = 7,
                       base_wait: float = 2.0, cap: float = 45.0,
                       timeout: int = 45) -> FetchResult:
    """GET with patient, jittered exponential backoff.

    Retries every transient transport failure: all 5xx (including Cloudflare's
    520-527 family that show up when their edge cannot reach the DataUSA
    origin), 408/425/429, read timeouts, and connection/SSL errors. Returns a
    FetchResult so the caller can tell 'no data' from 'server failed'.
    """
    last_detail = ""
    last_http = None
    for attempt in range(max_retries):
        try:
            r = SESSION.get(BASE_URL, params=params, timeout=timeout)
            last_http = r.status_code
            if r.status_code == 200:
                try:
                    rows = r.json().get("data", [])
                except ValueError as e:
                    # 200 but unparseable body -> treat as transient
                    last_detail = f"bad JSON: {e}"
                    log.warning("      200 but invalid JSON, retrying: %s", e)
                else:
                    return FetchResult("ok" if rows else "empty",
                                       rows=rows, http=200)
            elif r.status_code in RETRYABLE_HTTP:
                wait = _backoff(base_wait, attempt, cap)
                last_detail = _short_err(r)
                log.warning("      server %s (%s), retry in %.0fs "
                            "(attempt %d/%d)", r.status_code, last_detail,
                            wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            else:
                # 4xx other than 408/425/429 -> our request is wrong, no retry
                detail = _short_err(r)
                log.info("      client error %s: %s", r.status_code, detail)
                return FetchResult("client_error", http=r.status_code,
                                   detail=detail)
        except (requests.Timeout, requests.ConnectionError) as e:
            wait = _backoff(base_wait, attempt, cap)
            last_detail = type(e).__name__
            log.warning("      %s, retry in %.0fs (attempt %d/%d)",
                        last_detail, wait, attempt + 1, max_retries)
            time.sleep(wait)
        except requests.RequestException as e:
            log.error("      request error: %s", e)
            return FetchResult("server_error", http=last_http, detail=str(e))
    log.error("      gave up after %d attempts (last: %s)",
              max_retries, last_detail or last_http)
    return FetchResult("server_error", http=last_http,
                       detail=last_detail or "retries exhausted")


def _backoff(base, attempt, cap):
    return min(base * (2 ** attempt), cap) * (0.7 + 0.6 * random.random())


def _short_err(resp) -> str:
    """Pull a compact reason out of a non-200 body (Cloudflare JSON or HTML)."""
    try:
        j = resp.json()
        return str(j.get("error_name") or j.get("title") or j.get("detail")
                   or "")[:120]
    except ValueError:
        return resp.text.strip().replace("\n", " ")[:120]


# --- query builders --------------------------------------------------------

def build_include(geo_level: str, geo_id: str, year=None) -> str:
    inc = f"{geo_level}:{geo_id}"
    if year is not None:
        inc += f";Year:{year}"
    return inc


def _params(geo_level, geo_id, spec, year=None, with_extra=True, latest=False):
    dd = [geo_level, "Year"] + (spec["extra_dd"] if with_extra else [])
    p = {
        "cube": spec["cube"],
        "drilldowns": ",".join(dd),
        "measures": ",".join(spec["measures"]),
        "include": build_include(geo_level, geo_id, year),
    }
    if latest:
        p["time"] = "Year.latest"
    return p


# --- availability probe (does NOT gate on server errors) -------------------

def check_availability(geo_id: str, geo_level: str) -> dict:
    """Probe each cube for the latest year.

    Returns {key: status} where status is "available" (200+rows),
    "no_data" (200+0 rows), or "server_error" (could not reach origin).
    Crucially, "server_error" does NOT mean the cube is unavailable - the
    caller still attempts the real pull. Only "no_data" / client errors cause
    a skip.
    """
    log.info("  Availability check:")
    status = {}
    for key, spec in CUBES.items():
        res = request_with_retry(_params(geo_level, geo_id, spec, latest=True),
                                 max_retries=5)
        if res.has_data:
            status[key] = "available"
        elif res.status == "empty":
            status[key] = "no_data"
        elif res.status == "client_error":
            status[key] = "no_data"   # e.g. cube/geo combination invalid
        else:
            status[key] = "server_error"
        label = {"available": "available",
                 "no_data": "NOT available (no data)",
                 "server_error": "server error (will still try)"}[status[key]]
        log.info("    %-28s %s", spec["label"], label)
    return status


# --- fetching --------------------------------------------------------------

def fetch_cube_all_years(geo_id, geo_level, spec) -> FetchResult:
    res = request_with_retry(_params(geo_level, geo_id, spec))
    if res.ok:
        log.info("      all-years pull: %d row(s)", len(res.rows))
    else:
        log.info("      all-years pull: %s (%s)", res.status, res.detail)
    return res


def discover_years(geo_id, geo_level, spec) -> list:
    res = request_with_retry(
        _params(geo_level, geo_id, spec, with_extra=False), max_retries=4)
    if res.has_data:
        years = sorted({int(r["Year"]) for r in res.rows if r.get("Year")})
        if years:
            return years
    # Generous ACS 5-year default span when discovery itself can't reach origin
    return list(range(2013, 2024))


def fetch_cube_by_year(geo_id, geo_level, spec, failures, group):
    """One request per year. Returns (rows, all_year_server_error).

    all_year_server_error is True only if EVERY year ended in a server_error
    and zero rows were recovered - i.e. we truly couldn't reach the origin for
    this cube, as opposed to the cube simply having no data.
    """
    years = discover_years(geo_id, geo_level, spec)
    all_rows = []
    any_ok = False
    any_server_error = False
    for yr in years:
        res = request_with_retry(_params(geo_level, geo_id, spec, year=yr))
        if res.ok:
            any_ok = True
            if res.rows:
                all_rows.extend(res.rows)
                log.info("      %s: %d row(s)", yr, len(res.rows))
            else:
                log.info("      %s: no data", yr)
        else:
            any_server_error = True
            log.warning("      %s: %s (%s)", yr, res.status, res.detail)
            failures.append({"geo_id": geo_id, "measure_group": group,
                             "year": yr, "status": res.status,
                             "http": res.http, "detail": res.detail})
        time.sleep(0.3)
    all_year_server_error = (not any_ok) and any_server_error and not all_rows
    return all_rows, all_year_server_error


# --- IO --------------------------------------------------------------------

def save_rows(rows, path, fieldnames=None) -> None:
    if not rows:
        return
    if fieldnames is None:
        fieldnames = sorted({k for row in rows for k in row.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_rows(path) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_master(rows, path) -> None:
    if not rows:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LONG_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def to_long_records(rows, geo_id, geo_name, geo_level, group, spec) -> list:
    out = []
    cat_fields = spec["extra_dd"]
    for r in rows:
        category = (" | ".join(str(r.get(f, "")) for f in cat_fields)
                    if cat_fields else "")
        for m in spec["measures"]:
            if m in r:
                out.append({
                    "geo_id": geo_id, "geo_name": geo_name,
                    "geo_level": geo_level, "year": r.get("Year", ""),
                    "measure_group": group, "category": category,
                    "measure": m, "value": r.get(m),
                })
    return out


# --- per-geography orchestration -------------------------------------------

def fetch_all(geo_id, geo_name="", by_year=False, resume=False,
              master_path=None, failures=None) -> dict:
    """Fetch all three cubes for one geography. Never raises for data/server
    problems; returns a per-cube summary dict for the run report."""
    if failures is None:
        failures = []
    geo_level = detect_geo_level(geo_id)
    label = geo_name or geo_id
    safe = (geo_name or geo_id).replace(",", "").replace(" ", "_")
    os.makedirs(OUT_DIR, exist_ok=True)

    log.info("\n%s", "=" * 64)
    log.info("  DataUSA Fetch - %s", label)
    log.info("  Geo ID: %s   Level: %s   Mode: %s", geo_id, geo_level,
             "year-by-year" if by_year else "all-years")
    log.info("%s", "=" * 64)

    availability = check_availability(geo_id, geo_level)
    combined_long = []
    summary = {}

    for key, spec in CUBES.items():
        raw_path = os.path.join(OUT_DIR, f"{safe}__{key}.csv")

        # --- resume: reuse an already-saved per-cube CSV ---
        if resume and os.path.exists(raw_path):
            cached = load_rows(raw_path)
            if cached:
                log.info("\n  [resume] %s: reusing %d saved row(s) -> %s",
                         spec["label"], len(cached), raw_path)
                combined_long.extend(
                    to_long_records(cached, geo_id, label, geo_level, key, spec))
                summary[key] = ("resumed", len(cached))
                continue

        # --- skip ONLY on genuine no-data, never on a server error ---
        if availability.get(key) == "no_data":
            log.info("\n  Skipping %s (API returned no data for this geo).",
                     spec["label"])
            summary[key] = ("no_data", 0)
            continue

        log.info("\n  Fetching %s ...", spec["label"])
        rows = []
        status = "ok"

        if by_year:
            rows, all_err = fetch_cube_by_year(
                geo_id, geo_level, spec, failures, key)
            if all_err:
                status = "server_error"
        else:
            res = fetch_cube_all_years(geo_id, geo_level, spec)
            if res.has_data:
                rows = res.rows
            elif res.status == "empty":
                # 200 with zero rows on the all-years pull => genuinely no data
                status = "no_data"
            else:
                # server_error (or client_error): fall back to year-by-year,
                # which gives each year its own retry budget and is far more
                # likely to slip through a brief origin up-window.
                log.info("      all-years pull failed (%s); falling back to "
                         "year-by-year ...", res.status)
                rows, all_err = fetch_cube_by_year(
                    geo_id, geo_level, spec, failures, key)
                if not rows and all_err:
                    status = "server_error"
                elif not rows:
                    status = "no_data"

        if rows:
            save_rows(rows, raw_path)
            log.info("      saved %d row(s) -> %s", len(rows), raw_path)
            combined_long.extend(
                to_long_records(rows, geo_id, label, geo_level, key, spec))
            summary[key] = ("ok", len(rows))
        elif status == "server_error":
            log.error("      SERVER ERROR: could not retrieve %s for %s "
                      "(origin unreachable). NOT marking as no-data.",
                      spec["label"], label)
            failures.append({"geo_id": geo_id, "measure_group": key,
                             "year": "ALL", "status": "server_error",
                             "http": "", "detail": "all retries exhausted"})
            summary[key] = ("server_error", 0)
        else:
            log.info("      no data for %s", spec["label"])
            summary[key] = ("no_data", 0)

    # combined per-geo long file (crash-safe, written now)
    if combined_long:
        cpath = os.path.join(OUT_DIR, f"{safe}__combined.csv")
        save_rows(combined_long, cpath, fieldnames=LONG_FIELDS)
        log.info("\n  Combined file -> %s (%d rows)", cpath, len(combined_long))
        if master_path:
            append_master(combined_long, master_path)
    else:
        log.info("\n  No data retrieved for this geography.")

    # availability snapshot (now tri-state and honest)
    save_rows(
        [{"geo_id": geo_id, "geo_name": label, "geo_level": geo_level,
          "measure_group": k, "status": v} for k, v in availability.items()],
        os.path.join(OUT_DIR, f"{safe}__availability.csv"),
    )
    return summary


# --- batch driver ----------------------------------------------------------

# Canonical verification set: one geography at every level the API supports.
EXAMPLES = {
    "United States":      "01000US",
    "Colorado":           "04000US08",
    "West Virginia":      "04000US54",
    "Boulder County, CO": "05000US08013",
    "Boulder city, CO":   "16000US0807850",
}

# 50 states + DC + PR, as (name, FIPS). geo_id is "04000US" + FIPS. Lets you
# run the whole country at State level with one flag (--all-states).
STATE_FIPS = {
    "Alabama": "01", "Alaska": "02", "Arizona": "04", "Arkansas": "05",
    "California": "06", "Colorado": "08", "Connecticut": "09", "Delaware": "10",
    "District of Columbia": "11", "Florida": "12", "Georgia": "13",
    "Hawaii": "15", "Idaho": "16", "Illinois": "17", "Indiana": "18",
    "Iowa": "19", "Kansas": "20", "Kentucky": "21", "Louisiana": "22",
    "Maine": "23", "Maryland": "24", "Massachusetts": "25", "Michigan": "26",
    "Minnesota": "27", "Mississippi": "28", "Missouri": "29", "Montana": "30",
    "Nebraska": "31", "Nevada": "32", "New Hampshire": "33", "New Jersey": "34",
    "New Mexico": "35", "New York": "36", "North Carolina": "37",
    "North Dakota": "38", "Ohio": "39", "Oklahoma": "40", "Oregon": "41",
    "Pennsylvania": "42", "Rhode Island": "44", "South Carolina": "45",
    "South Dakota": "46", "Tennessee": "47", "Texas": "48", "Utah": "49",
    "Vermont": "50", "Virginia": "51", "Washington": "53",
    "West Virginia": "54", "Wisconsin": "55", "Wyoming": "56",
    "Puerto Rico": "72",
}


def all_states():
    return [("04000US" + fips, name) for name, fips in STATE_FIPS.items()]


def parse_geos_file(path) -> list:
    """File of 'geo_id,Display Name' lines (# comments / blanks ignored)."""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",", 1)]
            out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def setup_logging():
    os.makedirs(OUT_DIR, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(ch)
    fh = logging.FileHandler(os.path.join(OUT_DIR, "run.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)


def run_batch(geos, by_year=False, resume=False):
    setup_logging()
    master_path = os.path.join(OUT_DIR, "MASTER_all_geographies.csv")
    if not resume and os.path.exists(master_path):
        os.remove(master_path)   # fresh master unless resuming
    failures = []
    overall = {}
    for geo_id, name in geos:
        try:
            summary = fetch_all(geo_id, name, by_year=by_year, resume=resume,
                                master_path=master_path, failures=failures)
            overall[name or geo_id] = summary
        except Exception as e:
            # A truly unexpected error in one geography must never kill the run.
            log.exception("  FATAL while processing %s (%s): %s",
                          name or geo_id, geo_id, e)
            failures.append({"geo_id": geo_id, "measure_group": "ALL",
                             "year": "ALL", "status": "exception",
                             "http": "", "detail": str(e)})
        time.sleep(0.5)

    if failures:
        fpath = os.path.join(OUT_DIR, "_failures.csv")
        save_rows(failures, fpath,
                  fieldnames=["geo_id", "measure_group", "year",
                              "status", "http", "detail"])
        log.warning("\n  %d failure record(s) logged -> %s",
                    len(failures), fpath)

    _print_report(overall, master_path)
    return overall


def _print_report(overall, master_path):
    log.info("\n%s", "=" * 64)
    log.info("  RUN SUMMARY (rows per measure group)")
    log.info("%s", "=" * 64)
    hdr = f"  {'Geography':<22}{'income':>14}{'insurance':>14}{'race':>10}"
    log.info(hdr)
    for geo, summary in overall.items():
        cells = []
        for key in ("income", "insurance", "race"):
            st, n = summary.get(key, ("-", 0))
            cells.append(f"{n} ({st})" if st != "-" else "-")
        log.info("  %-22s%14s%14s%10s", geo[:22], cells[0], cells[1], cells[2])
    if os.path.exists(master_path):
        total = max(0, sum(1 for _ in open(master_path, encoding="utf-8")) - 1)
        log.info("\n  MASTER -> %s (%d data rows)", master_path, total)
    log.info("%s\n", "=" * 64)


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Fetch DataUSA demographics for U.S. geographies.")
    p.add_argument("--geo", help="Geo ID, or comma-separated list "
                                 "(e.g. 04000US08,04000US54)")
    p.add_argument("--name", default="", help="Display name (single geo only)")
    p.add_argument("--examples", action="store_true",
                   help="Run the canonical Nation/State/County/Place set")
    p.add_argument("--all-states", action="store_true",
                   help="Run all 50 states + DC + PR at State level")
    p.add_argument("--geos-file",
                   help="File of 'geo_id,Display Name' lines for batch runs")
    p.add_argument("--by-year", action="store_true",
                   help="Pull one year per request (slower, more resilient)")
    p.add_argument("--resume", action="store_true",
                   help="Reuse already-saved per-cube CSVs instead of refetching")
    args = p.parse_args()

    if args.examples:
        geos = [(gid, name) for name, gid in EXAMPLES.items()]
    elif args.all_states:
        geos = all_states()
    elif args.geos_file:
        geos = parse_geos_file(args.geos_file)
    elif args.geo:
        ids = [g.strip() for g in args.geo.split(",") if g.strip()]
        if len(ids) == 1:
            geos = [(ids[0], args.name)]
        else:
            geos = [(gid, "") for gid in ids]
    else:
        geos = [("04000US08", "Colorado")]

    run_batch(geos, by_year=args.by_year, resume=args.resume)
