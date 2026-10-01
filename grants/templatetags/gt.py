from datetime import date
from decimal import Decimal

import markdown as md_lib
import nh3
from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ..icons import ICONS

register = template.Library()


@register.simple_tag
def icon(name, size=16, cls=""):
    inner = ICONS.get(name, ICONS["circle"])
    return mark_safe(
        f'<svg class="icon {cls}" width="{int(size)}" height="{int(size)}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true" focusable="false">{inner}</svg>'
    )


@register.filter
def money(value):
    if value in (None, ""):
        return "—"
    try:
        return f"${Decimal(value):,.0f}"
    except Exception:  # noqa: BLE001
        return value


@register.filter
def money_short(value):
    if value in (None, ""):
        return "—"
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        s = f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}${s}M"
    if v >= 1_000:
        return f"{sign}${v / 1_000:.0f}K"
    return f"{sign}${v:.0f}"


@register.filter
def pm(value):
    """Person-months, trimmed: 1.20 -> 1.2"""
    if value in (None, ""):
        return "—"
    return f"{Decimal(value):.2f}".rstrip("0").rstrip(".")


@register.filter
def num(value, places=0):
    if value in (None, ""):
        return "—"
    return f"{Decimal(value):,.{int(places)}f}"


@register.filter
def days_until(value):
    if not value:
        return ""
    if not isinstance(value, date):
        return ""
    delta = (value - timezone.localdate()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if delta > 0:
        if delta < 60:
            return f"in {delta} days"
        return f"in {round(delta / 30.4)} months"
    if -delta < 60:
        return f"{-delta} days ago"
    return f"{round(-delta / 30.4)} months ago"


@register.filter
def due_class(value, done=False):
    if not value or done:
        return ""
    delta = (value - timezone.localdate()).days
    if delta < 0:
        return "due-overdue"
    if delta <= 7:
        return "due-soon"
    if delta <= 30:
        return "due-month"
    return "due-later"


@register.simple_tag
def status_badge(status, label=None, prefix="s"):
    if hasattr(status, "status"):
        label = status.get_status_display()
        status = status.status
    return format_html('<span class="badge {}-{}">{}</span>', prefix, status, label or status)


@register.simple_tag
def tag_chip(tag):
    return format_html('<span class="chip chip-{}">{}</span>', tag.color, tag.name)


ALLOWED_TAGS = {
    "p", "br", "strong", "em", "b", "i", "ul", "ol", "li", "a", "code", "pre", "blockquote",
    "h3", "h4", "h5", "hr", "table", "thead", "tbody", "tr", "th", "td", "del",
}


@register.filter
def markdown(text):
    if not text:
        return ""
    html = md_lib.markdown(text, extensions=["sane_lists", "tables", "nl2br"])
    return mark_safe(nh3.clean(html, tags=ALLOWED_TAGS, link_rel="noopener noreferrer"))


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def percent_of(value, total):
    try:
        if not total:
            return 0
        return max(0, min(100, round(float(value) * 100 / float(total))))
    except (TypeError, ValueError):
        return 0


@register.filter
def initials(user):
    return getattr(user, "initials", "?")


@register.filter
def snippet(text):
    """Escape a search headline, then turn the \\x02/\\x03 markers into <mark> tags."""
    if not text:
        return ""
    from django.utils.html import escape

    return mark_safe(escape(text).replace("\x02", "<mark>").replace("\x03", "</mark>"))
