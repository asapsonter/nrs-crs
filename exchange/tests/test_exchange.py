"""Exchange tests: MessageRefID, packaging, XML, and CRS702 corrections."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import Client

from core import config
from core.models import IssuedCredential, OfficerProfile, Roles
from exchange.models import ExchangePackage, PartnerJurisdiction, RecordError, StatusMessage
from exchange.services import build_nil_packages, build_packages, generate_crs_xml, next_message_ref_id
from portal.models import AccountReport, Filing, ReportingFI

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR


@pytest.fixture
def partners() -> None:
    for code, name, since, fingerprint in config.PARTNER_JURISDICTIONS:
        PartnerJurisdiction.objects.create(
            code=code, name=name, activated_since=since, key_fingerprint=fingerprint
        )


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
        pu_designation="Head",
        pu_email="pu@zenithtrust.ng",
        pu_phone="8000000000",
    )


def accepted_filing_with_records(rfi: ReportingFI, countries: list[str]) -> Filing:
    filing = Filing.objects.create(
        reference=f"FIL-{YEAR}-{Filing.objects.count() + 1:05d}",
        rfi=rfi,
        reporting_year=YEAR,
        kind=Filing.Kind.MANUAL,
        status=Filing.Status.ACCEPTED,
    )
    for index, country in enumerate(countries, start=1):
        AccountReport.objects.create(
            filing=filing,
            doc_ref_id=f"NG{YEAR}-{rfi.reference}-{AccountReport.objects.count() + 1:06d}",
            holder_name=f"Holder {index}",
            residence_country=country,
            foreign_tin=f"TIN{index:06d}",
            account_number=f"ACC{index:06d}",
            balance=Decimal("500000.00"),
        )
    return filing


def exchange_officer_client() -> Client:
    officer = OfficerProfile.objects.create(
        name="Exchange Officer", roles=Roles.INTERNAL_ADMIN, email="exchange@nrs.gov.ng"
    )
    credential, passcode = IssuedCredential.objects.issue(officer, 2.0)
    client = Client()
    client.post("/backoffice/login/", {"username": credential.username, "passcode": passcode})
    return client


class TestMessageRefId:
    def test_format_sending_year_receiving_sequence(self, partners):
        ref = next_message_ref_id("GB", YEAR)
        assert ref == f"NG{YEAR}GB000001"

    def test_sequence_advances_per_corridor(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB"])
        build_packages(YEAR)
        assert next_message_ref_id("GB", YEAR) == f"NG{YEAR}GB000002"
        assert next_message_ref_id("FR", YEAR) == f"NG{YEAR}FR000001"


class TestPackaging:
    def test_records_sorted_by_residence_jurisdiction(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB", "GB", "FR", "AE"])
        packages = build_packages(YEAR)
        by_code = {package.jurisdiction.code: package for package in packages}
        assert set(by_code) == {"GB", "FR", "AE"}
        assert by_code["GB"].records.count() == 2

    def test_filing_moves_to_included_in_exchange(self, partners, rfi):
        filing = accepted_filing_with_records(rfi, ["GB", "FR"])
        build_packages(YEAR)
        filing.refresh_from_db()
        assert filing.status == Filing.Status.IN_EXCHANGE

    def test_xml_contains_message_spec_and_records(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB"])
        package = build_packages(YEAR)[0]
        xml = package.xml_content
        assert f"<crs:MessageRefId>{package.message_ref_id}</crs:MessageRefId>" in xml
        assert "<crs:ReceivingCountry>GB</crs:ReceivingCountry>" in xml
        assert "<crs:MessageTypeIndic>CRS701</crs:MessageTypeIndic>" in xml
        assert xml.count("<crs:AccountReport>") == 1


class TestNilReturns:
    def test_nil_built_only_for_empty_corridors(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB"])
        build_packages(YEAR)
        nil_packages = build_nil_packages(YEAR)
        codes = {package.jurisdiction.code for package in nil_packages}
        # GB exchanged data; every other activated partner gets a CRS703.
        assert "GB" not in codes
        assert codes == {code for code, *_ in config.PARTNER_JURISDICTIONS} - {"GB"}
        for package in nil_packages:
            assert package.message_type == "CRS703"
            assert package.records.count() == 0
            assert "<crs:MessageTypeIndic>CRS703</crs:MessageTypeIndic>" in package.xml_content
            assert "SendingCompanyIN" not in package.xml_content
            assert "CrsBody" not in package.xml_content

    def test_corridor_with_unpackaged_records_gets_no_nil(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB"])
        nil_packages = build_nil_packages(YEAR)
        assert "GB" not in {package.jurisdiction.code for package in nil_packages}

    def test_accepted_nil_filing_advances_to_in_exchange(self, partners, rfi):
        filing = Filing.objects.create(
            reference=f"FIL-{YEAR}-NIL01",
            rfi=rfi,
            reporting_year=YEAR,
            kind=Filing.Kind.NIL,
            status=Filing.Status.ACCEPTED,
        )
        build_nil_packages(YEAR)
        filing.refresh_from_db()
        assert filing.status == Filing.Status.IN_EXCHANGE

    def test_nil_build_is_idempotent(self, partners, rfi):
        first = build_nil_packages(YEAR)
        assert first
        assert build_nil_packages(YEAR) == []

    def test_generator_refuses_records_on_nil_package(self, partners, rfi):
        filing = accepted_filing_with_records(rfi, ["GB"])
        package = ExchangePackage.objects.create(
            jurisdiction=PartnerJurisdiction.objects.get(code="GB"),
            reporting_year=YEAR,
            message_ref_id=next_message_ref_id("GB", YEAR),
            message_type="CRS703",
        )
        package.records.set(filing.account_reports.all())
        with pytest.raises(ValueError):
            generate_crs_xml(package)


class TestCorrectionCycle:
    def _transmit_and_get_record_errors(self, client: Client, package: ExchangePackage) -> None:
        for _ in range(8):
            client.post(f"/backoffice/exchange/packages/{package.pk}/advance/")
        package.refresh_from_db()
        assert package.status == ExchangePackage.Status.TRANSMITTED
        record = package.records.first()
        client.post(
            f"/backoffice/exchange/packages/{package.pk}/partner-response/",
            {"outcome": "record_errors", "record_ids": [record.pk], "record_error_code": "80008"},
        )
        package.refresh_from_db()
        assert package.status == ExchangePackage.Status.RECORD_ERRORS

    def test_crs702_correction_carries_corr_doc_ref_id(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB"])
        package = build_packages(YEAR)[0]
        client = exchange_officer_client()
        self._transmit_and_get_record_errors(client, package)
        error = RecordError.objects.get(resolved=False)
        original_doc_ref = error.doc_ref_id
        client.post(
            f"/backoffice/exchange/packages/{package.pk}/correct/",
            {f"action_{error.pk}": "correct", f"tin_{error.pk}": "GB-TIN-99881", f"country_{error.pk}": ""},
        )
        correction = ExchangePackage.objects.get(message_type="CRS702")
        assert correction.corrects_package == package
        corrected = correction.records.get()
        assert corrected.doc_type_indic == AccountReport.DocTypeIndic.OECD2
        assert corrected.corr_doc_ref_id == original_doc_ref
        # The lineage resolves back to the original record.
        original = AccountReport.objects.get(doc_ref_id=corrected.corr_doc_ref_id)
        assert original.superseded
        # DocSpec children are in the stf namespace, not crs.
        assert f"<stf:CorrDocRefId>{original_doc_ref}</stf:CorrDocRefId>" in correction.xml_content
        assert "<stf:DocTypeIndic>OECD2</stf:DocTypeIndic>" in correction.xml_content
        # The ReportingFI is resent unmodified under OECD0.
        assert "<stf:DocTypeIndic>OECD0</stf:DocTypeIndic>" in correction.xml_content
        error.refresh_from_db()
        assert error.resolved

    def test_accept_archives_package(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB"])
        package = build_packages(YEAR)[0]
        client = exchange_officer_client()
        for _ in range(8):
            client.post(f"/backoffice/exchange/packages/{package.pk}/advance/")
        client.post(
            f"/backoffice/exchange/packages/{package.pk}/partner-response/", {"outcome": "accept"}
        )
        package.refresh_from_db()
        assert package.status == ExchangePackage.Status.ARCHIVED
        assert package.status_messages.filter(outcome=StatusMessage.Outcome.ACCEPTED).exists()

    def test_file_error_reopens_whole_package_with_new_ref(self, partners, rfi):
        accepted_filing_with_records(rfi, ["GB"])
        package = build_packages(YEAR)[0]
        old_ref = package.message_ref_id
        client = exchange_officer_client()
        for _ in range(8):
            client.post(f"/backoffice/exchange/packages/{package.pk}/advance/")
        client.post(
            f"/backoffice/exchange/packages/{package.pk}/partner-response/",
            {"outcome": "file_error", "file_error_code": "50010"},
        )
        package.refresh_from_db()
        assert package.status == ExchangePackage.Status.FILE_ERROR
        client.post(f"/backoffice/exchange/packages/{package.pk}/resubmit/")
        package.refresh_from_db()
        assert package.status == ExchangePackage.Status.BUILT
        assert package.message_ref_id != old_ref
        assert package.pipeline_step == 1
