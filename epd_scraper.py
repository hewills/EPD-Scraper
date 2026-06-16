"""
Eugene Police Department Dispatch Log Scraper
================================================
Scrapes the public EPD dispatch log search tool at
coeapps.eugene-or.gov and stores results in a local SQLite database.

The search tool only returns up to 250 rows per query. For busy date
ranges that hit this cap, the script automatically re-queries the same
window filtered by each individual priority level (P, 0-9) to split
the results into smaller chunks that stay under the limit.

Resume capability: every date window that's processed is logged to a
'scrape_progress' table. Re-running the script skips any window
already marked "done", so an interrupted run can simply be restarted.

Usage:
    python epd_scraper.py

Configure the date range, window size, and request delay in the
"Config" section below.
"""

import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Public search endpoint for EPD's dispatch log tool. No authentication
# required — this is a public records search form.
URL = "https://coeapps.eugene-or.gov/epddispatchlog/Home/Search"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": URL,
}

# Date range to scrape, in calendar order.
START_DATE = datetime(2026, 4, 1)
END_DATE = datetime(2026, 6, 16)

# How many days of dispatch data to request per query. Smaller windows
# mean more requests but lower odds of hitting the 250-row response cap.
WINDOW_DAYS = 1

# Pause between requests, in seconds. Keeps load on the city's server
# reasonable and reduces the chance of being rate-limited or blocked.
SLEEP_SECONDS = 5

# The site returns a max of 250 rows per query. If a window hits this
# cap, we re-query it once per priority level below to split the
# results into smaller, hopefully-under-the-cap chunks.
ROW_LIMIT = 250
PRIORITY_LEVELS = ["P", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]

DB_PATH = "epd_dispatch.db"


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

import sqlite3

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("""
    CREATE TABLE IF NOT EXISTS dispatch (
        event_number  TEXT PRIMARY KEY,
        call_time     TEXT,
        dispatch_time TEXT,
        incident_desc TEXT,
        disposition   TEXT,
        location      TEXT,
        priority      TEXT,
        case_number   TEXT,
        lat           REAL,
        lon           REAL,
        geocoded      INTEGER DEFAULT 0,
        scraped_at    TEXT
    )
""")

# Progress tracking table. Each row represents one date window that's
# been attempted; the `status` column drives the resume logic in run().
cur.execute("""
    CREATE TABLE IF NOT EXISTS scrape_progress (
        id            INTEGER PRIMARY KEY,
        date_from     TEXT,
        date_through  TEXT,
        rows_found    INTEGER,
        status        TEXT,   -- 'done', 'error', 'hit_limit'
        scraped_at    TEXT
    )
""")
conn.commit()


# ---------------------------------------------------------------------------
# Date range generator
# ---------------------------------------------------------------------------

