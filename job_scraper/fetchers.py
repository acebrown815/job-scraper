"""Per-ATS job fetchers.

Every fetcher takes a company slug and returns ``(slug, jobs, status_code)``.
``status_code`` is ``None`` on network errors; 404/410 mark the slug as dead.
Jobs are normalized to a common dict shape (see ``_job``).
"""

import html
import json
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter

from .geolocation import parse_job_location

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
]

RECRUITER_TERMS = [
    "recruit", "recruiting", "recruiter", "staffing", "staff", "talent",
    "talenthub", "talentgroup", "solutions", "consulting", "placement",
    "search", "resources", "agency",
]

# guid -> company name, filled by load_paylocity()
PAYLOCITY_NAMES = {}

PAGEDATA_RE = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*</script>", re.DOTALL)

# one requests.Session per worker thread, with a capped connection pool
_paylocity_local = threading.local()


# ============================================================
# Classification helpers
# ============================================================


def is_recruiter_company(slug):
    slug = slug.lower()
    return any(term in slug for term in RECRUITER_TERMS)


_TIER_PATTERNS = [
    (re.compile(r"\b(?:chief|cto|ceo|cfo|vp|vice president|director)\b"), 50),
    (re.compile(r"\b(?:principal|distinguished|fellow)\b"), 40),
    (re.compile(r"\b(?:staff|lead|head of)\b"), 30),
    (re.compile(r"\b(?:senior|sr\.?)\b"), 20),
    (re.compile(r"\b(?:architect|manager)\b"), 15),
    (re.compile(r"\b(?:iii|iv|v|vi)\b"), 15),
    (re.compile(r"\blevel\s*[4-9]\b"), 15),
    (re.compile(r"\bengr?\s*[4-6]\b"), 15),
    (re.compile(r"\b(?:counsel|of\s*counsel)\b"), 20),
    (re.compile(r"\b(?:attending|charge)\b"), 20),
    (re.compile(r"\b(?:ii|2)\b"), 5),
    (re.compile(r"\blevel\s*3\b"), 5),
    (re.compile(r"\b(?:associate)\b"), -10),
    (re.compile(r"\b(?:junior|jr\.?)\b"), -20),
    (re.compile(r"\bentry[\s-]?level\b"), -25),
    (re.compile(r"\b(?:i|1)\b(?!\s*-|\d)"), -15),
    (re.compile(r"\b(?:trainee|graduate|new\s*grad)\b"), -25),
    (re.compile(r"\b(?:paralegal|clerk)\b"), -15),
    (re.compile(r"\b(?:resident|clinical\s*fellow)\b"), -15),
    (re.compile(r"\b(?:aide|assistant|tech)\b"), -10),
    (re.compile(r"\bintern(?:ship)?\b"), -100),
]


def job_tier_classification(title):
    title_lower = (title or "").lower()
    score = 0
    for pattern, weight in _TIER_PATTERNS:
        if pattern.search(title_lower):
            score += weight
    if score <= -50:
        return "intern"
    elif score <= -5:
        return "entry"
    elif score >= 15:
        return "senior"
    else:
        return "mid"


