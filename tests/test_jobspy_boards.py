"""Offline tests for converting Indeed / Glassdoor (JobSpy) results.
Run: python tests/test_jobspy_boards.py
Uses stand-in objects, so JobSpy doesn't need to be installed."""

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_scraper.jobspy_boards import BOARDS, _iso_code, _settings, _to_job
from job_scraper.scrape import _in_countries, expand_countries


def country(names):  # stands in for a jobspy.model.Country enum member
    return SimpleNamespace(value=(names, "www", "com"))


def post(title="Senior Data Engineer", company="Acme GmbH", country_obj=None, remote=True,
         display="Home Office, Germany"):
    location = SimpleNamespace(country=country_obj, display_location=lambda: display)
    return SimpleNamespace(id="in-abc", title=title, company_name=company, location=location,
                           job_url="https://de.indeed.com/viewjob?jk=abc", is_remote=remote,
                           date_posted=date(2026, 10, 6))


def test_iso_code():
    assert _iso_code(country("germany"), None) == "DE"
    assert _iso_code(country("usa,us,united states"), None) == "US"
    assert _iso_code(country("uk,united kingdom"), None) == "GB"
    assert _iso_code("Germany", None) == "DE"  # JobSpy sometimes gives a plain name
    assert _iso_code(None, "FR") == "FR"       # unknown -> the searched country


def test_to_job_shape():
    job = _to_job(post(country_obj=country("germany")), "DE")
    assert job["ats"] == "Indeed" and job["company"] == "Acme GmbH"
    assert job["country"] == "DE" and job["remote"] is True
    assert job["url"] == "https://de.indeed.com/viewjob?jk=abc"
    assert job["updated_at"] == "2026-10-06"
    assert job["location"] == "Home Office, Germany"


def test_country_comes_from_indeed_not_the_location_text():
    # "Remote" alone names no place; the country still lands the job in the EU tab.
    job = _to_job(post(country_obj=None, display="Remote"), "DE")
    assert job["country"] == "DE"
    assert _in_countries(job, expand_countries(["EU"]))
    assert not _in_countries(job, {"US"})


def test_glassdoor_remote_job_without_location_goes_to_us():
    # Glassdoor gives remote jobs no location at all.
    p = post(title="Senior Backend Engineer", company="Prelim")
    p.location = None
    job = _to_job(p, "US", "Glassdoor")
    assert job["ats"] == "Glassdoor" and job["country"] == "US"
    assert job["location"] == "Remote"
    assert _in_countries(job, {"US"}) and not _in_countries(job, expand_countries(["EU"]))


def test_board_defaults_and_overrides():
    assert set(BOARDS) == {"indeed", "glassdoor"}
    assert _settings("glassdoor", None) == {"countries": ["USA"], "workers": 2}
    assert _settings("indeed", {"workers": 8})["workers"] == 8


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
