"""
DataUSA Data Fetcher  (v4 - resilient + derived profiles + geographic fallback)
===============================================================================
Pulls household income, health-insurance coverage, and race/ethnicity from the
DataUSA Tesseract API for any U.S. geography (Nation, State, County, Place) and
turns them into the analysis-ready "profile" the synthetic-population pipeline
needs:

  * population  - total population (derived from the race cube; see note below)
  * diversity   - race breakdown with Hispanic LUMPED into one group:
                  "Hispanic (Any Race)" + each race as "(Non-Hispanic)", % of pop
  * insurance   - the coverage members grouped into Private / Public / Uninsured,
                  plus each member, as % of the covered universe
  * income      - every household-income bracket (all bins kept) as % of households

GEOGRAPHIC FALLBACK (new in v4)
-------------------------------
"Fill in State or National data when data is missing for an area." For each
measure group we walk a fallback chain and use the first geography that has
data:

      Place  ->  its State  ->  Nation
      County ->  its State  ->  Nation
      State  ->  Nation
      Nation ->  (no fallback)

Every profile row carries source_geo_id / source_geo_level / is_fallback, so a
value filled from the state or the nation is clearly labelled and never silently
passed off as the area's own measurement. Percentages (distributions) are what
fall back - using the state's income distribution as a proxy for a county that
the API can't return is exactly what a synthetic population needs, as long as it
is labelled, which it is.

This fallback also makes the scraper robust to the DataUSA origin's intermittent
outages (see DIAGNOSIS.md): if a county/place URL is a cache miss against a sick
origin, the run still produces a usable, clearly-labelled profile from the
state/national distribution instead of an empty row.

WHY POPULATION IS DERIVED FROM THE RACE CUBE
--------------------------------------------
The dedicated population cube `acs_yg_total_population_5` exists in the schema
but returns 200-with-zero-rows for the geographies tested here (a real, cached
empty - the "probes available but returns nothing" trap). The race cube
(B03002) sums to the full population by definition, so we derive population from
it. Verified: US 2024 race-sum = 334,922,508 (correct), Hispanic = 19.3%.

ROOT CAUSE OF THE OLD STATE-LEVEL "500s" (unchanged from v3, see DIAGNOSIS.md)
-----------------------------------------------------------------------------
Not query size and not state-specific: the DataUSA origin is intermittently
unreachable behind Cloudflare (525/502/500/timeouts). County/Place "worked"
only because those URLs were warm in Cloudflare's edge cache; fresh State URLs
were cache misses that hit the sick origin. The lightest state query (16 rows)
failed exactly as often as the heaviest, and the income cube failed at every
level including County. The actionable bug was the old availability probe
treating any failed request as "NOT available" and skipping the cube, turning
fixable 5xx into silent no-data. Fixed via tri-state classification below.

Cube names / measures / dimensions (verified live against /tesseract/cubes):
  income    : acs_yg_household_income_5      "Household Income"  + Household Income Bucket
  insurance : acs_health_coverage_s_5        "Number Covered"    + Health Coverage, Age Group
  race      : acs_ygr_race_with_hispanic_5   "Hispanic Population" + Race, Ethnicity

Year filtering uses include=Year:<yr> (time= only accepts .latest/.oldest/.trailing).

Usage (Windows PowerShell: use `python` or `py`):
    pip install requests
    python datausa_scraper.py --geo 16000US0807850 --name "Boulder city, CO"
    python datausa_scraper.py --examples
    python datausa_scraper.py --all-states
    python datausa_scraper.py --geos-file geos.txt --resume
    python datausa_scraper.py --geo 05000US08013 --name "Boulder County, CO" --by-year

Outputs (in datausa_output/):
    <geo>__profile.csv         tidy population/diversity/insurance/income, % + source
    MASTER_profiles.csv        all geographies' profiles stitched together
    <geo>__<group>.csv         raw API rows for the geo (only when it has its own data)
    <geo>__sources.csv         where each measure group's data came from (honest report)
    _failures.csv, run.log     what failed and why (server_error vs no_data)
"""

