"""Fetch pages (requests, Playwright fallback) and run Discovery searches."""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse
import sys
from datetime import date

import requests

UA = "Mozilla/5.0 (compatible; EventTracker/1.0; +https://github.com/AyushKishor/Event_Tracker)"
TIMEOUT = 30


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def fetch(url: str) -> str | None:
    """Plain HTTP fetch. Returns None on failure (caller logs it)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        _log(f"  fetch failed: {url}: {e}")
        return None


def fetch_rendered(url: str) -> str | None:
    """Headless-browser fetch for JS-rendered pages. None if Playwright is unavailable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            page = browser.new_page(user_agent=UA)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)  # let client-side rendering finish
            html = page.content()
            browser.close()
            return html
    except Exception as e:  # playwright raises many error types
        _log(f"  render failed: {url}: {e}")
        return None


def fetch_firecrawl(url: str) -> str | None:
    """Last-resort fetch (markdown) for bot-protected pages (ServiceNow, Dell, ...). Needs FIRECRAWL_API_KEY."""
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True, "waitFor": 3000},
            timeout=90,
        )
        r.raise_for_status()
        return (r.json().get("data") or {}).get("markdown")
    except (requests.RequestException, ValueError) as e:
        _log(f"  firecrawl failed: {url}: {e}")
        return None


def discovery_queries(config: dict, today: date | None = None) -> list[tuple[str, str]]:
    """(label, query) pairs: company searches plus city searches for the priority regions.

    From July on, next year is searched too (that is when most next-year events are announced).
    """
    d = config.get("discovery", {})
    today = today or date.today()
    years = [today.year] + ([today.year + 1] if today.month >= 7 else [])
    out: list[tuple[str, str]] = []
    for year in years:
        for company in d.get("companies") or list(config["companies"]):
            out += [(company, t.format(company=company, year=year)) for t in d.get("queries", [])]
        for city in d.get("cities", []):
            out += [(city, t.format(city=city, year=year)) for t in d.get("city_queries", [])]
    return out


def discover(config: dict) -> list[dict]:
    """Search the web for candidate event pages. Returns [{url, title, description, label}].

    Requires BRAVE_API_KEY; returns [] (with a warning) when absent or disabled.
    """
    d = config.get("discovery", {})
    if not d.get("enabled"):
        return []
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        _log("Discovery skipped: BRAVE_API_KEY not set.")
        return []

    skip = tuple(d.get("exclude_domains", []))
    found: dict[str, dict] = {}
    for label, q in discovery_queries(config):
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                params={"q": q, "count": d.get("results_per_query", 10)},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            _log(f"  search failed: {q!r}: {e}")
            continue
        for item in r.json().get("web", {}).get("results", []):
            host = urlparse(item["url"]).netloc.lower()
            if skip and host.endswith(skip):  # social/video/news hosts: no event pages worth scraping
                continue
            found.setdefault(item["url"], {"url": item["url"], "title": item.get("title", ""), "label": label,
                                            "description": item.get("description", "")})
        time.sleep(1.1)  # Brave free tier: 1 request/second
    return list(found.values())
