import json
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from refresh import load_config  # noqa: E402
from tracker import extract, workbook  # noqa: E402

ROOT = Path(__file__).parent.parent
TODAY = date(2027, 1, 15)


def cfg(keep_out_of_region=True):
    """Test config. Most tests exercise tagging with out-of-region events kept; the shipped default is strict."""
    c = load_config(ROOT / "config.yaml")
    c["keep_out_of_region"] = keep_out_of_region
    return c


def jsonld_page(events):
    return "<html><script type='application/ld+json'>" + json.dumps(events) + "</script></html>"


def ev(name, start, city, organizer="", mode=None, performer=None):
    d = {"@type": "Event", "name": name, "startDate": start,
         "location": {"@type": "Place", "address": {"addressLocality": city}}}
    if organizer:
        d["organizer"] = {"name": organizer}
    if mode:
        d["eventAttendanceMode"] = mode
    if performer:
        d["performer"] = [{"name": p} for p in performer]
    return d


def test_company_alias_matching():
    c = cfg()["_companies"]
    assert extract.detect_companies("AWS and Microsoft partner day", c) == ["Microsoft", "AWS"]
    assert extract.detect_companies("laws of physics", c) == []  # no substring match on "aws"
    assert extract.detect_companies("Oracle Cloud Infrastructure", c) == ["Oracle"]


def test_region_detection():
    r = cfg()["_regions"]
    assert extract.detect_region("Austin", r) == "Texas"
    assert extract.detect_region("Bengaluru, Karnataka", r) == "India"
    assert extract.detect_region("Redmond", r) == "Seattle"
    assert extract.detect_region("Chicago", r) == "Other US"
    assert extract.detect_region("Las Vegas, NV", r) == "Other US"
    assert extract.detect_region("Toronto", r) == ""
    assert extract.detect_region("Indianapolis", r) == "Other US"  # not "India"
    assert extract.detect_region("Brooklyn, NY", r) == "New York"


def test_parse_filters_region_window_and_company():
    html = jsonld_page([
        ev("AWS Partner Summit", "2027-03-10", "Seattle", "AWS", performer=["Jane Doe"]),
        ev("Random Meetup", "2027-03-10", "Seattle", "Someone"),             # no tracked company
        ev("Google Cloud Partner Day", "2027-03-11", "Chicago", "Google"),   # wrong region
        ev("Oracle Partner Webinar", "2027-03-12", "", "Oracle",
           mode="https://schema.org/OnlineEventAttendanceMode"),            # virtual kept
        ev("Salesforce Partner Day", "2029-01-01", "Austin", "Salesforce"),  # outside window
        ev("Past Partner Day", "2026-01-01", "Austin", "Salesforce"),        # past
    ])
    out = extract.parse_events(html, "http://x", cfg(), {"label": "t", "require_company": True}, TODAY)
    assert [e.name for e in out] == ["AWS Partner Summit", "Google Cloud Partner Day", "Oracle Partner Webinar"]
    aws, google, oracle = out
    assert (google.region, google.companies) == ("Other US", ["Google"])  # out-of-region flagship-style kept
    assert (aws.region, aws.companies, aws.role, aws.speakers) == ("Seattle", ["AWS"], "Host", "Jane Doe")
    assert oracle.region == "Virtual"


def test_virtual_toggle():
    c = cfg()
    c["include_virtual"] = False
    c["keep_out_of_region"] = False
    html = jsonld_page([ev("Oracle Webinar", "2027-03-12", "", "Oracle", mode="OnlineEventAttendanceMode")])
    assert extract.parse_events(html, "http://x", c, {"label": "t"}, TODAY) == []


def test_card_fallback_and_date_range():
    html = """<ul><li><a href="/e/1"><h3>ServiceNow Partner Forum</h3></a>
              <span>Mar 3-5, 2027</span> Austin, TX</li></ul>"""
    out = extract.parse_events(html, "http://x.com/events", cfg(), {"label": "t", "host": "ServiceNow"}, TODAY)
    assert len(out) == 1
    e = out[0]
    assert (e.start, e.end, e.region, e.url) == (date(2027, 3, 3), date(2027, 3, 5), "Texas", "http://x.com/e/1")
    assert e.companies == ["ServiceNow"] and e.role == "Host"


