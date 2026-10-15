"""Find calendar dates in free text."""

import re
from datetime import date

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
_MON = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"

DATE_PATTERNS = [
    # June 5, 2026 / June 05 2026 / Jun. 5th, 2026
    (re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "mdy"),
    # 5 June 2026
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON},?\s+(\d{{4}})\b", re.I), "dmy"),
    # 2026-06-05
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "iso"),
    # 06/05/2026 or 6/5/26
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"), "us"),
]


def _month(name):
    return MONTHS.get(name.lower().rstrip(".")[:3])


def _build(kind, groups):
    try:
        if kind == "mdy":
            return date(int(groups[2]), _month(groups[0]), int(groups[1]))
        if kind == "dmy":
            return date(int(groups[2]), _month(groups[1]), int(groups[0]))
        if kind == "iso":
            return date(int(groups[0]), int(groups[1]), int(groups[2]))
        if kind == "us":
            year = int(groups[2])
            year = year + 2000 if year < 100 else year
            return date(year, int(groups[0]), int(groups[1]))
    except (TypeError, ValueError):
        return None
    return None


def find_dates(text):
    """All dates in reading order as (date, start, end). Overlapping matches are dropped."""
    found = []
    taken = []
    for pattern, kind in DATE_PATTERNS:
        for m in pattern.finditer(text or ""):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            d = _build(kind, m.groups())
            if d and 1990 <= d.year <= 2100:
                found.append((d, m.start(), m.end()))
                taken.append((m.start(), m.end()))
    found.sort(key=lambda x: x[1])
    return found


def first_date(text):
    dates = find_dates(text)
    return dates[0][0] if dates else None


def parse_date(value):
    """Parse a single date string in any common format (used for API fields)."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    d = first_date(str(value))
    if d:
        return d
    try:
        from dateutil import parser

        return parser.parse(str(value), fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None
