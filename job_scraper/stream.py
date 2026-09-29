"""Stream jobs to their tabs while the scrape runs, instead of after it finishes.

Fetch threads hand each company's jobs to ``Streamer.put``. One background thread
filters them per tab and pushes a tab's matches to the sheet every ``batch_size`` jobs,
or every ``flush_seconds`` when fewer arrive. Jobs that match no tab are dropped right
away, so memory stays small however many companies are scraped.
"""

import queue
import threading
import time

from .scrape import apply_filters, clean_job_data, enrich_salary, job_identity


class Tab:
    """One output tab: its filters, the jobs kept this run, and an optional SheetWriter."""

    def __init__(self, name, filters, writer=None):
        self.name, self.filters, self.writer = name, filters, writer
        self.kept = []     # every job kept this run, for output/<tab>.json
        self.pending = []  # kept but not yet pushed to the sheet
        self._keys, self._idents = set(), set()

    def offer(self, jobs):
        """Queue the jobs that pass this tab's filters. A job whose key or company + title
        was already kept this run (a repost, or the same job on another board) is skipped."""
        for job in apply_filters(jobs, self.filters, verbose=False):
            ident = job_identity(job.get("company"), job.get("title"))
            if job["key"] in self._keys or ident in self._idents:
                continue
            self._keys.add(job["key"])
            self._idents.add(ident)
            self.pending.append(job)

    def flush(self):
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        enrich_salary(batch)
        self.kept.extend(batch)
        if self.writer:
            added = self.writer.add(batch)
            print(f"  [{self.name}] +{added:,} new rows ({len(self.kept):,} matched so far)")


class Streamer:
    def __init__(self, tabs, batch_size=100, flush_seconds=60):
        self.tabs, self.batch_size, self.flush_seconds = tabs, batch_size, flush_seconds
        # Bounded, so fetch threads wait instead of piling up jobs if the sheet is slow.
        self._queue = queue.Queue(maxsize=500)
        self._error = None
        self._thread = threading.Thread(target=self._run, name="sheet-sync", daemon=True)
        self._thread.start()

    def put(self, jobs):
        """Called from fetch threads with one company's jobs."""
        while True:
            if self._error:
                raise RuntimeError("sheet sync failed; stopping the scrape") from self._error
            try:
                self._queue.put(jobs, timeout=1)
                return
            except queue.Full:
                continue

    def close(self):
        """Push whatever is left and stop the thread. Raises if syncing failed."""
        if not self._error:
            self._queue.put(None)
        self._thread.join()
        if self._error:
            raise self._error

    def _run(self):
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    jobs = self._queue.get(timeout=1)
                except queue.Empty:
                    jobs = []
                if jobs is None:
                    break
                if jobs:
                    jobs = clean_job_data(jobs)
                    for tab in self.tabs:
                        tab.offer(jobs)
                due = time.monotonic() - last_flush >= self.flush_seconds
                for tab in self.tabs:
                    if len(tab.pending) >= self.batch_size or due:
                        tab.flush()
                if due:
                    last_flush = time.monotonic()
            for tab in self.tabs:
                tab.flush()
        except BaseException as e:  # surfaced to fetch threads by put() and to main by close()
            self._error = e