import os
import sys
import csv
import time
import random
import argparse
import logging
import requests
from collections import defaultdict


BASE_URL = "https://api.datausa.io/tesseract/data.jsonrecords"
OUT_DIR = "datausa_output"

# Raw long-format schema (per-geo self data; kept for backward compatibility).
LONG_FIELDS = [
    "geo_id", "geo_name", "geo_level", "year",
    "measure_group", "category", "measure", "value",
]

# Derived profile schema - THE deliverable for the synthetic-population pipeline.
PROFILE_FIELDS = [
    "geo_id", "geo_name", "geo_level", "year",
    "measure_group", "category", "count", "percent",
    "source_geo_id", "source_geo_name", "source_geo_level", "is_fallback",
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


# --- friendly console progress (no external deps) --------------------------

class UI:
    """Single-line live progress bar on a real terminal; concise milestone
    lines when output is piped/redirected. All the noisy retry detail goes to
    the run.log file instead of the console (see setup_logging)."""

    BAR = 22

    def __init__(self, total_units, enabled=True):
        self.total = max(1, total_units)
        self.done_units = 0
        self.geo = ""
        self.step = ""
        self.note_txt = ""
        self.tty = enabled and sys.stderr.isatty()
        self.width = 110

    def start_geo(self, idx, total_geos, name):
        self.geo = f"{idx}/{total_geos} {name}"
        self.step = ""
        self.note_txt = ""
        if not self.tty:
            print(f"\n[{idx}/{total_geos}] {name} ...", flush=True)
        self._draw()

    def set_step(self, label):
        self.step = label
        self.note_txt = ""
        self._draw()

    def note(self, txt):
        self.note_txt = txt
        self._draw()

    def advance(self):
        self.done_units += 1
        self.note_txt = ""
        self._draw()

    def milestone(self, txt):
        if self.tty:
            sys.stderr.write("\r" + " " * self.width + "\r")
            sys.stderr.write("   " + txt + "\n")
            self._draw()
        else:
            print("   " + txt, flush=True)

    def _draw(self):
        if not self.tty:
            return
        frac = min(1.0, self.done_units / self.total)
        fill = int(self.BAR * frac)
        bar = "[" + "#" * fill + "-" * (self.BAR - fill) + "]"
        msg = f"\r{bar} {int(frac*100):3d}% | {self.geo}"
        if self.step:
            msg += f" | {self.step}"
        if self.note_txt:
            msg += f" ({self.note_txt})"
        sys.stderr.write(msg[:self.width].ljust(self.width))
        sys.stderr.flush()

    def close(self):
        if self.tty:
            sys.stderr.write("\r" + " " * self.width + "\r")
            sys.stderr.flush()


# Active UI for the current run; request_with_retry pokes it so the bar stays
# alive during backoff sleeps instead of looking frozen.
_UI = None


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
    return "Place"


# The three real data cubes. Population is derived from "race" (see header).
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

# Coverage member -> Private / Public / Uninsured. Military Health Insurance is
# the 7th member (not in the user's "6"); folded into Public so the percentages
# still sum to 100 and no one is dropped.
COVERAGE_GROUP = {
    "Employer": "Private",
    "Direct Purchase": "Private",
    "Medicare": "Public",
    "Medicaid": "Public",
    "Veterans Affairs": "Public",
    "Military Health Insurance": "Public",
    "Uninsured": "Uninsured",
}
COVERAGE_ORDER = ["Employer", "Direct Purchase", "Medicare", "Medicaid",
                  "Veterans Affairs", "Military Health Insurance", "Uninsured"]
GROUP_ORDER = ["Private", "Public", "Uninsured"]

HISPANIC = "Hispanic or Latino"
NOT_HISPANIC = "Not Hispanic or Latino"


# --- tri-state fetch result ------------------------------------------------

class FetchResult:
    """ok (200+rows) / empty (200+0 rows) / server_error / client_error."""

    def __init__(self, status, rows=None, http=None, detail=""):
        self.status = status
        self.rows = rows or []
        self.http = http
        self.detail = detail

    @property
    def ok(self):
        return self.status in ("ok", "empty")

    @property
    def has_data(self):
        return self.status == "ok" and len(self.rows) > 0

    @property
    def is_server_error(self):
        return self.status == "server_error"


RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504,
                  520, 521, 522, 523, 524, 525, 526, 527, 530}


