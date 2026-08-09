"""Fixtures shared across the portal test modules."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from core import config
from exchange.models import PartnerJurisdiction
from portal.models import PortalUser, ReportingFI


@pytest.fixture
def rfi() -> ReportingFI:
    return ReportingFI.objects.create(
        reference="NRS-RFI-2025-0001",
        legal_name="Zenith Trust Bank Plc",
        tin="0450088801",
        category="DEPOSITORY_INSTITUTION",
        enrolment_type="FINANCIAL_ENTITY",
        status=ReportingFI.Status.ACTIVE,
        pu_surname="Bello",
        pu_first_name="Ngozi",
        pu_designation="Head, Regulatory Reporting",
        pu_email="pu@zenithtrust.ng",
        pu_phone="8000000000",
    )


@pytest.fixture
def partners() -> None:
    for code, name, since, fingerprint in config.PARTNER_JURISDICTIONS:
        PartnerJurisdiction.objects.create(
            code=code, name=name, activated_since=since, key_fingerprint=fingerprint
        )


@pytest.fixture
def portal_client(rfi) -> Client:
    """A signed-in Primary User for the `rfi` institution."""
    email = "pu@zenithtrust.ng"
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
    response = client.post("/portal/login/", {"email": email, "password": "Secret123!"})
    assert response.status_code == 302
    return client
