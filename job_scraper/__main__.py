"""CLI: scrape ATS job boards and upsert the results into a Google Sheet.

    python -m job_scraper                         # uses ./config.toml
    python -m job_scraper --platforms greenhouse lever --max-companies 50
    python -m job_scraper --dry-run               # skip the sheet, write output/jobs.json
"""

import argparse
import json
import os
import time
import tomllib

from .fetchers import FETCHERS
from .scrape import (
    ROOT_DIR, apply_filters, clean_job_data, collapse_reposts, dedupe, enrich_salary, scrape,
)
from .sheets import sync_to_sheet


def load_config(path):
    if not os.path.exists(path):
        print(f"No config at {path}; using defaults (see config.example.toml)")
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def main():
    parser = argparse.ArgumentParser(description="Scrape ATS job boards into Google Sheets")
    parser.add_argument("--config", default=os.path.join(ROOT_DIR, "config.toml"))
    parser.add_argument("--platforms", nargs="+", choices=sorted(FETCHERS),
                        help="Override [scrape].platforms")
    parser.add_argument("--max-companies", type=int,
                        help="Only check the first N companies per platform (for testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't touch Google Sheets; just write output/jobs.json")
    args = parser.parse_args()

    config = load_config(args.config)
    scrape_cfg = config.get("scrape", {})
    sheets_cfg = config.get("sheets", {})

    platforms = args.platforms or scrape_cfg.get("platforms") or sorted(FETCHERS)
    max_companies = args.max_companies or scrape_cfg.get("max_companies_per_platform")

    started = time.time()
    jobs = scrape(platforms, max_companies=max_companies)
    print(f"Scraped {len(jobs):,} jobs in {time.time() - started:.0f}s")

    jobs = clean_job_data(jobs)
    jobs = dedupe(jobs)
    jobs = apply_filters(jobs, config.get("filters", {}))
    jobs = collapse_reposts(jobs)
    enrich_salary(jobs)

    output_dir = os.path.join(ROOT_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "jobs.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=1)
    print(f"Saved {len(jobs):,} jobs to {output_file}")

    if args.dry_run:
        return

    sync_to_sheet(
        jobs,
        webapp_url=os.environ.get("JOB_SCRAPER_WEBAPP_URL") or sheets_cfg.get("webapp_url"),
        token=os.environ.get("JOB_SCRAPER_TOKEN") or sheets_cfg.get("token"),
        worksheet=sheets_cfg.get("worksheet", "Jobs"),
        prune_after_days=sheets_cfg.get("prune_after_days", 30),
        max_rows=sheets_cfg.get("max_rows", 100_000),
    )


if __name__ == "__main__":
    main()