def request_with_retry(params: dict, max_retries: int = 7,
                       base_wait: float = 2.0, cap: float = 45.0,
                       timeout: int = 45) -> FetchResult:
    """GET with patient, jittered backoff. Retries all transient transport
    failures (5xx incl. Cloudflare 520-527, 408/425/429, timeouts, conn/SSL)."""
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
                    last_detail = f"bad JSON: {e}"
                    log.warning("      200 but invalid JSON, retrying: %s", e)
                else:
                    return FetchResult("ok" if rows else "empty",
                                       rows=rows, http=200)
            elif r.status_code in RETRYABLE_HTTP:
                wait = _backoff(base_wait, attempt, cap)
                last_detail = _short_err(r)
                log.debug("      server %s (%s), retry in %.0fs "
                          "(attempt %d/%d)", r.status_code, last_detail,
                          wait, attempt + 1, max_retries)
                if _UI:
                    _UI.note(f"server {r.status_code}, retry {attempt+1}/{max_retries}")
                time.sleep(wait)
                continue
            else:
                detail = _short_err(r)
                log.debug("      client error %s: %s", r.status_code, detail)
                return FetchResult("client_error", http=r.status_code,
                                   detail=detail)
        except (requests.Timeout, requests.ConnectionError) as e:
            wait = _backoff(base_wait, attempt, cap)
            last_detail = type(e).__name__
            log.debug("      %s, retry in %.0fs (attempt %d/%d)",
                      last_detail, wait, attempt + 1, max_retries)
            if _UI:
                _UI.note(f"{last_detail}, retry {attempt+1}/{max_retries}")
            time.sleep(wait)
        except requests.RequestException as e:
            log.debug("      request error: %s", e)
            return FetchResult("server_error", http=last_http, detail=str(e))
    log.debug("      gave up after %d attempts (last: %s)",
              max_retries, last_detail or last_http)
    return FetchResult("server_error", http=last_http,
                       detail=last_detail or "retries exhausted")


def _backoff(base, attempt, cap):
    return min(base * (2 ** attempt), cap) * (0.7 + 0.6 * random.random())


def _short_err(resp) -> str:
    try:
        j = resp.json()
        return str(j.get("error_name") or j.get("title") or j.get("detail")
                   or "")[:120]
    except ValueError:
        return resp.text.strip().replace("\n", " ")[:120]


# --- query builders --------------------------------------------------------

def build_include(geo_level, geo_id, year=None):
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


# --- fetching (with year-by-year recovery) ---------------------------------

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
    return list(range(2013, 2024))


def fetch_cube_by_year(geo_id, geo_level, spec, failures, group):
    """One request per year. Returns (rows, all_year_server_error)."""
    years = discover_years(geo_id, geo_level, spec)
    all_rows, any_ok, any_err = [], False, False
    for yr in years:
        res = request_with_retry(_params(geo_level, geo_id, spec, year=yr))
        if res.ok:
            any_ok = True
            if res.rows:
                all_rows.extend(res.rows)
                log.info("      %s: %d row(s)", yr, len(res.rows))
        else:
            any_err = True
            failures.append({"geo_id": geo_id, "measure_group": group,
                             "year": yr, "status": res.status,
                             "http": res.http, "detail": res.detail})
        time.sleep(0.3)
    return all_rows, ((not any_ok) and any_err and not all_rows)


# --- raw fetch with fallback + cache ---------------------------------------

# (geo_id, group) -> rows. Lets state/nation be fetched once and reused across
# every place/county in a batch.
_RAW_CACHE = {}


def state_geo_of(geo_id):
    """Derive the containing State geo id from a Place/County id, else None."""
    level = detect_geo_level(geo_id)
    if level in ("Place", "County") and "US" in geo_id:
        fips = geo_id.split("US", 1)[1][:2]
        if len(fips) == 2 and fips.isdigit():
            return "04000US" + fips
    return None


