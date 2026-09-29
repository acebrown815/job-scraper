"""Incrementally sync scraped jobs into a Google Sheet through an Apps Script web app.

The web app (apps_script/Code.gs) applies changes addressed by the ``key`` column
(see scrape.get_dedup_key); what to change is decided here. On each sync:
  * only jobs not in the sheet yet are added, at the top, first_seen = today. A job is
    already there if its key or its company + title (scrape.job_identity) matches a row,
    so reposts and the same job on another board aren't added twice
  * jobs already in the sheet are left alone, even if scraped again
  * rows are removed ``prune_after_days`` after their first_seen date
  * the sheet is capped at ``max_rows`` by dropping the oldest (bottom) rows
Columns you add by hand (status, notes, ...) are never touched.
"""

import re
import time
from datetime import date, timedelta

import requests

from .scrape import job_identity

# Columns this tool owns. Anything else in the header row is user data and is kept as-is.
COLUMNS = [
    "key", "title", "company", "location", "country", "remote", "skill_level",
    "salary", "ats", "is_recruiter", "posted_at", "first_seen", "url",
]

READ_CHUNK_ROWS = 5_000
WRITE_CHUNK_ROWS = 2_000
KEY_CHUNK = 20_000

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _job_to_fields(job):
    return {
        "key": job["key"],
        "title": job.get("title") or "",
        "company": job.get("company") or "",
        "location": job.get("location") or "",
        "country": job.get("country") or "",
        "remote": bool(job.get("remote")),
        "skill_level": job.get("skill_level") or "",
        "salary": job.get("salary") if job.get("salary") is not None else "",
        "ats": job.get("ats") or "",
        "is_recruiter": bool(job.get("is_recruiter")),
        "posted_at": (job.get("updated_at") or "")[:10],
        "url": job.get("url") or "",
    }


def plan_sync(header, keys, first_seen, jobs, today=None, prune_after_days=30,
              companies=(), titles=()):
    """Pure sync planner.

    header: current header row ([] for an empty sheet).
    keys / first_seen / companies / titles: those sheet columns, row-aligned.
    Returns a dict with:
      header     the header to write, or None if unchanged (our missing columns appended)
      new_rows   rows to insert (jobs whose key isn't in the sheet), aligned with the header
      prune      keys of rows added more than prune_after_days ago
      known_keys / known_idents   keys and company + title identities already in the sheet
    """
    today = today or date.today()

    full_header = list(header)
    for col in COLUMNS:
        if col not in full_header:
            full_header.append(col)

    existing = {str(k): str(fs or "") for k, fs in zip(keys, first_seen) if k}
    known_idents = {job_identity(c, t) for c, t in zip(companies, titles) if c or t}
    new_rows = build_new_rows(jobs, full_header, set(existing), known_idents, today.isoformat())

    prune = []
    if prune_after_days:
        cutoff = (today - timedelta(days=prune_after_days)).isoformat()
        # ISO dates compare correctly as strings; rows without a date are kept.
        prune = [k for k, fs in existing.items() if fs and fs[:10] < cutoff]

    print(f"Sheet sync: {len(new_rows):,} new, {len(existing):,} already in sheet, "
          f"{len(prune):,} to prune")
    return {
        "header": full_header if full_header != list(header) else None,
        "new_rows": new_rows,
        "prune": prune,
        "known_keys": set(existing),
        "known_idents": known_idents,
    }


def build_new_rows(jobs, header, known_keys, known_idents, today_s):
    """Rows (aligned with header) for jobs whose key and company + title aren't known yet.
    Adds the new ones to known_keys / known_idents, so later batches skip them too."""
    idx = {col: header.index(col) for col in COLUMNS}
    new_jobs = []
    for job in jobs:  # first arrival wins among duplicates
        ident = job_identity(job.get("company"), job.get("title"))
        if job["key"] in known_keys or ident in known_idents:
            continue
        known_keys.add(job["key"])
        known_idents.add(ident)
        new_jobs.append(job)

    rows = []
    for job in sorted(new_jobs, key=lambda j: ((j.get("company") or "").lower(),
                                               (j.get("title") or "").lower())):
        row = [""] * len(header)
        for col, value in _job_to_fields(job).items():
            row[idx[col]] = value
        row[idx["first_seen"]] = today_s
        rows.append(row)
    return rows


