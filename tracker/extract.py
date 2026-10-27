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
_SEP = r"\s*[-–—]\s*"
DATE_RE = re.compile(
    rf"\b({MONTHS}\.?\s+\d{{1,2}}(?:{_SEP}(?:{MONTHS}\.?\s+)?\d{{1,2}})?(?:,?\s+(?:19|20)\d{{2}})?)\b",
    re.IGNORECASE,
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
    if alias.endswith("*"):  # prefix match, e.g. "Knowledge 20*" -> "Knowledge 2026"
        return re.compile(rf"(?<![\w]){re.escape(alias[:-1])}", re.IGNORECASE)
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


_RANGE_RE = re.compile(
    rf"({MONTHS})\.?\s+(\d{{1,2}})(?:{_SEP}(?:({MONTHS})\.?\s+)?(\d{{1,2}}))?(?:,?\s+((?:19|20)\d{{2}}))?",
    re.IGNORECASE,
)


def _range_from_match(text: str, today: date | None = None, year: int | None = None) -> tuple[date | None, date | None]:
    """'Mar 3-5, 2027' / 'Nov 30 – Dec 4, 2026' / 'October 25–28' -> (start, end or None).

    A missing year means the next occurrence on or after today.
    """
    today = today or date.today()
    m = _RANGE_RE.match(text.strip())
    if not m:
        return _parse_date(text), None
    mon1, d1, mon2, d2, yr = m.groups()

    def build(mon, day, year):
        try:
            return dateparser.parse(f"{mon} {day} {year}").date()
        except (ValueError, OverflowError):
            return None

    start = build(mon1, d1, yr or year or today.year)
    if start and not yr and not year and start < today:
        start = build(mon1, d1, today.year + 1)
    end = None
    if d2 and start:
        end = build(mon2 or mon1, d2, start.year)
        if end and end < start:
            end = build(mon2 or mon1, d2, start.year + 1)
    return start, end


def _is_event_type(node: dict) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.endswith("Event") for t in types)


