"""CRS_OECD upload: DocSpec handling, schema deviations and name parts.

The rules exercised here come from the CRS XML Schema User Guide v3.0
(schema v2.0) sections on DocSpec_Type and the correction process, and are
pinned against the UAT samples the NRS issues to reporting institutions.
"""
from __future__ import annotations

import pytest

from core import config
from portal.xml_ingest import parse_crs_upload

YEAR = config.CURRENT_REPORTING_YEAR

HEADER = f"""<?xml version="1.0" encoding="UTF-8"?>
<crs:CRS_OECD xmlns:crs="urn:oecd:ties:crs:v2"
              xmlns:cfc="urn:oecd:ties:commontypesfatcacrs:v2"
              xmlns:stf="urn:oecd:ties:crsstf:v5" version="2.0">
  <crs:MessageSpec>
    <crs:TransmittingCountry>NG</crs:TransmittingCountry>
    <crs:ReceivingCountry>GB</crs:ReceivingCountry>
    <crs:MessageType>CRS</crs:MessageType>
    <crs:MessageRefId>NG{YEAR}GB000001</crs:MessageRefId>
    <crs:MessageTypeIndic>{{indic}}</crs:MessageTypeIndic>
    <crs:ReportingPeriod>{YEAR}-12-31</crs:ReportingPeriod>
    <crs:Timestamp>{YEAR}-05-01T09:00:00</crs:Timestamp>
  </crs:MessageSpec>
  <crs:CrsBody>
    <crs:ReportingFI>
      <crs:ResCountryCode>NG</crs:ResCountryCode>
      <crs:IN issuedBy="NG">0450088801</crs:IN>
      <crs:Name>Zenith Trust Bank Plc</crs:Name>
      <crs:Address>
        <cfc:CountryCode>NG</cfc:CountryCode>
        <cfc:AddressFree>12 Broad Street, Lagos</cfc:AddressFree>
      </crs:Address>
      <crs:DocSpec>
        <stf:DocTypeIndic>OECD1</stf:DocTypeIndic>
        <stf:DocRefId>NG{YEAR}GB-FI-1</stf:DocRefId>
      </crs:DocSpec>
    </crs:ReportingFI>
    <crs:ReportingGroup>
"""

FOOTER = """    </crs:ReportingGroup>
  </crs:CrsBody>
</crs:CRS_OECD>
"""


def _report(doc_type="OECD1", doc_ref="NG-AR-1", corr="", name_block=None):
    corr_xml = f"<stf:CorrDocRefId>{corr}</stf:CorrDocRefId>" if corr else ""
    name_block = name_block or (
        "<crs:FirstName>Amina</crs:FirstName>"
        "<crs:MiddleName>Ngozi</crs:MiddleName>"
        "<crs:LastName>Yusuf</crs:LastName>"
    )
    return f"""      <crs:AccountReport>
        <crs:DocSpec>
          <stf:DocTypeIndic>{doc_type}</stf:DocTypeIndic>
          <stf:DocRefId>{doc_ref}</stf:DocRefId>
          {corr_xml}
        </crs:DocSpec>
        <crs:AccountNumber>ACC000001</crs:AccountNumber>
        <crs:AccountHolder>
          <crs:Individual>
            <crs:ResCountryCode>GB</crs:ResCountryCode>
            <crs:Name>{name_block}</crs:Name>
            <crs:Address>
              <cfc:CountryCode>GB</cfc:CountryCode>
              <cfc:AddressFree>1 High Street, London</cfc:AddressFree>
            </crs:Address>
          </crs:Individual>
        </crs:AccountHolder>
        <crs:AccountBalance currCode="GBP">1000.00</crs:AccountBalance>
      </crs:AccountReport>
"""


def _doc(*reports, indic="CRS701"):
    return (HEADER.format(indic=indic) + "".join(reports) + FOOTER).encode("utf-8")


def _parse(*reports, indic="CRS701"):
    return parse_crs_upload(_doc(*reports, indic=indic), YEAR)