class WebAppClient:
    """Minimal client for the Apps Script endpoint. Every action is idempotent, so retries are safe."""

    def __init__(self, url, token, sheet, retries=4):
        if not url or not token:
            raise SystemExit("Set [sheets].webapp_url and [sheets].token "
                             "(or JOB_SCRAPER_WEBAPP_URL / JOB_SCRAPER_TOKEN).")
        self.url, self.token, self.sheet, self.retries = url, token, sheet, retries

    def call(self, action, **params):
        body = {"token": self.token, "sheet": self.sheet, "action": action, **params}
        for attempt in range(self.retries + 1):
            try:
                # Apps Script answers the POST with a 302 to the result; requests follows it as a GET.
                resp = requests.post(self.url, json=body, timeout=360)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
            except requests.RequestException as e:
                if attempt == self.retries:
                    raise
                wait = 2 ** attempt * 5
                print(f"  web app {action} failed ({e}); retrying in {wait}s")
                time.sleep(wait)
                continue
            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError(
                    "Web app returned non-JSON (check the URL ends in /exec and the "
                    f"deployment's access is 'Anyone'): {resp.text[:200]!r}"
                )
            if not data.get("ok"):
                raise RuntimeError(f"Web app {action} error: {data.get('error')}")
            if "service" in data:
                # doGet's health-check reply: the POST reached the script as a GET.
                raise RuntimeError(
                    f"Web app {action} hit doGet instead of doPost; redeploy the latest "
                    f"Code.gs (Deploy > Manage deployments > New version): {resp.text[:200]!r}"
                )
            return data


def _to_cell(value):
    """Sheets parses written strings like typed input. A leading apostrophe forces text,
    which keeps titles like "=foo" or "+1 555" from turning into formulas/numbers.
    ISO dates are left bare so they become real, sortable date cells."""
    if value is None:
        return ""
    if isinstance(value, str) and value and not ISO_DATE_RE.match(value):
        return "'" + value
    return value


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def read_columns(client, columns):
    """Read the header and the given columns for every data row."""
    header, values, offset = [], {c: [] for c in columns}, 0
    while True:
        data = client.call("read_columns", columns=columns, offset=offset, limit=READ_CHUNK_ROWS)
        if "columns" not in data or "total_rows" not in data:
            raise RuntimeError(f"Web app read returned an unexpected response: {data!r}; "
                               "is the deployment running the current apps_script/Code.gs?")
        header = data["header"]
        got = len(data["columns"][columns[0]])
        for c in columns:
            values[c].extend(data["columns"][c])
        offset += got
        if not got or offset >= data["total_rows"]:
            return header, values


class SheetWriter:
    """Incremental writer for one tab. On creation it reads the tab once, fixes the header
    and prunes old rows; then ``add(jobs)`` can be called repeatedly while the scrape runs,
    inserting only jobs not already in the sheet or in an earlier batch."""

    def __init__(self, webapp_url, token, worksheet="Jobs", prune_after_days=30, max_rows=None):
        self.client = WebAppClient(webapp_url, token, worksheet)
        self.worksheet, self.max_rows = worksheet, max_rows
        self.today_s = date.today().isoformat()

        header, cols = read_columns(self.client, ["key", "first_seen", "company", "title"])
        print(f"Read {len(cols['key']):,} existing rows from '{worksheet}'")
        plan = plan_sync(header, cols["key"], cols["first_seen"], [],
                         prune_after_days=prune_after_days,
                         companies=cols["company"], titles=cols["title"])
        if plan["header"]:
            self.client.call("set_header", header=plan["header"])
        self.header = plan["header"] or header
        # Pruned rows stay known, so a job removed today isn't re-added in the same run.
        self.known_keys, self.known_idents = plan["known_keys"], plan["known_idents"]

        self.inserted = self.deleted = 0
        for chunk in _chunks(plan["prune"], KEY_CHUNK):
            self.deleted += self.client.call("delete_keys", keys=chunk)["deleted"]

    def add(self, jobs):
        """Insert the jobs that are new to this tab, at the top. Returns how many."""
        rows = build_new_rows(jobs, self.header, self.known_keys, self.known_idents,
                              self.today_s)
        rows = [[_to_cell(v) for v in row] for row in rows]
        inserted = 0
        # Each chunk goes in at the top, so send the last chunk first to keep the order.
        for chunk in reversed(list(_chunks(rows, WRITE_CHUNK_ROWS))):
            inserted += self.client.call("insert_rows", values=chunk)["inserted"]
        self.inserted += inserted
        return inserted

    def finish(self):
        if self.max_rows:
            self.deleted += self.client.call("trim", max_rows=self.max_rows)["deleted"]
        print(f"Sheet '{self.worksheet}': added {self.inserted:,}, removed {self.deleted:,}")