def fallback_chain(geo_id):
    """[self, (state), nation] honoring the requested level."""
    chain = [geo_id]
    sg = state_geo_of(geo_id)
    if sg and sg not in chain:
        chain.append(sg)
    if geo_id != "01000US" and "01000US" not in chain:
        chain.append("01000US")
    return chain


def cached_fetch(geo_id, group, spec, failures, by_year=False):
    """Fetch one cube for one geo with the resilient path; cached per run."""
    key = (geo_id, group)
    if key in _RAW_CACHE:
        return _RAW_CACHE[key]
    level = detect_geo_level(geo_id)
    if by_year:
        rows, _ = fetch_cube_by_year(geo_id, level, spec, failures, group)
    else:
        # Probe first: a cheap latest-year request decides quickly whether the
        # origin can serve this geo at all. If it can't (origin down / cache
        # miss against a sick origin), fall back in seconds instead of grinding
        # through the full retry budget on the big all-years query.
        if _UI:
            _UI.note("checking origin")
        probe = request_with_retry(
            _params(level, geo_id, spec, latest=True), max_retries=3)
        if probe.status == "empty":
            rows = []  # 200-with-zero-rows -> genuinely no data at this geo
        elif not probe.ok:
            log.info("      %s %s: origin unreachable (%s); using fallback",
                     geo_id, group, probe.status)
            rows = []
            failures.append({"geo_id": geo_id, "measure_group": group,
                             "year": "ALL", "status": "server_error",
                             "http": probe.http, "detail": probe.detail})
        else:
            # origin answered -> pull every year (recover year-by-year if the
            # bigger all-years query trips a transient error)
            if _UI:
                _UI.note("fetching all years")
            res = fetch_cube_all_years(geo_id, level, spec)
            if res.has_data:
                rows = res.rows
            elif res.status == "empty":
                rows = []
            else:
                if _UI:
                    _UI.note("recovering year-by-year")
                rows, _ = fetch_cube_by_year(geo_id, level, spec, failures, group)
    _RAW_CACHE[key] = rows
    return rows


def fetch_group_with_fallback(geo_id, group, spec, failures, by_year=False):
    """Walk self -> state -> nation; return (rows, source_id, source_level,
    is_fallback). rows is [] only if nothing in the chain has data."""
    for src in fallback_chain(geo_id):
        rows = cached_fetch(src, group, spec, failures, by_year=by_year)
        if rows:
            return rows, src, detect_geo_level(src), (src != geo_id)
    return [], None, None, False


# --- derived profile builders (return rows of dicts) -----------------------

def _years(rows):
    by = defaultdict(list)
    for r in rows:
        by[str(r.get("Year", ""))].append(r)
    return by


def _num(r, key):
    v = r.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_population_diversity(rows):
    """From the race cube: total population + diversity (Hispanic lumped)."""
    out = []
    race_id = {}
    for r in rows:
        race_id.setdefault(r.get("Race"), int(r.get("Race ID", 99) or 99))
    for y, rs in _years(rows).items():
        total = sum(_num(r, "Hispanic Population") for r in rs)
        if total <= 0:
            continue
        out.append((y, "population", "Total Population", total, None))
        hisp = sum(_num(r, "Hispanic Population")
                   for r in rs if r.get("Ethnicity") == HISPANIC)
        out.append((y, "diversity", "Hispanic (Any Race)",
                    hisp, 100.0 * hisp / total))
        byrace = defaultdict(float)
        for r in rs:
            if r.get("Ethnicity") == NOT_HISPANIC:
                byrace[r.get("Race")] += _num(r, "Hispanic Population")
        for race in sorted(byrace, key=lambda x: race_id.get(x, 99)):
            cnt = byrace[race]
            out.append((y, "diversity", f"{race} (Non-Hispanic)",
                        cnt, 100.0 * cnt / total))
    return out


