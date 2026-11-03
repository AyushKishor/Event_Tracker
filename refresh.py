#!/usr/bin/env python3
"""Monthly refresh: scrape the watchlist, run discovery, append new events to tracker.xlsx."""
from __future__ import annotations

import argparse
import html
import sys
from datetime import date
from pathlib import Path

import yaml

from tracker import extract, sources, workbook

ROOT = Path(__file__).parent


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["_companies"] = extract.compile_aliases(cfg["companies"])
    cfg["_regions"] = extract.compile_aliases(cfg["regions"])
    return cfg


def scrape(source: dict, cfg: dict, render: bool = True) -> list[extract.Event]:
    url = source["url"]
    html = sources.fetch(url)
    events = extract.parse_events(html, url, cfg, source) if html else []
    # static HTML had nothing usable (JS-rendered or bot-blocked): try a browser, then Firecrawl
    fallbacks = ((sources.fetch_rendered, False), (sources.fetch_firecrawl, True)) if render else ()
    for fallback, is_markdown in fallbacks:
        if events:
            break
        page = fallback(url)
        if page and len(page) > 1500:  # tiny pages are block/error notices
            events = extract.parse_events(page, url, cfg, source, markdown=is_markdown)
    return events


def snippet_events(hit: dict, src: dict, cfg: dict) -> list[extract.Event]:
    """Read date/place from a search result's title + snippet (for pages we cannot or did not fetch)."""
    page = (f"<html><head><title>{html.escape(hit['title'])}</title></head>"
            f"<body><p>{html.escape(hit['title'])} {html.escape(hit['description'])}</p></body></html>")
    return extract.parse_events(page, hit["url"], cfg, src)


def lookup_by_search(src: dict, cfg: dict) -> list[extract.Event]:
    """Search for one named flagship ("ServiceNow Knowledge 2027") and read its date from the result snippets."""
    if not sources.search_provider():
        return []
    today = date.today()
    host = src.get("host", "")
    # results often drop the company ("Knowledge 2027", not "ServiceNow Knowledge 2027"): match the short name too
    short = src["name"][len(host):].strip() if host and src["name"].lower().startswith(host.lower()) else ""
    for year in (today.year, today.year + 1):
        for hit in sources.search(f"{src['name']} {year} dates location", 5):
            for match in filter(None, (src["name"], short)):
                events = snippet_events(hit, {**src, "name": match, "url": hit["url"]}, cfg)
                if events:
                    for e in events:
                        e.name = f"{src['name']} {e.start.year}"
                        e.url, e.source = src["url"], f"{src['label']} (search)"
                    return events
    return []


def placeholder(src: dict, cfg: dict) -> extract.Event:
    """A 'Date TBD' row for a tracked flagship whose next date is not published yet."""
    host = src.get("host", "")
    return extract.Event(name=src["name"], start=None, host=host, region="TBD", url=src["url"],
                         companies=extract.detect_companies(f"{src['name']} {host}", cfg["_companies"]),
                         source=f"{src['label']} (date not announced)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    ap.add_argument("--sources", type=Path, default=ROOT / "sources.yaml")
    ap.add_argument("--output", type=Path, default=ROOT / "tracker.xlsx")
    ap.add_argument("--no-discovery", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    watchlist = yaml.safe_load(args.sources.read_text())["sources"]

    all_events: list[extract.Event] = []
    failed: list[dict] = []
    for src in watchlist:
        print(f"Watchlist: {src['label']}")
        events = scrape(src, cfg)
        print(f"  {len(events)} matching event(s)")
        if not events:
            failed.append(src)
        all_events += events

    # Named flagships whose page gave no date (blocked site, or next edition not announced on it yet):
    # look the event up by name; if still nothing, keep it as a 'Date TBD' row for a later run to fill in.
    for src in [s for s in failed if s.get("name") and not s.get("name_with_heading")]:
        found = lookup_by_search(src, cfg) if not args.no_discovery else []
        if found:
            print(f"Search lookup: {src['name']} -> {found[0].start}")
            all_events += found
        else:
            all_events.append(placeholder(src, cfg))

    if not args.no_discovery:
        known = {s["url"] for s in watchlist}
        for hit in sources.discover(cfg):
            if hit["url"] in known:
                continue
            src = {"label": f"Discovery ({hit['label']})", "url": hit["url"], "require_keyword": True}
            events = scrape(src, cfg, render=False)  # hundreds of hits: plain fetch only, keeps the job short
            if not events and hit.get("description"):  # blocked/JS page: the search snippet often has date + city
                events = snippet_events(hit, src, cfg)
            if events:
                print(f"Discovery: {hit['url']} -> {len(events)} event(s)")
            all_events += events

    added, total = workbook.merge(args.output, all_events, earliest=cfg.get("earliest_date"))
    print(f"\nAdded {added} new event(s); {total} total in {args.output.name}")
    if failed:
        print("Sources with no matching events (check manually): " + "; ".join(s["label"] for s in failed),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