def _walk(obj) -> Iterable[dict]:
    """Yield every dict nested anywhere in obj (lists, ItemList wrappers, @graph, ...)."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def _jsonld_nodes(soup: BeautifulSoup) -> Iterable[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            if _is_event_type(node):
                yield node


START_KEYS = ("startDate", "start_at", "startAt", "start_date", "startTime", "start_time")


def _embedded_json_events(soup: BeautifulSoup, base_url: str) -> list[Event]:
    """Events inside app-state JSON (Next.js __NEXT_DATA__ etc.): dicts with a title and a start date."""
    out = []
    for tag in soup.find_all("script"):
        if tag.get("type") == "application/ld+json" or not (tag.string or "").lstrip().startswith(("{", "[")):
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            key = next((k for k in START_KEYS if isinstance(node.get(k), str)), None)
            if not key:
                continue
            inner = node.get("event") if isinstance(node.get("event"), dict) else node
            name = inner.get("name") or inner.get("title")
            start = _parse_date(node[key][:10] if re.match(r"\d{4}-\d\d-\d\d", node[key]) else node[key])
            if not (isinstance(name, str) and name.strip() and start):
                continue
            geo = inner.get("geo_address_info") or inner.get("location") or {}
            geo = geo if isinstance(geo, dict) else {}
            city = geo.get("city") or geo.get("city_state") or inner.get("city") or ""
            slug = inner.get("url") or inner.get("slug") or ""
            out.append(Event(
                name=name.strip(), start=start, city=str(city),
                url=(base_url.rstrip("/").rsplit("/", 1)[0] + "/" + slug) if slug and not str(slug).startswith("http") else str(slug or base_url),
                location_text=f"{city} {geo.get('full_address', '')} {inner.get('description', '') if isinstance(inner.get('description'), str) else ''}",
                host=_name_of(node.get("hosts")) if node.get("hosts") else "",
            ))
    return out


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
        desc = node.get("description")
        out[-1].location_text = f"{loc_text} {desc if isinstance(desc, str) else ''}".strip()
    return out


def _card_events(soup: BeautifulSoup, base_url: str, today: date | None = None) -> list[Event]:
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
        start, end = _range_from_match(m.group(1), today)
        if not (name and start) or (name, start) in seen:
            continue
        seen.add((name, start))
        out.append(Event(name=name, start=start, end=end, url=urljoin(base_url, link["href"]),
                         location_text=text))
    return out


def _city_in(text: str, cities: list[str]) -> str:
    """First known event city mentioned in text (earliest position wins)."""
    hits = [(m.start(), c) for c in cities if (m := re.search(rf"(?<!\w){re.escape(c)}(?!\w)", text))]
    return min(hits)[1] if hits else ""


def _named_date_events(soup: BeautifulSoup, source: dict, url: str, today: date, cities: list[str]) -> list[Event]:
    """Flagship pages. Find the event name in the page text and read the date written next to it.

    `name` + `single`: one event, the nearest upcoming date written within 220 chars of the name.
    `name_pattern` (regex): every match is its own event, e.g. 'NVIDIA GTC [A-Za-z., ]+ 20\\d\\d';
    its date is the nearest date within 80 chars, before or after.
    """
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    found: dict[tuple, Event] = {}

    def dates_in(window: str, offset: int, year: int | None = None):
        for dm in DATE_RE.finditer(window):
            start, end = _range_from_match(dm.group(1), today, year)
            if start:
                yield offset + dm.start(), start, end

    if source.get("name_pattern"):
        for m in re.finditer(source["name_pattern"], text):
            lo = max(0, m.start() - 80)
            ym = re.search(r"(?:19|20)\d\d", m.group(0))  # a year in the name fixes the date's year
            cands = list(dates_in(text[lo: m.end() + 80], lo, int(ym.group(0)) if ym else None))
            if not cands:
                continue
            pos, start, end = min(cands, key=lambda c: min(abs(c[0] - m.start()), abs(c[0] - m.end())))
            if start < today:  # nearest date is past: the event is over, don't borrow a neighbour's date
                continue
            name = " ".join(m.group(0).split()).strip(" ,.")
            window = text[max(0, m.start() - 80): m.end() + 120]
            found.setdefault((name, start), Event(
                name=name, start=start, end=end, url=url, city=_city_in(name + " " + window, cities),
                location_text=window))
    elif source.get("name"):
        key = re.escape(source["name"].split(",")[0].strip())
        best = None
        for m in re.finditer(key, text, re.IGNORECASE):
            lo = max(0, m.start() - 60)
            window = text[lo: m.end() + 220]
            for pos, start, end in dates_in(window, lo):
                if start >= today and (best is None or start < best[0]):
                    best = (start, end, text[pos: pos + 120], window)
        if best:
            start, end, after, window = best
            found[(source["name"], start)] = Event(
                name=source["name"], start=start, end=end, url=url, city=_city_in(after, cities),
                location_text=window)
    return list(found.values())


_NOISE_LINE = re.compile(r"^(!\[|\[?(video cta|learn more|register|register now|add to calendar|find your city|get updates)\b|https?://)", re.IGNORECASE)


def _text_events(text: str, base_url: str, today: date, cities: list[str]) -> list[Event]:
    """Markdown / plain-text fallback: each future date line gets the nearest preceding title line."""
    lines = [re.sub(r"\\([\[\]])", r"\1", l.strip(" *#|>\t")) for l in text.splitlines()]
    lines = [l for l in lines if l]
    out, seen = [], set()
    for i, line in enumerate(lines):
        m = DATE_RE.search(line)
        if not m or len(line) > 160:
            continue
        start, end = _range_from_match(m.group(1), today)
        if not start or start < today:
            continue
        # the second date of "Oct 27, 2026 - Oct 28, 2026" is part of the same range
        tail = line[m.end():]
        dm2 = re.match(rf"\s*[-–—]\s*({MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}})", tail, re.IGNORECASE)
        if dm2:
            end = _parse_date(dm2.group(1))
        title = line[:m.start()].strip(" |-–—:")
        if len(title) < 4:
            for back in range(1, 4):
                if i - back < 0:
                    break
                cand = lines[i - back]
                if _NOISE_LINE.match(cand) or DATE_RE.search(cand) or len(cand) > 120 or len(cand) < 4:
                    continue
                title = cand
                break
        if len(title) < 4 or (title, start) in seen:
            continue
        seen.add((title, start))
        after = " ".join(lines[i: i + 2])
        city = _city_in(f"{title} {after}", cities)
        out.append(Event(name=title, start=start, end=end, city=city, url=base_url,
                         location_text=f"{title} {after}"))
    return out


def _page_event(soup: BeautifulSoup, url: str, today: date | None = None) -> list[Event]:
    """Fallback for single-event pages: title + first date in body text."""
    text = soup.get_text(" ", strip=True)
    m = DATE_RE.search(text)
    title = (soup.find("meta", property="og:title") or {}).get("content") or (
        soup.title.get_text(strip=True) if soup.title else ""
    )
    if not (m and title):
        return []
    start, end = _range_from_match(m.group(1), today)
    if not start:
        return []
    return [Event(name=title.strip(), start=start, end=end, url=url, location_text=text[:2000])]


def parse_events(html: str, url: str, settings: dict, source: dict, today: date | None = None,
                 markdown: bool = False) -> list[Event]:
    """Parse one page into filtered, tagged events."""
    today = today or date.today()
    soup = BeautifulSoup(html, "html.parser")
    raw = []
    if markdown:
        raw = _text_events(html, url, today, settings.get("known_cities", []))
        html = ""
    elif source.get("name_pattern") or (source.get("single") and source.get("name")):
        raw = _named_date_events(BeautifulSoup(html, "html.parser"), source, url, today,
                                 settings.get("known_cities", []))
    raw = raw or ([] if markdown else (_jsonld_events(soup, url) or _embedded_json_events(soup, url)
                  or _card_events(soup, url, today) or _page_event(soup, url, today)))
    page_text = soup.get_text(" ", strip=True)

    companies = settings["_companies"]
    regions = settings["_regions"]
    keywords = settings["keywords"]
    horizon = _add_months(today, settings["lookahead_months"])
    results: list[Event] = []

    for ev in raw:
        if not (today <= ev.start <= horizon):
            continue
        if JUNK_NAME_RE.search(ev.name) or ev.name.startswith("http"):
            if not source.get("name"):
                continue
        if source.get("name_with_heading"):  # listing of cities / editions, e.g. Microsoft AI Tour
            ev.city = ev.city or ev.name
            ev.name = f"{source['name_with_heading']} - {ev.name}"
        if source.get("name") and not source.get("name_with_heading") and not source.get("name_pattern"):
            # curated flagship pages: card/heading text is unreliable
            ev.name = f"{source['name']} {ev.start.year}"
        # card/page fallbacks have little text of their own; look at the page too for tagging
        extra = page_text[:5000] if len(raw) == 1 and not ev.host and not source.get("name") else ""
        v = classify(ev.name, ev.host or source.get("host", ""), f"{ev.location_text} {ev.speakers}",
                     ev.city, ev.fmt, source, settings, extra)
        if not v.keep:
            continue
        ev.region, ev.host, ev.companies = v.region, v.host, v.companies
        ev.fmt = ev.fmt or ("Virtual" if v.virtual else "")
        ev.role = v.role
        ev.source = source.get("label", url)
        if INVITE_RE.search(f"{ev.location_text} {extra}") and not ev.cost:
            ev.cost = "Invite only"
        results.append(ev)
    if source.get("single") and results:  # one-event pages: keep the nearest upcoming date only
        results = [min(results, key=lambda e: e.start)]
    return results


@dataclass
class Verdict:
    keep: bool
    reason: str
    region: str = ""
    host: str = ""
    companies: list[str] = field(default_factory=list)
    role: str = ""
    virtual: bool = False


def classify(name: str, host: str, text: str, city: str, fmt: str, source: dict, settings: dict,
             extra: str = "") -> Verdict:
    """Decide whether an event belongs in the tracker and tag it.

    Keep when it is in a priority region (or virtual) AND is relevant, or out of region but
    clearly relevant: a tracked account, a channel/partner-ecosystem event, or an always_track name.
    Relevant = tracked company, channel/partner keyword, always_track name, or (in region only)
    a generic event word or tech topic word (e.g. "AI Summit").
    """
    companies, regions = settings["_companies"], settings["_regions"]
    blob = f"{name} {host} {text}"
    tag_text = f"{blob} {extra}"

    found = detect_companies(tag_text, companies)
    if host and not found:
        found = detect_companies(host, companies)
    if source.get("require_company") and not found:
        return Verdict(False, "source requires a tracked company")
    if source.get("require_keyword") and not has_keyword(f"{name} {text}", settings["keywords"]):
        return Verdict(False, "source requires a channel/partner keyword")

    virtual = fmt == "Virtual" or bool(VIRTUAL_RE.search(f"{name} {text}"))
    region = detect_region(f"{city} {text}", regions)
    in_scope = bool(region) or (virtual and settings["include_virtual"])
    if virtual and not region:
        region = "Virtual"

    always = has_keyword(name, settings.get("always_track", []))
    channel = has_keyword(name, settings["keywords"]) or has_keyword(name, settings.get("channel_events", []))
    generic = has_keyword(name, settings.get("event_words", [])) or has_keyword(name, settings.get("topic_words", []))

    if found or channel or always:
        relevant = True
    else:
        relevant = in_scope and generic
    if not relevant:
        return Verdict(False, "no tracked company, channel keyword or tracked event name")
    if not in_scope and not settings.get("keep_out_of_region", True):
        return Verdict(False, "outside priority regions")
    return Verdict(True, "ok", region or "Other", host, found, detect_role(tag_text, host, companies, found), virtual)


JUNK_NAME_RE = re.compile(r"^(learn more|register|check out|what.?s next|read more|save the date|see you|sign up|view|explore)\b", re.IGNORECASE)
INVITE_RE = re.compile(r"\b(invite[- ]only|by invitation|invitation only|application[- ]based)\b", re.IGNORECASE)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, min(d.day, 28))