class TestDocSpecCapture:
    """The FI's own record identity must survive ingest."""

    def test_doc_ref_id_is_preserved(self):
        result = _parse(_report(doc_ref="NG2025-BANK-000042"))
        assert result.ok, result.errors
        assert result.records[0].doc_ref_id == "NG2025-BANK-000042"

    def test_doc_type_indic_is_captured(self):
        result = _parse(_report(doc_type="OECD1"))
        assert result.records[0].doc_type_indic == "OECD1"

    def test_missing_doc_ref_id_is_rejected(self):
        result = _parse(_report(doc_ref=""))
        assert not result.ok
        assert any("DocRefId is required" in e for e in result.errors)

    def test_duplicate_doc_ref_id_within_a_document_is_rejected(self):
        result = _parse(_report(doc_ref="SAME"), _report(doc_ref="SAME"))
        assert not result.ok
        assert any("used more than once" in e for e in result.errors)


class TestCorrections:
    def test_correction_carries_corr_doc_ref_id(self):
        result = _parse(
            _report(doc_type="OECD2", doc_ref="NG-AR-2", corr="NG-AR-1"), indic="CRS702"
        )
        assert result.ok, result.errors
        record = result.records[0]
        assert record.doc_type_indic == "OECD2"
        assert record.corr_doc_ref_id == "NG-AR-1"

    def test_correction_without_corr_doc_ref_id_is_rejected(self):
        result = _parse(_report(doc_type="OECD2", doc_ref="NG-AR-2"), indic="CRS702")
        assert not result.ok
        assert any("requires CorrDocRefId" in e for e in result.errors)

    def test_new_data_with_corr_doc_ref_id_is_rejected(self):
        result = _parse(_report(doc_type="OECD1", corr="NG-AR-1"))
        assert not result.ok
        assert any("must not be present on new data" in e for e in result.errors)

    def test_new_message_cannot_carry_a_correction(self):
        result = _parse(
            _report(doc_type="OECD2", doc_ref="NG-AR-2", corr="NG-AR-1"), indic="CRS701"
        )
        assert not result.ok
        assert any("cannot carry a OECD2 record" in e for e in result.errors)

    def test_oecd0_is_rejected_on_an_account_report(self):
        """OECD0 resends the ReportingFI element only, never a record."""
        result = _parse(_report(doc_type="OECD0"), indic="CRS702")
        assert not result.ok
        assert any("applies only to the ReportingFI" in e for e in result.errors)


class TestTestDataIndicators:
    """OECD10-OECD13 are accepted, normalised, and flagged."""

    def test_oecd11_is_normalised_to_oecd1_and_flagged(self):
        result = _parse(_report(doc_type="OECD11"))
        assert result.ok, result.errors
        assert result.contains_test_data
        assert result.records[0].doc_type_indic == "OECD1"
        assert any("test data" in w for w in result.warnings)

    def test_oecd12_is_normalised_to_oecd2(self):
        result = _parse(
            _report(doc_type="OECD12", doc_ref="NG-AR-2", corr="NG-AR-1"), indic="CRS702"
        )
        assert result.ok, result.errors
        assert result.records[0].doc_type_indic == "OECD2"

    def test_live_document_is_not_flagged_as_test(self):
        result = _parse(_report(doc_type="OECD1"))
        assert not result.contains_test_data

    def test_new_indicator_in_a_corrections_message_is_relabelled(self):
        """NRS's amended UAT samples label corrections OECD11 rather than
        OECD12. The CorrDocRefId is the reliable signal, so the record is
        stored as a correction and the discrepancy is reported."""
        result = _parse(
            _report(doc_type="OECD11", doc_ref="NG-AR-2", corr="NG-AR-1"), indic="CRS702"
        )
        assert result.ok, result.errors
        assert result.records[0].doc_type_indic == "OECD2"
        assert any("recorded as OECD2" in w for w in result.warnings)


