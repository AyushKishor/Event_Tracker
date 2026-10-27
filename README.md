# Partner Event Tracker

Monthly tracker of channel/partner events involving Microsoft, AWS, Google, Salesforce, ServiceNow and Oracle,
limited to New York, Seattle, India, Bay Area, Texas (plus virtual). Output is one sheet, `tracker.xlsx` > **Events**.

## How it works
`python refresh.py` scrapes the pages in `sources.yaml` (the finite watchlist), optionally runs web-search
**Discovery** for new pages (needs `BRAVE_API_KEY`), tags companies and regions using `config.yaml`, and appends
only new events. Existing rows, including your **Status** and **Notes**, are never modified.

GitHub Actions (`.github/workflows/monthly-refresh.yml`) runs this on the 1st of each month and commits the
updated workbook. Trigger it manually from the Actions tab (workflow_dispatch). Optional repo secrets: `BRAVE_API_KEY` (Discovery) and
`FIRECRAWL_API_KEY` (pages that block normal scrapers, e.g. ServiceNow). Scheduled workflows only run from the
default branch, so merge this branch first. Commit your edits to `tracker.xlsx` before the next run.

## Local use
```
pip install -r requirements.txt
python -m playwright install chromium   # for JS-rendered pages
python refresh.py [--no-discovery]
python -m pytest
```

## What gets kept
Mirrors the manually kept 2026 tracker (`scripts/coverage_check.py <xlsx>` replays any such sheet through the filters):
- Events naming a tracked account (the six core partners plus the other target accounts in `config.yaml`), anywhere in the world; Region is `Other` outside the five priority regions.
- Channel / partnership-industry events (Channel Partners, MSP Summit, PartnerTechX, Catalyst, ...) and named general-tech events (CES, TechCrunch Disrupt, ...).
- In the five priority regions or virtual: local summits/meetups with a tech topic or event word, even with no company named (e.g. "AI Agents Seattle").
- `python scripts/import_legacy.py <old.xlsx>` loads the old sheet into `tracker.xlsx` (already done for the 2026 sheet).

## Source options (`sources.yaml`)
`host` default organiser · `require_company` / `require_keyword` filters · `name` + `single` for one-event flagship
pages (reads the date written next to the name) · `name_pattern` regex for pages listing several named editions
(e.g. NVIDIA GTC) · `name_with_heading` for city listings (Microsoft AI Tour).
Each page is tried as: JSON-LD / embedded app JSON -> HTML cards -> headless browser -> Firecrawl markdown.

## Limits
- Extraction uses schema.org Event data first, then a card/date heuristic. Pages that show neither need a
  custom parser or a better URL; sources returning nothing are listed at the end of each run.
- Company attendance and speakers are only detected where the page names them.
- channelsalesassociation.com has no public events page; use vendor/third-party sources and Discovery.
- Pages that return nothing (listed at the end of every run) usually need `FIRECRAWL_API_KEY`, a `name`/`single` entry, or a more specific URL.
