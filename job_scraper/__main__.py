"""CLI: scrape ATS job boards and upsert the results into a Google Sheet.

    python -m job_scraper                         # uses ./config.toml
    python -m job_scraper --platforms greenhouse lever --max-companies 50
    python -m job_scraper --dry-run               # skip the sheet, write output/<tab>.json
"""

import argparse
import json
import os
import re
import time
import tomllib

from .fetchers import FETCHERS
from .scrape import ROOT_DIR, scrape
from .sheets import SheetWriter
from .stream import Streamer, Tab


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
                        help="Don't touch Google Sheets; just write output/<tab>.json")
    args = parser.parse_args()

    config = load_config(args.config)
    scrape_cfg = config.get("scrape", {})
    sheets_cfg = config.get("sheets", {})

    platforms = args.platforms or scrape_cfg.get("platforms") or sorted(FETCHERS)
    max_companies = args.max_companies or scrape_cfg.get("max_companies_per_platform")

    base_filters = config.get("filters", {})
    tabs = []
    for tab in get_tabs(sheets_cfg):
        # Each tab's filter keys override [filters]; the rest of the tab is sheet options.
        tab_filters = {k: v for k, v in tab.items() if k not in TAB_SHEET_KEYS}
        writer = None
        if not args.dry_run:
            writer = SheetWriter(
                webapp_url=os.environ.get("JOB_SCRAPER_WEBAPP_URL") or sheets_cfg.get("webapp_url"),
                token=os.environ.get("JOB_SCRAPER_TOKEN") or sheets_cfg.get("token"),
                worksheet=tab["worksheet"],
                prune_after_days=tab.get("prune_after_days",
                                         sheets_cfg.get("prune_after_days", 30)),
                max_rows=tab.get("max_rows", sheets_cfg.get("max_rows", 100_000)),
            )
        tabs.append(Tab(tab["worksheet"], {**base_filters, **tab_filters}, writer))

    # Matches are pushed to the sheet in batches while the scrape is still running.
    streamer = Streamer(tabs, batch_size=sheets_cfg.get("batch_size", 100),
                        flush_seconds=sheets_cfg.get("flush_seconds", 60))
    started = time.time()
    total = scrape(platforms, streamer.put, max_companies=max_companies)
    streamer.close()
    print(f"Scraped {total:,} jobs in {time.time() - started:.0f}s")

    output_dir = os.path.join(ROOT_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    for tab in tabs:
        output_file = os.path.join(output_dir, f"{_slug(tab.name)}.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(tab.kept, f, ensure_ascii=False, indent=1)
        print(f"[{tab.name}] {len(tab.kept):,} jobs matched; saved to {output_file}")
        if tab.writer:
            tab.writer.finish()


# Keys in a [[sheets.tabs]] entry that configure the sheet rather than filter jobs.
TAB_SHEET_KEYS = {"worksheet", "prune_after_days", "max_rows"}


def get_tabs(sheets_cfg):
    """[[sheets.tabs]] entries, or a single tab from [sheets].worksheet when none are set."""
    tabs = sheets_cfg.get("tabs") or [{"worksheet": sheets_cfg.get("worksheet", "Jobs")}]
    names = [t.get("worksheet") for t in tabs]
    if not all(names) or len(set(names)) != len(names):
        raise SystemExit("Each [[sheets.tabs]] entry needs a unique worksheet name.")
    return tabs


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "jobs"


if __name__ == "__main__":
    main()
