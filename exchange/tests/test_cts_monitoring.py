"""CTS monitoring: certificate inventory, expiry alerts, exception report.

Covers OR-CTS-006 + AC-CTS-006 (certificate inventory / expiry alerts),
FR-CTS-010 (alerts), RR-CTS-001/003 (dashboard + exception report).
"""
from __future__ import annotations

import datetime

import pytest
from django.test import Client
from django.utils import timezone

from core import config
from core.models import IssuedCredential, OfficerProfile, Roles
from exchange.models import ExchangePackage, PartnerJurisdiction, TransmissionCertificate
from exchange.services import cts_exceptions, expiring_certificates

pytestmark = pytest.mark.django_db


def view_client():
    officer = OfficerProfile.objects.create(name="Monitor", roles=Roles.INTERNAL_ADMIN, email="mon@nrs.gov.ng")
    cred, pc = IssuedCredential.objects.issue(officer, 2.0)
    c = Client()
    c.post("/backoffice/login/", {"username": cred.username, "passcode": pc})
    return c


@pytest.fixture
def partner():
    return PartnerJurisdiction.objects.create(
        code="GB", name="United Kingdom", activated_since="2019-04-01", key_fingerprint="AA:BB"
    )


def _cert(partner, days_to_expiry, revoked=False):
    today = timezone.localdate()
    return TransmissionCertificate.objects.create(
        owner=TransmissionCertificate.Owner.PARTNER, jurisdiction=partner,
        subject=f"{partner.name} cert", fingerprint="AA:BB",
        valid_from=today - datetime.timedelta(days=365),
        valid_to=today + datetime.timedelta(days=days_to_expiry), revoked=revoked,
    )


def test_certificate_status_bands(partner):
    valid = _cert(partner, 200)
    expiring = _cert(partner, 10)
    expired = _cert(partner, -3)
    assert valid.status_label == "Valid" and not valid.is_expiring and not valid.is_expired
    assert expiring.status_label == "Expiring" and expiring.is_expiring
    assert expired.status_label == "Expired" and expired.is_expired


def test_expiring_window_uses_config_threshold(partner):
    _cert(partner, config.CERTIFICATE_EXPIRY_WARNING_DAYS - 1)  # inside window
    _cert(partner, config.CERTIFICATE_EXPIRY_WARNING_DAYS + 50)  # outside window
    _cert(partner, -2)  # already expired -> still an exception
    flagged = list(expiring_certificates())
    assert len(flagged) == 2  # the near-expiry one and the expired one


def test_exceptions_aggregate_failed_and_pending(partner):
    ExchangePackage.objects.create(jurisdiction=partner, reporting_year=2025,
                                   message_ref_id="NG2025GB000001", status=ExchangePackage.Status.FILE_ERROR)
    ExchangePackage.objects.create(jurisdiction=partner, reporting_year=2025,
                                   message_ref_id="NG2025GB000002", status=ExchangePackage.Status.RECORD_ERRORS)
    ex = cts_exceptions()
    assert ex["failed_transmissions"].count() == 1
    assert ex["pending_corrections"].count() == 1


def test_monitoring_page_renders_with_alert(partner):
    _cert(partner, 5)  # expiring -> alert
    ExchangePackage.objects.create(jurisdiction=partner, reporting_year=2025,
                                   message_ref_id="NG2025GB000009", status=ExchangePackage.Status.FILE_ERROR)
    html = view_client().get("/backoffice/exchange/monitoring/").content.decode()
    assert "CTS Monitoring" in html
    assert "Certificate inventory" in html
    assert "open CTS exception" in html  # the alert banner fired
    assert "Failed transmissions" in html


def test_seed_populates_certificate_inventory():
    from django.core.management import call_command
    call_command("demo_seed", verbosity=0)
    assert TransmissionCertificate.objects.filter(owner=TransmissionCertificate.Owner.NRS).exists()
    assert TransmissionCertificate.objects.filter(owner=TransmissionCertificate.Owner.PARTNER).exists()
    # the seed deliberately includes at least one expiring/expired certificate
    assert expiring_certificates().exists()
