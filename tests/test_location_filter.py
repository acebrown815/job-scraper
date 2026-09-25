"""Offline tests for the country filter on remote jobs and repost collapsing.
Run: python -m pytest tests"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_scraper.geolocation import parse_job_location
from job_scraper.scrape import _in_countries, collapse_reposts, expand_countries

US = {"US"}


def remote_job(location):
    return {"location": location, "remote": True, "country": parse_job_location(location)["country"]}


def test_remote_us_locations_pass():
    for loc in ["Remote", "Anywhere", "Remote (US)", "Remote-US", "Remote, United States",
                "United States - Remote", "Select USA Remote Locations", "Remote - CST Timezone",
                "Remote - U.S.", "Remote (US/Canada)", "Remote (North America)",
                "Remote - Located in CA, CO, NY, TX", "Oklahoma - Remote", "Remote, US, Virginia"]:
        assert _in_countries(remote_job(loc), US), loc


def test_remote_non_us_locations_dropped():
    for loc in ["Europe - Remote", "EU only (remote)", "Remote - LATAM - MEX", "Remote Latam",
                "Asia", "Brazil", "Remote (Ukraine)", "Polska (praca zdalna)", "Colombo",
                "Toronto, ON", "London, Ontario", "Remote - EMEA", "CO-Colombia-Remote",
                "IN Remote India", "Bangalore, IN ; Pune, IN; Remote", "Remote (Non-U.S.)",
                "CA Remote Ontario", "Sydney"]:
        assert not _in_countries(remote_job(loc), US), loc


def test_eu_tab():
    eu = expand_countries(["EU"])
    for loc in ["Remote - Europe", "EU only (remote)", "Germany", "Remote, Poland", "Remote - EMEA",
                "Remote (Estonia)", "Polska (praca zdalna)", "Remote (U.S. or Europe)", "Remote"]:
        assert _in_countries(remote_job(loc), eu), loc
    for loc in ["Remote (US)", "Remote - LATAM", "Brazil", "Remote - EST", "India (Remote)"]:
        assert not _in_countries(remote_job(loc), eu), loc
    assert not _in_countries(remote_job("Remote"), eu, keep_unplaced=False)


def test_remote_parse_keeps_single_country():
    assert parse_job_location("Remote, United States")["country"] == "US"
    assert parse_job_location("Remote (Ukraine)")["country"] == "UA"
    assert parse_job_location("Remote - Europe")["country"] is None


def test_collapse_reposts_keeps_smallest_key():
    jobs = [
        {"key": "b", "company": "Jobgether", "title": "Data  Engineer", "remote": True},
        {"key": "a", "company": "jobgether", "title": "data engineer", "remote": True},
        {"key": "c", "company": "jobgether", "title": "Data Engineer II", "remote": True},
    ]
    assert [j["key"] for j in collapse_reposts(jobs)] == ["a", "c"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
