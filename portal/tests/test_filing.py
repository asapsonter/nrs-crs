"""Filing, validation rule, and correction lineage tests."""
from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from backoffice.validation import run_validation
from core import config
from exchange.models import PartnerJurisdiction
from exchange.services import next_doc_ref_id
from portal.models import AccountReport, Filing, PortalUser, ReportingFI, ValidationFinding
from portal.xml_ingest import parse_crs_upload

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR


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
        holder_address="1 King Street, London",
        self_certification="OBTAINED",
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

    def test_missing_tin_without_reason_is_error(self, rfi, partners):
        # CRS 2.0: a missing TIN with no reason is an error, not a soft warning.
        filing = make_filing(rfi)
        make_record(filing, foreign_tin="")
        run_validation(filing)
        assert filing.findings.filter(code="R-204", severity="ERROR").exists()

    def test_missing_tin_with_reason_is_warning(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, foreign_tin="", tin_unavailable_reason="Holder did not supply a TIN")
        run_validation(filing)
        assert filing.findings.get(code="R-201").severity == "WARNING"

    def test_missing_address_is_error(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, holder_address="")
        run_validation(filing)
        assert filing.findings.filter(code="R-106", severity="ERROR").exists()

    def test_undocumented_account_is_warning(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, self_certification="NOT_OBTAINED")
        run_validation(filing)
        assert filing.findings.filter(code="R-206", severity="WARNING").exists()

    def test_passive_nfe_without_controlling_person_is_error(self, rfi, partners):
        filing = make_filing(rfi)
        make_record(filing, holder_type="ORGANISATION", acct_holder_type="CRS101")
        run_validation(filing)
        assert filing.findings.filter(code="R-108", severity="ERROR").exists()

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


