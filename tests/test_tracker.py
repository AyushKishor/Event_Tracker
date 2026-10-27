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


def cfg():
    return load_config(ROOT / "config.yaml")


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
    assert extract.detect_region("Chicago", r) == ""
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
    assert (google.region, google.companies) == ("Other", ["Google"])  # out-of-region flagship-style kept
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


def test_merge_idempotent_and_preserves_user_edits(tmp_path):
    path = tmp_path / "t.xlsx"
    html = jsonld_page([ev("AWS Partner Summit", "2027-03-10", "Seattle", "AWS")])
    events = extract.parse_events(html, "http://x", cfg(), {"label": "t"}, TODAY)

    assert workbook.merge(path, events, TODAY) == (1, 1)

    from openpyxl import load_workbook
    wb = load_workbook(path)
    ws = wb[workbook.SHEET]
    ws.cell(row=2, column=workbook.IDX["Status"] + 1, value="Attending")
    ws.cell(row=2, column=workbook.IDX["Notes"] + 1, value="meet partner lead")
    wb.save(path)

    assert workbook.merge(path, events, TODAY) == (0, 1)  # no duplicate
    row = workbook.load_rows(path)[0]
    assert row[workbook.IDX["Status"]] == "Attending"
    assert row[workbook.IDX["Notes"]] == "meet partner lead"
    assert load_workbook(path).sheetnames == ["Events"]  # single sheet only


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
        ("AI Agents Seattle - August 2026", "Seattle", ""),           # local meetup, no company
        ("Build with Gemini - Seattle", "Seattle", ""),
        ("Games & AI Meetup / Co-hosted by AWS", "Seattle", ""),
    ]
    for name, city, host in keep:
        assert verdict(name, city, host).keep, name


def test_irrelevant_out_of_region_event_is_dropped():
    assert not verdict("Local Pottery Fair", "Boise").keep
    assert not verdict("Regional Dental Expo", "Boise").keep  # event word alone, out of region
