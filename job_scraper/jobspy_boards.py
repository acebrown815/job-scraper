"""Job-board searches through JobSpy (https://github.com/speedyapply/JobSpy, MIT):
Indeed and Glassdoor.

Unlike the ATS boards, these are search-based: each (country, search term) pair is one
search, capped by the board at about 1,000 results. Results are converted to the same job
shape as the ATS fetchers (see fetchers._job) and go through the same filters and tabs.
Each board is configured by its own section ([indeed], [glassdoor]).

Needs ``pip install python-jobspy``; it's only imported when one of these boards is in
the platforms.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import metadata

from .fetchers import _job
from .geolocation import COUNTRY_ALIASES

# platform name -> (display name, JobSpy scraper class name, defaults)
BOARDS = {
    "indeed": ("Indeed", "Indeed", {"countries": ["USA"], "workers": 4}),
    # Glassdoor ignores the country (glassdoor.de returns the same US jobs as .com), so
    # it's US only; fewer workers because it rate-limits sooner.
    "glassdoor": ("Glassdoor", "Glassdoor", {"countries": ["USA"], "workers": 2}),
}
# Oldest JobSpy whose scraper still works for the board (older Glassdoor code gets 403s
# and returns nothing, without raising).
MIN_JOBSPY = {"glassdoor": (1, 2, 0)}
DEFAULT_SEARCH_TERMS = ["software engineer"]


def _iso_code(country, fallback):
    """ISO code for a JobSpy Country (enum or name), e.g. Country.GERMANY -> "DE"."""
    names = getattr(country, "value", (country or "",))[0]
    for name in str(names).split(","):
        code = COUNTRY_ALIASES.get(name.strip().lower())
        if code:
            return code
    return fallback


def _to_job(post, search_country_iso, ats="Indeed"):
    location = post.location.display_location() if post.location else ""
    if not location:  # Glassdoor gives remote jobs no location
        location = "Remote" if post.is_remote else "Not specified"
    job = _job(
        post.company_name or "", None, post.title, location, post.job_url,
        ats,
        updated_at=post.date_posted.isoformat() if post.date_posted else None,
        remote=bool(post.is_remote),
        job_id=post.id,
    )
    job["company_slug"] = job["company"]
    # The board knows the country; don't rely on parsing the location string. Remote
    # jobs often have no location at all: use the country that was searched.
    job["country"] = _iso_code(post.location.country if post.location else None,
                               search_country_iso)
    return job


def _jobspy():
    try:
        import jobspy.glassdoor
        import jobspy.indeed
        from jobspy.model import Country, ScraperInput, Site
        from jobspy.util import set_logger_level
    except ImportError:
        raise SystemExit("Indeed/Glassdoor need JobSpy: pip install -U python-jobspy")
    scrapers = {"Indeed": jobspy.indeed.Indeed, "Glassdoor": jobspy.glassdoor.Glassdoor}
    return scrapers, Country, ScraperInput, Site, set_logger_level


def _settings(board, cfg):
    return {**BOARDS[board][2], **(cfg or {})}


def _scraper_input(board, cfg, country, term):
    _, _, ScraperInput, Site, _ = _jobspy()
    params = dict(
        search_term=term,
        country=country,
        is_remote=cfg.get("remote_only", True),
        hours_old=cfg.get("hours_old", 48),
        results_wanted=cfg.get("results_per_search", 1000),
    )
    if "site_type" in ScraperInput.model_fields:  # required before JobSpy 1.2.0
        params["site_type"] = [Site[board.upper()]]
    return ScraperInput(**params)


def check_board(board, cfg):
    """Fail fast, before scraping starts: JobSpy is installed, this version accepts our
    search input, and every configured country name is valid."""
    _, Country, _, _, _ = _jobspy()
    cfg = _settings(board, cfg)
    if board in MIN_JOBSPY:
        try:
            installed = metadata.version("python-jobspy")
        except metadata.PackageNotFoundError:
            installed = None  # e.g. a source checkout on PYTHONPATH; can't tell
        version = tuple(int(p) for p in re.findall(r"\d+", installed or "")[:3])
        if installed and version < MIN_JOBSPY[board]:
            need = ".".join(map(str, MIN_JOBSPY[board]))
            raise SystemExit(f"[{board}] needs python-jobspy >= {need} (installed: {installed}; "
                             f"older versions get blocked): pip install -U python-jobspy")
    for name in cfg["countries"]:
        try:
            Country.from_string(name)
        except ValueError as e:
            raise SystemExit(f"[{board}] countries: {e}")
    try:
        _scraper_input(board, cfg, Country.from_string(cfg["countries"][0]), "test")
    except Exception as e:
        raise SystemExit(f"[{board}] this JobSpy version doesn't accept the search input "
                         f"(try: pip install -U python-jobspy): {e}")


def fetch_board(board, cfg, on_jobs):
    """Run every configured search on one board, handing each search's jobs to
    ``on_jobs``. Returns the number of jobs fetched (before filtering)."""
    scrapers, Country, _, _, set_logger_level = _jobspy()
    set_logger_level(0)  # JobSpy logs every page at INFO; keep errors only
    name, scraper_name, _ = BOARDS[board]
    cfg = _settings(board, cfg)

    countries = cfg["countries"]
    terms = cfg.get("search_terms", DEFAULT_SEARCH_TERMS)
    searches = [(c, t) for c in countries for t in terms]
    print(f"[{board}] running {len(searches):,} searches "
          f"({len(countries)} countries x {len(terms)} terms)")

    def search(country_name, term):
        country = Country.from_string(country_name)
        result = scrapers[scraper_name]().scrape(_scraper_input(board, cfg, country, term))
        iso = _iso_code(country, None)
        return [_to_job(post, iso, name) for post in result.jobs]

    total = 0
    executor = ThreadPoolExecutor(max_workers=cfg["workers"])
    try:
        futures = {executor.submit(search, c, t): (c, t) for c, t in searches}
        for i, future in enumerate(as_completed(futures), 1):
            country_name, term = futures[future]
            try:
                jobs = future.result()
            except Exception as e:
                print(f"Error searching {name} {country_name} for {term!r}: {e}")
                continue
            if jobs:
                on_jobs(jobs)
                total += len(jobs)
            if i % 25 == 0:
                print(f"  [{board}] {i:,}/{len(searches):,} searches, {total:,} jobs so far")
    finally:
        executor.shutdown(cancel_futures=True)

    print(f"[{board}] done: {total:,} jobs from {len(searches):,} searches")
    return total
