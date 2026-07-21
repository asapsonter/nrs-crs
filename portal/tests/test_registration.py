"""Registration state machine and four-eyes tests."""
from __future__ import annotations

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from core.models import IssuedCredential, OfficerProfile, Roles
from portal.models import PortalUser, ReportingFI

pytestmark = pytest.mark.django_db


def backoffice_client(roles: str, name: str = "Officer One", email: str = "officer.one@nrs.gov.ng") -> Client:
    officer = OfficerProfile.objects.create(name=name, roles=roles, email=email)
    credential, passcode = IssuedCredential.objects.issue(officer, 2.0)
    client = Client()
    client.post("/backoffice/login/", {"username": credential.username, "passcode": passcode})
    return client


def submit_enrolment(client: Client | None = None) -> ReportingFI:
    client = client or Client()
    letter = SimpleUploadedFile("ceo_letter.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
    identity = SimpleUploadedFile("pu_id.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
    response = client.post(
        "/portal/enrol/",
        {
            "legal_name": "Zenith Trust Bank Plc",
            "tin": "0450088801",
            "category": "DEPOSITORY_INSTITUTION",
            "enrolment_type": "FINANCIAL_ENTITY",
            "email": "info@zenithtrust.ng",
            "email_confirm": "info@zenithtrust.ng",
            "phone_cc": "+234",
            "phone": "12017000900",
            "street": "1 Ajose Adeogun Street",
            "city": "Victoria Island",
            "state_province": "Lagos",
            "post_code": "101241",
            "pu_surname": "Bello",
            "pu_first_name": "Ngozi",
            "pu_designation": "Head, Regulatory Reporting",
            "pu_email": "ngozi.bello@zenithtrust.ng",
            "pu_phone_cc": "+234",
            "pu_phone": "8012345678",
            "id_document": identity,
            "ceo_letter": letter,
        },
    )
    assert response.status_code == 200
    return ReportingFI.objects.get(tin="0450088801")


class TestEnrolmentStateMachine:
    def test_submission_enters_submitted_with_reference(self):
        rfi = submit_enrolment()
        assert rfi.status == ReportingFI.Status.SUBMITTED
        assert rfi.reference.startswith("NRS-RFI-")

    def test_review_moves_to_under_review(self):
        rfi = submit_enrolment()
        client = backoffice_client(Roles.ASSISTANT_ADMIN)
        client.post(
            f"/backoffice/registration/{rfi.pk}/review/",
            {
                "check_tin": "on",
                "check_ceo_letter": "on",
                "check_licence": "on",
                "check_classification": "on",
                "review_notes": "All checks satisfactory.",
                "recommendation": "APPROVE",
            },
        )
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.UNDER_REVIEW
        assert rfi.recommendation == "APPROVE"
        assert rfi.reviewer is not None

    def test_supervisor_approval_provisions_primary_user(self):
        rfi = submit_enrolment()
        reviewer = backoffice_client(Roles.ASSISTANT_ADMIN)
        reviewer.post(
            f"/backoffice/registration/{rfi.pk}/review/",
            {"check_tin": "on", "review_notes": "ok", "recommendation": "APPROVE"},
        )
        supervisor = backoffice_client(
            Roles.INTERNAL_ADMIN, name="Supervisor Two", email="sup.two@nrs.gov.ng"
        )
        supervisor.post(f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "approve"})
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.APPROVED
        pu = rfi.portal_users.get(is_primary_user=True)
        assert pu.role == PortalUser.Role.CHECKER
        assert pu.must_change_password
        assert any(rfi.pu_email in message.to for message in mail.outbox)

    def test_rejection_requires_reason_and_notifies(self):
        rfi = submit_enrolment()
        reviewer = backoffice_client(Roles.ASSISTANT_ADMIN)
        reviewer.post(
            f"/backoffice/registration/{rfi.pk}/review/",
            {"review_notes": "Licence not verifiable.", "recommendation": "REJECT"},
        )
        supervisor = backoffice_client(
            Roles.INTERNAL_ADMIN, name="Supervisor Two", email="sup.two@nrs.gov.ng"
        )
        supervisor.post(f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "reject"})
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.UNDER_REVIEW  # no reason given, refused
        supervisor.post(
            f"/backoffice/registration/{rfi.pk}/decide/",
            {"decision": "reject", "rejection_reason": "Sector licence could not be confirmed."},
        )
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.REJECTED
        assert any(rfi.pu_email in message.to for message in mail.outbox)

    def test_assistant_admin_decides_submitted_directly(self):
        # No reallocation: an Assistant Admin accepts a fresh submission with
        # no separate review or handoff.
        rfi = submit_enrolment()
        client = backoffice_client(Roles.ASSISTANT_ADMIN)
        client.post(f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "approve"})
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.APPROVED
        assert rfi.portal_users.filter(is_primary_user=True).exists()

    def test_assistant_admin_rejects_directly(self):
        rfi = submit_enrolment()
        client = backoffice_client(Roles.ASSISTANT_ADMIN)
        client.post(
            f"/backoffice/registration/{rfi.pk}/decide/",
            {"decision": "reject", "rejection_reason": "Not within CRS scope."},
        )
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.REJECTED

    def test_admin_can_decide_its_own_reviewed_application(self):
        # A single admin may record an assessment and then decide it; the old
        # four-eyes handoff no longer blocks the same person.
        rfi = submit_enrolment()
        client = backoffice_client(Roles.INTERNAL_ADMIN)
        client.post(
            f"/backoffice/registration/{rfi.pk}/review/",
            {"review_notes": "ok", "recommendation": "APPROVE"},
        )
        client.post(f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "approve"})
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.APPROVED

    def test_read_only_user_cannot_decide(self):
        rfi = submit_enrolment()
        client = backoffice_client(Roles.VIEW_ONLY)
        response = client.post(f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "approve"})
        assert response.status_code == 404
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.SUBMITTED
        html = client.get(f"/backoffice/registration/{rfi.pk}/").content.decode()
        assert "Awaiting a decision" in html
        assert "read-only" in html
        assert "Approve enrolment" not in html

    def test_suspension_from_active(self):
        rfi = submit_enrolment()
        rfi.status = ReportingFI.Status.ACTIVE
        rfi.save()
        supervisor = backoffice_client(
            Roles.INTERNAL_ADMIN, name="Supervisor Two", email="sup.two@nrs.gov.ng"
        )
        supervisor.post(f"/backoffice/registration/{rfi.pk}/standing/", {"action": "suspend"})
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.SUSPENDED
