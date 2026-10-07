"""Indeed searches through JobSpy (https://github.com/speedyapply/JobSpy, MIT).

Unlike the ATS boards, Indeed is search-based: each (country, search term) pair is one
search, capped by Indeed at about 1,000 results. Results are converted to the same job
shape as the ATS fetchers (see fetchers._job) and go through the same filters and tabs.

Needs ``pip install python-jobspy``; it's only imported when "indeed" is in the platforms.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from .fetchers import _job
from .geolocation import COUNTRY_ALIASES

DEFAULT_COUNTRIES = ["USA"]
DEFAULT_SEARCH_TERMS = ["software engineer"]


def _iso_code(country, fallback):
    """ISO code for a JobSpy Country (enum or name), e.g. Country.GERMANY -> "DE"."""
    names = getattr(country, "value", (country or "",))[0]
    for name in str(names).split(","):
        code = COUNTRY_ALIASES.get(name.strip().lower())
        if code:
            return code
    return fallback


def _to_job(post, search_country_iso):
    location = post.location.display_location() if post.location else ""
    job = _job(
        post.company_name or "", None, post.title, location or "Not specified", post.job_url,
        "Indeed",
        updated_at=post.date_posted.isoformat() if post.date_posted else None,
        remote=bool(post.is_remote),
        job_id=post.id,
    )
    job["company_slug"] = job["company"]
    # Indeed knows the country; don't rely on parsing the location string.
    job["country"] = _iso_code(post.location.country if post.location else None,
                               search_country_iso)
    return job


def fetch_indeed(indeed_cfg, on_jobs):
    """Run every configured search, handing each search's jobs to ``on_jobs``.
    Returns the number of jobs fetched (before filtering)."""
    try:
        from jobspy.indeed import Indeed
        from jobspy.model import Country, ScraperInput
        from jobspy.util import set_logger_level
    except ImportError:
        raise SystemExit("Indeed needs JobSpy: pip install python-jobspy")
    set_logger_level(0)  # JobSpy logs every page at INFO; keep errors only

    countries = indeed_cfg.get("countries", DEFAULT_COUNTRIES)
    terms = indeed_cfg.get("search_terms", DEFAULT_SEARCH_TERMS)
    searches = [(c, t) for c in countries for t in terms]
    print(f"[indeed] running {len(searches):,} searches "
          f"({len(countries)} countries x {len(terms)} terms)")

    def search(country_name, term):
        country = Country.from_string(country_name)
        result = Indeed().scrape(ScraperInput(
            search_term=term,
            country=country,
            is_remote=indeed_cfg.get("remote_only", True),
            hours_old=indeed_cfg.get("hours_old", 48),
            results_wanted=indeed_cfg.get("results_per_search", 1000),
        ))
        iso = _iso_code(country, None)
        return [_to_job(post, iso) for post in result.jobs]

    total = 0
    executor = ThreadPoolExecutor(max_workers=indeed_cfg.get("workers", 4))
    try:
        futures = {executor.submit(search, c, t): (c, t) for c, t in searches}
        for i, future in enumerate(as_completed(futures), 1):
            country_name, term = futures[future]
            try:
                jobs = future.result()
            except Exception as e:
                print(f"Error searching Indeed {country_name} for {term!r}: {e}")
                continue
            if jobs:
                on_jobs(jobs)
                total += len(jobs)
            if i % 25 == 0:
                print(f"  [indeed] {i:,}/{len(searches):,} searches, {total:,} jobs so far")
    finally:
        executor.shutdown(cancel_futures=True)

    print(f"[indeed] done: {total:,} jobs from {len(searches):,} searches")
    return total
