"""Turn page HTML into Event records and tag companies / regions."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_RE = re.compile(
    rf"\b({MONTHS}\.?\s+\d{{1,2}}(?:\s*[-–]\s*\d{{1,2}})?,?\s+\d{{4}})\b", re.IGNORECASE
)
VIRTUAL_RE = re.compile(r"\b(virtual|online|webinar|livestream|live stream)\b", re.IGNORECASE)


@dataclass
class Event:
    name: str
    start: date
    end: date | None = None
    host: str = ""
    city: str = ""
    region: str = ""
    fmt: str = ""
    companies: list[str] = field(default_factory=list)
    role: str = ""
    speakers: str = ""
    cost: str = ""
    url: str = ""
    source: str = ""
    location_text: str = ""  # raw location / snippet text used for region + tagging


def _pattern(alias: str) -> re.Pattern:
    """Word-boundary pattern. Aliases starting with ',' (e.g. ', NY') match as-is."""
    esc = re.escape(alias)
    if alias.startswith(","):
        return re.compile(esc + r"\b")
    flags = 0 if len(alias) <= 3 else re.IGNORECASE
    return re.compile(rf"(?<![\w]){esc}(?![\w])", flags)


def compile_aliases(mapping: dict[str, list[str]]) -> dict[str, list[re.Pattern]]:
    return {label: [_pattern(a) for a in aliases] for label, aliases in mapping.items()}


def detect_companies(text: str, companies: dict[str, list[re.Pattern]]) -> list[str]:
    return [label for label, pats in companies.items() if any(p.search(text) for p in pats)]


def detect_region(text: str, regions: dict[str, list[re.Pattern]]) -> str:
    for label, pats in regions.items():
        if any(p.search(text) for p in pats):
            return label
    return ""


def has_keyword(text: str, keywords: Iterable[str]) -> bool:
    return any(_pattern(k).search(text) for k in keywords)


def detect_role(text: str, host: str, companies: dict[str, list[re.Pattern]], found: list[str]) -> str:
    """Host if the organiser is a tracked company, else Sponsor/Speaker from nearby words."""
    roles: list[str] = []
    host_hit = any(any(p.search(host) for p in companies[c]) for c in found) if host else False
    if host_hit:
        roles.append("Host")
    for word, label in (("sponsor", "Sponsor"), ("speaker|keynote|panel|fireside|presenting", "Speaker")):
        for c in found:
            for p in companies[c]:
                if re.search(rf"(?:{word}).{{0,80}}{p.pattern}|{p.pattern}.{{0,80}}(?:{word})",
                             text, re.IGNORECASE | re.DOTALL):
                    if label not in roles:
                        roles.append(label)
                    break
    return ", ".join(roles)


def _parse_date(value: str) -> date | None:
    try:
        return dateparser.parse(value, default=datetime(2000, 1, 1)).date()
    except (ValueError, OverflowError, TypeError):
        return None


def _range_from_match(text: str) -> tuple[date | None, date | None]:
    """'Mar 3-5, 2027' -> (2027-03-03, 2027-03-05); 'Mar 3, 2027' -> (2027-03-03, None)."""
    m = re.match(rf"({MONTHS}\.?)\s+(\d{{1,2}})(?:\s*[-–]\s*(\d{{1,2}}))?,?\s+(\d{{4}})", text, re.IGNORECASE)
    if not m:
        return _parse_date(text), None
    mon, d1, d2, yr = m.groups()
    return _parse_date(f"{mon} {d1}, {yr}"), (_parse_date(f"{mon} {d2}, {yr}") if d2 else None)


def _jsonld_nodes(soup: BeautifulSoup) -> Iterable[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(reversed(node))
            elif isinstance(node, dict):
                stack.extend(node.get("@graph", []))
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if any(isinstance(t, str) and t.endswith("Event") for t in types):
                    yield node


def _name_of(obj) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return str(obj.get("name", "") or "")
    if isinstance(obj, list):
        return ", ".join(filter(None, (_name_of(o) for o in obj)))
    return ""


def _jsonld_events(soup: BeautifulSoup, base_url: str) -> list[Event]:
    out = []
    for node in _jsonld_nodes(soup):
        start = _parse_date(str(node.get("startDate", "")))
        name = str(node.get("name", "")).strip()
        if not (start and name):
            continue
        loc = node.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        loc_text = ""
        city = ""
        if isinstance(loc, dict):
            addr = loc.get("address") or {}
            if isinstance(addr, dict):
                city = str(addr.get("addressLocality", ""))
                loc_text = " ".join(str(v) for v in addr.values() if isinstance(v, str))
            elif isinstance(addr, str):
                loc_text = addr
            loc_text = f"{loc.get('name', '')} {loc_text}".strip()
        offers = node.get("offers")
        cost = ""
        if isinstance(offers, dict):
            cost = str(offers.get("price", "") or "")
        mode = str(node.get("eventAttendanceMode", ""))
        fmt = "Virtual" if "Online" in mode else "Hybrid" if "Mixed" in mode else "In-person" if mode else ""
        out.append(Event(
            name=name,
            start=start,
            end=_parse_date(str(node.get("endDate", ""))) if node.get("endDate") else None,
            host=_name_of(node.get("organizer")),
            city=city or loc_text,
            fmt=fmt,
            speakers=_name_of(node.get("performer")),
            cost=cost,
            url=str(node.get("url") or base_url),
        ))
        out[-1].location_text = loc_text
    return out


def _card_events(soup: BeautifulSoup, base_url: str) -> list[Event]:
    """Fallback for listing pages: any card-like element with a link, heading and a date."""
    from urllib.parse import urljoin

    out, seen = [], set()
    for el in soup.find_all(["article", "li", "div"]):
        if el.find(["article", "li"]) and el.name == "div":
            continue
        text = el.get_text(" ", strip=True)
        if not 15 < len(text) < 600:
            continue
        m = DATE_RE.search(text)
        link = el.find("a", href=True)
        head = el.find(["h1", "h2", "h3", "h4"]) or link
        if not (m and link and head):
            continue
        name = head.get_text(" ", strip=True)
        start, end = _range_from_match(m.group(1))
        if not (name and start) or (name, start) in seen:
            continue
        seen.add((name, start))
        out.append(Event(name=name, start=start, end=end, url=urljoin(base_url, link["href"]),
                         location_text=text))
    return out


def _page_event(soup: BeautifulSoup, url: str) -> list[Event]:
    """Fallback for single-event pages: title + first date in body text."""
    text = soup.get_text(" ", strip=True)
    m = DATE_RE.search(text)
    title = (soup.find("meta", property="og:title") or {}).get("content") or (
        soup.title.get_text(strip=True) if soup.title else ""
    )
    if not (m and title):
        return []
    start, end = _range_from_match(m.group(1))
    if not start:
        return []
    return [Event(name=title.strip(), start=start, end=end, url=url, location_text=text[:2000])]


def parse_events(html: str, url: str, settings: dict, source: dict, today: date | None = None) -> list[Event]:
    """Parse one page into filtered, tagged events."""
    today = today or date.today()
    soup = BeautifulSoup(html, "html.parser")
    raw = _jsonld_events(soup, url) or _card_events(soup, url) or _page_event(soup, url)
    page_text = soup.get_text(" ", strip=True)

    companies = settings["_companies"]
    regions = settings["_regions"]
    keywords = settings["keywords"]
    horizon = _add_months(today, settings["lookahead_months"])
    results: list[Event] = []

    for ev in raw:
        if not (today <= ev.start <= horizon):
            continue
        loc_text = ev.location_text
        blob = f"{ev.name} {ev.host} {loc_text} {ev.speakers}"
        # card/page fallbacks have little text of their own; look at the page too for tagging
        single_page = len(raw) == 1 and not ev.host
        tag_text = f"{blob} {page_text[:5000]}" if single_page else blob

        found = detect_companies(tag_text, companies)
        host = ev.host or source.get("host", "")
        if host and not found:
            found = detect_companies(host, companies)
        if source.get("require_company") and not found:
            continue
        if source.get("require_keyword") and not has_keyword(f"{ev.name} {loc_text}", keywords):
            continue

        virtual = ev.fmt == "Virtual" or bool(VIRTUAL_RE.search(f"{ev.name} {loc_text}"))
        region = detect_region(f"{ev.city} {loc_text}", regions)
        if not region:
            if virtual and settings["include_virtual"]:
                region = "Virtual"
            else:
                continue
        ev.region = region
        ev.host = host
        ev.companies = found
        ev.fmt = ev.fmt or ("Virtual" if virtual else "")
        ev.role = detect_role(tag_text, host, companies, found)
        ev.source = source.get("label", url)
        results.append(ev)
    return results


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, min(d.day, 28))