class TestNameParts:
    def test_middle_name_is_kept_separate_from_first_name(self):
        result = _parse(_report())
        record = result.records[0]
        assert record.holder_first_name == "Amina"
        assert record.holder_middle_name == "Ngozi"
        assert record.holder_last_name == "Yusuf"
        assert record.holder_name == "Amina Ngozi Yusuf"

    def test_name_without_middle_name(self):
        result = _parse(
            _report(name_block="<crs:FirstName>Amina</crs:FirstName><crs:LastName>Yusuf</crs:LastName>")
        )
        record = result.records[0]
        assert record.holder_first_name == "Amina"
        assert record.holder_middle_name == ""


class TestMessageSpec:
    def test_message_type_must_be_crs(self):
        raw = _doc(_report()).replace(b"<crs:MessageType>CRS<", b"<crs:MessageType>FATCA<")
        result = parse_crs_upload(raw, YEAR)
        assert not result.ok
        assert any("MessageType must be 'CRS'" in e for e in result.errors)

    def test_transmitting_country_must_be_ng(self):
        raw = _doc(_report()).replace(
            b"<crs:TransmittingCountry>NG<", b"<crs:TransmittingCountry>GB<"
        )
        result = parse_crs_upload(raw, YEAR)
        assert not result.ok
        assert any("must declare NG" in e for e in result.errors)

    def test_missing_message_ref_id_is_rejected(self):
        raw = _doc(_report()).replace(
            f"<crs:MessageRefId>NG{YEAR}GB000001</crs:MessageRefId>".encode(), b""
        )
        result = parse_crs_upload(raw, YEAR)
        assert not result.ok
        assert any("MessageRefId is required" in e for e in result.errors)

    def test_reporting_fi_identity_is_captured(self):
        result = _parse(_report())
        assert result.reporting_fi_name == "Zenith Trust Bank Plc"
        assert result.reporting_fi_in == "0450088801"


class TestSchemaDeviations:
    """Schema findings are reported without blocking a sound filing."""

    def test_out_of_sequence_docspec_child_is_a_warning_not_an_error(self):
        # CorrMessageRefId must precede CorrDocRefId in DocSpec_Type. NRS's
        # own amended samples emit them the other way round.
        report = _report(doc_type="OECD2", doc_ref="NG-AR-2", corr="NG-AR-1").replace(
            "</stf:CorrDocRefId>",
            "</stf:CorrDocRefId><stf:CorrMessageRefId>NG-MSG-1</stf:CorrMessageRefId>",
        )
        result = _parse(report, indic="CRS702")
        assert result.ok, result.errors
        assert any("Schema deviation" in w for w in result.warnings)
        assert result.records[0].corr_doc_ref_id == "NG-AR-1"

    def test_conformant_document_produces_no_schema_warnings(self):
        result = _parse(_report())
        assert result.ok, result.errors
        assert not [w for w in result.warnings if "Schema deviation" in w]