def build_income(rows):
    """Household-income brackets as counts and % of households (all bins kept)."""
    out = []
    for y, rs in _years(rows).items():
        total = sum(_num(r, "Household Income") for r in rs)
        if total <= 0:
            continue
        for r in sorted(rs, key=lambda r: int(r.get("Household Income Bucket ID", 0) or 0)):
            cnt = _num(r, "Household Income")
            out.append((y, "income", r.get("Household Income Bucket"),
                        cnt, 100.0 * cnt / total))
    return out


def build_coverage(rows):
    """Coverage members + Private/Public/Uninsured groups, as % of universe."""
    out = []
    for y, rs in _years(rows).items():
        member = defaultdict(float)
        for r in rs:                       # sum across Age Group
            member[r.get("Health Coverage")] += _num(r, "Number Covered")
        total = sum(member.values())
        if total <= 0:
            continue
        for m in COVERAGE_ORDER:
            if m in member:
                out.append((y, "insurance", f"{m} ({COVERAGE_GROUP.get(m, '?')})",
                            member[m], 100.0 * member[m] / total))
        grp = defaultdict(float)
        for m, c in member.items():
            grp[COVERAGE_GROUP.get(m, "Other")] += c
        for g in GROUP_ORDER:
            if g in grp:
                out.append((y, "insurance", f"GROUP: {g}",
                            grp[g], 100.0 * grp[g] / total))
    return out


# group key -> (source cube key, builder). population+diversity both come from race.
PROFILE_BUILDERS = [
    ("race", build_population_diversity),
    ("income", build_income),
    ("insurance", build_coverage),
]


# --- geo display names -----------------------------------------------------

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
FIPS_NAME = {v: k for k, v in STATE_FIPS.items()}


def geo_display(geo_id):
    if geo_id == "01000US":
        return "United States"
    if geo_id.startswith("04000US"):
        return FIPS_NAME.get(geo_id[len("04000US"):], geo_id)
    return geo_id


def all_states():
    return [("04000US" + fips, name) for name, fips in STATE_FIPS.items()]


# --- IO --------------------------------------------------------------------

def save_rows(rows, path, fieldnames=None):
    if not rows:
        return
    if fieldnames is None:
        fieldnames = sorted({k for row in rows for k in row.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_master(rows, path, fieldnames):
    if not rows:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def to_long_records(rows, geo_id, geo_name, geo_level, group, spec):
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
                    "measure": m, "value": r.get(m)})
    return out


# --- per-geography orchestration -------------------------------------------

