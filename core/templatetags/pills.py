"""Status pill rendering shared by both surfaces."""
from __future__ import annotations

from django import template
from django.utils.html import format_html

register = template.Library()

# Maps every workflow status, validation severity, and status-message outcome
# to a semantic pill colour class.
PILL_CLASSES: dict[str, str] = {
    # Inactive (gray)
    "DRAFT": "draft",
    "CONSUMED": "neutral",
    "EXPIRED": "draft",
    "ARCHIVED": "neutral",
    "DEACTIVATED": "neutral",
    "DISABLED": "neutral",
    "NIL": "neutral",
    # In transit (blue)
    "SUBMITTED": "submitted",
    "TRANSMITTING": "submitted",
    "TRANSMITTED": "submitted",
    "RECEIVED": "submitted",
    # Awaiting action (amber)
    "UNDER_REVIEW": "review",
    "UNDER_VALIDATION": "review",
    "PENDING_CHECKER": "review",
    "PENDING": "review",
    "ISSUED": "review",
    "BUILT": "review",
    "WARNING": "warning",
    # Positive (green)
    "APPROVED": "approved",
    "ACCEPTED": "approved",
    "ACTIVE": "approved",
    "IN_EXCHANGE": "approved",
    "DISSEMINATED": "approved",
    # Needs correction (orange)
    "RETURNED": "correction",
    "RECORD_ERRORS": "correction",
    "RECORD_ERROR": "correction",
    "SUSPENDED": "correction",
    # Rejected or error (red)
    "REJECTED": "rejected",
    "FILE_ERROR": "rejected",
    "REVOKED": "rejected",
    "LOCKED": "rejected",
    "ERROR": "error",
}


@register.simple_tag
def pill(status: str, label: str = "") -> str:
    """Render a status pill: {% pill obj.status obj.get_status_display %}."""
    css = PILL_CLASSES.get(status, "neutral")
    return format_html('<span class="pill {}">{}</span>', css, label or status.replace("_", " ").title())