def test_dedupe_key_normalises():
    a = workbook.event_key("AWS  Partner Summit!", date(2027, 3, 10), "Seattle")
    b = workbook.event_key("aws partner summit", date(2027, 3, 10), " seattle ")
    assert a == b
    assert a != workbook.event_key("aws partner summit", date(2027, 3, 11), "seattle")


def test_merge_idempotent_single_sheet_no_tracking_columns(tmp_path):
    path = tmp_path / "t.xlsx"
    html = jsonld_page([ev("AWS Partner Summit", "2027-03-10", "Seattle", "AWS")])
    events = extract.parse_events(html, "http://x", cfg(), {"label": "t"}, TODAY)

    assert workbook.merge(path, events, TODAY) == (1, 1)
    assert workbook.merge(path, events, TODAY) == (0, 1)  # no duplicate

    from openpyxl import load_workbook
    wb = load_workbook(path)
    assert wb.sheetnames == ["2026", "2027"]
    assert [c.value for c in wb["2027"][1]] == workbook.HEADERS
    assert not {"Date Added", "Status", "Notes"} & set(workbook.HEADERS)


def verdict(name, city="", host="", fmt=""):
    return extract.classify(name, host, "", city, fmt, {}, cfg())


def test_events_from_2026_sheet_are_covered():
    # (name, city, host) rows from the manually kept tracker that a narrow filter would drop
    keep = [
        ("Channel Partners Conference & Expo", "Las Vegas", ""),     # channel industry, out of region
        ("PartnerTechX", "Phoenix", ""),
        ("Catalyst by Partnership Leaders", "", ""),
        ("CES 2026", "Las Vegas", ""),                                # general tech, always_track
        ("TechCrunch Disrupt 2026", "San Francisco", ""),
        ("Knowledge 2026", "Las Vegas", ""),                          # vendor flagship, out of region
        ("Dreamforce 2026", "San Francisco", ""),
        ("Snowflake Summit 2026", "Moscone Center San Francisco", ""),  # non-core target account
        ("Dell Technologies World", "Las Vegas", ""),
        ("Microsoft AI Tour", "Boston", ""),
        ("AWS Marketplace Seller Forum", "Bellevue", ""),
        ("Oracle AI World Tour", "Mumbai", ""),
        ("Cypher 2026", "Bengaluru", ""),
        ("Build with Gemini - Seattle", "Seattle", ""),               # Google-run event
        ("SaaSify Partner Central Agentic Workshop", "Seattle", "AWS"),
    ]
    for name, city, host in keep:
        assert verdict(name, city, host).keep, name


def test_irrelevant_out_of_region_event_is_dropped():
    assert not verdict("Local Pottery Fair", "Boise").keep
    assert not verdict("Regional Dental Expo", "Boise").keep  # event word alone, out of region


SERVICENOW_MD = """
World Forum London, United Kingdom

Oct 20, 2026Excel London
Video CTA

![tokyo city image](https://example.com/x.png)World Forum Tokyo, Japan
Oct 27, 2026 - Oct 28, 2026Grand Prince Hotel Takanawa

now\\[dev\\]26
Nov 4-5, 2026San Jose Convention Center, San Jose
Register Now

Gartner IT Symposium/Xpo 2026
Oct 19-22, 2026Orlando, FL
"""


def test_markdown_fallback_for_blocked_pages():
    today = date(2026, 10, 20)
    out = extract.parse_events(SERVICENOW_MD, "http://sn", cfg(), {"label": "SN", "host": "ServiceNow"}, today,
                               markdown=True)
    by = {e.name: e for e in out}
    assert "World Forum London, United Kingdom" in by
    assert by["World Forum London, United Kingdom"].start == date(2026, 10, 20)
    assert by["now[dev]26"].city == "San Jose" and by["now[dev]26"].region == "Bay Area"
    assert all(e.companies == ["ServiceNow"] for e in out)


