"""Draft deletion and the three administrative notices.

The notice lifecycle mirrors the Vizor AEOI portal: create the notice as a
filing, complete its form from the Draft Filing screen (Validate & Save),
submit it, and the NRS applies the change on approval.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from backoffice.validation import run_validation
from core import config
from portal.models import Filing, PortalUser, ReportingFI
from portal.services import apply_notice

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR


def make_notice(rfi, kind, payload=None, **kwargs) -> Filing:
    defaults = dict(
        reference=f"FIL-{YEAR}-{Filing.objects.count() + 90001:05d}",
        rfi=rfi,
        reporting_year=YEAR,
        kind=kind,
        status=Filing.Status.DRAFT,
        notice_payload=payload or {},
    )
    defaults.update(kwargs)
    return Filing.objects.create(**defaults)


PU_PAYLOAD = {
    "new_pu_surname": "Okafor",
    "new_pu_middle_name": "",
    "new_pu_first_name": "Amina",
    "new_pu_designation": "Chief Compliance Officer",
    "new_pu_email": "amina.okafor@zenithtrust.ng",
    "new_pu_phone": "8011111111",
    "reason": "Departure of the current Primary User",
    "validated": True,
}


class TestDraftDeletion:
    def test_drafts_page_offers_delete(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.MANUAL)
        html = portal_client.get("/portal/filings/drafts/").content.decode()
        assert f"/portal/filings/{filing.pk}/delete/" in html

    def test_delete_returns_to_drafts(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.MANUAL)
        response = portal_client.post(
            f"/portal/filings/{filing.pk}/delete/", {"next": "/portal/filings/drafts/"}
        )
        assert response.status_code == 302
        assert response.url == "/portal/filings/drafts/"
        assert not Filing.objects.filter(pk=filing.pk).exists()

    def test_submitted_filing_is_not_deletable(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.MANUAL, status=Filing.Status.SUBMITTED)
        portal_client.post(f"/portal/filings/{filing.pk}/delete/", {"next": "/portal/filings/drafts/"})
        assert Filing.objects.filter(pk=filing.pk).exists()

    def test_external_next_target_is_ignored(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.MANUAL)
        response = portal_client.post(
            f"/portal/filings/{filing.pk}/delete/", {"next": "https://evil.example/"}
        )
        assert response.url.startswith("/portal/")


class TestNoticeForm:
    def test_create_filing_routes_to_form_view(self, rfi, portal_client):
        response = portal_client.post(
            "/portal/filings/create/",
            {"name": "PU change to Amina Okafor", "filing_type": "PU_CHANGE",
             "period_end_date": f"{YEAR}-08-08"},
        )
        assert response.status_code == 302
        filing = Filing.objects.latest("pk")
        assert filing.kind == Filing.Kind.PU_CHANGE
        # The filing detail redirects notices to the Form View.
        detail = portal_client.get(f"/portal/filings/{filing.pk}/")
        assert detail.status_code == 302
        assert detail.url.endswith("/view/")

    def test_form_view_shows_notice_node(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.PU_CHANGE)
        html = portal_client.get(f"/portal/filings/{filing.pk}/view/").content.decode()
        assert "Primary User Change Notice form" in html
        assert f"/portal/filings/{filing.pk}/notice/" in html
        assert "No Data" in html

    def test_validate_requires_mandatory_fields(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.PU_CHANGE)
        response = portal_client.post(
            f"/portal/filings/{filing.pk}/notice/",
            {"action": "validate", "new_pu_surname": "Okafor"},
        )
        assert response.status_code == 200
        assert b"required" in response.content
        filing.refresh_from_db()
        assert not filing.notice_validated

    def test_save_as_draft_keeps_progress(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.PU_CHANGE)
        response = portal_client.post(
            f"/portal/filings/{filing.pk}/notice/",
            {"action": "save", "new_pu_surname": "Okafor"},
        )
        assert response.status_code == 302
        filing.refresh_from_db()
        assert filing.notice_payload["new_pu_surname"] == "Okafor"
        assert not filing.notice_validated

    def test_validate_and_save_marks_ready(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.PU_CHANGE)
        data = {k: v for k, v in PU_PAYLOAD.items() if k != "validated"}
        response = portal_client.post(
            f"/portal/filings/{filing.pk}/notice/", {"action": "validate", **data}
        )
        assert response.status_code == 302
        filing.refresh_from_db()
        assert filing.notice_validated
        html = portal_client.get(f"/portal/filings/{filing.pk}/view/").content.decode()
        assert "Ready to Submit" in html

    def test_info_change_form_prefills_current_details(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.ENTITY_INFO_CHANGE)
        html = portal_client.get(f"/portal/filings/{filing.pk}/notice/").content.decode()
        assert "Zenith Trust Bank Plc" in html

    def test_unvalidated_notice_cannot_be_submitted(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.PU_CHANGE)
        portal_client.post(f"/portal/filings/{filing.pk}/stage/")
        filing.refresh_from_db()
        assert filing.status == Filing.Status.DRAFT

    def test_validated_notice_submits_cleanly(self, rfi, portal_client):
        filing = make_notice(rfi, Filing.Kind.PU_CHANGE, payload=PU_PAYLOAD)
        response = portal_client.post(f"/portal/filings/{filing.pk}/stage/")
        assert response.status_code == 302
        filing.refresh_from_db()
        assert filing.status == Filing.Status.UNDER_VALIDATION
        assert not filing.findings.exists()


class TestNoticeValidationEngine:
    def test_missing_fields_are_errors(self, rfi):
        filing = make_notice(
            rfi, Filing.Kind.PU_CHANGE, payload={"new_pu_surname": "Okafor"},
            status=Filing.Status.SUBMITTED,
        )
        file_count, record_count = run_validation(filing)
        assert record_count == 0
        assert file_count == 4  # other names, position, email, phone
        assert all(f.code == "N-001" for f in filing.findings.all())

    def test_complete_notice_has_no_findings(self, rfi):
        filing = make_notice(
            rfi, Filing.Kind.PU_CHANGE, payload=PU_PAYLOAD, status=Filing.Status.SUBMITTED
        )
        assert run_validation(filing) == (0, 0)


class TestApplyNotice:
    def test_pu_change_swaps_the_primary_user(self, rfi, portal_client):
        filing = make_notice(
            rfi, Filing.Kind.PU_CHANGE, payload=PU_PAYLOAD, status=Filing.Status.ACCEPTED
        )
        detail = apply_notice(filing)
        rfi.refresh_from_db()
        assert rfi.pu_surname == "Okafor"
        assert rfi.pu_email == "amina.okafor@zenithtrust.ng"
        old = PortalUser.objects.get(user__username="pu@zenithtrust.ng")
        assert old.status == PortalUser.Status.DISABLED
        assert not old.is_primary_user
        assert not get_user_model().objects.get(username="pu@zenithtrust.ng").is_active
        new = PortalUser.objects.get(user__username="amina.okafor@zenithtrust.ng")
        assert new.is_primary_user
        assert new.role == PortalUser.Role.CHECKER
        assert "Amina Okafor" in detail

    def test_deactivation_disables_entity_and_users(self, rfi, portal_client):
        filing = make_notice(
            rfi,
            Filing.Kind.ENTITY_DEACTIVATION,
            payload={"effective_date": f"{YEAR}-09-01", "reason": "Entity wound up", "validated": True},
            status=Filing.Status.ACCEPTED,
        )
        apply_notice(filing)
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.DEACTIVATED
        assert not rfi.portal_users.exclude(status=PortalUser.Status.DISABLED).exists()
        assert not get_user_model().objects.get(username="pu@zenithtrust.ng").is_active

    def test_info_change_applies_proposed_details(self, rfi):
        filing = make_notice(
            rfi,
            Filing.Kind.ENTITY_INFO_CHANGE,
            payload={
                "legal_name": "Zenith Trust Bank Limited", "street": "1 Marina Road",
                "city": "Lagos", "state_province": "Lagos", "post_code": "101001",
                "email": "reporting@zenithtrust.ng", "phone": "8022222222", "validated": True,
            },
            status=Filing.Status.ACCEPTED,
        )
        apply_notice(filing)
        rfi.refresh_from_db()
        assert rfi.legal_name == "Zenith Trust Bank Limited"
        assert rfi.street == "1 Marina Road"
        assert rfi.email == "reporting@zenithtrust.ng"
