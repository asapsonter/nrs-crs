"""Credential lifecycle tests: single use, expiry, revocation, reuse refusal."""
from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from core.models import AuditLog, IssuedCredential, OfficerProfile, Roles

pytestmark = pytest.mark.django_db


@pytest.fixture
def officer() -> OfficerProfile:
    return OfficerProfile.objects.create(
        name="Adaeze Okonkwo", roles=Roles.INTERNAL_ADMIN, email="adaeze.okonkwo@nrs.gov.ng"
    )


def issue(officer: OfficerProfile, months: int = 2) -> tuple[IssuedCredential, str]:
    return IssuedCredential.objects.issue(officer, months)


def login(client: Client, credential: IssuedCredential, passcode: str):
    return client.post(
        "/backoffice/login/", {"username": credential.username, "passcode": passcode}
    )


class TestIssuance:
    def test_issue_creates_issued_credential_with_hashed_passcode(self, officer):
        credential, passcode = issue(officer)
        assert credential.status == IssuedCredential.Status.ISSUED
        assert passcode not in credential.passcode_hash
        assert credential.check_passcode(passcode)
        assert credential.username.startswith("nrs-usr-")
        assert credential.role_list == [Roles.INTERNAL_ADMIN]

    def test_unused_credential_lapses_after_validity_window(self, officer):
        credential, passcode = issue(officer)
        credential.expires_if_unused_at = timezone.now() - timezone.timedelta(minutes=1)
        credential.save()
        client = Client()
        response = login(client, credential, passcode)
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.EXPIRED
        assert b"lapsed unused" in response.content


class TestSingleUse:
    def test_login_over_existing_superadmin_session_succeeds(self, officer):
        """Issuing then using a credential in the same browser must not crash.

        login() flushes the Super Admin session, leaving no session key; the
        credential must still bind cleanly rather than raising an integrity
        error on the bound session key.
        """
        from django.contrib.auth.models import User

        User.objects.create_superuser("superadmin", "sa@nrs.gov.ng", "AdminPass#1")
        credential, passcode = issue(officer)
        client = Client()
        client.post("/backoffice/login/", {"username": "superadmin", "passcode": "AdminPass#1"})
        response = login(client, credential, passcode)
        assert response.status_code == 302
        assert response.url == "/backoffice/"
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.ACTIVE
        assert credential.bound_session_key

    def test_first_login_activates_and_fixes_window(self, officer):
        from core.models import add_months

        credential, passcode = issue(officer, months=2)
        client = Client()
        response = login(client, credential, passcode)
        assert response.status_code == 302
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.ACTIVE
        assert credential.first_used_at is not None
        expected = add_months(credential.first_used_at, 2)
        assert abs((credential.session_expires_at - expected).total_seconds()) < 2
        assert credential.bound_session_key

    def test_second_login_from_other_session_is_refused_and_flagged(self, officer):
        credential, passcode = issue(officer)
        first = Client()
        login(first, credential, passcode)
        second = Client()
        response = login(second, credential, passcode)
        assert response.status_code == 200
        assert b"bound to a live session" in response.content
        assert AuditLog.objects.filter(
            action="CREDENTIAL_REUSE_REFUSED", flagged=True, credential=credential
        ).exists()
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.ACTIVE

    def test_logout_unbinds_and_allows_next_sign_in(self, officer):
        credential, passcode = issue(officer)
        client = Client()
        login(client, credential, passcode)
        client.get("/backoffice/logout/")
        credential.refresh_from_db()
        # Signing out ends the session but the credential stays valid.
        assert credential.status == IssuedCredential.Status.ACTIVE
        assert credential.bound_session_key == ""
        assert AuditLog.objects.filter(action="SESSION_ENDED", credential=credential).exists()
        # The officer signs back in with the same credential.
        again = Client()
        response = login(again, credential, passcode)
        assert response.status_code == 302
        credential.refresh_from_db()
        assert credential.bound_session_key
        assert AuditLog.objects.filter(action="SESSION_STARTED", credential=credential).exists()
        # The validity window was fixed at first use and did not move.
        assert AuditLog.objects.filter(action="CREDENTIAL_FIRST_USE", credential=credential).count() == 1

    def test_relogin_past_validity_is_refused_and_consumed(self, officer):
        credential, passcode = issue(officer)
        client = Client()
        login(client, credential, passcode)
        client.get("/backoffice/logout/")
        IssuedCredential.objects.filter(pk=credential.pk).update(
            session_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )
        response = login(Client(), credential, passcode)
        assert response.status_code == 200
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.CONSUMED