def fetch_all(geo_id, geo_name="", by_year=False, resume=False,
              master_long=None, master_profile=None, failures=None) -> dict:
    """Build the full profile for one geography (with fallback). Never raises."""
    if failures is None:
        failures = []
    geo_level = detect_geo_level(geo_id)
    label = geo_name or geo_display(geo_id)
    safe = label.replace(",", "").replace(" ", "_")
    os.makedirs(OUT_DIR, exist_ok=True)

    profile_path = os.path.join(OUT_DIR, f"{safe}__profile.csv")
    if resume and os.path.exists(profile_path):
        log.info("  [resume] %s: profile already exists, skipping", label)
        if _UI:
            _UI.milestone(f"{label}: already done (resume)")
            for _ in range(3):
                _UI.advance()
        return {"_resumed": True}

    log.info("\n%s", "=" * 64)
    log.info("  DataUSA Profile - %s", label)
    log.info("  Geo ID: %s   Level: %s   Mode: %s", geo_id, geo_level,
             "year-by-year" if by_year else "all-years")
    log.info("%s", "=" * 64)

    profile_rows = []
    long_rows = []
    sources = []
    summary = {}

    for cube_key, builder in PROFILE_BUILDERS:
        spec = CUBES[cube_key]
        log.info("\n  %s ...", spec["label"])
        if _UI:
            _UI.set_step(spec["label"])
        rows, src, src_level, is_fb = fetch_group_with_fallback(
            geo_id, cube_key, spec, failures, by_year=by_year)

        if not rows:
            log.warning("      no data for %s anywhere in fallback chain", cube_key)
            if _UI:
                _UI.milestone(f"{cube_key}: no data anywhere (skipped)")
                _UI.advance()
            for g in _groups_from_cube(cube_key):
                summary[g] = ("none", 0)
            sources.append({"geo_id": geo_id, "geo_name": label,
                            "measure_group": cube_key, "source_geo_id": "",
                            "source_geo_level": "", "is_fallback": "",
                            "status": "no_data_anywhere"})
            continue

        src_name = label if src == geo_id else geo_display(src)
        if is_fb:
            log.info("      FILLED from %s (%s) [fallback]", src_name, src_level)
            if _UI:
                _UI.milestone(f"{cube_key}: filled from {src_name} ({src_level}) "
                              f"[fallback]")
        else:
            log.info("      using this geography's own data")
            if _UI:
                _UI.milestone(f"{cube_key}: {len(rows)} rows (own data)")
        if _UI:
            _UI.advance()

        # raw per-cube CSV only when the geography has its OWN data (honest raw)
        if not is_fb:
            save_rows(rows, os.path.join(OUT_DIR, f"{safe}__{cube_key}.csv"))
            long_rows.extend(
                to_long_records(rows, geo_id, label, geo_level, cube_key, spec))

        # derived percentage rows
        derived = builder(rows)
        for (yr, mg, cat, cnt, pct) in derived:
            profile_rows.append({
                "geo_id": geo_id, "geo_name": label, "geo_level": geo_level,
                "year": yr, "measure_group": mg, "category": cat,
                "count": round(cnt, 2),
                "percent": "" if pct is None else round(pct, 4),
                "source_geo_id": src, "source_geo_name": src_name,
                "source_geo_level": src_level, "is_fallback": is_fb})
        for g in _groups_from_cube(cube_key):
            n = sum(1 for d in derived if d[1] == g)
            summary[g] = ("fallback:" + src_level if is_fb else "self", n)
        sources.append({"geo_id": geo_id, "geo_name": label,
                        "measure_group": cube_key, "source_geo_id": src,
                        "source_geo_level": src_level,
                        "is_fallback": is_fb, "status": "ok"})

    # write outputs (crash-safe, per geo)
    if profile_rows:
        save_rows(profile_rows, profile_path, fieldnames=PROFILE_FIELDS)
        log.info("\n  Profile -> %s (%d rows)", profile_path, len(profile_rows))
        if _UI:
            _UI.milestone(f"saved {os.path.basename(profile_path)} "
                          f"({len(profile_rows)} rows)")
        if master_profile:
            append_master(profile_rows, master_profile, PROFILE_FIELDS)
    else:
        log.warning("\n  No profile produced for %s", label)
        if _UI:
            _UI.milestone(f"{label}: no profile produced")
    if long_rows:
        save_rows(long_rows, os.path.join(OUT_DIR, f"{safe}__combined.csv"),
                  fieldnames=LONG_FIELDS)
        if master_long:
            append_master(long_rows, master_long, LONG_FIELDS)
    save_rows(sources, os.path.join(OUT_DIR, f"{safe}__sources.csv"),
              fieldnames=["geo_id", "geo_name", "measure_group", "source_geo_id",
                          "source_geo_level", "is_fallback", "status"])
    return summary


def _groups_from_cube(cube_key):
    return ["population", "diversity"] if cube_key == "race" else [cube_key]


# --- batch driver ----------------------------------------------------------

EXAMPLES = {
    "United States":      "01000US",
    "Colorado":           "04000US08",
    "West Virginia":      "04000US54",
    "Boulder County, CO": "05000US08013",
    "Boulder city, CO":   "16000US0807850",
}