def date_ranges(start, end, delta_days=3):
    """
    Yield (window_start, window_end) datetime pairs covering [start, end]
    in chunks of `delta_days` days each. The final chunk is clamped to
    `end` even if it's shorter than delta_days.
    """
    current = start
    while current < end:
        chunk_end = min(current + timedelta(days=delta_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def already_scraped(date_from, date_through):
    """Check whether a given date window was already successfully scraped,
    so `run()` can skip it on a resumed/re-run."""
    cur.execute("""
        SELECT id FROM scrape_progress
        WHERE date_from = ? AND date_through = ? AND status = 'done'
    """, (date_from, date_through))
    return cur.fetchone() is not None


def log_progress(date_from, date_through, rows_found, status):
    """Record the outcome of a scraped window for resume/auditing purposes."""
    cur.execute("""
        INSERT INTO scrape_progress
            (date_from, date_through, rows_found, status, scraped_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        date_from, date_through, rows_found, status,
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_window(date_from_str, date_through_str, priority=""):
    """
    Fetch dispatch records for a single date window, optionally filtered
    to one priority level (used to split oversized windows — see
    PRIORITY_LEVELS / ROW_LIMIT above).

    The site uses CSRF token (__RequestVerificationToken)
    that must be read from a fresh GET before it can be submitted with
    the search POST, so each call performs both steps.

    Returns:
        (rows, status) where status is one of:
          "done"      - query succeeded (may be zero rows)
          "hit_limit" - query returned exactly ROW_LIMIT rows, meaning
                        there are likely more rows than we captured
          "error"     - request failed or response was unparseable
    """
    session = requests.Session()

    try:
        # Step 1: GET the search page to obtain a valid CSRF token.
        response = session.get(URL, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        token = soup.find("input", {"name": "__RequestVerificationToken"})["value"]

        # Step 2: POST the search form with our date range (and optional
        # priority filter) plus the token we just retrieved.
        form_data = {
            "DateFrom": date_from_str,
            "DateThrough": date_through_str,
            "IncidentType": "",
            "Disposition": "",
            "Priority": priority,
            "EventNumberFilterOption": "IsExactly",
            "EventNumber": "",
            "StreetNumberFilterOption": "IsExactly",
            "StreetNumber": "",
            "StreetNameFilterOption": "IsExactly",
            "StreetName": "",
            "CaseNumberFilterOption": "IsExactly",
            "CaseNumber": "",
            "__RequestVerificationToken": token,
        }

        response = session.post(URL, headers=HEADERS, data=form_data, timeout=10)

        # A quick sanity check: if the results table marker isn't present
        # at all, treat it as "no results" rather than attempting to parse.
        if "calls" not in response.text:
            return [], "done"

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table", {"id": "calls"})
        rows = []

        for tr in table.find("tbody").find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 9:
                # Skip malformed/header rows that don't have all expected columns.
                continue
            rows.append({
                "event_number": cells[5].text.strip(),
                "call_time": cells[1].text.strip(),
                "dispatch_time": cells[2].text.strip(),
                "incident_desc": cells[3].text.strip(),
                "disposition": cells[4].text.strip(),
                "location": cells[6].text.strip(),
                "priority": cells[7].text.strip(),
                "case_number": cells[8].text.strip(),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

        # Exactly hitting the cap is our signal that there may be more
        # rows than we received — the caller decides how to split further.
        status = "hit_limit" if len(rows) == ROW_LIMIT else "done"
        return rows, status

    except Exception as e:
        print(f"  ❌ Error: {e}")
        return [], "error"


def save_rows(rows):
    """Insert or update dispatch rows. event_number is the primary key,
    so re-scraping the same window is idempotent. Existing rows are
    simply overwritten with the latest data."""
    if not rows:
        return
    cur.executemany("""
        INSERT OR REPLACE INTO dispatch VALUES (
            :event_number, :call_time, :dispatch_time, :incident_desc,
            :disposition, :location, :priority, :case_number,
            NULL, NULL, 0, :scraped_at
        )
    """, rows)
    conn.commit()


def scrape_window_by_priority(date_from_str, date_through_str):
    """
    Re-scrape a date window once per priority level, used when the
    unfiltered query for that window hit ROW_LIMIT. Saves and logs each
    priority's results as it goes (so partial progress survives a crash),
    and returns the combined row count across all priority levels.
    """
    total_rows = 0
    for priority in PRIORITY_LEVELS:
        rows, status = scrape_window(date_from_str, date_through_str, priority)
        save_rows(rows)
        log_progress(date_from_str, date_through_str, len(rows), status)
        total_rows += len(rows)
    return total_rows


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    all_windows = list(date_ranges(START_DATE, END_DATE, WINDOW_DAYS))
    total = len(all_windows)

    print(f"Total windows to scrape: {total}")
    print(f"Estimated time at {SLEEP_SECONDS}s each: ~{(total * SLEEP_SECONDS) / 60:.0f} minutes\n")

    for i, (date_from, date_through) in enumerate(all_windows):
        date_from_str = date_from.strftime("%Y-%m-%d")
        date_through_str = date_through.strftime("%Y-%m-%d")

        # Resume support: skip windows already marked "done" in a prior run.
        if already_scraped(date_from_str, date_through_str):
            print(f"[{i+1}/{total}] Skipping {date_from_str} → {date_through_str} (already done)")
            continue

        print(f"[{i+1}/{total}] Scraping {date_from_str} → {date_through_str}", end=" ... ")

        rows, status = scrape_window(date_from_str, date_through_str)

        if status == "hit_limit":
            # Too many rows for one query — split by priority level instead.
            print(f"⚠️  HIT LIMIT ({len(rows)}) — splitting by priority")
            total_rows = scrape_window_by_priority(date_from_str, date_through_str)
            print(f"  → {total_rows} rows across all priority levels")
            time.sleep(SLEEP_SECONDS)
            continue

        elif status == "error":
            print("❌ Error — will retry next run")
            log_progress(date_from_str, date_through_str, 0, "error")
            time.sleep(SLEEP_SECONDS)
            continue

        else:
            print(f"✅ {len(rows)} rows")

        save_rows(rows)
        log_progress(date_from_str, date_through_str, len(rows), status)

        time.sleep(SLEEP_SECONDS)

    print("\nDone! Checking totals...")
    cur.execute("SELECT COUNT(*) FROM dispatch")
    print(f"Total incidents in DB: {cur.fetchone()[0]:,}")

    cur.execute("SELECT COUNT(*) FROM scrape_progress WHERE status = 'error'")
    errors = cur.fetchone()[0]
    if errors:
        print(f"⚠️  {errors} windows errored — re-run to retry them")


if __name__ == "__main__":
    run()
    conn.close()
