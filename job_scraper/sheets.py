"""Upsert scraped jobs into a Google Sheet through an Apps Script web app.

The web app (apps_script/Code.gs) only reads and writes row ranges; the merge
happens here. Rows are keyed on the ``key`` column (see scrape.get_dedup_key).
On each sync:
  * new jobs are added with first_seen = today
  * jobs seen again get their scraped fields refreshed and last_seen = today
  * rows not seen for ``prune_after_days`` are removed
  * any extra columns you add by hand (status, notes, ...) are preserved per row
"""

import re
import time
from datetime import date, timedelta

import requests

# Columns this tool owns. Anything else in the header row is user data and is kept as-is.
COLUMNS = [
    "key", "title", "company", "location", "country", "remote", "skill_level",
    "salary", "ats", "is_recruiter", "posted_at", "first_seen", "last_seen", "url",
]

READ_CHUNK_ROWS = 5_000
WRITE_CHUNK_ROWS = 2_000

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


def merge_rows(existing_values, jobs, today=None, prune_after_days=30, max_rows=None):
    """Pure merge of the current sheet contents with freshly scraped jobs.

    existing_values: list of rows as read from the sheet (first row = header).
    Returns the full new sheet contents (header + rows) as a list of lists.
    """
    today = today or date.today()
    today_s = today.isoformat()

    # Keep the existing header (including user columns and their positions, even
    # unnamed ones), then append any of our columns that are missing.
    header = list(existing_values[0]) if existing_values else []
    for col in COLUMNS:
        if col not in header:
            header.append(col)
    idx = {col: header.index(col) for col in COLUMNS}
    width = len(header)

    def cell(row, col):
        return row[idx[col]]

    rows = {}  # key -> row (list aligned with header); dict preserves first-seen order
    for raw in existing_values[1:] if existing_values else []:
        row = (list(raw) + [""] * width)[:width]
        key = cell(row, "key")
        if key:
            rows[key] = row

    added = updated = 0
    for job in jobs:
        fields = _job_to_fields(job)
        row = rows.get(fields["key"])
        if row is None:
            row = [""] * width
            row[idx["first_seen"]] = today_s
            rows[fields["key"]] = row
            added += 1
        else:
            updated += 1
        for col, value in fields.items():
            row[idx[col]] = value
        row[idx["last_seen"]] = today_s

    pruned = 0
    if prune_after_days:
        cutoff = (today - timedelta(days=prune_after_days)).isoformat()
        for key in list(rows):
            last_seen = str(cell(rows[key], "last_seen") or "")
            # ISO dates compare correctly as strings; rows without a date are kept.
            if last_seen and last_seen[:10] < cutoff:
                del rows[key]
                pruned += 1

    ordered = list(rows.values())
    # Newest first, then company/title for a stable, readable order.
    ordered.sort(key=lambda r: (str(cell(r, "company")).lower(), str(cell(r, "title")).lower()))
    ordered.sort(key=lambda r: str(cell(r, "first_seen")), reverse=True)

    capped = 0
    if max_rows and len(ordered) > max_rows:
        capped = len(ordered) - max_rows
        ordered = ordered[:max_rows]

    print(f"Sheet merge: {added:,} new, {updated:,} refreshed, {pruned:,} pruned, "
          f"{capped:,} dropped by max_rows -> {len(ordered):,} rows")

    return [header] + ordered


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


def read_sheet(client):
    rows, offset = [], 0
    while True:
        data = client.call("read", offset=offset, limit=READ_CHUNK_ROWS)
        if "rows" not in data or "total_rows" not in data:
            raise RuntimeError(f"Web app read returned an unexpected response: {data!r}; "
                               "is the deployment running the current apps_script/Code.gs?")
        rows.extend(data["rows"])
        offset += len(data["rows"])
        if not data["rows"] or offset >= data["total_rows"]:
            return rows


def sync_to_sheet(jobs, webapp_url, token, worksheet="Jobs",
                  prune_after_days=30, max_rows=None):
    client = WebAppClient(webapp_url, token, worksheet)

    existing = read_sheet(client)
    print(f"Read {max(len(existing) - 1, 0):,} existing rows from '{worksheet}'")
    values = merge_rows(existing, jobs, prune_after_days=prune_after_days, max_rows=max_rows)
    values = [[_to_cell(v) for v in row] for row in values]

    # Overwrite in place, then trim: the sheet is never left empty mid-sync.
    for start in range(0, len(values), WRITE_CHUNK_ROWS):
        client.call("write", start_row=start + 1, values=values[start:start + WRITE_CHUNK_ROWS])
    client.call("finalize", total_rows=len(values))

    print(f"Wrote {len(values) - 1:,} jobs to worksheet '{worksheet}'")
