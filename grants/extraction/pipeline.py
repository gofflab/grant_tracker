"""Run every extractor over an announcement and assemble a reviewable Opportunity draft."""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlparse

from django.utils import timezone

from . import ai, grantsgov, rules
from .fetch import FetchError, fetch
from .text import Document, from_bytes, from_html

FIELD_LABELS = [
    ("title", "Title"), ("number", "Announcement number"), ("funder", "Funder"), ("sponsor_unit", "Institute / directorate"),
    ("mechanism", "Mechanism"), ("url", "Announcement link"), ("loi_deadline", "LOI due"), ("deadline", "Sponsor deadline"),
    ("recurring", "Recurring due dates"), ("recurrence_notes", "Due-date notes"), ("expires_on", "Expires"),
    ("max_award", "Award ceiling"), ("budget_notes", "Budget notes"), ("max_duration_years", "Max duration (years)"),
    ("limited_submission", "Limited submission"), ("summary", "Summary"), ("eligibility", "Eligibility"),
]
MAX_LEN = {"title": 300, "number": 60, "mechanism": 40, "sponsor_unit": 120, "budget_notes": 200,
           "recurrence_notes": 200, "url": 500}
NIH_GUIDE = {"PA": "pa-files", "PAR": "pa-files", "PAS": "pa-files", "RFA": "rfa-files", "NOT": "notice-files", "OTA": "ota-files"}


@dataclass
class Result:
    initial: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    duplicate_id: int | None = None
    used_ai: bool = False
    new_funder: dict | None = None

    def as_session(self):
        return {"initial": self.initial, "rows": self.rows, "notes": self.notes, "sources": self.sources,
                "duplicate_id": self.duplicate_id, "used_ai": self.used_ai, "new_funder": self.new_funder}


def looks_like_number(value):
    v = value.strip().upper()
    return bool(rules.NIH_NUMBER.fullmatch(v) or re.fullmatch(r"NSF\s+\d{2}-\d{3,4}", v))


def nih_guide_url(number):
    prefix = number.split("-")[0].upper()
    folder = NIH_GUIDE.get(prefix)
    return f"https://grants.nih.gov/grants/guide/{folder}/{number.upper()}.html" if folder else None


def _json_value(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("1")) if value == value.to_integral() else value)
    return value


def _display(name, value):
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, date):
        return f"{value:%a, %b} {value.day}, {value.year}"
    if name == "max_award":
        return f"${Decimal(value):,.0f}"
    return str(value)


