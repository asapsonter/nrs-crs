"""Supervision Centre: downloading a submitted filing as CRS XML."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import Client

from core import config
from core.models import IssuedCredential, OfficerProfile, Roles
from portal.models import AccountReport, Filing, ReportingFI

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR


def officer_client() -> Client:
    officer = OfficerProfile.objects.create(
        name="Returns Officer", roles=Roles.INTERNAL_ADMIN, email="ro@nrs.gov.ng"
    )
    cred, passcode = IssuedCredential.objects.issue(officer, 2.0)
    client = Client()
    client.post("/backoffice/login/", {"username": cred.username, "passcode": passcode})
    return client


@pytest.fixture
def filing():
    rfi = ReportingFI.objects.create(
        reference="NRS-RFI-2025-0001", legal_name="Zenith Trust Bank Plc",
        tin="0450088801", category="DEPOSITORY_INSTITUTION",
        enrolment_type="FINANCIAL_ENTITY", status=ReportingFI.Status.ACTIVE,
        street="12 Broad Street", city="Lagos",
        pu_surname="Bello", pu_first_name="Ngozi", pu_designation="Head",
        pu_email="pu@zenithtrust.ng", pu_phone="800",
    )
    filing = Filing.objects.create(
        reference=f"FIL-{YEAR}-70001", rfi=rfi, reporting_year=YEAR,
        kind=Filing.Kind.MANUAL, status=Filing.Status.UNDER_VALIDATION,
        receiving_country="GB", sending_company_in="0450088801",
        message_reference=f"NG{YEAR}GB-DL-1",
    )
    AccountReport.objects.create(
        filing=filing, doc_ref_id=f"NG{YEAR}-DL-000001",
        holder_name="Chukwu Emeka", residence_country="GB",
        foreign_tin="QQ123456C", holder_address="1 King Street, London",
        holder_city="London", account_number="ACC-7001",
        balance=Decimal("1000000.00"),
    )
    return filing


class TestFilingDownload:
    def test_downloads_manual_filing_as_crs_xml(self, filing):
        response = officer_client().get(f"/backoffice/returns/{filing.pk}/download/")
        assert response.status_code == 200
        assert response["Content-Type"] == "application/xml"
        assert filing.reference in response["Content-Disposition"]
        body = response.content.decode()
        assert "<crs:CRS_OECD" in body
        assert "Chukwu Emeka" in body
        assert filing.message_reference in body

    def test_queue_offers_download_link(self, filing):
        html = officer_client().get("/backoffice/returns/").content.decode()
        assert f"/backoffice/returns/{filing.pk}/download/" in html

    def test_notice_has_no_download(self, filing):
        notice = Filing.objects.create(
            reference=f"FIL-{YEAR}-70002", rfi=filing.rfi, reporting_year=YEAR,
            kind=Filing.Kind.PU_CHANGE, status=Filing.Status.UNDER_VALIDATION,
        )
        response = officer_client().get(f"/backoffice/returns/{notice.pk}/download/")
        assert response.status_code == 302