def test_itemlist_jsonld_and_description_tagging():
    html = ("<script type='application/ld+json'>" + json.dumps({"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "item": {"@type": "Event", "name": "Partner Workshop", "startDate": "2027-02-10",
         "description": "Join SaaSify and AWS in Seattle", "location": {"address": {"addressLocality": "Seattle"}}}}]})
        + "</script>")
    out = extract.parse_events(html, "http://x", cfg(), {"label": "t", "require_company": True}, TODAY)
    assert [(e.name, e.companies, e.region) for e in out] == [("Partner Workshop", ["AWS"], "Seattle")]


def test_date_ranges_and_year_inference():
    t = date(2026, 10, 27)
    assert extract._range_from_match("Nov 30 – Dec 4, 2026", t) == (date(2026, 11, 30), date(2026, 12, 4))
    assert extract._range_from_match("October 25–28", t) == (date(2027, 10, 25), date(2027, 10, 28))
    assert extract._range_from_match("November 30–December 3", t) == (date(2026, 11, 30), date(2026, 12, 3))
    assert extract._range_from_match("October 20-22", t, year=2026)[0] == date(2026, 10, 20)


def test_named_date_events_flagship_and_pattern():
    page = ("<p>AWS re:Invent | Nov 30 – Dec 4, 2026 | Las Vegas, NV AWS re:Invent is a week</p>"
            "<p>October 20-22 NVIDIA GTC Berlin 2026 Connect</p><p>March 15-18 NVIDIA GTC 2027 Connect</p>")
    c, today = cfg(), date(2026, 10, 27)
    one = extract.parse_events(page, "http://aws", c, {"label": "a", "host": "AWS", "name": "AWS re:Invent", "single": True}, today)
    assert [(e.name, e.start, e.city) for e in one] == [("AWS re:Invent 2026", date(2026, 11, 30), "Las Vegas")]
    many = extract.parse_events(page, "http://nv", c, {"label": "n", "host": "NVIDIA",
                                "name_pattern": r"NVIDIA GTC(?: [A-Za-z.,' ]+?)? 20\d\d"}, today)
    assert [e.name for e in many] == ["NVIDIA GTC 2027"]  # Berlin Oct 20 2026 is already past


def test_prefix_dedupe_and_junk_names(tmp_path):
    path = tmp_path / "t.xlsx"
    a = extract.Event(name="Channel Partners Conference & Expo 2027", start=date(2027, 3, 15), city="Las Vegas", region="Other")
    b = extract.Event(name="Channel Partners Conference & Expo 2027, co-located with MSP Summit", start=date(2027, 3, 15), region="Other")
    assert workbook.merge(path, [a, b], TODAY) == (1, 1)
    html = jsonld_page([ev("Learn More & Register", "2027-03-10", "Seattle", "AWS")])
    assert extract.parse_events(html, "http://x", cfg(), {"label": "t"}, TODAY) == []


def test_small_community_events_are_dropped():
    for name, city, host in [
        ("AI Agents Seattle - August 2026", "Seattle", ""),               # no company at all
        ("Games & AI Meetup / Co-hosted by AWS", "Seattle", ""),          # meetup
        ("AWS User Group Seattle", "Seattle", "AWS User Group"),
        ("Seattle Tech Mixer 2026", "Seattle", ""),
        ("San Francisco Syntax Meetup", "San Francisco", "Syntax FM"),
        ("Claude Touch Grass Summer Social", "Seattle", "Anthropic"),
    ]:
        assert not verdict(name, city, host).keep, name


def test_default_config_is_strict_about_regions():
    shipped = load_config(ROOT / "config.yaml")
    assert shipped["keep_out_of_region"] is False
    html = jsonld_page([
        ev("Microsoft AI Tour - Toronto", "2027-02-10", "Toronto", "Microsoft"),
        ev("Microsoft AI Tour - Utrecht", "2027-02-11", "Utrecht", "Microsoft"),
        ev("Microsoft AI Tour - Seattle", "2027-02-12", "Seattle", "Microsoft"),
        ev("Microsoft AI Tour - Houston", "2027-02-13", "Houston", "Microsoft"),
        ev("Microsoft AI Tour - Mumbai", "2027-02-14", "Mumbai", "Microsoft"),
        ev("Microsoft AI Tour - New York", "2027-02-15", "New York", "Microsoft"),
        ev("Microsoft AI Tour - San Francisco", "2027-02-16", "San Francisco", "Microsoft"),
        ev("Microsoft AI Tour - Chicago", "2027-02-17", "Chicago", "Microsoft"),
        ev("AWS re:Invent 2027", "2027-02-18", "Las Vegas, NV", "AWS"),
    ])
    out = extract.parse_events(html, "http://x", shipped, {"label": "t"}, TODAY)
    # all of the US + India; Toronto and Utrecht (non-US) are dropped
    assert [e.region for e in out] == ["Seattle", "Texas", "India", "New York", "Bay Area", "Other US", "Other US"]


