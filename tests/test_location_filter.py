"""Offline tests for the country filter on remote jobs and repost collapsing.
Run: python -m pytest tests"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from job_scraper.geolocation import parse_job_location
from job_scraper.scrape import _in_countries, apply_filters, classify_role, expand_countries
from job_scraper.stream import Streamer, Tab

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


ROLES = [
    {"name": "ML/AI", "title_include": [r"\b(ml|ai)\b(\W+\w+){0,2}\W+engineer\b",
                                        r"\bengineer\b.*\b(ml|ai)\b"]},
    {"name": "Data", "title_include": [r"\bdata\b(\W+\w+){0,2}\W+engineer\b"]},
    {"name": "Software", "title_include": [r"\bsoftware\b(\W+\w+){0,2}\W+engineer\b",
                                           r"\bdevelopers?\b"]},
]


def test_first_matching_role_wins():
    assert classify_role("Senior ML Engineer", ROLES) == "ML/AI"
    assert classify_role("Software Engineer, ML Infrastructure", ROLES) == "ML/AI"
    assert classify_role("Senior Data Engineer", ROLES) == "Data"
    assert classify_role("Senior Software Engineer", ROLES) == "Software"
    assert classify_role("Mechanical Engineer", ROLES) is None


def test_tab_role_filter():
    jobs = [{"title": t} for t in ["ML Engineer", "Data Engineer", "Software Engineer",
                                   "Civil Engineer", "QA Developer"]]
    titles = lambda f: [j["title"] for j in apply_filters(jobs, f, verbose=False)]
    assert titles({"roles": ROLES, "role": "Data"}) == ["Data Engineer"]
    assert titles({"roles": ROLES, "title_exclude": [r"\bqa\b"]}) == \
        ["ML Engineer", "Data Engineer", "Software Engineer"]  # no role: any role passes
    try:
        apply_filters(jobs, {"roles": ROLES, "role": "Softwear"})
        raise AssertionError("unknown role accepted")
    except SystemExit as e:
        assert "Softwear" in str(e)


def test_tab_skips_reposts_within_a_run():
    tab = Tab("t", {})
    tab.offer([
        {"key": "b", "company": "Jobgether", "title": "Data  Engineer", "remote": True},
        {"key": "a", "company": "jobgether", "title": "data engineer", "remote": True},
        {"key": "c", "company": "jobgether", "title": "Data Engineer II", "remote": True},
    ])
    tab.offer([{"key": "b", "company": "other", "title": "Other", "remote": True}])
    assert [j["key"] for j in tab.pending] == ["b", "c"]


class FakeWriter:
    def __init__(self):
        self.batches = []

    def add(self, jobs):
        self.batches.append([j["key"] for j in jobs])
        return len(jobs)


def test_streamer_pushes_in_batches():
    writer = FakeWriter()
    tab = Tab("t", {"title_include": ["engineer"]}, writer)
    streamer = Streamer([tab], batch_size=100, flush_seconds=3600)
    for company in range(25):  # 25 companies x 10 jobs, half of them matching
        streamer.put([{"title": f"Engineer {company}-{i}" if i % 2 else "Accountant",
                       "company": f"c{company}", "url": f"u{company}-{i}"} for i in range(10)])
    streamer.close()
    assert [len(b) for b in writer.batches] == [100, 25]
    assert len(tab.kept) == 125


def test_streamer_surfaces_sheet_errors():
    class BrokenWriter:
        def add(self, jobs):
            raise RuntimeError("web app down")

    streamer = Streamer([Tab("t", {}, BrokenWriter())], batch_size=1, flush_seconds=3600)
    job = {"title": "Engineer", "company": "c", "url": "u"}
    try:
        for i in range(1000):  # the failure reaches put() once the sync thread hits it
            streamer.put([{**job, "url": f"u{i}"}])
            time.sleep(0.01)
        raise AssertionError("put() kept accepting jobs after the sync failed")
    except RuntimeError as e:
        assert "web app down" in str(e.__cause__ or e)
    try:
        streamer.close()
        raise AssertionError("close() didn't raise")
    except RuntimeError as e:
        assert "web app down" in str(e)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
