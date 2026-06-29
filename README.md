# U.S. demographics: scrapers + searchable web app

This repo has two things:

1. **A searchable web app** — type any city, county, state, or the nation and
   see its population, age structure, sex, income, diversity, health insurance,
   and key socioeconomic indicators (median age, median household income,
   poverty rate, education, disability rate) profile. Covers **every
   ACS geography (~35,000)**. See "Web app" below.
2. **Command-line scrapers** that produce the underlying CSV profiles for a
   modeling pipeline (`census_scraper.py`, and the older `datausa_scraper.py`).

---

## Web app (search any place)

Zero install beyond Python. You need a free Census API key
(https://api.census.gov/data/key_signup.html) in `CENSUS_API_KEY` or
`census_key.txt`.

```powershell
py census_bulk.py     # pull every geography (~35k) for 2013/2018/2024 into webapp/data/  (~3-4 min, one time)
py serve.py           # opens http://localhost:8000 in your browser
```

Then just search — `Boulder city, Colorado`, `Cook County`, `Texas`,
`United States`. Use the arrow keys + Enter. Switch the **Year** (2013 / 2018 /
2024) to see a decade of change, and **+ Compare another place** to view two
geographies side by side. By default `census_bulk.py` pulls those three years;
change them with `--years` (e.g. `py census_bulk.py --years 2019-2024` or
`--years 2024`). The generated `webapp/data/` is gitignored (it's large and
regenerates from the API).

### Single-file version to share (no setup for the recipient)

To hand someone a copy they can just **double-click** — no Python, no server,
no API key, works offline:

```powershell
py census_bulk.py          # if you haven't already
py build_standalone.py     # -> Demographics_Explorer.html  (~18 MB, all 35k geographies x 3 years embedded)
```

`Demographics_Explorer.html` is one self-contained file (data is gzip+base64
embedded and decompressed in the browser). Email it / drop it on a shared
drive; the recipient opens it in any modern browser (Chrome/Edge/Firefox/
Safari). Great for a quick demo / review.

### What the web app shows (data dictionary)

**Source:** U.S. Census Bureau, American Community Survey (ACS) **5-year**
estimates, pulled directly from `api.census.gov`. Three snapshot years:
**2013, 2018, 2024** (switchable in the app).

**Geographic coverage (35,093 areas):**

| Level | Count | What it is |
|---|---|---|
| Nation | 1 | the whole United States |
| State | 52 | **50 states + District of Columbia + Puerto Rico** (DC & PR are *not* states) |
| County | 3,222 | **counties & county-equivalents** (incl. Louisiana parishes, Alaska boroughs, independent cities, PR municipios) |
| Place | 31,818 | **cities, towns & Census Designated Places (CDPs)** — the Census term is "places"; CDPs are named unincorporated communities |

**Measures shown for each place / year** (all percentages are of that place's
own population or households, so they're comparable across geographies):

| Measure | Meaning | ACS table |
|---|---|---|
| **Population** | total residents | B01003 |
| **Age** | share in each of 8 brackets (Under 18 … 75+) | B01001 |
| **Sex** | % female / % male — **self-reported binary sex as the Census collects it**, not gender identity | B01001 |
| **Diversity** | 5 groups: **Hispanic** (any race) + non-Hispanic **White**, **Black**, **Asian**, and **Other / Multiracial** | B03002 |
| **Household income** | share of households in each of 16 brackets (`< $10,000` … `$200,000+`) | B19001 |
| **Health insurance** | **Private**, **Public**, **Uninsured** + the 6 coverage types | B27010 |
| **Median age** | median age in years | B01002 |
| **Median household income** | median in dollars | B19013 |
| **Below poverty line** | % of people below the federal poverty level | B17001 |
| **Bachelor's degree or higher** | % of adults 25+ with a 4-year degree or more | B15003 |
| **With a disability** | % of people with one or more disabilities | B18101 |

Notes:

* **"Other / Multiracial"** combines four small non-Hispanic groups — American
  Indian & Alaska Native, Native Hawaiian & Pacific Islander, Some Other Race,
  and Two or More Races. Income, diversity, and age each sum to ~100%.
* **Insurance** — `Uninsured` is an exclusive share, but `Private` and `Public`
  **overlap** (a person can have both, e.g. employer + Medicare), so those and
  the per-type bars need not sum to 100%.
* **State-level failsafe** — a small place with no ACS data for a given year
  shows its **state's** figures instead (then the nation as a last resort),
  clearly labelled in the app, with population blanked. So no searchable place
  ever comes up empty.

---

## DataUSA demographic profile scraper

Pulls income, health-insurance coverage, and race/ethnicity from the DataUSA
Tesseract API for any U.S. geography (Nation / State / County / Place) and turns
them into analysis-ready percentage **profiles** for building a synthetic
population. Resilient to the DataUSA origin's intermittent outages, with a
geographic fallback that fills missing areas from their state or the nation.

See `DIAGNOSIS.md` for the root-cause write-up of the old state-level "500s".

## Install

```powershell
pip install requests
```

## Run

```powershell
# one geography
py datausa_scraper.py --geo 16000US0807850 --name "Boulder city, CO"

# the canonical Nation/State/County/Place sample set
py datausa_scraper.py --examples

# every state + DC + PR at State level (the national baseline)
py datausa_scraper.py --all-states

# your own batch list, only filling what isn't already done
py datausa_scraper.py --geos-file geos.txt --resume

# several ids at once
py datausa_scraper.py --geo 04000US08,04000US54
```

Geography IDs are Census-style: `01000US` = Nation, `04000US<ss>` = State,
`05000US<ssccc>` = County, `16000US<ssppppp>` = Place/city.

A `geos.txt` batch file is one `geo_id,Display Name` per line; `#` comments and
blank lines are ignored:

```
01000US,United States
04000US08,Colorado
05000US08013,Boulder County, CO
16000US0807850,Boulder city, CO
```

### Flags
| flag | meaning |
|---|---|
| `--geo` | one geo id, or a comma-separated list |
| `--name` | display name (single geo) |
| `--examples` | run the built-in Nation/State/County/Place set |
| `--all-states` | all 50 states + DC + PR at State level |
| `--geos-file` | batch list file |
| `--by-year` | one request per year (slower, more resilient on a flaky origin) |
| `--resume` | skip geographies whose `*__profile.csv` already exists |

## Output (in `datausa_output/`)

| file | what |
|---|---|
| **`MASTER_profiles.csv`** | **the deliverable** — every geography's profile, stitched |
| `<geo>__profile.csv` | one geography's profile |
| `<geo>__<group>.csv` | raw API rows (only when the geo has its own data) |
| `<geo>__sources.csv` | where each measure group's data came from |
| `_failures.csv` | every request that failed, with reason |
| `run.log` | full detail incl. retries (console stays clean) |

### Profile schema (`MASTER_profiles.csv`)

```
geo_id, geo_name, geo_level, year, measure_group, category,
count, percent, source_geo_id, source_geo_name, source_geo_level, is_fallback
```

`measure_group` is one of:

* **population** — total population (derived from the race cube; the dedicated
  population cube returns empty on this API).
* **diversity** — `Hispanic (Any Race)` + each race as `… (Non-Hispanic)`, `percent` of population.
* **insurance** — each coverage member **and** `GROUP: Private/Public/Uninsured`
  (Private = Employer + Direct Purchase; Public = Medicare + Medicaid + VA +
  Military; Uninsured), `percent` of the covered universe.
* **income** — all 16 household-income brackets, `percent` of households.

The **`percent`** column is what feeds the synthetic population. Within a
geography/year, the non-`GROUP` categories of each group sum to 100%.

### Important: `is_fallback`

When the API can't return an area's own data, the row is **filled from its state,
then the nation**, and `is_fallback=True` with `source_geo_*` showing where the
numbers actually came from. These are honestly labelled, never passed off as the
area's own measurement.

Because the DataUSA origin is intermittently down, some areas will come back as
fallback. **Re-run later with `--resume`** to replace those with real local data
once the origin recovers — it only fetches what's missing and keeps everything
already done.