def test_partnership_leaders_cards_and_borrowed_city():
    html = """<div class="pl-event-box"><span class="event-date">November 18, 2026 | 8:00 am - 11:00 AM PT</span>
      <span class="event-type">In-Person Event</span><h5>Catalyst Breakfast at Microsoft Ignite</h5>
      <div class="pl-event-desc"><p>Join partnership leaders</p></div><a href="/e/1">View Event</a></div>
      <div class="pl-event-box"><span class="event-date">November 3, 2026</span><span class="event-type">In-Person Event</span>
      <h5>Catalyst Summit London</h5><a href="/e/2">View</a></div>"""
    src = {"label": "PL", "host": "Partnership Leaders", "trusted": True,
           "card_selector": {"card": ".pl-event-box", "date": ".event-date", "title": "h5", "type": ".event-type",
                             "desc": ".pl-event-desc"}}
    c = cfg(False)
    out = extract.parse_events(html, "http://pl.com/events", c, src, date(2026, 10, 28))
    assert [(e.name, e.city, e.region, e.host) for e in out] == [
        ("Catalyst Breakfast at Microsoft Ignite", "San Francisco", "Bay Area", "Partnership Leaders")]  # London dropped
    assert out[0].url == "http://pl.com/e/1"


def test_text_listing_date_first_and_city_titles_are_junk():
    text = "November 11-12, 2026\nEMEA Channel Leader Summit\nRegister\nDecember 2, 2026\nCMA Power Circles\nVirtual\nRegister\nOctober 5, 2027\nDallas, TX\n"
    src = {"label": "CMA", "host": "Channel Marketing Association", "trusted": True, "text_listing": "after"}
    out = extract.parse_events("<html><body>" + text.replace("\n", "<br>") + "</body></html>",
                               "http://cma", cfg(False), src, date(2026, 10, 28))
    assert [(e.name, e.region) for e in out] == [("CMA Power Circles", "Virtual")]


def test_year_sheets_and_earliest_date_cutoff(tmp_path):
    path = tmp_path / "t.xlsx"
    mk = lambda n, d: extract.Event(name=n, start=d, city="Seattle", region="Seattle")
    events = [mk("Too Early Summit", date(2026, 7, 31)), mk("August Summit", date(2026, 8, 1)),
              mk("Winter Summit", date(2026, 12, 9)), mk("Spring Summit 2027", date(2027, 3, 15))]
    assert workbook.merge(path, events, earliest=date(2026, 8, 1)) == (3, 3)

    from openpyxl import load_workbook
    wb = load_workbook(path)
    names = lambda sheet: [r[0] for r in wb[sheet].iter_rows(min_row=2, values_only=True)]
    assert names("2026") == ["August Summit", "Winter Summit"]
    assert names("2027") == ["Spring Summit 2027"]
    assert (tmp_path / "t_2026.csv").exists() and (tmp_path / "t_2027.csv").exists()
    # existing rows before the cutoff are purged too
    assert workbook.merge(path, [], earliest=date(2026, 12, 1)) == (0, 2)


def test_csv_dates_are_plain_readable_text(tmp_path):
    path = tmp_path / "t.xlsx"
    mk = lambda n, a, b=None: extract.Event(name=n, start=a, end=b, city="Seattle", region="Seattle")
    workbook.merge(path, [mk("One day", date(2026, 8, 3)), mk("Same month", date(2026, 8, 10), date(2026, 8, 12)),
                          mk("Cross month", date(2026, 11, 30), date(2026, 12, 4))])
    import csv
    rows = list(csv.DictReader((tmp_path / "t_2026.csv").open(encoding="utf-8-sig")))
    assert [r["Date"] for r in rows] == ["Aug 3, 2026", "Aug 10\u201312, 2026", "Nov 30 \u2013 Dec 4, 2026"]
    assert "End Date" not in rows[0]


