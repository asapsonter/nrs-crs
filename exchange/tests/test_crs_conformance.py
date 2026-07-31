"""Conformance of the generated XML to the CRS XML Schema v2.0.

Each test here pins one requirement of the CRS XML Schema User Guide v3.0
(June 2019), which documents *schema* version 2.0. Section references in the
test names point at the User Guide headings.
"""
from __future__ import annotations

from decimal import Decimal
from xml.etree import ElementTree

import pytest

from core import config
from exchange.models import ExchangePackage, PartnerJurisdiction
from exchange.services import NS_CFC, NS_CRS, NS_STF, build_packages, generate_crs_xml
from portal.models import AccountReport, ControllingPerson, Filing, ReportingFI

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR

NS = {"crs": NS_CRS, "cfc": NS_CFC, "stf": NS_STF}


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
        street="12 Broad Street",
        city="Lagos",
        state_province="Lagos",
        post_code="100001",
        pu_surname="Bello",
        pu_first_name="Ngozi",
        pu_designation="Head",
        pu_email="pu@zenithtrust.ng",
        pu_phone="8000000000",
    )


def _filing(rfi: ReportingFI) -> Filing:
    return Filing.objects.create(
        reference=f"FIL-{YEAR}-{Filing.objects.count() + 1:05d}",
        rfi=rfi,
        reporting_year=YEAR,
        kind=Filing.Kind.MANUAL,
        status=Filing.Status.ACCEPTED,
    )


def _record(filing: Filing, **overrides) -> AccountReport:
    fields = {
        "filing": filing,
        "doc_ref_id": f"NG{YEAR}-{filing.rfi.reference}-{AccountReport.objects.count() + 1:06d}",
        "holder_name": "Amina Yusuf",
        "residence_country": "GB",
        "foreign_tin": "GB-TIN-1",
        "account_number": "ACC000001",
        "balance": Decimal("500000.00"),
    }
    fields.update(overrides)
    return AccountReport.objects.create(**fields)


def _xml_for(rfi: ReportingFI, **overrides) -> str:
    _record(_filing(rfi), **overrides)
    return build_packages(YEAR)[0].xml_content


def _parse(xml: str) -> ElementTree.Element:
    """Parse and return the root, proving the document is well-formed and that
    every prefix used is actually declared."""
    return ElementTree.fromstring(xml)


class TestNamespaces:
    def test_root_declares_all_prefixes_it_uses(self, partners, rfi):
        xml = _xml_for(rfi)
        # An undeclared prefix is a parse error, so this asserts declaration.
        root = _parse(xml)
        assert root.tag == f"{{{NS_CRS}}}CRS_OECD"
        assert root.get("version") == "2.0"

    def test_targets_schema_v2_namespace(self, partners, rfi):
        xml = _xml_for(rfi)
        assert 'xmlns:crs="urn:oecd:ties:crs:v2"' in xml
        assert 'xmlns:stf="urn:oecd:ties:crsstf:v5"' in xml
        assert 'xmlns:cfc="urn:oecd:ties:commontypesfatcacrs:v2"' in xml


