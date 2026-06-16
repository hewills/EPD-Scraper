# EPD-Scraper
# Eugene Police Department Dispatch Log Scraper

Scrapes the public dispatch log search tool at [coeapps.eugene-or.gov](https://coeapps.eugene-or.gov/epddispatchlog/Home/Search) and loads the results into a local SQLite database. Police dispatch logs are public record in most jurisdictions, including Eugene, OR; this tool is for personal analysis of that public-facing data.

<img width="1113" height="505" alt="image" src="https://github.com/user-attachments/assets/b6d3abdf-cb27-4905-a4c2-56a7109a450f" />

## What it does

1. Splits a date range into small windows (1 day by default) and queries the dispatch log search form for each window.
2. Handles the site's CSRF token flow automatically — fetches a fresh `__RequestVerificationToken` before each search POST.
3. If a window's results hit the site's 250-row response cap, automatically re-queries that same window once per priority level (`P`, `0`–`9`) to split it into smaller, complete chunks.
4. Logs every window's outcome (`done`, `hit_limit`, `error`) to a `scrape_progress` table, so an interrupted run can simply be restarted — already-completed windows are skipped automatically.
5. Upserts results into a `dispatch` table keyed by `event_number`, so re-scraping a window is safe and idempotent.

## Requirements

- Python 3.9+
- See `requirements.txt`

## Setup

```bash
pip install -r requirements.txt
```

## Usage

Edit the config block at the top of `epd_scraper.py`:

```python
START_DATE    = datetime(2016, 1, 1)
END_DATE      = datetime(2020, 1, 1)
WINDOW_DAYS   = 1     # days of data requested per query
SLEEP_SECONDS = 5     # delay between requests
```

Then run:

```bash
python epd_scraper.py
```

Progress prints to the console as it goes, and results land in `epd_dispatch.db` (SQLite) in the same directory.

## Resuming an interrupted run

Just re-run the script. Any date window already logged with `status = 'done'` in `scrape_progress` is skipped, so you can safely stop and restart a long scrape (e.g. spanning several years) without re-fetching everything.

## Database schema

**`dispatch`** — one row per incident, keyed by `event_number`. Includes `lat`/`lon`/`geocoded` columns reserved for an optional later geocoding step (not performed by this script).

**`scrape_progress`** — an audit log of every date window attempted, with row counts and status, used both for resuming and for spotting windows that errored out and need a re-run.

## Tuning notes

- **`WINDOW_DAYS`**: smaller windows mean more requests but lower odds of hitting the 250-row cap per query (which triggers the slower priority-split fallback). If a lot of windows are hitting the cap, consider lowering this.
- **`SLEEP_SECONDS`**: delay between requests. Keep this reasonable to avoid putting unnecessary load on a public municipal server.
- **Priority-split fallback**: when a window hits the row cap, the script automatically re-queries it 11 times (once per priority level) instead of once. This is slower but ensures no rows are silently dropped for busy date ranges.
