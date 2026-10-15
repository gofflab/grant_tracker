"""Rule-based extraction of opportunity fields from announcement text.

Works best on structured announcements (NIH Guide NOFOs, NSF solicitations), which label
their fields ("Letter of Intent Due Date(s)", "Award Project Period", ...), and falls back
to looser patterns for free-form foundation pages.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .dates import find_dates
from .text import Document

# Labels that start a new field or section; used to stop collecting a value.
STOP_LABELS = [
    r"Participating Organization", r"Components of Participating Organizations", r"Funding Opportunity Title",
    r"Activity Code", r"Announcement Type", r"Related Notices", r"Notice of Funding Opportunity",
    r"Funding Opportunity (?:Announcement )?(?:\(FOA\) )?Number", r"Companion", r"Assistance Listing", r"Number of Applications",
    r"Funding Opportunity Purpose", r"Funding Opportunity Goal", r"Key Dates", r"Posted Date", r"Open Date",
    r"Letter of Intent", r"Application Due Date", r"AIDS Application Due Date", r"Scientific Merit Review",
    r"Advisory Council Review", r"Earliest Start Date", r"Expiration Date", r"Due Dates for E\.O\.",
    r"Required Application Instructions", r"Table of Contents", r"Section [IVX]+\.", r"Part \d\.", r"Award Budget",
    r"Award Project Period", r"Funding Instrument", r"Application Types Allowed", r"Clinical Trial",
    r"Funds Available", r"Eligible Organizations", r"Eligible Individuals", r"Cost Sharing", r"Foreign ",
    r"Program Title", r"Synopsis of Program", r"Full Proposal (?:Deadline|Target Date)", r"Preliminary Proposal",
    r"Anticipated Funding Amount", r"Estimated Number of Awards", r"Anticipated Type of Award", r"Award Information",
    r"Eligibility Information", r"Proposal Preparation", r"Who May Submit", r"Limit on Number of Proposals",
    r"Scientific/Research Contact", r"Peer Review Contact", r"Financial/Grants Management Contact", r"Agency Contacts",
    r"Program Officer", r"Cognizant Program Officer", r"Important Information",
]
_STOP = re.compile(r"^\s*(?:" + "|".join(STOP_LABELS) + r")", re.I)

WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

NIH_NUMBER = re.compile(r"\b(?:PAR|PAS|PA|RFA|OTA|NOT)-(?:[A-Z]{2,3}-)?\d{2}-\d{3}\b")
NSF_NUMBER = re.compile(r"\bNSF\s+(\d{2}-\d{3,4})\b")
ACTIVITY = re.compile(r"\b([A-Z]{1,3}\d{2}(?:\s*/\s*[A-Z]{1,3}\d{2})?)\b")
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(million|billion|thousand|[MK])?\b", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LIMITED = re.compile(
    r"\b(?:only|no more than|limited to|maximum of|up to)\s+(?:one|two|three|four|five|\d+)\s+"
    r"(?:\w+\s+){0,3}(?:application|proposal|nomination|pre-?proposal|candidate|submission)s?\s+"
    r"(?:may be submitted\s+)?(?:per|from each|from an?|by each|by an?)\s+(?:\w+\s+){0,2}"
    r"(?:institution|organization|university|campus|school)",
    re.I,
)
_ORG_WORD = r"(?:Foundation|Fund|Trust|Society|Association|Alliance|Institute|Academy|Endowment|Philanthropies|Federation|Council|Initiative)"
_ORG_OF = r"[ \t]+(?:for|of|on)(?:[ \t]+[A-Z][\w&.'-]*){1,5}"
ORG_NAME = re.compile(
    rf"(?:[A-Z][\w&.'-]*[ \t]+){{1,6}}{_ORG_WORD}\b(?:{_ORG_OF})?|\b{_ORG_WORD}{_ORG_OF}"
)

# New-application standard due dates (month, day) by activity code family. Verify on NIH's
# standard due dates page; resubmissions and AIDS-related applications use other dates.
NIH_STANDARD_DATES = [
    (re.compile(r"^(R01|U01|R18|U18|R25|R10|DP3)"), [(2, 5), (6, 5), (10, 5)]),
    (re.compile(r"^(R03|R21|R33|R34|R36|U34|R21/R33|UG3)"), [(2, 16), (6, 16), (10, 16)]),
    (re.compile(r"^K"), [(2, 12), (6, 12), (10, 12)]),
    (re.compile(r"^F"), [(4, 8), (8, 8), (12, 8)]),
    (re.compile(r"^R15"), [(2, 25), (6, 25), (10, 25)]),
    (re.compile(r"^(P01|P20|P30|P50|P60|U19|U54)"), [(1, 25), (5, 25), (9, 25)]),
    (re.compile(r"^(R41|R42|R43|R44|U43|U44)"), [(1, 5), (4, 5), (9, 5)]),
]


@dataclass
class Candidate:
    value: Any
    source: str
    confidence: float
    snippet: str = ""


@dataclass
class Fields:
    candidates: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def add(self, name, value, source, confidence, snippet=""):
        if value in (None, "", [], {}):
            return
        self.candidates.setdefault(name, []).append(Candidate(value, source, confidence, snippet[:300]))

    def best(self, name):
        options = self.candidates.get(name) or []
        return max(options, key=lambda c: c.confidence) if options else None

    def merge(self, other):
        for name, options in other.candidates.items():
            self.candidates.setdefault(name, []).extend(options)
        self.notes.extend(n for n in other.notes if n not in self.notes)
        for k, v in other.extra.items():
            self.extra.setdefault(k, v)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _lines(text):
    return [line for line in text.split("\n")]


def _label_re(label):
    return re.compile(r"^\s*(?:" + label + r")\s*(?:\(s\))?\s*(?:[:|\-–]\s*|\s+(?=\S))?(.*)$", re.I)


def labeled(text, labels, max_lines=4):
    """Value following a field label: rest of the line, else the next few non-label lines."""
    lines = _lines(text)
    for label in labels:
        rx = _label_re(label)
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            value = m.group(1).strip(" |:")
            if value:
                return value, line.strip()
            collected = []
            for nxt in lines[i + 1:i + 1 + max_lines * 2]:
                if not nxt.strip():
                    if collected:
                        break
                    continue
                if _STOP.match(nxt):
                    break
                collected.append(nxt.strip(" |"))
                if len(collected) >= max_lines:
                    break
            if collected:
                return " ".join(collected), f"{line.strip()} {collected[0]}"
    return None, ""


def block(text, labels, max_chars=1500, max_lines=40):
    """Paragraph(s) following a section label, up to the next known label."""
    lines = _lines(text)
    for label in labels:
        rx = _label_re(label)
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            parts = [m.group(1).strip(" |:")] if m.group(1).strip(" |:") else []
            for nxt in lines[i + 1:i + 1 + max_lines]:
                if _STOP.match(nxt) and parts:
                    break
                if nxt.strip():
                    parts.append(nxt.strip(" |"))
                elif parts and sum(len(p) for p in parts) > max_chars * 0.6:
                    break
                if sum(len(p) for p in parts) >= max_chars:
                    break
            out = " ".join(p for p in parts if p).strip()
            if out:
                return _clip(out, max_chars), line.strip()
    return None, ""


def _clip(text, limit):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: end + 1] if end > limit * 0.5 else cut.rstrip() + "…").strip()


def money_values(text):
    out = []
    for m in MONEY.finditer(text or ""):
        try:
            v = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("million", "m"):
            v *= 1_000_000
        elif unit == "billion":
            v *= 1_000_000_000
        elif unit in ("thousand", "k"):
            v *= 1_000
        if v >= 100:
            out.append(v)
    return out


def years_value(text):
    vals = []
    for m in re.finditer(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)[\s-]+(?:\(\d+\)\s*)?years?\b", text or "", re.I):
        token = m.group(1).lower()
        vals.append(int(token) if token.isdigit() else WORD_NUMBERS[token])
    return max(vals) if vals else None


def _fmt(d):
    return f"{d:%b} {d.day}, {d.year}"


def nih_standard_dates(mechanism, today, count=3):
    if not mechanism:
        return []
    code = mechanism.replace(" ", "").upper()
    for rx, monthdays in NIH_STANDARD_DATES:
        if rx.match(code):
            out = []
            for year in (today.year, today.year + 1, today.year + 2):
                for month, day in monthdays:
                    d = date(year, month, day)
                    if d >= today:
                        out.append(d)
            return out[:count]
    return []


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

LABELED = "Announcement field"
PATTERN = "Pattern in text"


def extract(doc: Document, url: str = "", today: date | None = None) -> Fields:
    from django.utils import timezone

    today = today or timezone.localdate()
    text = doc.text
    head = text[:6000]
    f = Fields()

    # --- Title
    value, snip = labeled(text, [r"Funding Opportunity Title", r"Program Title", r"Opportunity Title", r"Solicitation Title"], 3)
    f.add("title", value, f"{LABELED}: title", 0.85, snip)
    if doc.title:
        clean = re.sub(r"^\s*(?:" + NIH_NUMBER.pattern + r"|NSF\s+\d{2}-\d{3,4})\s*[:\-–]\s*", "", doc.title)
        first = re.split(r"\s+[|–—]\s+|\s+-\s+", clean)[0].strip()
        f.add("title", (first if len(first) >= 12 else clean).strip()[:300], "Page title", 0.6, doc.title)
    if doc.headings:
        f.add("title", doc.headings[0][:300], "First heading", 0.45, doc.headings[0])

    # --- Announcement number
    value, snip = labeled(text, [
        r"Notice of Funding Opportunity \(NOFO\) Number", r"Funding Opportunity Announcement \(FOA\) Number",
        r"Funding Opportunity Number", r"NOFO Number", r"Opportunity Number", r"Solicitation Number", r"Program Solicitation",
    ], 1)
    if value:
        m = NIH_NUMBER.search(value) or NSF_NUMBER.search(value)
        number = (m.group(0) if m else value.split(" ")[0])[:60]
        f.add("number", number, f"{LABELED}: number", 0.85, snip)
    m = NIH_NUMBER.search(url or "")
    if m:
        f.add("number", m.group(0), "Link address", 0.8, url)
    m = NIH_NUMBER.search(head)
    if m and not m.group(0).startswith("NOT-"):
        f.add("number", m.group(0), PATTERN, 0.55, m.group(0))
    m = NSF_NUMBER.search(head)
    if m:
        f.add("number", f"NSF {m.group(1)}", PATTERN, 0.55, m.group(0))

    # --- Mechanism / activity code
    value, snip = labeled(text, [r"Activity Code"], 2)
    if value:
        m = ACTIVITY.search(value)
        if m:
            f.add("mechanism", m.group(1).replace(" ", ""), f"{LABELED}: activity code", 0.85, snip)
    title_best = f.best("title")
    for source_text, label in ((title_best.value if title_best else "", "Title"), (doc.title, "Page title")):
        m = re.search(r"\(\s*([A-Z]{1,3}\d{2}(?:\s*/\s*[A-Z]{1,3}\d{2})?)\b", source_text or "")
        if m:
            f.add("mechanism", m.group(1).replace(" ", ""), label, 0.7, source_text)
            break
    if re.search(r"Faculty Early Career Development|\bCAREER\b", head):
        f.add("mechanism", "CAREER", PATTERN, 0.5, "CAREER")
    value, snip = labeled(text, [r"Anticipated Type of Award"], 1)
    f.add("mechanism", value[:40] if value else None, f"{LABELED}: award type", 0.4, snip)

    # --- Sponsor unit (institutes, directorates)
    value, snip = block(text, [r"Components of Participating Organizations"], 800, 15)
    if value:
        abbrevs = list(dict.fromkeys(re.findall(r"\(([A-Z][A-Za-z]{1,7})\)", value)))
        if abbrevs:
            f.add("sponsor_unit", ", ".join(abbrevs)[:120], f"{LABELED}: components", 0.85, snip)
    nsf_units = re.findall(r"(?:Directorate|Division|Office) (?:for|of) [A-Z][A-Za-z ,&-]+?\s*\(([A-Z]{2,6})\)", head)
    if nsf_units:
        f.add("sponsor_unit", "/".join(dict.fromkeys(nsf_units))[:120], PATTERN, 0.6, ", ".join(nsf_units))

    # --- Funder name (resolved to a Funder record later)
    value, snip = labeled(text, [r"Participating Organization"], 2)
    f.add("funder_name", value[:200] if value else None, f"{LABELED}: organization", 0.85, snip)
    if re.search(r"National Science Foundation", head):
        f.add("funder_name", "National Science Foundation", PATTERN, 0.55, "National Science Foundation")
    if re.search(r"National Institutes of Health", head):
        f.add("funder_name", "National Institutes of Health", PATTERN, 0.5, "National Institutes of Health")
    # Foundations and societies rarely label themselves; fall back to the site name or a named organization.
    if doc.site_name:
        f.add("funder_name", doc.site_name[:200], "Page site name", 0.45, doc.site_name)
    suffix = re.split(r"\s+[|\u2013\u2014]\s+|\s+-\s+", doc.title or "")[1:]
    for part in reversed(suffix):
        if ORG_NAME.fullmatch(part.strip()):
            f.add("funder_name", re.sub(r"^The[ \t]+", "", part.strip())[:200], "Page title", 0.45, doc.title)
            break
    m = ORG_NAME.search(head)
    if m:
        f.add("funder_name", re.sub(r"^The[ \t]+", "", m.group(0))[:200], PATTERN, 0.35, m.group(0))

    # --- Summary
    value, snip = block(text, [r"Funding Opportunity Purpose", r"Synopsis of Program", r"Program Synopsis",
                               r"Purpose", r"Program Description", r"Overview", r"About the Program", r"Summary"], 1500)
    f.add("summary", value, f"{LABELED}: purpose", 0.8, snip)
    f.add("summary", _clip(doc.description, 1000) if doc.description else None, "Page description", 0.5, doc.description)
    for para in text.split("\n"):
        if len(para) > 220 and not _STOP.match(para):
            f.add("summary", _clip(para, 1000), "First paragraph", 0.3, para)
            break

    # --- Eligibility
    value, snip = block(text, [r"Eligible Individuals(?: \(Program Director/Principal Investigator\))?", r"Who May Serve as PI",
                               r"Who May Submit Proposals", r"Eligibility Requirements", r"Eligibility Criteria",
                               r"Eligibility Information", r"Eligibility", r"Eligible Organizations"], 1200)
    f.add("eligibility", value, f"{LABELED}: eligibility", 0.7, snip)

    # --- Deadlines
    _deadlines(text, f, today)

    value, snip = labeled(text, [r"Expiration Date", r"Expires", r"Close Date"], 2)
    if value:
        d = find_dates(value)
        if d:
            f.add("expires_on", d[0][0], f"{LABELED}: expiration", 0.85, snip)

    value, snip = labeled(text, [r"Posted Date", r"Release Date"], 1)
    if value and find_dates(value):
        f.extra["Posted"] = _fmt(find_dates(value)[0][0])

    # --- Budget
    _budget(text, f)

    # --- Duration
    value, snip = block(text, [r"Award Project Period", r"Project Period", r"Duration of Award", r"Award Duration",
                               r"Award Period", r"Grant Period", r"Duration"], 500, 8)
    years = years_value(value) if value else None
    f.add("max_duration_years", years, f"{LABELED}: project period", 0.8, snip)
    m = (re.search(r"(?:maximum (?:project|award) period|up to|for a period of up to)\s+(?:is\s+)?(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)[\s-]+years?", text, re.I)
         or re.search(r"\b(?:over|across|for)\s+(?:a period of\s+)?(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)[\s-]+years?\b", text, re.I))
    if m:
        token = m.group(1).lower()
        f.add("max_duration_years", int(token) if token.isdigit() else WORD_NUMBERS[token], PATTERN, 0.5, m.group(0))

    # --- Limited submission
    m = LIMITED.search(text)
    if m:
        f.add("limited_submission", True, PATTERN, 0.7, m.group(0))
    elif re.search(r"\blimited[- ]submission\b", text, re.I):
        f.add("limited_submission", True, PATTERN, 0.55, "limited submission")

    # --- Contact
    contact = _contact(text)
    if contact:
        f.add("contact", contact[0], f"{LABELED}: contact", 0.7, contact[1])

    # --- Extras kept as custom fields
    value, snip = labeled(text, [r"Assistance Listing Number\(s\)", r"Assistance Listing", r"CFDA Number"], 1)
    if value and re.search(r"\d{2}\.\d{3}", value):
        f.extra["Assistance listing"] = ", ".join(re.findall(r"\d{2}\.\d{3}", value))[:80]
    m = re.search(r"Clinical Trials? (Not Allowed|Required|Optional)", head, re.I)
    if m:
        f.extra["Clinical trials"] = m.group(1).capitalize()
    value, snip = labeled(text, [r"Application Types Allowed"], 3)
    if value:
        f.extra["Application types"] = _clip(value, 160)
    value, snip = labeled(text, [r"Estimated Number of Awards", r"Anticipated Number of Awards", r"Number of Awards"], 2)
    if value:
        f.extra["Expected awards"] = _clip(value, 120)
    return f


def _contact(text):
    lines = _lines(text)
    for label in (r"Scientific/Research Contact", r"Cognizant Program Officer", r"Program Officer", r"Program Contact",
                  r"Contact Information", r"Contacts?"):
        rx = _label_re(label)
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            following = [m.group(1).strip(" |:")] + [l.strip(" |") for l in lines[i + 1:i + 9]]
            following = [l for l in following if l]
            email = next((EMAIL.search(l) for l in following if EMAIL.search(l)), None)
            if not email:
                continue
            name = re.split(r",|\||\b(?:telephone|phone|tel|email|e-mail)\b|:", following[0], flags=re.I)[0].strip()
            if EMAIL.search(name) or len(name) > 80 or len(name) < 3:
                name = ""
            return (f"{name} <{email.group(0)}>" if name else email.group(0)), line.strip()
    return None


def _deadlines(text, f, today):
    # Letter of intent / pre-proposal
    value, snip = labeled(text, [r"Letters? of Intent Due Dates?", r"Letters? of Intent Deadline", r"Letters? of Intent",
                                 r"Preliminary Proposal Due Dates?", r"Pre-?proposals? Due Dates?", r"Pre-?proposals? Deadline",
                                 r"Pre-?proposals?(?: are)? due", r"Letters? of Inquiry(?: Due| Deadline)?", r"Concept Papers?(?: Due)?",
                                 r"LOI Due Dates?", r"LOI Deadline", r"LOIs? due"], 6)
    loi_dates = [d for d, *_ in find_dates(value or "")]
    if loi_dates:
        upcoming = [d for d in loi_dates if d >= today]
        f.add("loi_deadline", (upcoming or loi_dates)[0], f"{LABELED}: letter of intent", 0.85, snip)
    elif value and re.search(r"(\d+)\s+days\s+(?:prior|before)", value, re.I):
        days = int(re.search(r"(\d+)\s+days\s+(?:prior|before)", value, re.I).group(1))
        f.notes.append(f"Letter of intent is due {days} days before the application due date.")
        f.extra["LOI timing"] = f"{days} days before the due date"
        f.candidates["_loi_days"] = [Candidate(days, LABELED, 0.8, snip)]
    elif value and re.search(r"not applicable|n/a|none", value, re.I):
        f.notes.append("No letter of intent required.")

    # Application due dates (often a table: first date per row is the "new" deadline)
    lines = _lines(text)
    due_rx = [_label_re(r) for r in (
        r"Application Due Dates?", r"Full Proposal Deadlines?(?: Date)?", r"Full Proposal Target Dates?",
        r"Full Proposals?(?: (?:are )?due)?(?: Dates?)?", r"Final Proposals?(?: due)?", r"Applications?(?: are)? due by",
        r"Proposal Due Dates?", r"Proposal Deadlines?", r"Application Deadlines?", r"Submission Deadlines?",
        r"Applications? (?:are )?Due", r"Deadlines?", r"Due Dates?",
    )]
    due_dates, standard, snippet = [], False, ""
    for rx in due_rx:
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            snippet = line.strip()
            rows = [m.group(1)] + lines[i + 1:i + 30]
            for j, row in enumerate(rows):
                if j and _STOP.match(row) and not re.match(r"^\s*(New|Renewal|AIDS|Standard)", row, re.I):
                    break
                if re.search(r"standard (?:due )?dates? (?:apply|listed)|standard due dates", row, re.I):
                    standard = True
                if re.search(r"annually|each year|every year|yearly", row, re.I):
                    f.add("recurring", True, f"{LABELED}: due dates", 0.8, row.strip())
                    f.add("recurrence_notes", _clip(row.strip(), 200), f"{LABELED}: due dates", 0.6, row.strip())
                found = find_dates(row)
                if found:
                    due_dates.append(found[0][0])
            if due_dates or standard:
                break
        if due_dates or standard:
            break

    if due_dates:
        unique = sorted(set(due_dates))
        upcoming = [d for d in unique if d >= today]
        if upcoming:
            f.add("deadline", upcoming[0], f"{LABELED}: due dates", 0.85, snippet)
        else:
            f.add("deadline", unique[-1], f"{LABELED}: due dates", 0.6, snippet)
            f.notes.append("All listed application due dates have passed; check for a reissued announcement.")
        if len(unique) > 1:
            shown = upcoming[:8] or unique[-3:]
            f.add("recurring", True, f"{LABELED}: due dates", 0.85, snippet)
            notes = "; ".join(_fmt(d) for d in shown) + ("; …" if len(upcoming) > 8 else "")
            f.add("recurrence_notes", f"Due dates: {notes}"[:200], f"{LABELED}: due dates", 0.85, snippet)
    if standard:
        f.add("recurring", True, f"{LABELED}: standard dates", 0.8, snippet)
        f.candidates["_nih_standard"] = [Candidate(True, LABELED, 0.8, snippet)]

    # Looser fallback: any line that says "deadline"/"due" with a date
    if not due_dates and not standard:
        best = None
        for line in lines:
            if re.search(r"deadline|due (?:date|by|on)|applications? due|submission (?:date|window)|closing date|closes on", line, re.I):
                for d, *_ in find_dates(line):
                    if d >= today and (best is None or d < best[0]):
                        best = (d, line)
        if best:
            f.add("deadline", best[0], PATTERN, 0.45, best[1])


def _budget(text, f):
    value, snip = block(text, [r"Award Budget", r"Award Ceiling", r"Award Size", r"Award Amount", r"Maximum Award",
                               r"Funding Amount", r"Amount of Award", r"Budget"], 700, 10)
    if value:
        amounts = money_values(value)
        if re.search(r"not limited|are not limited", value, re.I):
            f.add("budget_notes", "Not limited; must reflect actual needs", f"{LABELED}: award budget", 0.75, snip)
        if amounts:
            top = max(amounts)
            f.add("max_award", top, f"{LABELED}: award budget", 0.75, snip)
            sentence = next((s for s in re.split(r"(?<=[.;])\s+", value) if "$" in s), value)
            f.add("budget_notes", _clip(sentence, 200), f"{LABELED}: award budget", 0.7, snip)
    value, snip = labeled(text, [r"Anticipated Funding Amount", r"Funds Available and Anticipated Number of Awards",
                                 r"Total Funding", r"Estimated Total Program Funding"], 3)
    if value and money_values(value):
        f.extra["Program funding"] = _clip(value, 160)
        if not f.best("budget_notes"):
            f.add("budget_notes", _clip(f"Total program funding: {value}", 200), f"{LABELED}: funding amount", 0.5, snip)
    if not f.best("max_award"):
        m = re.search(r"up to \$\s?\d[\d,]*(?:\.\d+)?\s*(?:million|M|K|thousand)?[^.]{0,80}", text, re.I)
        if m:
            amounts = money_values(m.group(0))
            if amounts:
                f.add("max_award", amounts[0], PATTERN, 0.4, m.group(0))
                f.add("budget_notes", _clip(m.group(0), 200), PATTERN, 0.4, m.group(0))
