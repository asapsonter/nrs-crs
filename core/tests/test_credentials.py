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


def issue(officer: OfficerProfile, hours: float = 2.0) -> tuple[IssuedCredential, str]:
    return IssuedCredential.objects.issue(officer, hours)


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
        credential, passcode = issue(officer, hours=2.0)
        client = Client()
        response = login(client, credential, passcode)
        assert response.status_code == 302
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.ACTIVE
        assert credential.first_used_at is not None
        expected = credential.first_used_at + timezone.timedelta(hours=2)
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

    def test_logout_consumes_credential_and_blocks_reuse(self, officer):
        credential, passcode = issue(officer)
        client = Client()
        login(client, credential, passcode)
        client.get("/backoffice/logout/")
        credential.refresh_from_db()
        assert credential.status == IssuedCredential.Status.CONSUMED
        response = login(Client(), credential, passcode)
        assert b"has been consumed" in response.content
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
        assert b"Your access window has ended" in response.content
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
