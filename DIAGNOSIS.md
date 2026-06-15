# State-level "500" diagnosis & fix

## TL;DR

The state-level failures were **not** caused by the query being too large, and
**not** by anything state-specific. They were caused by the DataUSA Tesseract
**origin server being intermittently unreachable behind Cloudflare**. The
"State fails / County works" pattern you saw was **Cloudflare edge caching**:
County/Place URLs you'd already pulled were served from cache (HTTP 200), while
fresh State URLs were cache misses that had to reach the sick origin and came
back 5xx.

Your row-count hypothesis is **disproven by the evidence** (below). I'm telling
you straight: the heavy-query theory was wrong.

## Evidence (live API, hit directly)

All numbers are success counts over repeated identical attempts, interleaved in
the same time window:

| Query | Success | Notes |
|---|---|---|
| State income — **heavy** (all years + bucket) | 0/15 | |
| State income — latest year + bucket | 0/15 | |
| State income — **light** (no bucket, 1 year, 16 rows) | 0/15 | same as heavy → size is irrelevant |
| County race (the "known-good" task URL) | 15/15 → then 525 | worked until its cache entry expired |
| income @ Nation / State / County / Place | 0/6 each | income failed at **every** level, incl. County |
| race @ County | 6/6 | only this stayed up |
| race @ State | 0/6 | |

Error codes seen (cycling, never identical): **525** (`ssl_handshake_failed` —
Cloudflare↔origin TLS failed), **502** (`origin_bad_gateway`), **500**, and
read timeouts.

Two facts nail the root cause:

1. The **lightest** state query (16 rows) failed exactly as often as the
   heaviest. Row count is not the variable.
2. The "known-good" County race URL returned 200 on every attempt for several
   minutes — then, the moment its Cloudflare cache entry expired, it **also**
   started returning 525. It was a cache hit the whole time; it never touched
   the origin until the cache lapsed.

A `525` is a Cloudflare-to-origin TLS handshake error. It is physically
impossible for it to depend on your `drilldowns`/`measures` — it happens before
the origin ever parses the query. That alone rules out "the big query chokes
the backend."

## Why it looked state-specific to you

You'd run County/Place pulls before, so those exact URLs were warm in
Cloudflare's edge cache and returned 200 instantly. Your State URLs were new
(cache misses) and had to reach the origin, which was flaky — so they 5xx'd.
Same origin, same code; only the cache state differed.

## The real bug in the old script

`check_availability()` treated **any failed request** as "NOT available" and
then **skipped the cube**. So a transient origin 5xx during the probe made the
script declare "State income: NOT available" and silently produce no data —
exactly the "fixable 500 masquerading as no-data" you flagged. The auto-
fallback also fired on *empty* results, conflating "200 with zero rows"
(genuinely no data) with "request failed".

## The fix (datausa_scraper.py v3)

1. **Retry every transient transport failure**, not just 500: all 5xx
   **including Cloudflare's 520–527 family**, plus 408/425/429, read timeouts,
   and connection/SSL errors. Patient, jittered, capped exponential backoff.
2. **Tri-state result** for every call so we never confuse outcomes:
   `ok` (200+rows) · `empty` (200+0 rows = genuinely no data) ·
   `server_error` (retries exhausted) · `client_error` (4xx, e.g. bad cube).
3. **Availability probe no longer gates on server errors.** A cube is skipped
   **only** when the API positively answers 200-with-zero-rows (or a 4xx). A
   `server_error` during the probe → we still attempt the real pull.
4. **Year-by-year is a recovery path for `server_error`** (each year gets its
   own retry budget and is more likely to be individually cacheable / to slip
   through a brief origin up-window), not a response to genuine emptiness.
5. **Unattended-batch hardening:** one geo/cube failing never crashes the run;
   each per-cube CSV is written immediately; the MASTER long file is appended
   after each geography; `--resume` reuses saved CSVs; `_failures.csv` and
   `run.log` record exactly which geo/cube/year failed and whether it was a
   `server_error` (retry later) or genuine `no_data`.

Net effect: identical schema at Nation / State / County / Place; a flaky origin
now degrades to "logged server_error, retry later," never to "silently empty"
or "not available."

## v4 additions (profiles, geographic fallback, friendly progress)

On top of the resilient core, v4 produces the analysis-ready output the
synthetic-population pipeline needs and adds quality-of-life features:

* **Derived percentage profiles** (`<geo>__profile.csv`, `MASTER_profiles.csv`):
  * `population` – total population (derived from the race cube; the dedicated
    `acs_yg_total_population_5` cube returns 200-with-zero-rows here, so it is
    not used).
  * `diversity` – race with **Hispanic lumped** into one "Hispanic (Any Race)"
    group plus each race as "(Non-Hispanic)", as % of population.
  * `insurance` – every coverage member **and** Private / Public / Uninsured
    group totals, as % of the covered universe (Employer+Direct Purchase →
    Private; Medicare+Medicaid+VA+Military → Public; Uninsured).
  * `income` – **all** household-income brackets kept, as % of households.
* **Geographic fallback** – when an area is missing a measure group, fill it
  from its **State**, then the **Nation**, and label the source
  (`source_geo_id`, `source_geo_level`, `is_fallback`). Distributions fall back;
  the fill is always labelled, never passed off as the area's own measurement.
  This is what lets a county/place that the flaky origin can't return still
  yield a usable, clearly-labelled profile.
* **Fast-outage gate** – if the all-years pull 5xx's, one quick latest-year
  probe decides whether to bother with year-by-year recovery (origin reachable)
  or fall back immediately (origin down). No more grinding through ~40 doomed
  requests during a full outage.
* **Friendly console** – a live progress bar with per-geography milestones; all
  the retry/backoff detail goes to `datausa_output/run.log` instead of spamming
  the terminal.

## Operational note

Because the cause is the origin's own uptime, the cure is patience + correct
classification, not a smarter query. If the origin is in a hard outage window,
even a perfect client gets nothing — but this version will (a) keep your
already-fetched data, (b) clearly log what couldn't be reached, and (c) let you
re-run with `--resume` to fill only the gaps once the origin recovers. Running
`--by-year` during a marginal window materially improves the hit rate.