def test_csv_all_years_file_and_empty_future_year_files(tmp_path):
    path = tmp_path / "t.xlsx"
    mk = lambda n, d: extract.Event(name=n, start=d, city="Seattle", region="Seattle")
    workbook.merge(path, [mk("A", date(2026, 9, 1)), mk("B", date(2027, 3, 1)), mk("C", date(2028, 1, 5))])
    import csv
    allrows = list(csv.DictReader((tmp_path / "t_all.csv").open(encoding="utf-8-sig")))
    assert [(r["Year"], r["Name"]) for r in allrows] == [("2026", "A"), ("2027", "B"), ("2028", "C")]
    for y in range(2026, 2031):  # per-year files exist up to 2030, empty ones are header-only
        assert (tmp_path / f"t_{y}.csv").exists()
    assert list(csv.DictReader((tmp_path / "t_2030.csv").open(encoding="utf-8-sig"))) == []
    from openpyxl import load_workbook
    assert load_workbook(path).sheetnames == ["2026", "2027", "2028"]


def test_discovery_queries_cover_companies_and_cities():
    from tracker import sources
    c = cfg()
    oct_q = [q for _, q in sources.discovery_queries(c, date(2026, 11, 2))]
    assert "partner summit Seattle 2026" in oct_q and "partner summit Seattle 2027" in oct_q  # next year from July
    assert "AWS channel partner event 2027" in oct_q
    assert "channel partner event Bengaluru 2026" in oct_q
    feb_q = [q for _, q in sources.discovery_queries(c, date(2027, 2, 1))]
    assert not any("2028" in q for q in feb_q)
    d = c["discovery"]
    per_year = len(d["companies"]) * len(d["queries"]) + len(d["cities"]) * len(d["city_queries"])
    assert len(oct_q) == 2 * per_year


def test_discover_skips_social_hosts_and_needs_key(monkeypatch):
    from tracker import sources
    c = cfg()
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert sources.discover(c) == []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"web": {"results": [{"url": "https://www.linkedin.com/events/123", "title": "x"},
                                        {"url": "https://example.com/partner-summit", "title": "Partner Summit"}]}}

    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    hits = sources.discover(c)
    assert [h["url"] for h in hits] == ["https://example.com/partner-summit"]


def test_falls_through_to_later_parser_and_cleans_page_title():
    # a card that gets filtered out (no company) must not hide the page-level event behind it
    html = """<html><head><title>Home - Biz Apps Partner Summit</title></head><body>
      <p>Microsoft partners: Super Early Bird August 3-4 2027 Renton, WA</p>
      <ul><li><a href="/x"><h3>Photo gallery</h3></a> Jul 29, 2027 Somewhere</li></ul></body></html>"""
    out = extract.parse_events(html, "http://bizapps", cfg(False), {"label": "d", "require_keyword": True},
                               date(2026, 11, 2))
    assert [(e.name, e.start, e.end, e.region) for e in out] == [
        ("Biz Apps Partner Summit", date(2027, 8, 3), date(2027, 8, 4), "Seattle")]


def test_same_name_same_year_is_one_event_unless_cities_differ(tmp_path):
    path = tmp_path / "t.xlsx"
    E = lambda n, d, c: extract.Event(name=n, start=d, city=c, region="Other US")
    events = [E("Oracle AI World", date(2026, 10, 26), "Las Vegas"),
              E("Oracle AI World 2026", date(2026, 11, 25), "Las Vegas"),   # same edition, wrong scraped date
              E("World Forum", date(2026, 9, 10), "New York"),
              E("World Forum", date(2026, 10, 6), "Mumbai")]                 # different city: separate event
    assert workbook.merge(path, events) == (3, 3)


def test_happy_hour_dropped_even_from_channel_association():
    v = extract.classify("CMA San Diego Happy Hour", "Channel Marketing Association", "", "San Diego", "",
                         {"trusted": True}, cfg())
    assert not v.keep


