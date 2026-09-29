"""Offline tests for the sheet sync planner and filters. Run: python -m pytest tests  (or python tests/test_sheets.py)"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_scraper.scrape import apply_filters, job_identity
from job_scraper.sheets import COLUMNS, build_new_rows, plan_sync

TODAY = date(2026, 9, 22)


def job(key, title="Software Engineer", company="acme", **kw):
    return {"key": key, "title": title, "company": company, "url": key, "ats": "Lever", **kw}


def as_dicts(header, rows):
    return {r[header.index("key")]: dict(zip(header, r)) for r in rows}


def test_empty_sheet_gets_header_and_rows():
    plan = plan_sync([], [], [], [job("a"), job("b", "Backend Engineer")], today=TODAY)
    assert plan["header"] == COLUMNS
    rows = as_dicts(COLUMNS, plan["new_rows"])
    assert set(rows) == {"a", "b"}
    assert rows["a"]["first_seen"] == "2026-09-22"
    assert plan["prune"] == []


def test_only_new_jobs_inserted_existing_left_alone():
    header = ["status", *COLUMNS, "notes"]
    plan = plan_sync(header, ["a", "old"], ["2026-09-21", "2026-09-20"],
                     [job("a", title="New title"), job("b"), job("b")], today=TODAY)
    assert plan["header"] is None  # user column positions untouched
    rows = as_dicts(header, plan["new_rows"])
    assert set(rows) == {"b"}  # "a" already exists; duplicate "b" inserted once
    assert rows["b"]["status"] == "" and len(plan["new_rows"][0]) == len(header)
    assert "update" not in plan  # nothing updates existing rows


def test_later_batches_skip_jobs_from_earlier_batches():
    keys, idents = {"in-sheet"}, set()
    first = build_new_rows([job("a"), job("b", "Backend Engineer")], COLUMNS, keys, idents,
                           "2026-09-22")
    second = build_new_rows([job("a2"), job("in-sheet", "X"), job("c", "QA Engineer")],
                            COLUMNS, keys, idents, "2026-09-22")
    key = COLUMNS.index("key")
    assert [r[key] for r in first] == ["b", "a"]  # sorted by company, then title
    assert [r[key] for r in second] == ["c"]  # a2 repeats a's company + title


def test_same_company_and_title_not_added_twice():
    # Existing row came from Lever; the same opening shows up on Greenhouse with a new
    # URL, plus two reposts of another job within this scrape.
    plan = plan_sync(COLUMNS, ["lever-url"], ["2026-09-21"], [
        job("gh-url", "Senior Engineer (Remote)", company="agiledefense"),
        job("x1", "Data Engineer", company="Acme Inc"),
        job("x2", "Data Engineer - Remote", company="acme"),
    ], today=TODAY, companies=["agile-defense"], titles=["Senior Engineer"])
    assert [r[COLUMNS.index("key")] for r in plan["new_rows"]] == ["x1"]


def test_job_identity():
    assert job_identity("Agile-Defense", "Senior Engineer (Remote)") == \
        job_identity("agiledefense", "senior  engineer")
    assert job_identity("Acme, Inc.", "Engineer [US]") == job_identity("acme", "Engineer")
    assert job_identity("remotely", "Remoteness Lead") == "remotely|remoteness lead"
    assert job_identity("acme", "Engineer I") != job_identity("acme", "Engineer II")


def test_missing_columns_appended_to_header():
    plan = plan_sync(["key", "notes"], [], [], [], today=TODAY)
    assert plan["header"] == ["key", "notes", *COLUMNS[1:]]


def test_rows_pruned_by_age_even_if_scraped_again():
    plan = plan_sync(COLUMNS, ["old", "", "nodate", "seen", "recent"],
                     ["2026-08-01", "2026-08-01", "", "2026-08-01", "2026-09-01"],
                     [job("seen")], today=TODAY, prune_after_days=30)
    assert plan["prune"] == ["old", "seen"]
    assert plan["new_rows"] == []


def test_prune_disabled():
    plan = plan_sync(COLUMNS, ["old"], ["2020-01-01"], [], today=TODAY, prune_after_days=0)
    assert plan["prune"] == []


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
