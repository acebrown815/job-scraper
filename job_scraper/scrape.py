"""Scrape orchestration: load company lists, fetch in parallel, clean, enrich, filter."""

import functools
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from .fetchers import FETCHERS, MAX_WORKERS, load_paylocity
from .geolocation import REGION_COUNTRIES, location_places

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PACKAGE_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
CACHE_DIR = os.path.join(ROOT_DIR, "cache")

COMPANY_FILES = {
    "greenhouse": "greenhouse_companies.json",
    "ashby": "ashby_companies.json",
    "bamboohr": "bamboohr_companies.json",
    "lever": "lever_companies.json",
    "workday": "workday_companies.json",
    "icims": "icims_companies.json",
    "paylocity": "paylocity_companies_clean.json",
}


# ============================================================
# Company lists + dead slug cache
# ============================================================


def load_companies(platform):
    filepath = os.path.join(DATA_DIR, COMPANY_FILES[platform])
    if platform == "paylocity":
        companies = load_paylocity(filepath)
    else:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                companies = set(json.load(f))
        except FileNotFoundError:
            print(f"File not found: {filepath}")
            companies = set()
    print(f"Loaded {len(companies):,} {platform} companies")
    return companies


def _dead_slug_path(platform):
    return os.path.join(CACHE_DIR, "dead_slugs", f"{platform}.json")


def load_dead_slugs(platform):
    filepath = _dead_slug_path(platform)
    if not os.path.exists(filepath):
        return set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, IOError):
        return set()


def save_dead_slugs(platform, slugs):
    filepath = _dead_slug_path(platform)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(sorted(slugs), f, ensure_ascii=False, indent=2)
    print(f"  Cached {len(slugs):,} dead slugs for {platform}")


# ============================================================
# Fetching
# ============================================================


def fetch_all_jobs(platform, companies, on_jobs):
    """Fetch jobs from all companies of one platform in parallel, handing each company's
    jobs to ``on_jobs`` as soon as they arrive. Returns the number of jobs fetched."""
    fetcher = FETCHERS[platform]
    dead_slugs = load_dead_slugs(platform)
    live_companies = [s for s in companies if s not in dead_slugs]
    print(f"[{platform}] checking {len(live_companies):,} companies "
          f"(skipping {len(dead_slugs):,} known dead)")

    total = 0
    active = 0
    new_dead = set()

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS.get(platform, 30))
    try:
        futures = [executor.submit(fetcher, slug) for slug in live_companies]
        for i, future in enumerate(as_completed(futures), 1):
            slug, jobs, status_code = future.result()
            if jobs:
                on_jobs(jobs)
                total += len(jobs)
                active += 1
            elif status_code in (404, 410):
                # Only cache permanent failures
                new_dead.add(slug)
            if i % 200 == 0:
                print(f"  [{platform}] {i:,}/{len(live_companies):,} checked, "
                      f"{total:,} jobs so far")
    finally:
        # On an error (e.g. the sheet sync failed) don't keep fetching the remaining companies.
        executor.shutdown(cancel_futures=True)

    if new_dead:
        save_dead_slugs(platform, dead_slugs | new_dead)

    print(f"[{platform}] done: {active:,} active companies, {total:,} jobs, "
          f"{len(new_dead):,} newly dead")
    return total


def scrape(platforms, on_jobs, max_companies=None):
    """Scrape the given platforms concurrently, calling ``on_jobs(jobs)`` once per company
    (from worker threads). Returns the total number of jobs fetched."""
    work = []
    for platform in platforms:
        companies = sorted(load_companies(platform))
        if max_companies:
            companies = companies[:max_companies]
        if companies:
            work.append((platform, companies))

    total = 0
    with ThreadPoolExecutor(max_workers=max(len(work), 1)) as executor:
        futures = {executor.submit(fetch_all_jobs, p, c, on_jobs): p for p, c in work}
        for future in as_completed(futures):
            total += future.result()
    return total


# ============================================================
# Clean / enrich / filter
# ============================================================


def clean_job_data(jobs):
    """Keep entries with a usable title, URL and company, and give each its ``key``."""
    cleaned = []
    for job in jobs:
        if ((job.get("title") or "").strip().lower() in ("", "not specified", "n/a", "unknown")
                or not job.get("url") or not (job.get("company") or job.get("company_slug"))):
            continue
        job["key"] = get_dedup_key(job)
        if job["key"]:
            cleaned.append(job)
    return cleaned


