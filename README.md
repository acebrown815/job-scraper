# job-scraper

Scrapes public job postings from seven ATS platforms (Greenhouse, Lever, Ashby,
BambooHR, Workday, iCIMS, Paylocity) plus **Indeed** searches, and upserts them into a
Google Sheet.

Extracted from `../job-board-aggregator` (`scripts/scraper.py`, `geolocation.py`,
and the dedup logic from `merge_data.py`). The website, chunked-gzip output, and
trend/anomaly CI were left behind.

## Setup

```sh
pip install -r requirements.txt
cp config.example.toml config.toml
```

## Google Sheet setup (one-time)

The scraper writes through a Google Apps Script web app bound to your sheet, so
no Google Cloud project or service account is needed.

1. Create a Google Sheet. Open **Extensions > Apps Script**, replace the contents
   of `Code.gs` with [apps_script/Code.gs](apps_script/Code.gs), and save.
2. In the Apps Script editor go to **Project Settings > Script Properties** and add
   `TOKEN` = a long random string (e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"`).
3. **Deploy > New deployment > Web app**:
   - Execute as: **Me**
   - Who has access: **Anyone** (the `TOKEN` is what protects it)

   Authorize when prompted, then copy the **Web app URL** (ends in `/exec`).
4. In `config.toml` set `[sheets].webapp_url` to that URL and `[sheets].token` to
   the same `TOKEN`. (Or use env vars `JOB_SCRAPER_WEBAPP_URL` / `JOB_SCRAPER_TOKEN`.)

Opening the `/exec` URL in a browser should show `{"ok":true,"service":"job-scraper"}`.

If you edit `Code.gs` later, use **Deploy > Manage deployments > Edit > New version**
so that the URL stays the same.

To use a standalone script instead of one bound to the sheet, also add a `SHEET_ID`
script property (the id from `docs.google.com/spreadsheets/d/<ID>/edit`).

## Run

```sh
python -m job_scraper                                   # full run per config.toml
python -m job_scraper --platforms greenhouse --max-companies 50   # quick test
python -m job_scraper --dry-run                         # no sheet; writes output/<tab>.json
python tests/test_sheets.py && python tests/test_location_filter.py && python tests/test_indeed.py   # offline tests
```

## Indeed

`indeed` in `[scrape].platforms` adds Indeed searches through
[JobSpy](https://github.com/speedyapply/JobSpy) (`pip install python-jobspy`, included
in `requirements.txt`). Unlike the ATS boards, which are crawled company by company,
Indeed is searched: one search per country × term in `[indeed]`, each capped by Indeed at
about 1,000 results, limited to jobs added in the last `hours_old` hours. The default
config runs 230 searches (USA + 22 European countries × 10 terms), which takes ~1-2
minutes. Indeed jobs go through the same filters, roles and tabs, and a job already found
on an ATS board (same company + title) isn't added twice.

Indeed's search API is unofficial, so it can change or start blocking without notice;
upgrading `python-jobspy` is the usual fix. `--max-companies` doesn't limit Indeed.

## Tabs and roles

The default config writes eight tabs: **US** and **EU** remote jobs, each split into
**Software**, **ML/AI**, **Data** and **DevOps/SRE** (`US - Software`, `EU - ML/AI`, ...).

- `[[roles]]` defines the role groups as title regexes. A job gets the **first** role whose
  patterns match, so it lands in exactly one role tab; titles matching no role are dropped.
- `[filters]` applies to every tab: `title_exclude` drops non-developer roles that still
  look like dev titles (support, sales, field service, managers, QA, security, hardware,
  ...), plus the remote, recruiter and age filters. Other engineering fields (civil,
  electrical, ...) never match a role, so they don't need excluding.
- Each `[[sheets.tabs]]` entry names its `worksheet`, `countries` and `role`, and can
  override any `[filters]` key.

`config.toml` is git-ignored, so copy filter changes to every machine that runs the
scraper (`config.example.toml` has the same filters, without the URL and token).

## How the sheet is maintained

Each row is keyed on the `key` column (the job URL, or a stable id for
Workday/Paylocity). Syncs are incremental. On every run:

- only jobs not in the sheet yet are added, at the top, with `first_seen` = today.
  A job counts as already there if its `key` **or its company + title** matches a row
  (case, punctuation, "Inc"/"LLC", and "(Remote)"-style suffixes are ignored), so
  reposts and the same job on another job board aren't added twice
- jobs already in the sheet are left alone, even if they're scraped again
- rows are removed `prune_after_days` after their `first_seen` date (`0` keeps them
  forever)
- **extra columns you add yourself (e.g. `status`, `notes`) are never touched**,
  and you can sort, insert or delete rows freely between runs

**The sheet fills in while the scrape runs.** Each company's jobs are filtered as soon
as they're downloaded, and a tab's new matches are pushed every `batch_size` (100) jobs
or every `flush_seconds` (60), whichever comes first. Jobs that match no tab are dropped
immediately, so memory stays small however many companies you scrape. If the sheet
can't be written, the scrape stops instead of carrying on without it.

At the start of a run, each tab's `key`, `first_seen`, `company` and `title` columns
are read once and old rows are pruned. After that, the sync decides what to add in Python
([sheets.py](job_scraper/sheets.py)), and sends only those changes to the web app. The
web app finds rows by `key`, never by position, and skips inserting keys that already
exist, so every request is safe to retry. Text is written with a leading `'`, so a
title like `=HYPERLINK(...)` stays text and is never evaluated. Dates (`first_seen`,
`posted_at`) are real date cells.

Apps Script limits: each request must finish in 6 minutes. The client reads 5,000 rows
and inserts 2,000 rows per request, well under that.

## Size limits

The full scrape is 1M+ jobs from 20k+ companies and takes hours. A spreadsheet
holds at most 10M cells, so use `[filters]` to narrow the scrape to what you need.
`max_rows` (default 100k) is a hard cap: if it's exceeded, the bottom (oldest) rows
are dropped, including any notes on them.

## Layout

```
job_scraper/
  fetchers.py     one fetcher per ATS + skill-level / recruiter classification
  indeed.py       Indeed searches via JobSpy, converted to the same job shape
  scrape.py       company loading, parallel fetch, dead-slug cache, clean, filter, salary
  sheets.py       incremental sync + client for the Apps Script web app
  stream.py       filters jobs per tab as they arrive and pushes them in batches
  geolocation.py  location parsing (used for remote + country)
  __main__.py     CLI
apps_script/      Code.gs, the web app to paste into Apps Script
data/             company slug lists per ATS, salary lookup
cache/            dead-slug cache (auto-created, safe to delete)
output/           <tab>.json per tab from the last run
```

Company lists in `data/` are static snapshots; refresh them by copying from the
aggregator repo.