def _job(company, slug, title, location, url, ats, *, updated_at=None,
         remote=False, job_id=None, departments=None, recruiter_name=None):
    """Build the normalized job record shared by every fetcher."""
    parsed = parse_job_location(location)
    return {
        "company": company,
        "company_slug": slug,
        "title": title,
        "location": location,
        "remote": bool(remote) or parsed["remote"],
        "country": parsed["country"],
        "url": url,
        "id": job_id,
        "departments": departments or [],
        "updated_at": updated_at,
        "is_recruiter": is_recruiter_company(recruiter_name or company),
        "ats": ats,
        "skill_level": job_tier_classification(title),
        "scraped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ============================================================
# Fetchers
# ============================================================


def fetch_company_jobs_greenhouse(slug):
    """https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"""
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        response = requests.get(url, timeout=30)
        if response.status_code != 200:
            return slug, [], response.status_code

        normalized = []
        for job in response.json().get("jobs", []):
            normalized.append(
                _job(
                    slug, slug,
                    job.get("title"),
                    (job.get("location") or {}).get("name") or "Not specified",
                    job.get("absolute_url"),
                    "Greenhouse",
                    updated_at=job.get("updated_at"),
                    job_id=job.get("id"),
                    departments=[d.get("name") for d in job.get("departments", [])],
                )
            )
        return slug, normalized, response.status_code
    except Exception as e:
        print(f"Error fetching Greenhouse for {slug}: {e}")
    return slug, [], None


def fetch_company_jobs_ashby(slug):
    try:
        url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
        payload = {
            "operationName": "ApiJobBoardWithTeams",
            "variables": {"organizationHostedJobsPageName": slug},
            "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { jobPostings { id title locationName } } }",
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": random.choice(USER_AGENTS),
        }

        # Jitter before request to spread out concurrent workers
        time.sleep(random.uniform(0.5, 2.0))

        max_retries = 2
        for attempt in range(max_retries + 1):
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code == 200:
                break
            if response.status_code in (429, 503, 502) and attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(0.5, 1.5))
                headers["User-Agent"] = random.choice(USER_AGENTS)
                continue
            return slug, [], response.status_code

        data = response.json()
        jobs = ((data.get("data") or {}).get("jobBoard") or {}).get("jobPostings") or []

        normalized = []
        for job in jobs:
            normalized.append(
                _job(
                    slug, slug,
                    job.get("title", ""),
                    (job.get("locationName") or "Not specified")[:50],
                    f"https://jobs.ashbyhq.com/{slug}/{job.get('id')}",
                    "Ashby",
                    job_id=job.get("id"),
                )
            )
        return slug, normalized, response.status_code
    except Exception as e:
        print(f"Error fetching Ashby for {slug}: {e}")
    return slug, [], None


def fetch_company_jobs_bamboohr(slug):
    """https://{slug}.bamboohr.com/careers/list"""
    url = f"https://{slug}.bamboohr.com/careers/list"

    time.sleep(random.uniform(0.5, 2.0))

    max_retries = 2
    for attempt in range(max_retries + 1):
        headers = {
            "Accept": "application/json",
            "User-Agent": random.choice(USER_AGENTS),
        }
        try:
            response = requests.get(url, timeout=30, headers=headers)

            if response.status_code == 200:
                if "application/json" not in response.headers.get("Content-Type", ""):
                    return slug, [], 404

                normalized = []
                for job in response.json().get("result", []):
                    loc = job.get("location") or {}
                    if isinstance(loc, dict):
                        location = (
                            ", ".join(filter(None, [loc.get("city", ""), loc.get("state", "")]))
                            or "Not specified"
                        )
                    else:
                        location = str(loc) if loc else "Not specified"

                    normalized.append(
                        _job(
                            slug, slug,
                            job.get("jobOpeningName"),
                            location[:50],
                            f"https://{slug}.bamboohr.com/careers/{job.get('id')}",
                            "BambooHR",
                            job_id=job.get("id"),
                        )
                    )
                return slug, normalized, response.status_code

            if response.status_code in (429, 503, 502) and attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(0.5, 1.5))
                continue

            return slug, [], response.status_code

        except requests.exceptions.SSLError:
            if attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(0.5, 1.5))
                continue
            return slug, [], None
        except Exception as e:
            print(f"Error fetching BambooHR for {slug}: {e}")
            return slug, [], None

    return slug, [], None


