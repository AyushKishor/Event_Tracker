"""Fetch pages (requests, Playwright fallback) and run Discovery searches."""
from __future__ import annotations

import os
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


def discover(config: dict) -> list[dict]:
    """Search the web for candidate event pages. Returns [{url, title, company}].

    Requires BRAVE_API_KEY; returns [] (with a warning) when absent or disabled.
    """
    d = config.get("discovery", {})
    if not d.get("enabled"):
        return []
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        _log("Discovery skipped: BRAVE_API_KEY not set.")
        return []

    found: dict[str, dict] = {}
    for company in config["companies"]:
        for template in d.get("queries", []):
            q = template.format(company=company, year=date.today().year)
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
                found.setdefault(item["url"], {"url": item["url"], "title": item.get("title", ""), "company": company})
    return list(found.values())
