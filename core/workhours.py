"""Supervision Centre working hours.

Credential-bound backoffice access runs during the working day only
(08:00-18:00 Africa/Lagos by default). The server clock is the source of
truth; the header countdown is presentation. Enforcement can be switched
off via settings.ENFORCE_WORKING_HOURS (the test suite does this so login
flows are time-of-day independent).
"""
from __future__ import annotations

import datetime

from django.conf import settings
from django.utils import timezone

from core import config


def _enforced() -> bool:
    return getattr(settings, "ENFORCE_WORKING_HOURS", True)


def within_working_hours(moment: datetime.datetime | None = None) -> bool:
    """Whether the given (or current) moment falls inside the working day."""
    if not _enforced():
        return True
    local = timezone.localtime(moment or timezone.now())
    return config.WORK_DAY_START_HOUR <= local.hour < config.WORK_DAY_END_HOUR


def seconds_to_day_close(moment: datetime.datetime | None = None) -> int | None:
    """Seconds until today's 18:00 close, or None when not enforced.

    Returns 0 when the moment is already outside the working day.
    """
    if not _enforced():
        return None
    local = timezone.localtime(moment or timezone.now())
    close = local.replace(hour=config.WORK_DAY_END_HOUR, minute=0, second=0, microsecond=0)
    if not within_working_hours(moment):
        return 0
    return max(0, int((close - local).total_seconds()))


def working_hours_label() -> str:
    """The working window as displayed to users, e.g. \"08:00-18:00\"."""
    return f"{config.WORK_DAY_START_HOUR:02d}:00–{config.WORK_DAY_END_HOUR:02d}:00"
