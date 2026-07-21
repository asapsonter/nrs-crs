"""Filing, validation rule, and correction lineage tests."""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from backoffice.validation import run_validation
from core import config
from exchange.models import PartnerJurisdiction
from exchange.services import next_doc_ref_id
from portal.models import AccountReport, Filing, PortalUser, ReportingFI
from portal.xml_ingest import parse_crs_upload

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR


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


def make_filing(rfi: ReportingFI, **kwargs) -> Filing:
    defaults = dict(
        reference=f"FIL-{YEAR}-{Filing.objects.count() + 1:05d}",
        rfi=rfi,
        reporting_year=YEAR,
        kind=Filing.Kind.MANUAL,
        status=Filing.Status.SUBMITTED,
    )
    defaults.update(kwargs)
    return Filing.objects.create(**defaults)


def make_record(filing: Filing, **kwargs) -> AccountReport:
    defaults = dict(
        doc_ref_id=next_doc_ref_id(filing.rfi, filing.reporting_year),
        holder_name="Chukwu Emeka",
        residence_country="GB",
        foreign_tin="QQ123456C",
        account_number=f"ACC-{AccountReport.objects.count() + 1:04d}",
        balance=Decimal("1000000.00"),
    )
    defaults.update(kwargs)
    return AccountReport.objects.create(filing=filing, **defaults)


class TestValidationRules:
    def test_clean_filing_has_no_findings(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing)
        file_count, record_count = run_validation(filing)
        assert (file_count, record_count) == (0, 0)

    def test_empty_filing_is_a_file_level_failure(self, rfi, partners):
        filing = make_filing(rfi)
        file_count, record_count = run_validation(filing)
        assert file_count == 1
        assert filing.findings.get().code == "F-001"

    def test_missing_mandatory_fields(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, holder_name="", account_number="X1")
        run_validation(filing)
        assert filing.findings.filter(code="R-101").exists()

    def test_unactivated_jurisdiction_rejected(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, residence_country="US")
        run_validation(filing)
        assert filing.findings.filter(code="R-104").exists()

    def test_missing_tin_is_warning_not_error(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, foreign_tin="")
        run_validation(filing)
        finding = filing.findings.get(code="R-201")
        assert finding.severity == "WARNING"

    def test_implausible_tin_is_error(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, foreign_tin="@@")
        run_validation(filing)
        assert filing.findings.filter(code="R-202", severity="ERROR").exists()

    def test_negative_balance_rejected(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, balance=Decimal("-5.00"))
        run_validation(filing)
        assert filing.findings.filter(code="R-301").exists()

    def test_date_outside_reporting_year_rejected(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, opened_date=datetime.date(YEAR + 1, 1, 15))
        run_validation(filing)
        assert filing.findings.filter(code="R-401").exists()

    def test_duplicate_accounts_within_filing_rejected(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, account_number="SAME-1")
        make_record(filing, account_number="SAME-1")
        run_validation(filing)
        assert filing.findings.filter(code="R-105").count() == 1

    def test_superseded_records_are_not_validated(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, balance=Decimal("-1.00"), superseded=True)
        make_record(filing)
        file_count, record_count = run_validation(filing)
        assert (file_count, record_count) == (0, 0)


class TestCorrectionLineage:
    def test_corr_doc_ref_id_resolves_to_original(self, rfi, partners):
        filing = make_filing(rfi, status=Filing.Status.RETURNED)
        original = make_record(filing, balance=Decimal("-10.00"))
        correction = make_record(
            filing,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
            balance=Decimal("10.00"),
        )
        original.superseded = True
        original.save()
        resolved = AccountReport.objects.get(doc_ref_id=correction.corr_doc_ref_id)
        assert resolved == original
        assert resolved.superseded
        assert correction.doc_type_indic == "OECD2"

    def test_doc_ref_ids_are_unique_per_rfi_and_year(self, rfi):
        filing = make_filing(rfi)
        first = make_record(filing)
        second = make_record(filing)
        assert first.doc_ref_id != second.doc_ref_id
        assert first.doc_ref_id.startswith(f"NG{YEAR}-{rfi.reference}-")


class TestXmlIngestion:
    def test_well_formed_document_parses(self):
        xml = f"""<CRSFiling year="{YEAR}">
  <ReportingFI tin="0450088801" name="Zenith Trust Bank Plc"/>
  <AccountReport>
    <HolderName>Jane Example</HolderName>
    <ResCountry>GB</ResCountry>
    <TIN>QQ123456C</TIN>
    <AccountNumber>0011223344</AccountNumber>
    <Balance>25000000.00</Balance>
  </AccountReport>
</CRSFiling>"""
        result = parse_crs_upload(xml.encode(), YEAR)
        assert result.ok
        assert len(result.records) == 1
        assert result.records[0].balance == Decimal("25000000.00")

    def test_malformed_xml_reports_line(self):
        xml = f'<CRSFiling year="{YEAR}">\n<ReportingFI tin="1" name="X"/>\n<AccountReport>\n</CRSFiling>'
        result = parse_crs_upload(xml.encode(), YEAR)
        assert not result.ok
        assert any("Line" in error for error in result.errors)

    def test_missing_required_elements_reported_per_record(self):
        xml = f"""<CRSFiling year="{YEAR}">
  <ReportingFI tin="1" name="X"/>
  <AccountReport><HolderName>A</HolderName></AccountReport>
</CRSFiling>"""
        result = parse_crs_upload(xml.encode(), YEAR)
        assert not result.ok
        assert any("AccountReport 1" in error and "ResCountry" in error for error in result.errors)

    def test_wrong_year_rejected(self):
        xml = f'<CRSFiling year="{YEAR - 3}"><ReportingFI tin="1" name="X"/><AccountReport><HolderName>A</HolderName><ResCountry>GB</ResCountry><AccountNumber>1</AccountNumber><Balance>1</Balance></AccountReport></CRSFiling>'
        result = parse_crs_upload(xml.encode(), YEAR)
        assert not result.ok