def parse_geos_file(path):
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
    """All detail (including retry noise) goes to run.log. The console shows
    only the friendly progress bar / milestones via the UI object."""
    os.makedirs(OUT_DIR, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fh = logging.FileHandler(os.path.join(OUT_DIR, "run.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)


def run_batch(geos, by_year=False, resume=False):
    global _UI
    setup_logging()
    master_long = os.path.join(OUT_DIR, "MASTER_all_geographies.csv")
    master_profile = os.path.join(OUT_DIR, "MASTER_profiles.csv")
    if not resume:
        for p in (master_long, master_profile):
            if os.path.exists(p):
                os.remove(p)
    failures = []
    overall = {}
    _UI = UI(total_units=len(geos) * 3)
    print(f"Fetching DataUSA profiles for {len(geos)} geograph"
          f"{'y' if len(geos) == 1 else 'ies'} "
          f"(detailed log -> {os.path.join(OUT_DIR, 'run.log')})")
    for i, (geo_id, name) in enumerate(geos, 1):
        _UI.start_geo(i, len(geos), name or geo_display(geo_id))
        try:
            overall[name or geo_display(geo_id)] = fetch_all(
                geo_id, name, by_year=by_year, resume=resume,
                master_long=master_long, master_profile=master_profile,
                failures=failures)
        except Exception as e:
            log.exception("  FATAL while processing %s (%s): %s",
                          name or geo_id, geo_id, e)
            _UI.milestone(f"ERROR: {name or geo_id} failed ({type(e).__name__})")
            failures.append({"geo_id": geo_id, "measure_group": "ALL",
                             "year": "ALL", "status": "exception",
                             "http": "", "detail": str(e)})
        time.sleep(0.3)
    _UI.close()
    _UI = None

    if failures:
        save_rows(failures, os.path.join(OUT_DIR, "_failures.csv"),
                  fieldnames=["geo_id", "measure_group", "year",
                              "status", "http", "detail"])
        log.warning("%d failure record(s) logged -> _failures.csv", len(failures))
    _print_report(overall, master_profile, len(failures))
    return overall


def _print_report(overall, master_profile, n_failures=0):
    line = "=" * 78
    out = ["", line,
           "  RUN SUMMARY  (category rows per measure group; data source in parens)",
           line]
    cols = ("population", "diversity", "insurance", "income")
    abbr = {"self": "self", "none": "none",
            "fallback:Nation": "fb:Nat", "fallback:State": "fb:St"}
    out.append("  %-22s%14s%14s%14s%14s" % ("Geography", *cols))
    for geo, summ in overall.items():
        if summ.get("_resumed"):
            out.append("  %-22s%s" % (geo[:22], "  (resumed)"))
            continue
        cells = []
        for k in cols:
            st, n = summ.get(k, ("-", 0))
            cells.append(f"{n} {abbr.get(st, st)}" if st != "-" else "-")
        out.append("  %-22s%14s%14s%14s%14s" % (geo[:22], *cells))
    if os.path.exists(master_profile):
        total = max(0, sum(1 for _ in open(master_profile, encoding="utf-8")) - 1)
        out.append("")
        out.append(f"  MASTER_profiles.csv -> {total} rows   "
                   f"(per-geo files + run.log in {OUT_DIR}/)")
    if n_failures:
        out.append(f"  {n_failures} request failure(s) logged -> "
                   f"{OUT_DIR}/_failures.csv (server errors, retried/fell back)")
    out.append(line)
    print("\n".join(out))


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Fetch DataUSA demographic profiles for U.S. geographies.")
    p.add_argument("--geo", help="Geo ID or comma-separated list")
    p.add_argument("--name", default="", help="Display name (single geo only)")
    p.add_argument("--examples", action="store_true",
                   help="Run the canonical Nation/State/County/Place set")
    p.add_argument("--all-states", action="store_true",
                   help="Run all 50 states + DC + PR at State level")
    p.add_argument("--geos-file", help="File of 'geo_id,Display Name' lines")
    p.add_argument("--by-year", action="store_true",
                   help="Pull one year per request (slower, more resilient)")
    p.add_argument("--resume", action="store_true",
                   help="Skip geographies whose profile CSV already exists")
    args = p.parse_args()

    if args.examples:
        geos = [(gid, name) for name, gid in EXAMPLES.items()]
    elif args.all_states:
        geos = all_states()
    elif args.geos_file:
        geos = parse_geos_file(args.geos_file)
    elif args.geo:
        ids = [g.strip() for g in args.geo.split(",") if g.strip()]
        geos = [(ids[0], args.name)] if len(ids) == 1 else [(g, "") for g in ids]
    else:
        geos = [("01000US", "United States")]

    run_batch(geos, by_year=args.by_year, resume=args.resume)
