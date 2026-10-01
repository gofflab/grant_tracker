"""Grants.gov public API (no key needed): look up a federal opportunity by its number.

https://api.grants.gov/v1/api/search2 and /fetchOpportunity. Field names are read defensively
because the response shape has changed over time.
"""

import logging
import re

from .dates import parse_date
from .fetch import FetchError, post_json
from .rules import Fields, _clip, money_values
from .text import from_html

logger = logging.getLogger(__name__)

API = "https://api.grants.gov/v1/api"
SOURCE = "Grants.gov"


def _get(d, *keys):
    for key in keys:
        if isinstance(d, dict) and d.get(key) not in (None, "", "none", "None"):
            return d[key]
    return None


def search(number):
    data = post_json(f"{API}/search2", {"oppNum": number, "rows": 5})
    hits = (_get(data, "data") or {}).get("oppHits") or []
    exact = [h for h in hits if str(_get(h, "number", "opportunityNumber") or "").upper() == number.upper()]
    return (exact or hits or [None])[0]


def lookup(number):
    """Return (Fields, funding_description_url) for an opportunity number, or (None, None)."""
    hit = search(number)
    if not hit:
        return None, None
    opp_id = _get(hit, "id", "opportunityId")
    detail = post_json(f"{API}/fetchOpportunity", {"opportunityId": int(opp_id)}) if opp_id else {}
    data = _get(detail, "data") or {}
    syn = _get(data, "synopsis") or {}
    f = Fields()

    f.add("title", _get(data, "opportunityTitle") or _get(hit, "title"), SOURCE, 0.9)
    f.add("number", _get(data, "opportunityNumber") or _get(hit, "number"), SOURCE, 0.95)
    f.add("funder_name", _get(syn, "agencyName") or _get(hit, "agency", "agencyName"), SOURCE, 0.9)

    close = parse_date(_get(syn, "responseDate", "responseDateStr", "closeDate") or _get(hit, "closeDate"))
    if close:
        # For multi-cycle NIH announcements this is the last due date, so it ranks below the NOFO's own table.
        f.add("deadline", close, f"{SOURCE} close date", 0.5)
    archive = parse_date(_get(syn, "archiveDate", "archiveDateStr"))
    if archive:
        f.add("expires_on", archive, f"{SOURCE} archive date", 0.45)

    ceiling = _get(syn, "awardCeiling", "awardCeilingFormatted")
    if ceiling:
        amounts = money_values(f"${ceiling}") if not str(ceiling).startswith("$") else money_values(str(ceiling))
        if amounts:
            f.add("max_award", amounts[0], SOURCE, 0.85)
    awards = _get(syn, "numberOfAwards", "expectedNumberOfAwards")
    if awards:
        f.extra["Expected awards"] = str(awards)
    funding = _get(syn, "estimatedFunding", "estimatedFundingFormatted")
    if funding and money_values(f"${funding}"):
        f.extra["Program funding"] = f"${money_values(f'${funding}')[0]:,.0f} total"

    desc = _get(syn, "synopsisDesc")
    if desc:
        f.add("summary", _clip(from_html(desc).text, 1500), SOURCE, 0.75)
    elig = _get(syn, "applicantEligibilityDesc")
    if elig:
        f.add("eligibility", _clip(from_html(elig).text, 1200), SOURCE, 0.65)

    name, email = _get(syn, "agencyContactName"), _get(syn, "agencyContactEmail")
    if email:
        f.add("contact", f"{name} <{email}>" if name else email, SOURCE, 0.6)

    cfdas = _get(data, "cfdas") or []
    numbers = [str(_get(c, "cfdaNumber")) for c in cfdas if isinstance(c, dict) and _get(c, "cfdaNumber")]
    if numbers:
        f.extra["Assistance listing"] = ", ".join(numbers)

    link = _get(syn, "fundingDescLinkUrl")
    if link and not re.match(r"^https?://", str(link)):
        link = None
    if link:
        f.add("url", link, SOURCE, 0.8)
    f.extra["Grants.gov ID"] = str(opp_id) if opp_id else ""
    return f, link


def safe_lookup(number):
    try:
        return lookup(number)
    except (FetchError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Grants.gov lookup for %s failed: %s", number, exc)
        f = Fields()
        f.notes.append(f"Grants.gov lookup didn't work for {number}, so values come from the announcement only. "
                       f"Details: {str(exc).rstrip('.')}.")
        return f, None
