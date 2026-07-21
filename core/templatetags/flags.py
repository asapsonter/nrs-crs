"""Country flag rendering shared by both surfaces.

Renders an ISO 3166-1 alpha-2 code as its flag using Unicode regional
indicator symbols, so no image assets are needed. Where a browser does not
render flag emoji, the two-letter code still shows, so nothing is lost.
"""
from __future__ import annotations

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

_A = ord("A")
_REGIONAL_INDICATOR_BASE = 0x1F1E6


def _emoji(code: str) -> str:
    return "".join(chr(_REGIONAL_INDICATOR_BASE + ord(ch) - _A) for ch in code)


@register.filter
def flag(code: str):
    """Return just the flag glyph for a country code: {{ obj.code|flag }}."""
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return mark_safe(f'<span class="flag" title="{code}">{_emoji(code)}</span>')


@register.filter
def flag_glyph(code: str) -> str:
    """Return the bare flag emoji for a country code, no markup.

    Use inside <option> labels and other places that render text only:
    {{ obj.code|flag_glyph }}.
    """
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return _emoji(code)


@register.simple_tag
def country(code: str, label: str = ""):
    """Render a flag followed by a label: {% country obj.code obj.name %}.

    Falls back to the code as the label when no name is given.
    """
    code = (code or "").strip().upper()
    text = label or code
    if len(code) == 2 and code.isalpha():
        return format_html('<span class="country">{}{}</span>', flag(code), text)
    return format_html('<span class="country">{}</span>', text)
