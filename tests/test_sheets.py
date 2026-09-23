"""Offline tests for the sheet merge logic. Run: python -m pytest tests  (or python tests/test_sheets.py)"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_scraper.scrape import apply_filters
from job_scraper.sheets import COLUMNS, merge_rows

TODAY = date(2026, 9, 22)


def job(key, title="Software Engineer", company="acme", **kw):
    return {"key": key, "title": title, "company": company, "url": key, "ats": "Lever", **kw}


def as_dicts(values):
    header = values[0]
    return {r[header.index("key")]: dict(zip(header, r)) for r in values[1:]}


def test_empty_sheet_gets_header_and_rows():
    values = merge_rows([], [job("a"), job("b")], today=TODAY)
    assert values[0] == COLUMNS
    rows = as_dicts(values)
    assert set(rows) == {"a", "b"}
    assert rows["a"]["first_seen"] == rows["a"]["last_seen"] == "2026-09-22"


def test_existing_row_keeps_first_seen_and_user_columns():
    header = ["status", *COLUMNS, "notes"]
    old = {c: "" for c in header}
    old.update(key="a", title="Old title", first_seen="2026-09-01", last_seen="2026-09-10",
               status="applied", notes="talked to recruiter")
    existing = [header, [old[c] for c in header]]

    values = merge_rows(existing, [job("a", title="New title")], today=TODAY)
    assert values[0] == header  # user column positions untouched
    row = as_dicts(values)["a"]
    assert row["title"] == "New title"
    assert row["first_seen"] == "2026-09-01"
    assert row["last_seen"] == "2026-09-22"
    assert row["status"] == "applied" and row["notes"] == "talked to recruiter"


def test_stale_rows_pruned_and_blank_rows_ignored():
    header = COLUMNS
    stale = ["" for _ in header]
    stale[header.index("key")] = "old"
    stale[header.index("last_seen")] = "2026-08-01"
    blank = ["" for _ in header]
    values = merge_rows([header, stale, blank], [job("new")], today=TODAY, prune_after_days=30)
    assert set(as_dicts(values)) == {"new"}


def test_max_rows_keeps_newest():
    header = COLUMNS
    older = ["" for _ in header]
    older[header.index("key")] = "older"
    older[header.index("first_seen")] = "2026-09-01"
    older[header.index("last_seen")] = "2026-09-21"
    values = merge_rows([header, older], [job("fresh")], today=TODAY, max_rows=1)
    assert set(as_dicts(values)) == {"fresh"}


def test_filters():
    jobs = [
        job("1", "Senior Software Engineer", skill_level="senior", country="US"),
        job("2", "Sales Engineer", skill_level="mid", country="US"),
        job("3", "Backend Developer", skill_level="mid", country="DE"),
        job("4", "Backend Developer", skill_level="mid", remote=True),
        job("5", "Accountant", skill_level="mid", country="US"),
        job("6", "Engineer", skill_level="mid", country="US", is_recruiter=True),
        job("7", "Engineer", skill_level="mid", country="US", updated_at="2026-01-01T00:00:00Z"),
    ]
    kept = apply_filters(jobs, {
        "title_include": ["engineer", "developer"],
        "title_exclude": ["sales"],
        "countries": ["us"],
        "exclude_recruiters": True,
        "max_age_days": 30,
    })
    assert [j["key"] for j in kept] == ["1", "4"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
