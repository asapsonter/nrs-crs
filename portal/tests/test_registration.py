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
            "pu_middle_name": "Chidinma",
            "pu_first_name": "Ngozi",
            "pu_dob": "1985-06-15",
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

    def test_date_of_birth_is_captured(self):
        rfi = submit_enrolment()
        assert str(rfi.pu_dob) == "1985-06-15"

    def test_name_captured_as_surname_middle_other(self):
        rfi = submit_enrolment()
        assert rfi.pu_surname == "Bello"
        assert rfi.pu_middle_name == "Chidinma"
        assert rfi.pu_first_name == "Ngozi"  # other names
        assert rfi.pu_name == "Ngozi Chidinma Bello"

    def test_date_of_birth_is_required(self):
        letter = SimpleUploadedFile("ceo_letter.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
        identity = SimpleUploadedFile("pu_id.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
        response = Client().post(
            "/portal/enrol/",
            {
                "legal_name": "No DOB Bank", "tin": "0450088899", "category": "DEPOSITORY_INSTITUTION",
                "enrolment_type": "INDIVIDUAL", "email": "a@b.ng", "email_confirm": "a@b.ng",
                "phone_cc": "+234", "phone": "8010000000", "street": "1 A St", "city": "Lagos",
                "state_province": "Lagos", "post_code": "100001", "pu_surname": "Doe",
                "pu_first_name": "Jane", "pu_designation": "Self", "pu_email": "jane@b.ng",
                "pu_phone_cc": "+234", "pu_phone": "8010000001",
                "id_document": identity, "ceo_letter": letter,
            },
        )
        assert response.status_code == 200
        assert "Date of birth is required." in response.content.decode()
        assert not ReportingFI.objects.filter(tin="0450088899").exists()

    def test_assessment_step_is_retired(self):
        # The recorded-assessment route is not part of the Vizor enrolment
        # structure and is no longer routed.
        rfi = submit_enrolment()
        client = backoffice_client(Roles.ASSISTANT_ADMIN)
        response = client.post(
            f"/backoffice/registration/{rfi.pk}/review/",
            {"review_notes": "ok", "recommendation": "APPROVE"},
        )
        assert response.status_code == 404
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.SUBMITTED

    def test_approval_provisions_primary_user(self):
        rfi = submit_enrolment()
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

    def test_decline_requires_reason_and_notifies(self):
        rfi = submit_enrolment()
        supervisor = backoffice_client(
            Roles.INTERNAL_ADMIN, name="Supervisor Two", email="sup.two@nrs.gov.ng"
        )
        supervisor.post(f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "decline"})
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.SUBMITTED  # no reason given, refused
        supervisor.post(
            f"/backoffice/registration/{rfi.pk}/decide/",
            {"decision": "decline", "rejection_reason": "Sector licence could not be confirmed."},
        )
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.REJECTED
        assert rfi.get_status_display() == "Declined"
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

    def test_legacy_reject_value_still_declines(self):
        # "reject" is accepted as a legacy alias for the Vizor "decline".
        rfi = submit_enrolment()
        client = backoffice_client(Roles.INTERNAL_ADMIN)
        client.post(
            f"/backoffice/registration/{rfi.pk}/decide/",
            {"decision": "reject", "rejection_reason": "Out of CRS scope."},
        )
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.REJECTED

    def test_read_only_user_cannot_decide(self):
        rfi = submit_enrolment()
        client = backoffice_client(Roles.VIEW_ONLY)
        response = client.post(f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "approve"})
        # A signed-in officer lacking the capability sees an explanation, not a 404.
        assert response.status_code == 403
        assert "Access restricted" in response.content.decode()
        rfi.refresh_from_db()
        assert rfi.status == ReportingFI.Status.SUBMITTED
        html = client.get(f"/backoffice/registration/{rfi.pk}/").content.decode()
        assert "Awaiting a decision" in html
        assert "read-only" in html
        assert "Approve Enrolment" not in html

    def test_superadmin_views_operations_but_cannot_act(self):
        # The Super Admin views the combined console's operational pages
        # read-only; processing actions remain with credentialled officers and
        # are refused with an explanation rather than a bare 404.
        from django.contrib.auth import get_user_model

        get_user_model().objects.create_superuser(username="root", password="RootPass1!")
        client = Client()
        client.post("/backoffice/login/", {"username": "root", "passcode": "RootPass1!"})
        response = client.get("/backoffice/registration/")
        if response.status_code == 302:
            # Super admin login may use a separate field name; sign in directly.
            client.force_login(get_user_model().objects.get(username="root"))
            response = client.get("/backoffice/registration/")
        assert response.status_code == 200
        rfi = submit_enrolment()
        response = client.post(
            f"/backoffice/registration/{rfi.pk}/decide/", {"decision": "approve"}
        )
        assert response.status_code == 403
        html = response.content.decode()
        assert "Access restricted" in html
        assert "Admin Console" in html

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
