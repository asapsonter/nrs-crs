"""Inbound taxpayer-matching tests (BPMN IB-08/IB-09): match, risk, reattempt."""
from __future__ import annotations

from decimal import Decimal

import pytest

from exchange.matching import match_inbound_file, resolve_unmatched_identities, risk_for_balance
from exchange.models import InboundFile, InboundRecord, PartnerJurisdiction, Taxpayer

pytestmark = pytest.mark.django_db

YEAR = 2025


@pytest.fixture
def partner() -> PartnerJurisdiction:
    return PartnerJurisdiction.objects.create(
        code="GB", name="United Kingdom", activated_since="2019-04-01", key_fingerprint="AA:BB"
    )


def _file(partner: PartnerJurisdiction) -> InboundFile:
    return InboundFile.objects.create(
        jurisdiction=partner,
        reporting_year=YEAR,
        message_ref_id=f"GB{YEAR}NG000001",
        status=InboundFile.Status.APPROVED,
    )


def _record(file: InboundFile, tin: str, name: str, balance: str, **kwargs) -> InboundRecord:
    return InboundRecord.objects.create(
        file=file,
        doc_ref_id=f"GB{YEAR}-X-{InboundRecord.objects.count() + 1:04d}",
        holder_name=name,
        ng_tin=tin,
        account_number=f"ACC-{InboundRecord.objects.count() + 1}",
        balance=Decimal(balance),
        **kwargs,
    )


def test_risk_bands():
    assert risk_for_balance(Decimal("150000")) == InboundRecord.RiskRating.ENHANCED
    assert risk_for_balance(Decimal("50000")) == InboundRecord.RiskRating.SPECIFIC
    assert risk_for_balance(Decimal("49999.99")) == InboundRecord.RiskRating.GENERAL


def test_match_on_tin_sets_risk(partner):
    inbound = _file(partner)
    taxpayer = Taxpayer.objects.create(tin="10293847-0002", name="Folake Adesina")
    record = _record(inbound, "10293847-0002", "Folake Adesina", "152400")

    summary = match_inbound_file(inbound)

    record.refresh_from_db()
    assert summary == {"matched": 1, "unmatched": 0}
    assert record.match_status == InboundRecord.MatchStatus.MATCHED
    assert record.match_basis == "TIN"
    assert record.matched_taxpayer == taxpayer
    assert record.risk_rating == InboundRecord.RiskRating.ENHANCED


def test_match_falls_back_to_identity(partner):
    inbound = _file(partner)
    Taxpayer.objects.create(tin="99999999-0001", name="Chukwuemeka Obi")
    # No TIN on the record, so matching must fall back to the name.
    record = _record(inbound, "", "Chukwuemeka Obi", "85000")

    match_inbound_file(inbound)

    record.refresh_from_db()
    assert record.match_status == InboundRecord.MatchStatus.MATCHED
    assert record.match_basis == "IDENTITY"
    assert record.risk_rating == InboundRecord.RiskRating.SPECIFIC


def test_unmatched_then_reattempt_after_identity_resolution(partner):
    inbound = _file(partner)
    record = _record(inbound, "10293847-0003", "Ibrahim Danjuma", "40100")

    first = match_inbound_file(inbound)
    record.refresh_from_db()
    assert first == {"matched": 0, "unmatched": 1}
    assert record.match_status == InboundRecord.MatchStatus.UNMATCHED
    assert record.match_attempts == 1

    added = resolve_unmatched_identities(inbound)
    assert added == 1
    assert Taxpayer.objects.filter(name="Ibrahim Danjuma").exists()

    second = match_inbound_file(inbound)
    record.refresh_from_db()
    assert second == {"matched": 1, "unmatched": 0}
    assert record.match_status == InboundRecord.MatchStatus.MATCHED
    assert record.match_attempts == 2
    assert record.risk_rating == InboundRecord.RiskRating.GENERAL


def test_records_with_open_errors_are_skipped(partner):
    inbound = _file(partner)
    Taxpayer.objects.create(tin="10293847-0002", name="Folake Adesina")
    _record(inbound, "10293847-0002", "Folake Adesina", "10000",
            has_error=True, error_code="80008", corrected=False)

    summary = match_inbound_file(inbound)

    assert summary == {"matched": 0, "unmatched": 0}
