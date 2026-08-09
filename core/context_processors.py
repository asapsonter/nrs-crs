"""Template context shared by both surfaces."""
from __future__ import annotations

from core import config
from core.access import SUPERADMIN_CAPS, capabilities_for


def surface_context(request) -> dict:
    credential = getattr(request, "credential", None)
    user = getattr(request, "user", None)
    if credential:
        caps = capabilities_for(credential.role_list)
    elif user is not None and user.is_authenticated and user.is_superuser:
        caps = set(SUPERADMIN_CAPS)
    else:
        caps = set()
    # The header countdown runs to the end of the credential's validity
    # window. An unlimited credential has no clock, so no countdown is shown.
    remaining = None
    if credential and not credential.is_unlimited:
        remaining = credential.remaining_seconds
    return {
        "surface": getattr(request, "surface", "public"),
        "credential": credential,
        "credential_remaining_seconds": remaining,
        "credential_valid_until": credential.session_expires_at if credential else None,
        "bo_caps": caps,
        "REPORTING_YEAR": config.CURRENT_REPORTING_YEAR,
    }
