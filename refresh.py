#!/usr/bin/env python3
"""Monthly refresh: scrape the watchlist, run discovery, append new events to tracker.xlsx."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from tracker import extract, sources, workbook

ROOT = Path(__file__).parent


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["_companies"] = extract.compile_aliases(cfg["companies"])
    cfg["_regions"] = extract.compile_aliases(cfg["regions"])
    return cfg


def scrape(source: dict, cfg: dict) -> list[extract.Event]:
    url = source["url"]
    html = sources.fetch(url)
    events = extract.parse_events(html, url, cfg, source) if html else []
    # static HTML had nothing usable (JS-rendered or bot-blocked): try a browser, then Firecrawl
    for fallback, is_markdown in ((sources.fetch_rendered, False), (sources.fetch_firecrawl, True)):
        if events:
            break
        page = fallback(url)
        if page and len(page) > 1500:  # tiny pages are block/error notices
            events = extract.parse_events(page, url, cfg, source, markdown=is_markdown)
    return events


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
    failed: list[str] = []
    for src in watchlist:
        print(f"Watchlist: {src['label']}")
        events = scrape(src, cfg)
        print(f"  {len(events)} matching event(s)")
        if not events:
            failed.append(src["label"])
        all_events += events

    if not args.no_discovery:
        known = {s["url"] for s in watchlist}
        for hit in sources.discover(cfg):
            if hit["url"] in known:
                continue
            src = {"label": f"Discovery ({hit['company']})", "url": hit["url"], "require_keyword": True}
            events = scrape(src, cfg)
            if events:
                print(f"Discovery: {hit['url']} -> {len(events)} event(s)")
            all_events += events

    added, total = workbook.merge(args.output, all_events)
    print(f"\nAdded {added} new event(s); {total} total in {args.output.name}")
    if failed:
        print("Sources with no matching events (check manually): " + "; ".join(failed), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
