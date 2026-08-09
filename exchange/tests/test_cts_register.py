"""CTS register hardening: regime, responsible officer, reviewer, version, warning.

Covers FR-CTS-003 (responsible officer), FR-CTS-004 (assigned reviewer),
FR-CTS-006 (warning outcome class) and DR-CTS-004 (file version).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import Client

from core import config
from core.models import IssuedCredential, OfficerProfile, Roles
from exchange.models import (
    PIPELINE_STEPS,
    ExchangePackage,
    InboundFile,
    InboundRecord,
    PartnerJurisdiction,
    RecordError,
    StatusMessage,
)
from portal.models import AccountReport, Filing, ReportingFI

pytestmark = pytest.mark.django_db
YEAR = config.CURRENT_REPORTING_YEAR


def operate_client(name="Ops Officer", email="ops@nrs.gov.ng"):
    officer = OfficerProfile.objects.create(name=name, roles=Roles.INTERNAL_ADMIN, email=email)
    cred, pc = IssuedCredential.objects.issue(officer, 2.0)
    c = Client()
    c.post("/backoffice/login/", {"username": cred.username, "passcode": pc})
    return c, officer


@pytest.fixture
def partners():
    for code, nm, since, fp in config.PARTNER_JURISDICTIONS:
        PartnerJurisdiction.objects.create(code=code, name=nm, activated_since=since, key_fingerprint=fp)


@pytest.fixture
def accepted_filing(partners):
    rfi = ReportingFI.objects.create(
        reference=f"NRS-RFI-{YEAR}-7001", legal_name="Zenith Trust Bank Plc", tin="0450088801",
        category="DEPOSITORY_INSTITUTION", enrolment_type="FINANCIAL_ENTITY", status=ReportingFI.Status.ACTIVE,
        pu_surname="Bello", pu_first_name="Ngozi", pu_designation="Head", pu_email="pu@z.ng", pu_phone="800",
    )
    filing = Filing.objects.create(reference=f"FIL-{YEAR}-70001", rfi=rfi, reporting_year=YEAR,
                                   kind=Filing.Kind.MANUAL, status=Filing.Status.ACCEPTED)
    for i, country in enumerate(["GB", "FR"], start=1):
        AccountReport.objects.create(
            filing=filing, doc_ref_id=f"NG{YEAR}-7001-{i:06d}", holder_name=f"Holder {i}",
            residence_country=country, foreign_tin=f"TIN{i}", account_number=f"ACC{i}", balance=Decimal("1000"))
    return filing


def test_build_sets_regime_version_and_responsible_officer(accepted_filing):
    c, officer = operate_client()
    c.post("/backoffice/exchange/build/")
    pkg = ExchangePackage.objects.first()
    assert pkg.regime == "CRS"
    assert pkg.file_version == 1
    assert pkg.responsible_officer == officer


def test_correction_bumps_version_and_inherits_regime(accepted_filing):
    c, officer = operate_client()
    c.post("/backoffice/exchange/build/")
    pkg = ExchangePackage.objects.get(jurisdiction__code="GB")
    # transmit, then have the partner report a record error to open a correction
    for _ in range(len(PIPELINE_STEPS) + 1):
        c.post(f"/backoffice/exchange/packages/{pkg.pk}/advance/")
    rec = pkg.records.first()
    c.post(f"/backoffice/exchange/packages/{pkg.pk}/partner-response/",
           {"outcome": "record_errors", "record_error_code": "80008", "record_ids": [rec.pk]})
    error = RecordError.objects.get(status_message__package=pkg, resolved=False)
    c.post(f"/backoffice/exchange/packages/{pkg.pk}/correct/", {f"action_{error.pk}": "correct"})
    correction = ExchangePackage.objects.filter(message_type="CRS702").first()
    assert correction is not None
    assert correction.file_version == 2
    assert correction.regime == "CRS"
    assert correction.responsible_officer == officer


def test_inbound_file_check_assigns_reviewer(partners):
    gb = PartnerJurisdiction.objects.get(code="GB")
    inbound = InboundFile.objects.create(jurisdiction=gb, reporting_year=YEAR, message_ref_id=f"GB{YEAR}NG000900")
    InboundRecord.objects.create(file=inbound, doc_ref_id="D1", holder_name="X", account_number="A1")
    c, officer = operate_client(email="rev@nrs.gov.ng")
    c.post(f"/backoffice/exchange/inbound/{inbound.pk}/file-check/")
    inbound.refresh_from_db()
    assert inbound.regime == "CRS"
    assert inbound.assigned_reviewer == officer


def test_partner_accept_with_warnings_records_warning_and_archives(accepted_filing):
    c, officer = operate_client()
    c.post("/backoffice/exchange/build/")
    pkg = ExchangePackage.objects.get(jurisdiction__code="GB")
    from exchange.models import PIPELINE_STEPS
    for _ in range(len(PIPELINE_STEPS) + 1):
        c.post(f"/backoffice/exchange/packages/{pkg.pk}/advance/")
    c.post(f"/backoffice/exchange/packages/{pkg.pk}/partner-response/",
           {"outcome": "accept_warning", "warning_detail": "Minor TIN formatting."})
    pkg.refresh_from_db()
    assert pkg.status == ExchangePackage.Status.ARCHIVED
    sm = pkg.status_messages.get(outcome=StatusMessage.Outcome.WARNING)
    assert "Minor TIN formatting" in sm.detail


def test_registers_render_with_new_columns(accepted_filing):
    c, officer = operate_client()
    c.post("/backoffice/exchange/build/")
    workspace = c.get("/backoffice/exchange/").content.decode()
    assert "Outgoing transmission register" in workspace
    assert "Responsible officer" in workspace and "Regime" in workspace
    inbound = c.get("/backoffice/exchange/inbound/").content.decode()
    assert "Assigned reviewer" in inbound and "Regime" in inbound