def test_region_from_event_name_when_text_is_vague():
    v = extract.classify("TechCon 365, DATACON & PWRCON Seattle 2027", "Microsoft", "Washington state", "", "", {}, cfg())
    assert (v.keep, v.region, v.city) == (True, "Seattle", "Seattle")


def test_name_prefix_for_company_event_listings():
    html = "<ul><li><a href='/wt/nyc'><h3>World Tour New York</h3></a> Dec 3, 2026 New York</li></ul>"
    out = extract.parse_events(html, "http://sf.com/events", cfg(False),
                               {"label": "SF", "host": "Salesforce", "name_prefix": "Salesforce "}, date(2026, 11, 2))
    assert [(e.name, e.region, e.host) for e in out] == [("Salesforce World Tour New York", "New York", "Salesforce")]


def test_tbd_row_kept_then_replaced_when_date_found(tmp_path):
    path = tmp_path / "t.xlsx"
    tbd = extract.Event(name="Datadog DASH", start=None, host="Datadog", region="TBD")
    assert workbook.merge(path, [tbd], date(2026, 11, 3), earliest=date(2026, 8, 1)) == (1, 1)
    assert workbook.merge(path, [tbd], date(2026, 11, 3), earliest=date(2026, 8, 1)) == (0, 1)  # no duplicate TBD
    import csv
    row = list(csv.DictReader((tmp_path / "t_all.csv").open(encoding="utf-8-sig")))[0]
    assert (row["Year"], row["Date"], row["Name"]) == ("TBD", "TBD", "Datadog DASH")
    from openpyxl import load_workbook
    assert "Date TBD" in load_workbook(path).sheetnames

    dated = extract.Event(name="Datadog DASH 2027", start=date(2027, 6, 8), city="New York", region="New York")
    assert workbook.merge(path, [dated], date(2026, 11, 3)) == (1, 1)  # replaced, not added alongside
    assert [r[0] for r in workbook.load_rows(path)] == ["Datadog DASH 2027"]
    # a dated upcoming row means no new TBD placeholder is added for it
    assert workbook.merge(path, [extract.Event(name="Datadog DASH", start=None)], date(2026, 11, 3)) == (0, 1)


def test_tavily_search_and_flagship_lookup(monkeypatch):
    from tracker import sources
    import refresh

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"url": "https://www.servicenow.com/events/knowledge.html",
                                 "title": "Knowledge 2027 | ServiceNow",
                                 "content": "Knowledge 2027 May 5-7, 2027 | Las Vegas. Celebrate 20 years..."}]}

    calls = []
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(sources.requests, "post", lambda url, **k: calls.append((url, k["json"]["query"])) or Resp())
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    assert sources.search_provider() == "tavily"
    hits = sources.search("ServiceNow Knowledge 2027")
    assert hits[0]["description"].startswith("Knowledge 2027") and calls[0][0] == "https://api.tavily.com/search"

    src = {"label": "ServiceNow Knowledge", "url": "https://www.servicenow.com/events/knowledge.html",
           "host": "ServiceNow", "name": "ServiceNow Knowledge", "single": True}
    found = refresh.lookup_by_search(src, cfg(False))  # snippet says "Knowledge 2027", not "ServiceNow Knowledge"
    assert [(e.name, e.start, e.end, e.region) for e in found] == [
        ("ServiceNow Knowledge 2027", date(2027, 5, 5), date(2027, 5, 7), "Other US")]


def test_placeholder_for_undated_flagship():
    import refresh
    ev = refresh.placeholder({"label": "Datadog DASH", "url": "https://www.dashcon.io/", "host": "Datadog",
                              "name": "Datadog DASH", "single": True}, cfg())
    assert (ev.name, ev.start, ev.region, ev.companies) == ("Datadog DASH", None, "TBD", ["Datadog"])


def test_co_branded_x_meetups_are_small():
    v = extract.classify("Building Voice AI That Holds Up in Production: Coval x Cloudflare", "Coval, Cloudflare Developer",
                         "", "San Francisco", "", {}, cfg())
    assert not v.keep
    assert extract.classify("XChange 2027", "The Channel Company", "", "", "", {}, cfg()).keep  # 'Xchange' is not 'x'