def fetch_company_jobs_lever(slug):
    """https://api.lever.co/v0/postings/{slug}"""
    try:
        url = f"https://api.lever.co/v0/postings/{slug}"
        response = requests.get(url, timeout=30)
        if response.status_code != 200:
            return slug, [], response.status_code

        normalized = []
        for job in response.json() or []:
            categories = job.get("categories") or {}
            created = job.get("createdAt")  # epoch millis
            updated_at = (
                datetime.fromtimestamp(created / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
                if isinstance(created, (int, float))
                else None
            )
            normalized.append(
                _job(
                    slug, slug,
                    job.get("text"),
                    (categories.get("location") or "Not specified")[:50],
                    job.get("hostedUrl"),
                    "Lever",
                    updated_at=updated_at,
                    remote=job.get("workplaceType") == "remote",
                    job_id=job.get("id"),
                    departments=[categories["team"]] if categories.get("team") else [],
                )
            )
        return slug, normalized, response.status_code
    except Exception as e:
        print(f"Error fetching Lever for {slug}: {e}")
    return slug, [], None


def _parse_workday_posted_on(text):
    """Convert Workday's relative string (e.g. 'Posted 2 Days Ago') to an ISO date."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip().lower()
    today = datetime.now(timezone.utc).date()
    if "today" in t:
        return today.isoformat()
    m = re.search(r"(\d+)\s+day", t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s+week", t)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s+month", t)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).isoformat()
    return None


def fetch_company_jobs_workday(slug):
    """
    slug format: "company|wd#|site_id" e.g. "kohls|wd1|kohlscareers"
    url: https://{company}.wd{num}.myworkdayjobs.com/wday/cxs/{company}/{site_id}/jobs
    """
    try:
        parts = slug.split("|")
        if len(parts) != 3:
            return slug, [], None

        company, wd, site_id = parts
        wd_num = wd.replace("wd", "")

        base_url = f"https://{company}.wd{wd_num}.myworkdayjobs.com"
        api_url = f"{base_url}/wday/cxs/{company}/{site_id}/jobs"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": random.choice(USER_AGENTS),
            "Origin": base_url,
            "Referer": f"{base_url}/{site_id}",
        }

        normalized = []
        offset = 0
        limit = 20
        retries = 0
        max_retries = 2
        observed_total = None

        while True:
            payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
            response = requests.post(api_url, json=payload, headers=headers, timeout=30)

            if response.status_code != 200:
                if retries < max_retries:
                    retries += 1
                    time.sleep(random.uniform(2.0, 4.0))
                    continue
                break

            data = response.json()
            jobs = data.get("jobPostings", [])
            total = data.get("total", 0)

            # Workday sometimes lies about the total mid-pagination when blocking
            if observed_total is None:
                observed_total = total
            elif total != observed_total:
                break

            if not jobs:
                break

            for job in jobs:
                normalized.append(
                    _job(
                        company, slug,
                        job.get("title"),
                        (job.get("locationsText") or "Not specified")[:50],
                        f"{base_url}/{site_id}{job.get('externalPath', '')}",
                        "Workday",
                        updated_at=_parse_workday_posted_on(job.get("postedOn")),
                    )
                )

            offset += limit
            if offset >= total:
                break

            # Jitter between pages (critical)
            time.sleep(random.uniform(0.3, 1.0))

        return slug, normalized, response.status_code
    except Exception:
        return slug, [], None


def fetch_company_jobs_icims(slug):
    """
    https://careers-{slug}.icims.com/sitemap.xml

    Title is extracted from the job URL path; location is not available via sitemap.
    """
    sitemap_url = f"https://careers-{slug}.icims.com/sitemap.xml"
    headers = {"Accept": "application/xml", "User-Agent": random.choice(USER_AGENTS)}

    try:
        resp = requests.get(sitemap_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return slug, [], resp.status_code

        root = ET.fromstring(resp.content)
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        normalized = []
        for url_el in root.findall(".//s:url", ns):
            loc_el = url_el.find("s:loc", ns)
            if loc_el is None:
                continue
            job_url = loc_el.text.strip() if loc_el.text else ""
            if not job_url or "/jobs/" not in job_url or job_url.endswith("/jobs/intro"):
                continue

            parts = job_url.split("/jobs/")[-1].split("/")
            if len(parts) < 2:
                continue
            title = unquote(parts[1]).replace("-", " ").strip().title()

            lastmod_el = url_el.find("s:lastmod", ns)
            updated_at = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else None

            normalized.append(
                _job(slug, slug, title, "Not specified", job_url, "iCIMS",
                     updated_at=updated_at, job_id=parts[0])
            )

        return slug, normalized, resp.status_code
    except Exception as e:
        print(f"Error fetching iCIMS for {slug}: {e}")
        return slug, [], None


def load_paylocity(filepath):
    """Paylocity file is [{guid, name, jobs}, ...], not a flat slug list.
    Returns a set of GUIDs and fills PAYLOCITY_NAMES (guid -> name)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        print(f"File not found: {filepath}")
        return set()
    guids = set()
    for r in rows:
        g = r.get("guid")
        if not g:
            continue
        guids.add(g)
        PAYLOCITY_NAMES[g] = html.unescape(r.get("name") or g)
    return guids


def _paylocity_session():
    s = getattr(_paylocity_local, "session", None)
    if s is None:
        s = requests.Session()
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4))
        _paylocity_local.session = s
    return s