class TestDocSpec:
    """User Guide: DocSpec_Type — children belong to the stf namespace."""

    def test_doc_spec_children_are_stf_qualified(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        doc_spec = root.find(".//crs:AccountReport/crs:DocSpec", NS)
        assert doc_spec is not None
        assert doc_spec.find("stf:DocTypeIndic", NS) is not None
        assert doc_spec.find("stf:DocRefId", NS) is not None
        # The crs-qualified spelling must be gone.
        assert doc_spec.find("crs:DocTypeIndic", NS) is None


class TestNamePerson:
    """User Guide IIc — FirstName and LastName are both Validation elements."""

    def test_individual_carries_first_and_last_name(self, partners, rfi):
        root = _parse(_xml_for(rfi, holder_first_name="Amina", holder_last_name="Yusuf"))
        name = root.find(".//crs:Individual/crs:Name", NS)
        assert name.find("crs:FirstName", NS).text == "Amina"
        assert name.find("crs:LastName", NS).text == "Yusuf"

    def test_fullname_is_never_emitted(self, partners, rfi):
        assert "FullName" not in _xml_for(rfi)

    def test_unsplit_name_falls_back_to_nfn(self, partners, rfi):
        """A record holding only a single free-text name still yields both
        Validation elements, using the NFN placeholder the guide permits."""
        root = _parse(_xml_for(rfi, holder_name="Amina Yusuf"))
        name = root.find(".//crs:Individual/crs:Name", NS)
        assert name.find("crs:FirstName", NS).text == "NFN"
        assert name.find("crs:LastName", NS).text == "Amina Yusuf"


class TestAccountHolderType:
    """User Guide IVe — AcctHolderType is an element, not an attribute."""

    def test_entity_holder_type_is_a_sibling_element(self, partners, rfi):
        root = _parse(
            _xml_for(rfi, holder_type="ORGANISATION", acct_holder_type="CRS101",
                     holder_name="Acme Holdings Ltd")
        )
        holder = root.find(".//crs:AccountHolder", NS)
        assert holder.find("crs:Organisation", NS) is not None
        assert holder.find("crs:AcctHolderType", NS).text == "CRS101"
        assert holder.find("crs:Organisation", NS).get("crsAcctHolderType") is None


class TestAccountNumberAttributes:
    """User Guide IVd — ClosedAccount, DormantAccount, AcctNumberType."""

    def test_closed_account_attribute(self, partners, rfi):
        root = _parse(_xml_for(rfi, closed_account=True, balance=Decimal("0.00")))
        assert root.find(".//crs:AccountNumber", NS).get("ClosedAccount") == "true"

    def test_dormant_and_number_type_attributes(self, partners, rfi):
        root = _parse(_xml_for(rfi, dormant_account=True, acct_number_type="OECD601"))
        number = root.find(".//crs:AccountNumber", NS)
        assert number.get("DormantAccount") == "true"
        assert number.get("AcctNumberType") == "OECD601"

    def test_attributes_absent_when_not_set(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        number = root.find(".//crs:AccountNumber", NS)
        assert number.get("ClosedAccount") is None
        assert number.get("DormantAccount") is None


class TestAddress:
    """User Guide IId — AddressFix preferred, AddressFree as fallback."""

    def test_address_fix_used_when_components_known(self, partners, rfi):
        root = _parse(
            _xml_for(rfi, holder_street="10 High Street", holder_city="London",
                     holder_post_code="EC1A 1BB", holder_country_subentity="Greater London")
        )
        fix = root.find(".//crs:Individual/crs:Address/cfc:AddressFix", NS)
        assert fix is not None
        assert fix.find("cfc:City", NS).text == "London"
        assert fix.find("cfc:Street", NS).text == "10 High Street"
        assert fix.find("cfc:PostCode", NS).text == "EC1A 1BB"

    def test_address_free_fallback_without_city(self, partners, rfi):
        root = _parse(_xml_for(rfi, holder_address="Somewhere in London"))
        address = root.find(".//crs:Individual/crs:Address", NS)
        assert address.find("cfc:AddressFix", NS) is None
        assert address.find("cfc:AddressFree", NS).text == "Somewhere in London"

    def test_country_code_always_present(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        assert root.find(".//crs:Individual/crs:Address/cfc:CountryCode", NS).text == "GB"


class TestBirthInfo:
    """User Guide IIf — CountryInfo accompanies a reported place of birth."""

    def test_country_info_emitted_with_place_of_birth(self, partners, rfi):
        import datetime

        root = _parse(
            _xml_for(rfi, birth_date=datetime.date(1980, 5, 4), birth_city="Kano",
                     birth_country_code="NG")
        )
        birth = root.find(".//crs:BirthInfo", NS)
        assert birth.find("crs:BirthDate", NS).text == "1980-05-04"
        assert birth.find("crs:City", NS).text == "Kano"
        assert birth.find("crs:CountryInfo/crs:CountryCode", NS).text == "NG"

    def test_former_country_name_alternative(self, partners, rfi):
        import datetime

        root = _parse(
            _xml_for(rfi, birth_date=datetime.date(1975, 1, 1), birth_city="Leningrad",
                     birth_former_country_name="USSR")
        )
        info = root.find(".//crs:BirthInfo/crs:CountryInfo", NS)
        assert info.find("crs:FormerCountryName", NS).text == "USSR"
        assert info.find("crs:CountryCode", NS) is None

    def test_no_birth_info_without_a_date(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        assert root.find(".//crs:BirthInfo", NS) is None


class TestControllingPerson:
    def test_controlling_person_uses_person_party_name_pair(self, partners, rfi):
        record = _record(_filing(rfi), holder_type="ORGANISATION",
                         acct_holder_type="CRS101", holder_name="Acme Holdings Ltd")
        ControllingPerson.objects.create(
            account_report=record, first_name="John", last_name="Owner",
            residence_country="GB", tin="GB999", city="London",
            ctrlg_person_type="CRS801",
        )
        root = _parse(build_packages(YEAR)[0].xml_content)
        cp = root.find(".//crs:ControllingPerson", NS)
        name = cp.find("crs:Individual/crs:Name", NS)
        assert name.find("crs:FirstName", NS).text == "John"
        assert name.find("crs:LastName", NS).text == "Owner"
        assert cp.find("crs:CtrlgPersonType", NS).text == "CRS801"


class TestCorrectionMessages:
    """User Guide correction guidance — ReportingFI is resent under OECD0."""

    def test_reporting_fi_resent_as_oecd0_with_stable_doc_ref_id(self, partners, rfi):
        _record(_filing(rfi))
        original = build_packages(YEAR)[0]
        original_fi_ref = _parse(original.xml_content).find(
            ".//crs:ReportingFI/crs:DocSpec/stf:DocRefId", NS
        ).text

        correction = ExchangePackage.objects.create(
            jurisdiction=original.jurisdiction,
            reporting_year=YEAR,
            message_ref_id=f"NG{YEAR}GB000099",
            message_type="CRS702",
            corrects_package=original,
        )
        correction.records.set(original.records.all())
        root = _parse(generate_crs_xml(correction))

        doc_spec = root.find(".//crs:ReportingFI/crs:DocSpec", NS)
        assert doc_spec.find("stf:DocTypeIndic", NS).text == "OECD0"
        # Same DocRefID as the immediately preceding version of the element.
        assert doc_spec.find("stf:DocRefId", NS).text == original_fi_ref

    def test_new_data_message_uses_oecd1_for_reporting_fi(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        doc_spec = root.find(".//crs:ReportingFI/crs:DocSpec", NS)
        assert doc_spec.find("stf:DocTypeIndic", NS).text == "OECD1"

    def test_reporting_fi_doc_ref_id_differs_per_partner(self, partners, rfi):
        filing = _filing(rfi)
        _record(filing, residence_country="GB")
        _record(filing, residence_country="FR", account_number="ACC000002")
        refs = {
            _parse(package.xml_content)
            .find(".//crs:ReportingFI/crs:DocSpec/stf:DocRefId", NS)
            .text
            for package in build_packages(YEAR)
        }
        assert len(refs) == 2, "DocRefID must be unique in space and time"


class TestNilReturn:
    """User Guide I — CRS703 omits both CrsBody and SendingCompanyIN."""

    def test_nil_return_omits_crs_body_and_sending_company_in(self, partners, rfi):
        package = ExchangePackage.objects.create(
            jurisdiction=PartnerJurisdiction.objects.get(code="GB"),
            reporting_year=YEAR,
            message_ref_id=f"NG{YEAR}GB000500",
            message_type="CRS703",
        )
        root = _parse(generate_crs_xml(package))
        assert root.find("crs:MessageSpec/crs:MessageTypeIndic", NS).text == "CRS703"
        assert root.find("crs:CrsBody", NS) is None
        assert root.find("crs:MessageSpec/crs:SendingCompanyIN", NS) is None

    def test_new_data_message_keeps_sending_company_in(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        assert root.find("crs:MessageSpec/crs:SendingCompanyIN", NS) is not None


class TestMessageSpec:
    def test_header_elements_and_order(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        spec = root.find("crs:MessageSpec", NS)
        tags = [child.tag.split("}")[1] for child in spec]
        assert tags == [
            "SendingCompanyIN", "TransmittingCountry", "ReceivingCountry", "MessageType",
            "Contact", "MessageRefId", "MessageTypeIndic", "ReportingPeriod", "Timestamp",
        ]

    def test_reporting_period_is_last_day_of_year(self, partners, rfi):
        root = _parse(_xml_for(rfi))
        assert root.find("crs:MessageSpec/crs:ReportingPeriod", NS).text == f"{YEAR}-12-31"

    def test_corr_message_ref_id_never_emitted(self, partners, rfi):
        """Optional (non-CRS) — must not appear in a CRS file."""
        assert "CorrMessageRefId" not in _xml_for(rfi)


@pytest.fixture(scope="module")
def schema():
    xmlschema = pytest.importorskip("xmlschema")
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "schemas" / "crs-v2.0" / "CrsXML_v2.0.xsd"
    return xmlschema.XMLSchema(str(path))


class TestXsdValidation:
    """The generated XML validates against the published OECD schema files.

    This is the authoritative conformance check: CrsXML_v2.0.xsd and its
    imports in exchange/schemas/crs-v2.0/ are the OECD-issued schema package.
    """

    def test_individual_data_package_validates(self, schema, partners, rfi):
        import datetime

        xml = _xml_for(
            rfi,
            holder_first_name="Amina",
            holder_last_name="Yusuf",
            holder_address="1 Market St, London",
            holder_street="1 Market St",
            holder_city="London",
            birth_date=datetime.date(1980, 1, 1),
            birth_city="Lagos",
            birth_country_code="NG",
            acct_number_type="OECD601",
            closed_account=True,
            balance=Decimal("0.00"),
            currency="GBP",
        )
        schema.validate(xml)

    def test_organisation_with_controlling_person_validates(self, schema, partners, rfi):
        record = _record(
            _filing(rfi),
            holder_name="Alpha Holdings Ltd",
            holder_type="ORGANISATION",
            acct_holder_type="CRS101",
            holder_address="2 King Street, London",
            currency="GBP",
        )
        ControllingPerson.objects.create(
            account_report=record,
            name="John Owner",
            first_name="John",
            last_name="Owner",
            residence_country="GB",
            tin="GB-CP-1",
            address="3 Queen Street, London",
            city="London",
            ctrlg_person_type="CRS801",
        )
        schema.validate(build_packages(YEAR)[0].xml_content)

    def test_nil_return_validates(self, schema, partners):
        from exchange.services import build_nil_packages

        packages = build_nil_packages(YEAR)
        assert packages
        schema.validate(packages[0].xml_content)