class TestExpiry:
    def test_session_past_window_is_ended_and_credential_consumed(self, officer):
        credential, passcode = issue(officer)
        client = Client()
        login(client, credential, passcode)
        IssuedCredential.objects.filter(pk=credential.pk).update(
            session_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )
        response = client.get("/backoffice/")
        assert response.status_code == 403
        assert b"validity window has ended" in response.content
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.CONSUMED
        # The session itself is dead: the next request goes to sign in.
        response = client.get("/backoffice/")
        assert response.status_code == 302
        assert response.url == "/backoffice/login/"


class TestRevocation:
    def test_revoked_active_credential_ends_session_on_next_request(self, officer):
        credential, passcode = issue(officer)
        client = Client()
        login(client, credential, passcode)
        credential.refresh_from_db()
        credential.revoke("Super Admin")
        response = client.get("/backoffice/")
        assert response.status_code == 403
        assert b"revoked" in response.content

    def test_revoked_issued_credential_cannot_log_in(self, officer):
        credential, passcode = issue(officer)
        credential.revoke("Super Admin")
        response = login(Client(), credential, passcode)
        assert b"revoked" in response.content


class TestWorkingHours:
    """Credential access runs 08:00-18:00 (Africa/Lagos) when enforced."""

    @staticmethod
    def _at_hour(hour: int):
        """A tz-aware moment today at the given local hour."""
        return timezone.localtime().replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_within_working_hours_boundaries(self, settings):
        from core import workhours

        settings.ENFORCE_WORKING_HOURS = True
        assert workhours.within_working_hours(self._at_hour(8)) is True
        assert workhours.within_working_hours(self._at_hour(17)) is True
        assert workhours.within_working_hours(self._at_hour(7)) is False
        assert workhours.within_working_hours(self._at_hour(18)) is False
        assert workhours.within_working_hours(self._at_hour(22)) is False

    def test_enforcement_off_is_always_open(self, settings):
        from core import workhours

        settings.ENFORCE_WORKING_HOURS = False
        assert workhours.within_working_hours(self._at_hour(3)) is True
        assert workhours.seconds_to_day_close() is None

    def test_login_refused_outside_working_hours(self, officer, settings, monkeypatch):
        from core import workhours

        settings.ENFORCE_WORKING_HOURS = True
        credential, passcode = issue(officer)
        monkeypatch.setattr(
            "backoffice.views.workhours.within_working_hours", lambda moment=None: False
        )
        response = login(Client(), credential, passcode)
        assert response.status_code == 200
        assert b"working hours" in response.content
        credential.refresh_from_db()
        # The credential is untouched: still awaiting its first working-day use.
        assert credential.status == IssuedCredential.Status.ISSUED

    def test_request_after_day_close_unbinds_but_keeps_credential(
        self, officer, settings, monkeypatch
    ):
        settings.ENFORCE_WORKING_HOURS = True
        import core.middleware as mw

        credential, passcode = issue(officer)
        client = Client()
        # Sign in during the working day…
        monkeypatch.setattr(mw.workhours, "within_working_hours", lambda moment=None: True)
        monkeypatch.setattr(
            "backoffice.views.workhours.within_working_hours", lambda moment=None: True
        )
        login(client, credential, passcode)
        credential.refresh_from_db()
        assert credential.bound_session_key
        # …then the clock passes 18:00.
        monkeypatch.setattr(mw.workhours, "within_working_hours", lambda moment=None: False)
        response = client.get("/backoffice/")
        assert response.status_code == 403
        assert b"working day has closed" in response.content
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.ACTIVE
        assert credential.bound_session_key == ""

    def test_superadmin_not_confined_to_working_hours(self, settings, monkeypatch):
        from django.contrib.auth.models import User

        settings.ENFORCE_WORKING_HOURS = True
        User.objects.create_superuser("superadmin", "sa@nrs.gov.ng", "AdminPass#1")
        monkeypatch.setattr(
            "backoffice.views.workhours.within_working_hours", lambda moment=None: False
        )
        client = Client()
        response = client.post(
            "/backoffice/login/", {"username": "superadmin", "passcode": "AdminPass#1"}
        )
        assert response.status_code == 302