@functools.cache
def _salary_lookup():
    path = os.path.join(DATA_DIR, "salary_lookup.json")
    if not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("primary", {}), data.get("fallback", {})


def enrich_salary(jobs):
    """Attach median salary from the static lookup (company|title|level, then title|level)."""
    primary, fallback = _salary_lookup()
    for job in jobs:
        company = (job.get("company") or "").lower().strip()
        title = (job.get("title") or "").lower().strip()
        level = job.get("skill_level", "mid")
        salary = primary.get(f"{company}|{title}|{level}") or fallback.get(f"{title}|{level}")
        job["salary"] = salary.get("median") if salary else None


def _parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expand_countries(names):
    """ISO codes and region names ("EU", "Europe", "North America") -> set of ISO codes."""
    codes = set()
    for name in names:
        region = REGION_COUNTRIES.get(name.strip().lower())
        codes |= region if region else {name.strip().upper()}
    return codes


def _in_countries(job, countries, keep_unplaced=True):
    """Country filter. On-site jobs need a parsed country in the list. Remote jobs pass
    if their location names an allowed country or region (e.g. "Remote, US", "North
    America"), or names no place at all ("Remote", "Anywhere") when ``keep_unplaced``.
    Remote jobs tied to other countries ("Remote - LATAM", "Brazil") or to unrecognized
    places are dropped."""
    if job.get("country"):
        return job["country"] in countries
    if not job.get("remote"):
        return False
    codes, unknown = location_places(job.get("location"))
    if codes:
        return bool(codes & countries)
    return keep_unplaced and not unknown


def apply_filters(jobs, filters, verbose=True):
    """Keep only jobs matching the [filters] config section. Empty/missing options are ignored."""
    include = [re.compile(p, re.I) for p in filters.get("title_include", [])]
    exclude = [re.compile(p, re.I) for p in filters.get("title_exclude", [])]
    levels = set(filters.get("skill_levels", []))
    countries = expand_countries(filters.get("countries", []))
    keep_unplaced = filters.get("keep_unplaced_remote", True)
    remote_only = filters.get("remote_only", False)
    exclude_recruiters = filters.get("exclude_recruiters", False)
    max_age_days = filters.get("max_age_days")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days) if max_age_days else None
    )

    kept = []
    for job in jobs:
        title = job.get("title") or ""
        if include and not any(p.search(title) for p in include):
            continue
        if any(p.search(title) for p in exclude):
            continue
        if levels and job.get("skill_level") not in levels:
            continue
        if remote_only and not job.get("remote"):
            continue
        if countries and not _in_countries(job, countries, keep_unplaced):
            continue
        if exclude_recruiters and job.get("is_recruiter"):
            continue
        if cutoff:
            posted = _parse_date(job.get("updated_at"))
            # Jobs with no date (Ashby, BambooHR) are kept; we can't tell their age.
            if posted and posted < cutoff:
                continue
        kept.append(job)

    if verbose:
        print(f"Filters kept {len(kept):,} of {len(jobs):,} jobs")
    return kept


def get_dedup_key(job):
    """Stable identity for a job across runs."""
    url = job.get("url", "")
    ats = job.get("ats")
    if ats == "Workday":
        match = re.search(r"/jobs/(\d+)", url)
        if match:
            return f"workday:{job.get('company', '')}:{match.group(1)}"
    if ats == "Paylocity":
        jid = job.get("id")
        if jid:
            return f"paylocity:{jid}"
        # no job_id: key on company+title so they don't collapse to one listing URL
        return f"paylocity:{job.get('company', '')}:{job.get('title', '')}"
    return url


COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|hq|careers|jobs)\b")


def job_identity(company, title):
    """Company + title, normalized, so the same opening matches across job boards and
    reposts: "Agile-Defense" / "agiledefense", "Senior Engineer (Remote)" / "senior engineer"."""
    company = (company or "").lower()
    company = COMPANY_SUFFIXES.sub(" ", re.sub(r"[-_.]", " ", company))
    company = re.sub(r"[^a-z0-9]", "", company)
    title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", (title or "").lower())
    title = re.sub(r"\b(remote|hybrid)\b", " ", title)
    title = " ".join(re.sub(r"[^a-z0-9+#]+", " ", title).split())
    return f"{company}|{title}"