class TestRoundTrip:
    """Ingest a real NRS UAT sample, store it, re-emit it, and validate.

    This is the end-to-end guarantee: what a reporting institution uploads
    survives the database and comes back out as schema-valid CRS XML.
    """

    @pytest.fixture
    def partners(self, db):
        from exchange.models import PartnerJurisdiction

        for code, name, since, fingerprint in config.PARTNER_JURISDICTIONS:
            PartnerJurisdiction.objects.create(
                code=code, name=name, activated_since=since, key_fingerprint=fingerprint
            )

    @pytest.mark.django_db
    def test_uat_sample_round_trips_to_valid_xml(self, partners):
        import pathlib

        import xmlschema

        from exchange.services import build_packages
        from portal.models import AccountReport, ControllingPerson, Filing, ReportingFI

        raw = pathlib.Path(
            "portal/tests/samples/2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml"
        ).read_bytes()
        result = parse_crs_upload(raw, YEAR)
        assert result.ok, result.errors

        rfi = ReportingFI.objects.create(
            reference="NRS-RFI-2025-0001", legal_name="Zenith Trust Bank Plc",
            tin="0450088801", category="DEPOSITORY_INSTITUTION",
            enrolment_type="FINANCIAL_ENTITY", status=ReportingFI.Status.ACTIVE,
            street="12 Broad Street", city="Lagos", state_province="Lagos",
            post_code="100001", pu_surname="B", pu_first_name="N",
            pu_designation="Head", pu_email="pu@x.ng", pu_phone="800",
        )
        filing = Filing.objects.create(
            reference="FIL-2025-00001", rfi=rfi, reporting_year=YEAR,
            kind=Filing.Kind.XML_UPLOAD, status=Filing.Status.ACCEPTED,
        )
        for parsed in result.records:
            record = AccountReport.objects.create(
                filing=filing,
                doc_ref_id=parsed.doc_ref_id,
                doc_type_indic=parsed.doc_type_indic,
                corr_doc_ref_id=parsed.corr_doc_ref_id,
                holder_name=parsed.holder_name,
                holder_first_name=parsed.holder_first_name,
                holder_middle_name=parsed.holder_middle_name,
                holder_last_name=parsed.holder_last_name,
                holder_type=parsed.holder_type,
                acct_holder_type=parsed.acct_holder_type,
                residence_country=parsed.residence_country,
                foreign_tin=parsed.foreign_tin,
                holder_address=parsed.holder_address,
                address_country=parsed.address_country,
                holder_street=parsed.holder_street,
                holder_building_identifier=parsed.holder_building_identifier,
                holder_suite_identifier=parsed.holder_suite_identifier,
                holder_floor_identifier=parsed.holder_floor_identifier,
                holder_district_name=parsed.holder_district_name,
                holder_pob=parsed.holder_pob,
                holder_post_code=parsed.holder_post_code,
                holder_city=parsed.holder_city,
                holder_country_subentity=parsed.holder_country_subentity,
                birth_date=parsed.birth_date,
                birth_city=parsed.birth_city,
                birth_country_code=parsed.birth_country_code,
                account_number=parsed.account_number,
                acct_number_type=parsed.acct_number_type,
                closed_account=parsed.closed_account,
                dormant_account=parsed.dormant_account,
                currency=parsed.currency or "NGN",
                balance=parsed.balance,
                dividends=parsed.dividends,
                interest=parsed.interest,
                gross_proceeds=parsed.gross_proceeds,
                other_income=parsed.other_income,
            )
            for cp in parsed.controlling_persons:
                ControllingPerson.objects.create(
                    account_report=record, name=cp.name, first_name=cp.first_name,
                    middle_name=cp.middle_name, last_name=cp.last_name,
                    residence_country=cp.residence_country, tin=cp.tin,
                    address=cp.address, city=cp.city, birth_date=cp.birth_date,
                    ctrlg_person_type=cp.ctrlg_person_type,
                )

        packages = build_packages(YEAR)
        assert packages, "the ingested records produced no exchange package"

        schema = xmlschema.XMLSchema("exchange/schemas/crs-v2.0/CrsXML_v2.0.xsd")
        for package in packages:
            schema.validate(package.xml_content)

        # The FI's DocRefId survived the whole journey.
        emitted = "\n".join(p.xml_content for p in packages)
        for parsed in result.records:
            assert parsed.doc_ref_id in emitted

        # Every AddressFix component in the sample was captured and re-emitted,
        # including the optional ones (suite, floor, district, POB).
        holder = result.records[0]
        assert holder.holder_suite_identifier == "SuiteIdentifier_AccountReport_ORG_1"
        assert holder.holder_floor_identifier == "FloorIdentifier_AccountReport_ORG_1"
        assert holder.holder_district_name == "DistrictName_AccountReport_ORG_1"
        assert holder.holder_pob == "POB_AccountReport_ORG_1"
        for value in (
            holder.holder_suite_identifier, holder.holder_floor_identifier,
            holder.holder_district_name, holder.holder_pob,
        ):
            assert value in emitted