class TestUnlimitedValidity:
    """session_months=0: no expiry and no working-hours confinement."""

    def test_unlimited_never_gets_an_expiry(self, officer):
        credential, passcode = issue(officer, months=0)
        client = Client()
        response = login(client, credential, passcode)
        assert response.status_code == 302
        credential.refresh_from_db()
        assert credential.is_unlimited
        assert credential.status == IssuedCredential.Status.ACTIVE
        assert credential.session_expires_at is None
        assert credential.validity_display == "Unlimited"

    def test_unlimited_login_allowed_outside_working_hours(self, officer, settings, monkeypatch):
        settings.ENFORCE_WORKING_HOURS = True
        monkeypatch.setattr(
            "backoffice.views.workhours.within_working_hours", lambda moment=None: False
        )
        credential, passcode = issue(officer, months=0)
        response = login(Client(), credential, passcode)
        assert response.status_code == 302
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.ACTIVE

    def test_unlimited_session_survives_day_close(self, officer, settings, monkeypatch):
        settings.ENFORCE_WORKING_HOURS = True
        import core.middleware as mw

        monkeypatch.setattr(mw.workhours, "within_working_hours", lambda moment=None: False)
        monkeypatch.setattr(
            "backoffice.views.workhours.within_working_hours", lambda moment=None: False
        )
        credential, passcode = issue(officer, months=0)
        client = Client()
        login(client, credential, passcode)
        response = client.get("/backoffice/")
        assert response.status_code == 200
        credential.refresh_from_db()
        assert credential.bound_session_key

    def test_unlimited_shows_no_countdown(self, officer):
        credential, passcode = issue(officer, months=0)
        client = Client()
        login(client, credential, passcode)
        html = client.get("/backoffice/").content.decode()
        # The countdown element (with its data-remaining attribute) is not
        # rendered; only the inert script that would drive it remains.
        assert "data-remaining" not in html

    def test_unlimited_can_still_be_revoked(self, officer):
        credential, passcode = issue(officer, months=0)
        client = Client()
        login(client, credential, passcode)
        credential.refresh_from_db()
        credential.revoke("Super Admin")
        response = client.get("/backoffice/")
        assert response.status_code == 403
        assert b"revoked" in response.content


class TestSurfaceSeparation:
    def test_backoffice_session_cookie_is_surface_specific(self, officer):
        credential, passcode = issue(officer)
        client = Client()
        response = login(client, credential, passcode)
        assert "nrs_backoffice_sessionid" in response.cookies
        assert response.cookies["nrs_backoffice_sessionid"]["path"] == "/backoffice"

    def test_portal_urls_hidden_from_backoffice_session(self, officer):
        credential, passcode = issue(officer)
        client = Client()
        login(client, credential, passcode)
        # The backoffice session presents as anonymous on the portal surface.
        response = client.get("/portal/filings/")
        assert response.status_code in (302, 404)
