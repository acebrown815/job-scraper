"""Scrape orchestration: load company lists, fetch in parallel, clean, enrich, filter."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from .fetchers import FETCHERS, MAX_WORKERS, load_paylocity
from .geolocation import location_places

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


def fetch_all_jobs(platform, companies):
    """Fetch jobs from all companies of one platform in parallel."""
    fetcher = FETCHERS[platform]
    dead_slugs = load_dead_slugs(platform)
    live_companies = [s for s in companies if s not in dead_slugs]
    print(f"[{platform}] checking {len(live_companies):,} companies "
          f"(skipping {len(dead_slugs):,} known dead)")

    all_jobs = []
    active = 0
    new_dead = set()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS.get(platform, 30)) as executor:
        futures = [executor.submit(fetcher, slug) for slug in live_companies]
        for i, future in enumerate(as_completed(futures), 1):
            slug, jobs, status_code = future.result()
            if jobs:
                all_jobs.extend(jobs)
                active += 1
            elif status_code in (404, 410):
                # Only cache permanent failures
                new_dead.add(slug)
            if i % 200 == 0:
                print(f"  [{platform}] {i:,}/{len(live_companies):,} checked, "
                      f"{len(all_jobs):,} jobs so far")

    if new_dead:
        save_dead_slugs(platform, dead_slugs | new_dead)

    print(f"[{platform}] done: {active:,} active companies, {len(all_jobs):,} jobs, "
          f"{len(new_dead):,} newly dead")
    return all_jobs


def scrape(platforms, max_companies=None):
    """Scrape the given platforms concurrently. Returns a flat list of jobs."""
    work = []
    for platform in platforms:
        companies = sorted(load_companies(platform))
        if max_companies:
            companies = companies[:max_companies]
        if companies:
            work.append((platform, companies))

    all_jobs = []
    with ThreadPoolExecutor(max_workers=max(len(work), 1)) as executor:
        futures = {executor.submit(fetch_all_jobs, p, c): p for p, c in work}
        for future in as_completed(futures):
            all_jobs.extend(future.result())
    return all_jobs


# ============================================================
# Clean / enrich / filter
# ============================================================


def clean_job_data(jobs):
    """Remove entries without a usable title, URL or company."""
    cleaned = [
        job for job in jobs
        if (job.get("title") or "").strip().lower() not in ("", "not specified", "n/a", "unknown")
        and job.get("url")
        and (job.get("company") or job.get("company_slug"))
    ]
    if len(cleaned) != len(jobs):
        print(f"Dropped {len(jobs) - len(cleaned):,} invalid jobs")
    return cleaned


def enrich_salary(jobs):
    """Attach median salary from the static lookup (company|title|level, then title|level)."""
    path = os.path.join(DATA_DIR, "salary_lookup.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    primary, fallback = data.get("primary", {}), data.get("fallback", {})

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


def _in_countries(job, countries):
    """Country filter. On-site jobs need a parsed country in the list. Remote jobs pass
    if their location names an allowed country or region (e.g. "Remote, US", "North
    America"), or names no place at all ("Remote", "Anywhere"). Remote jobs tied to other
    countries ("Remote - LATAM", "Brazil") or to unrecognized places are dropped."""
    if job.get("country"):
        return job["country"] in countries
    if not job.get("remote"):
        return False
    codes, unknown = location_places(job.get("location"))
    if codes:
        return bool(codes & countries)
    return not unknown


def apply_filters(jobs, filters):
    """Keep only jobs matching the [filters] config section. Empty/missing options are ignored."""
    include = [re.compile(p, re.I) for p in filters.get("title_include", [])]
    exclude = [re.compile(p, re.I) for p in filters.get("title_exclude", [])]
    levels = set(filters.get("skill_levels", []))
    countries = {c.upper() for c in filters.get("countries", [])}
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
        if countries and not _in_countries(job, countries):
            continue
        if exclude_recruiters and job.get("is_recruiter"):
            continue
        if cutoff:
            posted = _parse_date(job.get("updated_at"))
            # Jobs with no date (Ashby, BambooHR) are kept; we can't tell their age.
            if posted and posted < cutoff:
                continue
        kept.append(job)

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


def dedupe(jobs):
    seen = {}
    for job in jobs:
        key = get_dedup_key(job)
        if key:
            job["key"] = key
            seen[key] = job
    if len(seen) != len(jobs):
        print(f"Removed {len(jobs) - len(seen):,} duplicate jobs")
    return list(seen.values())


def _repost_ident(job):
    company = (job.get("company_slug") or job.get("company") or "").lower().strip()
    title = " ".join((job.get("title") or "").lower().split())
    return company, title, bool(job.get("remote"))


def collapse_reposts(jobs):
    """Keep one job per company + title + remote. Companies often post the same opening
    once per location, each with its own URL. Run this after filtering so the survivor
    is one that matched. The smallest key wins, so the same row survives across runs."""
    best = {}
    for job in jobs:
        ident = _repost_ident(job)
        if ident not in best or job["key"] < best[ident]["key"]:
            best[ident] = job
    if len(best) != len(jobs):
        print(f"Collapsed {len(jobs) - len(best):,} reposts of the same job")
    return [job for job in jobs if best[_repost_ident(job)] is job]