def _paylocity_location(j):
    """JobLocation carries the real city/state. LocationName is an internal
    label ('Main', 'AVI') and is only a last-resort fallback."""
    loc = j.get("JobLocation") or {}
    city, state = loc.get("City"), loc.get("State")
    if city and state:
        return html.unescape(f"{city}, {state}")
    if city:
        return html.unescape(city)
    return html.unescape(j.get("LocationName") or "Not specified")


def fetch_company_jobs_paylocity(slug):
    """slug is the Paylocity tenant GUID. Jobs come from window.pageData in the
    page HTML, not a JSON API. Name is resolved from PAYLOCITY_NAMES."""
    url = f"https://recruiting.paylocity.com/recruiting/jobs/All/{slug}/"
    session = _paylocity_session()
    # jitter so concurrent workers don't fire in lockstep
    time.sleep(random.uniform(0.5, 2.0))
    max_retries = 3
    for attempt in range(max_retries + 1):
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            response = session.get(url, timeout=30, headers=headers)
        except requests.RequestException as e:
            # reset/abort = throttling. drop the poisoned connection, back off, retry.
            try:
                session.close()
            except Exception:
                pass
            _paylocity_local.session = None
            session = _paylocity_session()
            if attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(1.0, 2.0))
                continue
            print(f"Error fetching Paylocity for {slug}: {e}")
            return slug, [], None
        if response.status_code in (429, 503, 502):
            if attempt < max_retries:
                time.sleep((2**attempt) + random.uniform(1.0, 2.0))
                continue
            return slug, [], response.status_code
        if response.status_code != 200:
            return slug, [], response.status_code
        m = PAGEDATA_RE.search(response.text)
        if not m:
            # page loaded but blob missing = soft block or layout drift.
            # return 200 so it retries next run, NOT cached as dead.
            return slug, [], 200
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return slug, [], 200
        company = PAYLOCITY_NAMES.get(slug, slug)
        normalized = []
        for job in data.get("Jobs") or []:
            job_id = job.get("JobId")
            title = html.unescape(job.get("JobTitle") or "")
            dept = job.get("HiringDepartment")  # almost always null on Paylocity
            detail = (
                f"https://recruiting.paylocity.com/recruiting/Jobs/Details/{job_id}"
                if job_id
                else url
            )
            normalized.append(
                _job(
                    company, slug, title, _paylocity_location(job), detail, "Paylocity",
                    updated_at=job.get("PublishedDate"),
                    remote=job.get("IsRemote"),
                    job_id=job_id,
                    departments=[html.unescape(dept)] if dept else [],
                )
            )
        return slug, normalized, response.status_code
    return slug, [], None


FETCHERS = {
    "greenhouse": fetch_company_jobs_greenhouse,
    "ashby": fetch_company_jobs_ashby,
    "bamboohr": fetch_company_jobs_bamboohr,
    "lever": fetch_company_jobs_lever,
    "workday": fetch_company_jobs_workday,
    "icims": fetch_company_jobs_icims,
    "paylocity": fetch_company_jobs_paylocity,
}

# Tuned per platform to respect each one's rate limits.
MAX_WORKERS = {
    "bamboohr": 10,
    "greenhouse": 30,
    "ashby": 5,
    "lever": 30,
    "workday": 50,
    "icims": 30,
    "paylocity": 5,
}
