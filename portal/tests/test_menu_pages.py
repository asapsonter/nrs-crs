"""Smoke test: each signed-in portal menu page renders without error."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from portal.models import PortalUser, ReportingFI

pytestmark = pytest.mark.django_db

MENU_URLS = [
    "/portal/",
    "/portal/filings/drafts/",
    "/portal/submit/",
    "/portal/filings/",
    "/portal/documents/",
    "/portal/profile/",
    "/portal/me/",
    "/portal/help/",
]


@pytest.fixture
def portal_client() -> Client:
    rfi = ReportingFI.objects.create(
        reference="NRS-RFI-2025-0099",
        legal_name="Meridian Trust Bank Plc",
        tin="0450099901",
        category="DEPOSITORY_INSTITUTION",
        enrolment_type="FINANCIAL_ENTITY",
        status=ReportingFI.Status.ACTIVE,
        pu_surname="Bello",
        pu_first_name="Ngozi",
        pu_designation="Head, Regulatory Reporting",
        pu_email="pu@meridian.ng",
        pu_phone="8000000000",
    )
    email = "pu@meridian.ng"
    user = get_user_model().objects.create_user(username=email, password="Secret123!")
    PortalUser.objects.create(
        user=user,
        rfi=rfi,
        display_name="Ngozi Bello",
        designation="Head, Regulatory Reporting",
        role=PortalUser.Role.CHECKER,
        status=PortalUser.Status.ACTIVE,
        is_primary_user=True,
        must_change_password=False,
    )
    client = Client()
    resp = client.post("/portal/login/", {"email": email, "password": "Secret123!"})
    assert resp.status_code == 302, "login did not succeed"
    return client


@pytest.mark.parametrize("url", MENU_URLS)
def test_menu_page_renders(portal_client: Client, url: str) -> None:
    response = portal_client.get(url)
    assert response.status_code == 200, f"{url} returned {response.status_code}"


def test_shell_chrome_present(portal_client: Client) -> None:
    """The signed-in shell renders the grouped menu and topbar avatar chip."""
    html = portal_client.get("/portal/").content.decode()
    for marker in ["pt-menu-group", "pt-avatar", "pt-topbar-eyebrow", "Workspace"]:
        assert marker in html, f"missing shell marker {marker}"


def test_filing_detail_meta_strip(portal_client: Client) -> None:
    """A filing detail page renders the meta strip of key facts."""
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-90001",
        rfi=rfi,
        reporting_year=2024,
        kind=Filing.Kind.MANUAL,
        status=Filing.Status.DRAFT,
    )
    html = portal_client.get(f"/portal/filings/{filing.pk}/").content.decode()
    assert "filing-meta" in html
    assert "Reporting year" in html
    assert "Prepared by" in html