class TestCorrectionChainValidation:
    """The correction-chain rules: F-002, R-501, R-502, R-503."""

    def test_duplicate_message_reference_is_error(self, rfi, partners):
        make_filing(rfi, message_reference="NGCRS-2026-001")
        filing = make_filing(rfi, message_reference="NGCRS-2026-001", status=Filing.Status.DRAFT)
        make_record(filing)
        run_validation(filing)
        assert filing.findings.filter(code="F-002", severity="ERROR").exists()

    def test_unique_message_reference_passes(self, rfi, partners):
        make_filing(rfi, message_reference="NGCRS-2026-001")
        filing = make_filing(rfi, message_reference="NGCRS-2026-002", status=Filing.Status.DRAFT)
        make_record(filing)
        run_validation(filing)
        assert not filing.findings.filter(code="F-002").exists()

    def test_corr_doc_ref_id_must_reference_filed_record(self, rfi, partners):
        filing = make_filing(rfi, status=Filing.Status.DRAFT)
        make_record(
            filing,
            corr_doc_ref_id="NG2026-UNKNOWN-999",
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        run_validation(filing)
        assert filing.findings.filter(code="R-502", severity="ERROR").exists()

    def test_correction_of_submitted_record_passes(self, rfi, partners):
        prior = make_filing(rfi)
        original = make_record(prior)
        filing = make_filing(rfi, status=Filing.Status.DRAFT)
        make_record(
            filing,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        run_validation(filing)
        assert not filing.findings.filter(code__in=["R-501", "R-502", "R-503"]).exists()

    def test_same_filing_amendment_chain_passes(self, rfi, partners):
        # The returned-for-correction flow supersedes the original within the
        # same filing; its CorrDocRefId must not be flagged as dangling.
        filing = make_filing(rfi, status=Filing.Status.RETURNED)
        original = make_record(filing, superseded=True)
        make_record(
            filing,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        run_validation(filing)
        assert not filing.findings.filter(code__in=["R-501", "R-502", "R-503"]).exists()

    def test_same_record_corrected_twice_in_one_filing_is_error(self, rfi, partners):
        prior = make_filing(rfi)
        original = make_record(prior)
        filing = make_filing(rfi, status=Filing.Status.DRAFT)
        make_record(
            filing,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        make_record(
            filing,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        run_validation(filing)
        assert filing.findings.filter(code="R-501", severity="ERROR").count() == 1

    def test_correcting_an_already_corrected_record_is_error(self, rfi, partners):
        prior = make_filing(rfi)
        original = make_record(prior)
        earlier_correction = make_filing(rfi)
        make_record(
            earlier_correction,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        filing = make_filing(rfi, status=Filing.Status.DRAFT)
        make_record(
            filing,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        run_validation(filing)
        assert filing.findings.filter(code="R-503", severity="ERROR").exists()

    def test_draft_correction_elsewhere_does_not_block(self, rfi, partners):
        prior = make_filing(rfi)
        original = make_record(prior)
        draft_elsewhere = make_filing(rfi, status=Filing.Status.DRAFT)
        make_record(
            draft_elsewhere,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        filing = make_filing(rfi, status=Filing.Status.DRAFT)
        make_record(
            filing,
            corr_doc_ref_id=original.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
        )
        run_validation(filing)
        assert not filing.findings.filter(code="R-503").exists()


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


class TestReturnedFilingEditing:
    """A filing the NRS returned ("rejected") must be editable and resubmittable."""

    _FORM = {
        "holder_name": "Chukwu Emeka",
        "holder_type": "INDIVIDUAL",
        "residence_country": "GB",
        "holder_address": "1 King Street, London",
        "foreign_tin": "QQ123456C",
        "self_certification": "OBTAINED",
        "account_number": "ACC-0001",
        "currency": "GBP",
        "balance": "950000.00",
    }

    def test_unflagged_record_amendable_when_returned(self, rfi, partners, portal_client):
        filing = make_filing(rfi, status=Filing.Status.RETURNED, return_reason="Header mismatch.")
        record = make_record(filing)
        # Only a file-level finding: no record is individually flagged.
        ValidationFinding.objects.create(
            filing=filing, severity="ERROR", code="F-101", message="Header mismatch."
        )
        response = portal_client.post(
            f"/portal/filings/{filing.pk}/records/{record.pk}/", self._FORM
        )
        assert response.status_code == 302
        record.refresh_from_db()
        assert record.superseded is True
        replacement = filing.account_reports.get(superseded=False)
        assert replacement.doc_type_indic == AccountReport.DocTypeIndic.OECD2
        assert replacement.corr_doc_ref_id == record.doc_ref_id

    def test_detail_offers_amend_link_for_unflagged_records(self, rfi, partners, portal_client):
        filing = make_filing(rfi, status=Filing.Status.RETURNED, return_reason="See notes.")
        record = make_record(filing)
        html = portal_client.get(f"/portal/filings/{filing.pk}/").content.decode()
        assert f"/portal/filings/{filing.pk}/records/{record.pk}/" in html
        assert "Amend" in html

    def test_resubmit_allowed_when_only_file_level_findings(self, rfi, partners, portal_client):
        filing = make_filing(rfi, status=Filing.Status.RETURNED, return_reason="Header mismatch.")
        make_record(filing)
        ValidationFinding.objects.create(
            filing=filing, severity="ERROR", code="F-101", message="Header mismatch."
        )
        response = portal_client.post(f"/portal/filings/{filing.pk}/resubmit/")
        assert response.status_code == 302
        filing.refresh_from_db()
        # Resubmission auto-validates against the CRS schema on arrival.
        assert filing.status == Filing.Status.UNDER_VALIDATION
        assert filing.validated_at is not None

    def test_resubmit_blocked_while_flagged_record_uncorrected(self, rfi, partners, portal_client):
        filing = make_filing(rfi, status=Filing.Status.RETURNED, return_reason="Bad TIN.")
        record = make_record(filing)
        ValidationFinding.objects.create(
            filing=filing,
            account_report=record,
            severity="ERROR",
            code="R-102",
            message="TIN malformed.",
        )
        response = portal_client.post(f"/portal/filings/{filing.pk}/resubmit/")
        assert response.status_code == 302
        filing.refresh_from_db()
        assert filing.status == Filing.Status.RETURNED

    def test_flagged_record_correction_then_resubmit(self, rfi, partners, portal_client):
        filing = make_filing(rfi, status=Filing.Status.RETURNED, return_reason="Bad TIN.")
        record = make_record(filing)
        ValidationFinding.objects.create(
            filing=filing,
            account_report=record,
            severity="ERROR",
            code="R-102",
            message="TIN malformed.",
        )
        portal_client.post(f"/portal/filings/{filing.pk}/records/{record.pk}/", self._FORM)
        response = portal_client.post(f"/portal/filings/{filing.pk}/resubmit/")
        assert response.status_code == 302
        filing.refresh_from_db()
        # Resubmission auto-validates against the CRS schema on arrival.
        assert filing.status == Filing.Status.UNDER_VALIDATION
        assert filing.validated_at is not None


class TestOecdXmlSamples:
    """The 2026 FI Sensitization UAT samples (official CRS_OECD v2.0 form)."""

    SAMPLES = Path(__file__).resolve().parent / "samples"

    @staticmethod
    def _read(name: str) -> bytes:
        return (TestOecdXmlSamples.SAMPLES / name).read_bytes()

    @pytest.mark.parametrize(
        "name",
        [
            "2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml",
            "2026_New_Sample_UAT_CRS_XML_Test1_NG-BR.xml",
            "2026_Ammended_Sample_UAT_CRS_XML_Test1_NG-GB.xml",
            "2026_Ammended_Sample_UAT_CRS_XML_Test1_NG-BR.xml",
        ],
    )
    def test_sample_parses(self, name):
        result = parse_crs_upload(self._read(name), YEAR)
        assert result.ok, result.errors
        assert result.year == YEAR
        assert len(result.records) == 1

    def test_new_sample_fields_are_captured(self):
        result = parse_crs_upload(self._read("2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml"), YEAR)
        assert result.ok, result.errors
        assert result.message_type_indic == "CRS701"
        assert result.receiving_country == "GB"
        assert result.message_ref_id.startswith("NG2025GB")
        record = result.records[0]
        assert record.holder_type == "ORGANISATION"
        assert record.holder_name == "AccountReport_ORG_Name_1"
        assert record.acct_holder_type == "CRS102"
        assert record.residence_country == "GB"
        assert record.foreign_tin == "76-2347867654"
        assert record.account_number == "613657467"
        assert record.acct_number_type == "OECD605"
        assert record.closed_account is False and record.dormant_account is False
        assert record.currency == "USD"
        assert record.balance == Decimal("150000000.00")
        # CRS501 dividends and CRS502 interest payments.
        assert record.dividends == Decimal("120010.00")
        assert record.interest == Decimal("120010.00")
        assert record.holder_city == "City_AccountReport_ORG_1"
        assert record.holder_street == "Street_AccountReport_ORG_1"
        assert record.address_country == "NG"

    def test_amended_sample_carries_correction_header(self):
        result = parse_crs_upload(
            self._read("2026_Ammended_Sample_UAT_CRS_XML_Test1_NG-GB.xml"), YEAR
        )
        assert result.ok, result.errors
        assert result.message_type_indic == "CRS702"

    def test_upload_creates_filing_with_header_and_full_record(self, rfi, partners, portal_client):
        from django.core.files.uploadedfile import SimpleUploadedFile

        name = "2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml"
        response = portal_client.post(
            "/portal/filings/upload/",
            {"crs_file": SimpleUploadedFile(name, self._read(name), content_type="text/xml")},
        )
        assert response.status_code == 302, getattr(response, "context", None)
        filing = Filing.objects.latest("pk")
        assert filing.kind == Filing.Kind.XML_UPLOAD
        assert filing.message_type == "CRS701"
        assert filing.receiving_country == "GB"
        assert filing.message_reference.startswith("NG2025GB")
        record = filing.account_reports.get()
        assert record.holder_type == "ORGANISATION"
        assert record.acct_holder_type == "CRS102"
        assert record.currency == "USD"
        assert record.acct_number_type == "OECD605"
        assert record.holder_city == "City_AccountReport_ORG_1"

    def test_wrong_reporting_period_rejected(self):
        xml = self._read("2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml").decode()
        xml = xml.replace("<crs:ReportingPeriod>2025-12-31</crs:ReportingPeriod>",
                          "<crs:ReportingPeriod>2022-12-31</crs:ReportingPeriod>")
        result = parse_crs_upload(xml.encode(), YEAR)
        assert not result.ok
        assert any("2022" in error for error in result.errors)

    def test_organisation_without_acct_holder_type_rejected(self):
        xml = self._read("2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml").decode()
        xml = xml.replace("<crs:AcctHolderType>CRS102</crs:AcctHolderType>", "")
        result = parse_crs_upload(xml.encode(), YEAR)
        assert not result.ok
        assert any("AcctHolderType" in error for error in result.errors)

    def test_nil_indicator_rejected_for_upload(self):
        xml = self._read("2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml").decode()
        xml = xml.replace("CRS701", "CRS703")
        result = parse_crs_upload(xml.encode(), YEAR)
        assert not result.ok
        assert any("Nil Return" in error for error in result.errors)
