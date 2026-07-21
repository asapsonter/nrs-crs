"""Capability model mapping CRS internal roles to what they may do.

Access is expressed as capabilities. Each role grants a set of capabilities,
and a user's effective capabilities are the union across all their roles.
Views and templates gate on capabilities rather than on individual roles, so
a user holding several roles is handled uniformly.

Role design:
  Internal Admin User  full operational access, including final authorisation.
  Assistant Admin      processing and review, but not final authorisation.
  Export Only User     read and export of reports, packages, and inbound data.
  View Only User       read-only across the workspaces.
"""
from __future__ import annotations

from core.models import Roles

# Capability constants.
VIEW_REGISTRATION = "view_registration"
ACT_REGISTRATION = "act_registration"          # record an assessment / recommend
DECIDE_REGISTRATION = "decide_registration"    # final approve/reject, standing

VIEW_RETURNS = "view_returns"
ACT_RETURNS = "act_returns"                      # validate, return for correction
APPROVE_RETURNS = "approve_returns"              # approve for exchange, override

VIEW_EXCHANGE = "view_exchange"
OPERATE_EXCHANGE = "operate_exchange"            # build, transmit, corrections, inbound

VIEW_REPORTS = "view_reports"
VIEW_TAV = "view_tax_authority"                  # Tax Authority View
VIEW_AUDIT = "view_audit"

# Any internal role can reach the dashboard.
ANY_INTERNAL = frozenset(
    {Roles.INTERNAL_ADMIN, Roles.ASSISTANT_ADMIN, Roles.EXPORT_ONLY, Roles.VIEW_ONLY}
)

_VIEW_CAPS = {
    VIEW_REGISTRATION,
    VIEW_RETURNS,
    VIEW_EXCHANGE,
    VIEW_REPORTS,
    VIEW_TAV,
}

ROLE_CAPABILITIES: dict[str, set[str]] = {
    # Full operational access, including final authorisation and the audit log.
    Roles.INTERNAL_ADMIN: _VIEW_CAPS
    | {
        ACT_REGISTRATION,
        DECIDE_REGISTRATION,
        ACT_RETURNS,
        APPROVE_RETURNS,
        OPERATE_EXCHANGE,
        VIEW_AUDIT,
    },
    # Processing and review, plus direct accept/reject of new entity
    # enrolments (no reallocation to a separate authoriser). Returns approval
    # remains an Internal Admin authorisation.
    Roles.ASSISTANT_ADMIN: _VIEW_CAPS
    | {
        ACT_REGISTRATION,
        DECIDE_REGISTRATION,
        ACT_RETURNS,
        OPERATE_EXCHANGE,
    },
    # Read and export: reports, exchange packages and XML, inbound data.
    Roles.EXPORT_ONLY: {VIEW_REPORTS, VIEW_EXCHANGE, VIEW_TAV},
    # Read-only across the workspaces.
    Roles.VIEW_ONLY: set(_VIEW_CAPS),
}


def capabilities_for(role_codes) -> set[str]:
    """Union of capabilities granted by the given role codes."""
    caps: set[str] = set()
    for code in role_codes:
        caps |= ROLE_CAPABILITIES.get(code, set())
    return caps
