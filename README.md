# Partner Event Tracker

Monthly tracker of channel/partner events involving Microsoft, AWS, Google, Salesforce, ServiceNow and Oracle,
limited to New York, Seattle, India, Bay Area, Texas (plus virtual). Output is one sheet, `tracker.xlsx` > **Events**.

## How it works
`python refresh.py` scrapes the pages in `sources.yaml` (the finite watchlist), optionally runs web-search
**Discovery** for new pages (needs `BRAVE_API_KEY`), tags companies and regions using `config.yaml`, and appends
only new events. Existing rows, including your **Status** and **Notes**, are never modified.

GitHub Actions (`.github/workflows/monthly-refresh.yml`) runs this on the 1st of each month and commits the
updated workbook. Trigger it manually from the Actions tab (workflow_dispatch). Add `BRAVE_API_KEY` as a repo
secret to enable Discovery. Commit your edits to `tracker.xlsx` before the next run.

## Local use
```
pip install -r requirements.txt
python -m playwright install chromium   # for JS-rendered pages
python refresh.py [--no-discovery]
python -m pytest
```

## Limits
- Extraction uses schema.org Event data first, then a card/date heuristic. Pages that show neither need a
  custom parser or a better URL; sources returning nothing are listed at the end of each run.
- Company attendance and speakers are only detected where the page names them.
- channelsalesassociation.com has no public events page; use vendor/third-party sources and Discovery.
