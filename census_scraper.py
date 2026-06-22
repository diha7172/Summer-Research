"""
Census ACS demographic profile scraper
=======================================
Pulls income, race/ethnicity (diversity), health-insurance coverage, and
population straight from the U.S. Census Bureau ACS 5-year API and turns them
into the same analysis-ready percentage profiles as the DataUSA version - but
from the stable government source instead of DataUSA's flaky Tesseract cache.

WHY THIS EXISTS
---------------
DataUSA is just a re-packaging of these exact Census ACS tables. Its origin was
intermittently unreachable (see DIAGNOSIS.md). The Census API is the source of
record and is reliable. Tables used (verified live against the API metadata):

  income     B19001  Household Income (16 brackets)            -> income
  race       B03002  Hispanic or Latino Origin by Race         -> diversity + population
  population B01003  Total Population                           -> population
  insurance  B27010  Types of Health Insurance Coverage by Age -> insurance

GEOGRAPHY
---------
Census-style geo IDs map straight onto Census API geographies:
  01000US            -> us:1                          (Nation)
  04000US<ss>        -> state:<ss>                     (State)
  05000US<ss><ccc>   -> county:<ccc> in state:<ss>     (County)
  16000US<ss><ppppp> -> place:<ppppp> in state:<ss>    (Place / city)

GEOGRAPHIC FALLBACK
-------------------
Same as the DataUSA version: when an area has no data for a year, fill from its
State, then the Nation, labelling source_geo_id / source_geo_level /
is_fallback. Distributions fall back; the fill is always labelled.

API KEY (required)
------------------
The Census API requires a free key (instant signup, ~1 min):
    https://api.census.gov/data/key_signup.html
Provide it via either:
    * environment variable  CENSUS_API_KEY
    * a file  census_key.txt  in this folder (gitignored)

Usage (Windows PowerShell: use `python` or `py`):
    py census_scraper.py --geo 16000US0807850 --name "Boulder city, CO"
    py census_scraper.py --examples
    py census_scraper.py --all-states
    py census_scraper.py --geo 04000US08 --years 2023
    py census_scraper.py --geos-file geos.txt --resume

Outputs (in census_output/): identical schema to the DataUSA build -
    <geo>__profile.csv, MASTER_profiles.csv, <geo>__sources.csv,
    _failures.csv, run.log
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


ACS_URL = "https://api.census.gov/data/{year}/acs/acs5"
OUT_DIR = "census_output"
DEFAULT_YEARS = list(range(2013, 2025))   # 2013..2024; missing years are skipped

PROFILE_FIELDS = [
    "geo_id", "geo_name", "geo_level", "year",
    "measure_group", "category", "count", "percent",
    "source_geo_id", "source_geo_name", "source_geo_level", "is_fallback",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "census-acs-scraper/1.0"})

log = logging.getLogger("census")


# --- API key ---------------------------------------------------------------

def load_api_key():
    k = os.environ.get("CENSUS_API_KEY")
    if k and k.strip():
        return k.strip()
    for path in ("census_key.txt", os.path.expanduser("~/census_key.txt")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
    return None


KEY_HELP = (
    "\nNo Census API key found.\n"
    "  Get a free one (instant): https://api.census.gov/data/key_signup.html\n"
    "  Then set it one of these ways:\n"
    "    PowerShell:  $env:CENSUS_API_KEY = \"your-key-here\"\n"
    "    or save it in a file named  census_key.txt  next to this script.\n"
)


# --- friendly console progress --------------------------------------------

class UI:
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


_UI = None


# --- geography -------------------------------------------------------------

GEO_LEVEL_MAP = {"16000US": "Place", "05000US": "County",
                 "04000US": "State", "01000US": "Nation"}


def detect_geo_level(geo_id):
    for prefix, level in GEO_LEVEL_MAP.items():
        if geo_id.startswith(prefix):
            return level
    return "Place"


def census_geo(geo_id):
    """(level, {for:..., in:...}) for the Census API."""
    level = detect_geo_level(geo_id)
    body = geo_id.split("US", 1)[1] if "US" in geo_id else ""
    if level == "Nation":
        return level, {"for": "us:1"}
    if level == "State":
        return level, {"for": f"state:{body[:2]}"}
    if level == "County":
        return level, {"for": f"county:{body[2:]}", "in": f"state:{body[:2]}"}
    if level == "Place":
        return level, {"for": f"place:{body[2:]}", "in": f"state:{body[:2]}"}
    return level, {"for": "us:1"}


def state_geo_of(geo_id):
    level = detect_geo_level(geo_id)
    if level in ("Place", "County") and "US" in geo_id:
        fips = geo_id.split("US", 1)[1][:2]
        if len(fips) == 2 and fips.isdigit():
            return "04000US" + fips
    return None


def fallback_chain(geo_id):
    chain = [geo_id]
    sg = state_geo_of(geo_id)
    if sg and sg not in chain:
        chain.append(sg)
    if geo_id != "01000US" and "01000US" not in chain:
        chain.append("01000US")
    return chain


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


# --- ACS variable maps (verified live) -------------------------------------

INCOME_TOTAL = "B19001_001E"
INCOME_BINS = [
    ("B19001_002E", "< $10,000"), ("B19001_003E", "$10,000-$14,999"),
    ("B19001_004E", "$15,000-$19,999"), ("B19001_005E", "$20,000-$24,999"),
    ("B19001_006E", "$25,000-$29,999"), ("B19001_007E", "$30,000-$34,999"),
    ("B19001_008E", "$35,000-$39,999"), ("B19001_009E", "$40,000-$44,999"),
    ("B19001_010E", "$45,000-$49,999"), ("B19001_011E", "$50,000-$59,999"),
    ("B19001_012E", "$60,000-$74,999"), ("B19001_013E", "$75,000-$99,999"),
    ("B19001_014E", "$100,000-$124,999"), ("B19001_015E", "$125,000-$149,999"),
    ("B19001_016E", "$150,000-$199,999"), ("B19001_017E", "$200,000+"),
]

RACE_TOTAL = "B03002_001E"
HISPANIC_VAR = "B03002_012E"
RACE_NH = [
    ("B03002_003E", "White Alone"),
    ("B03002_004E", "Black or African American Alone"),
    ("B03002_005E", "American Indian & Alaska Native Alone"),
    ("B03002_006E", "Asian Alone"),
    ("B03002_007E", "Native Hawaiian & Other Pacific Islander Alone"),
    ("B03002_008E", "Some Other Race Alone"),
    ("B03002_009E", "Two or More Races"),
]
POP_VAR = "B01003_001E"

# call A pulls income + race + population in one request
DETAIL_VARS = (["NAME", INCOME_TOTAL] + [v for v, _ in INCOME_BINS]
               + [RACE_TOTAL, HISPANIC_VAR] + [v for v, _ in RACE_NH]
               + [POP_VAR])

# Insurance: B27010 leaf variables -> (types, is_private, is_public, is_uninsured)
INS_TOTAL = "B27010_001E"
INS_LEAVES = {
    "B27010_004E": (["Employer"], True, False, False),
    "B27010_005E": (["Direct-Purchase"], True, False, False),
    "B27010_006E": (["Medicare"], False, True, False),
    "B27010_007E": (["Medicaid"], False, True, False),
    "B27010_008E": (["TRICARE/Military"], False, True, False),
    "B27010_009E": (["VA"], False, True, False),
    "B27010_011E": (["Direct-Purchase", "Employer"], True, False, False),
    "B27010_012E": (["Employer", "Medicare"], True, True, False),
    "B27010_013E": (["Medicaid", "Medicare"], False, True, False),
    "B27010_014E": ([], True, False, False),
    "B27010_015E": ([], False, True, False),
    "B27010_016E": ([], True, True, False),
    "B27010_017E": ([], False, False, True),
    "B27010_020E": (["Employer"], True, False, False),
    "B27010_021E": (["Direct-Purchase"], True, False, False),
    "B27010_022E": (["Medicare"], False, True, False),
    "B27010_023E": (["Medicaid"], False, True, False),
    "B27010_024E": (["TRICARE/Military"], False, True, False),
    "B27010_025E": (["VA"], False, True, False),
    "B27010_027E": (["Direct-Purchase", "Employer"], True, False, False),
    "B27010_028E": (["Employer", "Medicare"], True, True, False),
    "B27010_029E": (["Medicaid", "Medicare"], False, True, False),
    "B27010_030E": ([], True, False, False),
    "B27010_031E": ([], False, True, False),
    "B27010_032E": ([], True, True, False),
    "B27010_033E": ([], False, False, True),
    "B27010_036E": (["Employer"], True, False, False),
    "B27010_037E": (["Direct-Purchase"], True, False, False),
    "B27010_038E": (["Medicare"], False, True, False),
    "B27010_039E": (["Medicaid"], False, True, False),
    "B27010_040E": (["TRICARE/Military"], False, True, False),
    "B27010_041E": (["VA"], False, True, False),
    "B27010_043E": (["Direct-Purchase", "Employer"], True, False, False),
    "B27010_044E": (["Employer", "Medicare"], True, True, False),
    "B27010_045E": (["Direct-Purchase", "Medicare"], True, True, False),
    "B27010_046E": (["Medicaid", "Medicare"], False, True, False),
    "B27010_047E": ([], True, False, False),
    "B27010_048E": ([], False, True, False),
    "B27010_049E": ([], True, True, False),
    "B27010_050E": ([], False, False, True),
    "B27010_053E": (["Employer"], True, False, False),
    "B27010_054E": (["Direct-Purchase"], True, False, False),
    "B27010_055E": (["Medicare"], False, True, False),
    "B27010_056E": (["TRICARE/Military"], False, True, False),
    "B27010_057E": (["VA"], False, True, False),
    "B27010_059E": (["Direct-Purchase", "Employer"], True, False, False),
    "B27010_060E": (["Employer", "Medicare"], True, True, False),
    "B27010_061E": (["Direct-Purchase", "Medicare"], True, True, False),
    "B27010_062E": (["Medicaid", "Medicare"], False, True, False),
    "B27010_063E": ([], True, False, False),
    "B27010_064E": ([], False, True, False),
    "B27010_065E": ([], True, True, False),
    "B27010_066E": ([], False, False, True),
}
TYPE_GROUP = {"Employer": "Private", "Direct-Purchase": "Private",
              "Medicare": "Public", "Medicaid": "Public",
              "TRICARE/Military": "Public", "VA": "Public"}
TYPE_ORDER = ["Employer", "Direct-Purchase", "Medicare", "Medicaid",
              "TRICARE/Military", "VA"]


# --- request with retry ----------------------------------------------------

class Result:
    def __init__(self, status, rows=None, http=None, detail=""):
        self.status = status          # ok | empty | server_error | key_error
        self.rows = rows or []
        self.http = http
        self.detail = detail

    @property
    def ok(self):
        return self.status == "ok"


RETRYABLE = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def census_request(year, get, geo_params, key, max_retries=4,
                   base_wait=2.0, cap=30.0, timeout=60):
    url = ACS_URL.format(year=year)
    params = {"get": get, **geo_params}
    if key:
        params["key"] = key
    last = ""
    for attempt in range(max_retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and "json" in ct:
                arr = r.json()
                header = arr[0]
                rows = [dict(zip(header, rec)) for rec in arr[1:]]
                return Result("ok" if rows else "empty", rows=rows, http=200)
            if r.status_code == 200:
                # HTML body -> almost always a missing/invalid key page
                if "key" in r.text.lower():
                    return Result("key_error", http=200,
                                  detail="missing or invalid Census API key")
                return Result("empty", http=200, detail="non-JSON 200")
            if r.status_code in (204, 400, 404):
                # no data / unsupported geo / year not available -> not an error
                return Result("empty", http=r.status_code,
                              detail=r.text[:80])
            if r.status_code in RETRYABLE:
                last = f"HTTP {r.status_code}"
                wait = _backoff(base_wait, attempt, cap)
                if _UI:
                    _UI.note(f"{last}, retry {attempt+1}/{max_retries}")
                log.debug("   %s retry in %.0fs (%d/%d)", last, wait,
                          attempt + 1, max_retries)
                time.sleep(wait)
                continue
            return Result("empty", http=r.status_code, detail=r.text[:80])
        except (requests.Timeout, requests.ConnectionError) as e:
            last = type(e).__name__
            wait = _backoff(base_wait, attempt, cap)
            if _UI:
                _UI.note(f"{last}, retry {attempt+1}/{max_retries}")
            log.debug("   %s retry in %.0fs (%d/%d)", last, wait,
                      attempt + 1, max_retries)
            time.sleep(wait)
        except requests.RequestException as e:
            return Result("server_error", detail=str(e))
    return Result("server_error", detail=last or "retries exhausted")


def _backoff(base, attempt, cap):
    return min(base * (2 ** attempt), cap) * (0.7 + 0.6 * random.random())


# --- fetch one geo-year (cached) -------------------------------------------

_CACHE = {}      # (geo_id, year) -> flat dict | None
_KEY_ERROR = []  # set once if the key is bad, to stop the run cleanly


def get_geo_year(geo_id, year, key, failures):
    ck = (geo_id, year)
    if ck in _CACHE:
        return _CACHE[ck]
    level, gp = census_geo(geo_id)

    a = census_request(year, ",".join(DETAIL_VARS), gp, key)
    if a.status == "key_error":
        _KEY_ERROR.append(a.detail)
        raise KeyError(a.detail)
    if not a.ok:
        if a.status == "server_error":
            failures.append({"geo_id": geo_id, "year": year, "table": "detail",
                             "status": a.status, "http": a.http,
                             "detail": a.detail})
        _CACHE[ck] = None
        return None
    data = dict(a.rows[0])

    b = census_request(year, "group(B27010)", gp, key)
    if b.status == "key_error":
        _KEY_ERROR.append(b.detail)
        raise KeyError(b.detail)
    if b.ok:
        data.update(b.rows[0])
    elif b.status == "server_error":
        failures.append({"geo_id": geo_id, "year": year, "table": "B27010",
                         "status": b.status, "http": b.http, "detail": b.detail})

    _CACHE[ck] = data
    return data


# --- value parsing ---------------------------------------------------------

def cval(data, key):
    """Numeric value or None. Census jam/suppression codes are large negatives."""
    x = data.get(key)
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f < 0 else f


# --- derived profile builders (per geo-year) -------------------------------

def build_population_diversity(data, year):
    out = []
    total = cval(data, RACE_TOTAL)
    if not total:
        return out
    pop = cval(data, POP_VAR) or total
    out.append((year, "population", "Total Population", pop, None))
    hisp = cval(data, HISPANIC_VAR) or 0.0
    out.append((year, "diversity", "Hispanic (Any Race)",
                hisp, 100.0 * hisp / total))
    for var, name in RACE_NH:
        cnt = cval(data, var) or 0.0
        out.append((year, "diversity", f"{name} (Non-Hispanic)",
                    cnt, 100.0 * cnt / total))
    return out


def build_income(data, year):
    out = []
    total = cval(data, INCOME_TOTAL)
    if not total:
        return out
    for var, label in INCOME_BINS:
        cnt = cval(data, var) or 0.0
        out.append((year, "income", label, cnt, 100.0 * cnt / total))
    return out


def build_insurance(data, year):
    out = []
    total = cval(data, INS_TOTAL)
    if not total:
        return out
    types = defaultdict(float)
    private = public = uninsured = 0.0
    seen_any = False
    for var, (tlist, is_priv, is_pub, is_unins) in INS_LEAVES.items():
        cnt = cval(data, var)
        if cnt is None:
            continue
        seen_any = True
        for t in tlist:
            types[t] += cnt
        if is_unins:
            uninsured += cnt
        else:
            if is_priv:
                private += cnt
            if is_pub:
                public += cnt
    if not seen_any:
        return out
    for t in TYPE_ORDER:
        if t in types:
            out.append((year, "insurance", f"{t} ({TYPE_GROUP[t]})",
                        types[t], 100.0 * types[t] / total))
    out.append((year, "insurance", "GROUP: Private", private,
                100.0 * private / total))
    out.append((year, "insurance", "GROUP: Public", public,
                100.0 * public / total))
    out.append((year, "insurance", "GROUP: Uninsured", uninsured,
                100.0 * uninsured / total))
    return out


BUILDERS = [build_population_diversity, build_income, build_insurance]
GROUP_KEYS = ("population", "diversity", "income", "insurance")


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


# --- per-geography orchestration -------------------------------------------

def fetch_all(geo_id, geo_name, key, years, resume, master_profile, failures):
    geo_level = detect_geo_level(geo_id)
    label = geo_name or geo_display(geo_id)
    safe = label.replace(",", "").replace(" ", "_")
    os.makedirs(OUT_DIR, exist_ok=True)

    profile_path = os.path.join(OUT_DIR, f"{safe}__profile.csv")
    if resume and os.path.exists(profile_path):
        if _UI:
            _UI.milestone(f"{label}: already done (resume)")
            _UI.advance()
        return {"_resumed": True}

    log.info("=== %s (%s) ===", label, geo_id)
    chain = fallback_chain(geo_id)
    profile_rows = []
    sources = []
    summary = {}
    src_used = defaultdict(int)

    if _UI:
        _UI.set_step("fetching")

    for year in years:
        data = None
        src = None
        for cand in chain:
            data = get_geo_year(cand, year, key, failures)
            if data:
                src = cand
                break
        if not data:
            continue
        is_fb = src != geo_id
        src_name = label if not is_fb else geo_display(src)
        src_level = detect_geo_level(src)
        src_used[(src_level, is_fb)] += 1
        for builder in BUILDERS:
            for (yr, mg, cat, cnt, pct) in builder(data, year):
                profile_rows.append({
                    "geo_id": geo_id, "geo_name": label, "geo_level": geo_level,
                    "year": yr, "measure_group": mg, "category": cat,
                    "count": round(cnt, 2),
                    "percent": "" if pct is None else round(pct, 4),
                    "source_geo_id": src, "source_geo_name": src_name,
                    "source_geo_level": src_level, "is_fallback": is_fb})

    if profile_rows:
        save_rows(profile_rows, profile_path, fieldnames=PROFILE_FIELDS)
        if master_profile:
            append_master(profile_rows, master_profile, PROFILE_FIELDS)
        # summary counts (latest year present)
        latest = max(r["year"] for r in profile_rows)
        for g in GROUP_KEYS:
            n = sum(1 for r in profile_rows
                    if r["measure_group"] == g and r["year"] == latest)
            fb = any(r["is_fallback"] for r in profile_rows
                     if r["measure_group"] == g and r["year"] == latest)
            summary[g] = ("fallback" if fb else "self", n)
        nfb = sum(v for (lvl, fb), v in src_used.items() if fb)
        if _UI:
            tag = f" ({nfb} yr(s) via fallback)" if nfb else ""
            _UI.milestone(f"saved {os.path.basename(profile_path)} "
                          f"({len(profile_rows)} rows){tag}")
    else:
        if _UI:
            _UI.milestone(f"{label}: no data")
        for g in GROUP_KEYS:
            summary[g] = ("none", 0)

    for cand in chain:
        sources.append({"geo_id": geo_id, "geo_name": label,
                        "source_geo_id": cand,
                        "source_geo_level": detect_geo_level(cand),
                        "years_used": src_used.get(
                            (detect_geo_level(cand), cand != geo_id), 0)})
    save_rows(sources, os.path.join(OUT_DIR, f"{safe}__sources.csv"),
              fieldnames=["geo_id", "geo_name", "source_geo_id",
                          "source_geo_level", "years_used"])
    if _UI:
        _UI.advance()
    return summary


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
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",", 1)]
            out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def parse_years(spec):
    if not spec:
        return DEFAULT_YEARS
    years = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            years.update(range(int(a), int(b) + 1))
        elif part:
            years.add(int(part))
    return sorted(years)


def setup_logging():
    os.makedirs(OUT_DIR, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fh = logging.FileHandler(os.path.join(OUT_DIR, "run.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)


def available_years(candidate, key, failures):
    """Probe the nation once to learn which ACS5 years exist (cheap, cached)."""
    avail = []
    for y in candidate:
        if get_geo_year("01000US", y, key, failures) is not None:
            avail.append(y)
    return avail or candidate


def run_batch(geos, years_spec=None, resume=False):
    global _UI
    setup_logging()
    key = load_api_key()
    if not key:
        print(KEY_HELP)
        return {}
    master_profile = os.path.join(OUT_DIR, "MASTER_profiles.csv")
    if not resume and os.path.exists(master_profile):
        os.remove(master_profile)

    failures = []
    overall = {}
    candidate = parse_years(years_spec)
    print(f"Census ACS scraper - {len(geos)} geograph"
          f"{'y' if len(geos) == 1 else 'ies'} "
          f"(detailed log -> {os.path.join(OUT_DIR, 'run.log')})")
    try:
        years = available_years(candidate, key, failures)
    except KeyError:
        print("\nERROR: " + (_KEY_ERROR[-1] if _KEY_ERROR else "bad key"))
        print(KEY_HELP)
        return {}
    print(f"Years available: {years[0]}-{years[-1]} ({len(years)})")

    _UI = UI(total_units=len(geos))
    for i, (geo_id, name) in enumerate(geos, 1):
        _UI.start_geo(i, len(geos), name or geo_display(geo_id))
        try:
            overall[name or geo_display(geo_id)] = fetch_all(
                geo_id, name, key, years, resume, master_profile, failures)
        except KeyError:
            _UI.close()
            print("\nERROR: " + (_KEY_ERROR[-1] if _KEY_ERROR else "bad key"))
            print(KEY_HELP)
            return overall
        except Exception as e:
            log.exception("FATAL %s (%s): %s", name or geo_id, geo_id, e)
            _UI.milestone(f"ERROR: {name or geo_id} ({type(e).__name__})")
            failures.append({"geo_id": geo_id, "year": "ALL", "table": "ALL",
                             "status": "exception", "http": "", "detail": str(e)})
        time.sleep(0.1)
    _UI.close()
    _UI = None

    if failures:
        save_rows(failures, os.path.join(OUT_DIR, "_failures.csv"),
                  fieldnames=["geo_id", "year", "table", "status", "http",
                              "detail"])
    _print_report(overall, master_profile, len(failures))
    return overall


def _print_report(overall, master_profile, n_failures):
    line = "=" * 74
    out = ["", line,
           "  RUN SUMMARY  (latest-year category counts; data source)",
           line,
           "  %-22s%13s%13s%13s%13s" % ("Geography", *GROUP_KEYS)]
    for geo, summ in overall.items():
        if summ.get("_resumed"):
            out.append("  %-22s%s" % (geo[:22], "  (resumed)"))
            continue
        cells = []
        for k in GROUP_KEYS:
            st, n = summ.get(k, ("-", 0))
            cells.append(f"{n} {st}" if st != "-" else "-")
        out.append("  %-22s%13s%13s%13s%13s" % (geo[:22], *cells))
    if os.path.exists(master_profile):
        total = max(0, sum(1 for _ in open(master_profile, encoding="utf-8")) - 1)
        out.append(f"\n  MASTER_profiles.csv -> {total} rows  (in {OUT_DIR}/)")
    if n_failures:
        out.append(f"  {n_failures} request failure(s) -> {OUT_DIR}/_failures.csv")
    out.append(line)
    print("\n".join(out))


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Fetch Census ACS demographic profiles for U.S. geographies.")
    p.add_argument("--geo", help="Geo ID or comma-separated list")
    p.add_argument("--name", default="", help="Display name (single geo only)")
    p.add_argument("--examples", action="store_true",
                   help="Run the Nation/State/County/Place sample set")
    p.add_argument("--all-states", action="store_true",
                   help="All 50 states + DC + PR at State level")
    p.add_argument("--geos-file", help="File of 'geo_id,Display Name' lines")
    p.add_argument("--years", help="e.g. 2023 or 2019-2023 or 2019,2021,2023 "
                                   "(default: all available 2013-2024)")
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

    run_batch(geos, years_spec=args.years, resume=args.resume)
