"""Repo-wide test fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _working_hours_always_open(settings):
    """Login flows must not depend on the wall-clock time the suite runs at.

    Dedicated working-hours tests re-enable enforcement and control the
    clock explicitly.
    """
    settings.ENFORCE_WORKING_HOURS = False