def run(source="", upload=None, pasted="", use_ai=True, today=None):
    """source: a link or an announcement number; upload: (filename, bytes); pasted: text or HTML."""
    from ..models import Funder, Opportunity

    today = today or timezone.localdate()
    result = Result()
    fields = rules.Fields()
    doc, url, pdf_bytes = None, "", None
    source = (source or "").strip()

    # 1. Where does the announcement come from?
    if source and looks_like_number(source):
        number = re.sub(r"\s+", " ", source.upper())
        result.sources.append(f"Grants.gov lookup for {number}")
        gg, link = grantsgov.safe_lookup(number)
        if gg is None:
            result.notes.append(f"Grants.gov has no listing for {number}.")
        else:
            fields.merge(gg)
        url = link or nih_guide_url(number) or ""
        if not url and not gg:
            result.notes.append("Paste the announcement link instead to read the full text.")
    elif source:
        url = source if re.match(r"^https?://", source, re.I) else f"https://{source}"

    if url:
        try:
            fetched = fetch(url)
            url = fetched.url
            doc = from_bytes(fetched.content, fetched.content_type, urlparse(url).path)
            if fetched.content_type == "application/pdf" or fetched.content[:5] == b"%PDF-":
                pdf_bytes = fetched.content
            result.sources.append(f"Announcement page ({urlparse(url).hostname})")
        except FetchError as exc:
            result.notes.append(f"Couldn't read {url}: {exc} Try uploading the PDF or pasting the text instead.")
    if upload:
        name, data = upload
        doc = from_bytes(data, "", name)
        if name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
            pdf_bytes = data
        result.sources.append(f"Uploaded file ({name})")
    if pasted and pasted.strip():
        doc = from_html(pasted) if re.search(r"<\s*(html|body|div|p|table)\b", pasted[:3000], re.I) else Document(text=pasted.strip())
        result.sources.append("Pasted text")

    # 2. Rule-based extraction over the document text
    if doc and doc.text.strip():
        fields.merge(rules.extract(doc, url, today))
        # A number found in the page means Grants.gov may have structured data too.
        number = fields.best("number")
        if number and not any(s.startswith("Grants.gov") for s in result.sources) and rules.NIH_NUMBER.fullmatch(str(number.value)):
            gg, _ = grantsgov.safe_lookup(str(number.value))
            if gg:
                fields.merge(gg)
                result.sources.append(f"Grants.gov lookup for {number.value}")
    elif not fields.candidates:
        result.notes.append("Nothing to read. Paste a link or announcement number, upload the RFA, or paste its text.")
        return result

    # 3. Optional AI pass
    if use_ai and ai.available() and ((doc and doc.text.strip()) or pdf_bytes):
        ai_fields = ai.extract(doc.text if doc else "", pdf_bytes, url or (upload[0] if upload else ""), today)
        fields.merge(ai_fields)
        result.used_ai = bool(ai_fields.candidates)
        result.sources.append("AI (Claude)")

    if url:
        fields.add("url", url, "Link you provided", 0.9)

    # 4. Derived values
    mechanism = fields.best("mechanism")
    best_deadline = fields.best("deadline")
    if fields.candidates.pop("_nih_standard", None) and (not best_deadline or best_deadline.confidence < 0.7):
        dates = rules.nih_standard_dates(mechanism.value if mechanism else "", today)
        if dates:
            fields.add("deadline", dates[0], "NIH standard due dates (verify)", 0.55)
            fields.add("recurrence_notes", "NIH standard due dates (new): " + " / ".join(f"{d:%b} {d.day}" for d in dates), "NIH standard due dates (verify)", 0.75)
            result.notes.append("This announcement uses NIH standard due dates; the next one is filled in. Confirm it on NIH's standard due dates page.")
        else:
            result.notes.append("This announcement uses NIH standard due dates; pick the next one for your activity code.")
    loi_days = fields.candidates.pop("_loi_days", None)
    best_deadline = fields.best("deadline")
    if loi_days and best_deadline and not fields.best("loi_deadline"):
        fields.add("loi_deadline", best_deadline.value - timedelta(days=loi_days[0].value), f"Computed: {loi_days[0].value} days before the due date", 0.6)

    # 5. Funder: match your Funders list by link domain, then by name
    funder = _match_funder(Funder, url, fields)
    if funder:
        fields.add("funder", funder.pk, "Matched to your funders", 0.9)
    else:
        name = fields.best("funder_name")
        if name:
            result.new_funder = _new_funder(str(name.value), url or (fields.best("url").value if fields.best("url") else ""))

    # 6. Assemble the draft
    result.notes.extend(n for n in fields.notes if n not in result.notes)
    initial = {"status": Opportunity.Status.WATCHING}
    for name, label in FIELD_LABELS:
        best = fields.best(name)
        if not best:
            continue
        value = best.value
        if isinstance(value, str):
            value = value.strip()[: MAX_LEN.get(name, 5000)]
        initial[name] = _json_value(value)
        others = []
        for c in sorted(fields.candidates.get(name, []), key=lambda c: -c.confidence):
            if c is best or _display(name, c.value) == _display(name, best.value):
                continue
            pair = (_display(name, c.value)[:160], c.source)
            if pair not in others:
                others.append(pair)
        display = _display(name, value)
        if name == "funder" and funder:
            display = funder.name
        result.rows.append({
            "field": name, "label": label, "value": display[:600], "source": best.source,
            "level": "high" if best.confidence >= 0.8 else "medium" if best.confidence >= 0.6 else "low",
            "snippet": best.snippet, "others": others[:3],
        })

    notes = [f"Imported from {url or (upload[0] if upload else 'pasted text')} on {today:%b} {today.day}, {today.year}."]
    contact = fields.best("contact")
    if contact:
        notes.append(f"Program contact: {contact.value}")
    reqs = fields.best("requirements")
    if reqs:
        notes.append("Key requirements:\n" + "\n".join(f"- {r}" for r in reqs.value))
    initial["notes"] = "\n\n".join(notes)
    initial["extra"] = {k: v for k, v in fields.extra.items() if v}
    result.initial = initial

    number = initial.get("number")
    dup = None
    if number:
        dup = Opportunity.objects.filter(number__iexact=number).first()
    if not dup and url:
        dup = Opportunity.objects.filter(url=url).first()
    result.duplicate_id = dup.pk if dup else None
    if not result.rows:
        result.notes.append("Couldn't find any opportunity details. Try the PDF version, or paste the text.")
    return result


def _host(value):
    host = (urlparse(value if "://" in value else f"https://{value}").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


AGGREGATOR_HOSTS = ("grants.gov", "sam.gov")  # listing sites, not the funder's own
FUNDER_TYPES = (
    (r"\b(?:Foundation|Fund|Trust|Endowment|Philanthropies|Charitable)\b", "foundation"),
    (r"\b(?:Society|Association|Academy|Federation|College of)\b", "society"),
    (r"\b(?:National|Federal|Department of|Agency|Administration|Office of|Institutes? of Health|Science Foundation)\b", "federal"),
)


def _new_funder(name, url):
    """A suggested Funder record for a funder that isn't in the list yet."""
    name = re.sub(r"\s+", " ", name).strip()
    short = ""
    m = re.fullmatch(r"(.+?)\s*\(([A-Z][A-Za-z&]{1,9})\)", name)
    if m:
        name, short = m.group(1).strip(), m.group(2)
    if short in ("NIH", "NSF") or name in ("National Institutes of Health", "National Science Foundation"):
        kind = "federal"
    else:
        kind = next((k for pattern, k in FUNDER_TYPES if re.search(pattern, name)), "other")
    host = _host(url) if url else ""
    website = ""
    if host and not any(host == h or host.endswith("." + h) for h in AGGREGATOR_HOSTS):
        website = f"https://{host}"
    return {"name": name[:200], "short_name": short[:40], "funder_type": kind, "website": website}


def _match_funder(Funder, url, fields):
    funders = list(Funder.objects.all())
    host = _host(url) if url else ""
    if host:
        for f in funders:
            fh = _host(f.website) if f.website else ""
            base = ".".join(fh.split(".")[-2:]) if fh else ""
            if fh and (host == fh or host.endswith("." + fh) or (base and (host == base or host.endswith("." + base)))):
                return f
    names = [str(c.value) for c in sorted(fields.candidates.get("funder_name", []), key=lambda c: -c.confidence)]
    for name in names:
        low = name.lower()
        for f in sorted(funders, key=lambda f: -len(f.name)):
            if f.name.lower() == low or (f.short_name and f.short_name.lower() == low) or f.name.lower() in low:
                return f
            if f.short_name and re.search(rf"\({re.escape(f.short_name)}\)", name):
                return f
    return None
