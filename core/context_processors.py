"""Template context shared by both surfaces."""
from __future__ import annotations

from core import config
from core.access import capabilities_for


def surface_context(request) -> dict:
    credential = getattr(request, "credential", None)
    caps = capabilities_for(credential.role_list) if credential else set()
    return {
        "surface": getattr(request, "surface", "public"),
        "credential": credential,
        "credential_remaining_seconds": credential.remaining_seconds if credential else None,
        "bo_caps": caps,
        "REPORTING_YEAR": config.CURRENT_REPORTING_YEAR,
    }
