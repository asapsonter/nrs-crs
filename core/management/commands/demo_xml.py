"""Generate fresh CRS demo XML files with never-used identifiers.

Every run mints a new token, so DocRefIds and MessageRefIds can never
collide with anything previously filed — no more used-up demo examples.
Each jurisdiction gets a CRS701 new-data file (three accounts: an
individual, an entity, and a Passive NFE with a controlling person) and a
matching CRS702 amendment (one correction, one deletion) referencing it.

    python manage.py demo_xml                    # GB and FR into ./demo_xml/
    python manage.py demo_xml --jurisdictions GB # one jurisdiction
"""
from __future__ import annotations

import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import config

YEAR = config.CURRENT_REPORTING_YEAR

_HOLDERS = {
    # Per-jurisdiction demo cast so files look real on screen.
    "GB": (
        ("Adaeze", "Okonkwo", "1984-05-12", "London", "QQ123456C", "14 Cheapside", "London", "EC2V 6AA"),
        ("Thames Gate Holdings Ltd", "8734921650", "1 Poultry", "London", "EC2R 8EJ"),
        ("Harcourt Estate Partners LLP", "5561092837", "22 Bishopsgate", "London", "EC2N 4AJ"),
        ("Oluwaseun", "Adebayo", "1975-11-03", "Manchester", "AB446677D"),
    ),
    "FR": (
        ("Chinelo", "Eze", "1990-02-27", "Paris", "1900227123456", "18 Rue de Rivoli", "Paris", "75004"),
        ("Loire Capital SARL", "552081317", "9 Avenue Montaigne", "Paris", "75008"),
        ("Fontaine Trust Associés", "775665019", "31 Rue la Boétie", "Paris", "75008"),
        ("Ibrahim", "Danjuma", "1968-07-19", "Lyon", "1680719654321"),
    ),
}
_DEFAULT_CAST = _HOLDERS["GB"]


def _message_spec(receiving: str, message_ref: str, indic: str) -> str:
    return f"""  <crs:MessageSpec>
    <crs:TransmittingCountry>NG</crs:TransmittingCountry>
    <crs:ReceivingCountry>{receiving}</crs:ReceivingCountry>
    <crs:MessageType>CRS</crs:MessageType>
    <crs:MessageRefId>{message_ref}</crs:MessageRefId>
    <crs:MessageTypeIndic>{indic}</crs:MessageTypeIndic>
    <crs:ReportingPeriod>{YEAR}-12-31</crs:ReportingPeriod>
    <crs:Timestamp>{YEAR + 1}-03-31T09:00:00</crs:Timestamp>
  </crs:MessageSpec>"""


def _reporting_fi(fi_doc_ref: str, doc_type: str) -> str:
    return f"""    <crs:ReportingFI>
      <crs:ResCountryCode>NG</crs:ResCountryCode>
      <crs:IN issuedBy="NG">0450088801</crs:IN>
      <crs:Name>Zenith Trust Bank Plc</crs:Name>
      <crs:Address>
        <cfc:CountryCode>NG</cfc:CountryCode>
        <cfc:AddressFix>
          <cfc:Street>12 Broad Street</cfc:Street>
          <cfc:PostCode>100001</cfc:PostCode>
          <cfc:City>Lagos</cfc:City>
          <cfc:CountrySubentity>Lagos</cfc:CountrySubentity>
        </cfc:AddressFix>
      </crs:Address>
      <crs:DocSpec>
        <stf:DocTypeIndic>{doc_type}</stf:DocTypeIndic>
        <stf:DocRefId>{fi_doc_ref}</stf:DocRefId>
      </crs:DocSpec>
    </crs:ReportingFI>"""


def _doc_spec(doc_ref: str, doc_type: str, corr: str = "") -> str:
    corr_line = f"\n          <stf:CorrDocRefId>{corr}</stf:CorrDocRefId>" if corr else ""
    return f"""        <crs:DocSpec>
          <stf:DocTypeIndic>{doc_type}</stf:DocTypeIndic>
          <stf:DocRefId>{doc_ref}</stf:DocRefId>{corr_line}
        </crs:DocSpec>"""


def _individual(code: str, first: str, last: str, dob: str, birth_city: str, tin: str,
                street: str, city: str, post_code: str) -> str:
    return f"""          <crs:Individual>
            <crs:ResCountryCode>{code}</crs:ResCountryCode>
            <crs:TIN issuedBy="{code}">{tin}</crs:TIN>
            <crs:Name>
              <crs:FirstName>{first}</crs:FirstName>
              <crs:LastName>{last}</crs:LastName>
            </crs:Name>
            <crs:Address>
              <cfc:CountryCode>{code}</cfc:CountryCode>
              <cfc:AddressFix>
                <cfc:Street>{street}</cfc:Street>
                <cfc:PostCode>{post_code}</cfc:PostCode>
                <cfc:City>{city}</cfc:City>
              </cfc:AddressFix>
            </crs:Address>
            <crs:BirthInfo>
              <crs:BirthDate>{dob}</crs:BirthDate>
              <crs:City>{birth_city}</crs:City>
              <crs:CountryInfo><crs:CountryCode>{code}</crs:CountryCode></crs:CountryInfo>
            </crs:BirthInfo>
          </crs:Individual>"""


def _organisation(code: str, name: str, entity_in: str, street: str, city: str, post_code: str) -> str:
    return f"""          <crs:Organisation>
            <crs:ResCountryCode>{code}</crs:ResCountryCode>
            <crs:IN issuedBy="{code}">{entity_in}</crs:IN>
            <crs:Name>{name}</crs:Name>
            <crs:Address>
              <cfc:CountryCode>{code}</cfc:CountryCode>
              <cfc:AddressFix>
                <cfc:Street>{street}</cfc:Street>
                <cfc:PostCode>{post_code}</cfc:PostCode>
                <cfc:City>{city}</cfc:City>
              </cfc:AddressFix>
            </crs:Address>
          </crs:Organisation>"""


def _payment(payment_type: str, currency: str, amount: str) -> str:
    return f"""        <crs:Payment>
          <crs:Type>{payment_type}</crs:Type>
          <crs:PaymentAmnt currCode="{currency}">{amount}</crs:PaymentAmnt>
        </crs:Payment>"""


def _document(message_spec: str, reporting_fi: str, account_reports: list[str]) -> str:
    reports = "\n".join(account_reports)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<crs:CRS_OECD xmlns:crs="urn:oecd:ties:crs:v2" xmlns:cfc="urn:oecd:ties:commontypesfatcacrs:v2" xmlns:stf="urn:oecd:ties:crsstf:v5" xmlns:iso="urn:oecd:ties:isocrstypes:v1" version="2.0">
{message_spec}
  <crs:CrsBody>
{reporting_fi}
    <crs:ReportingGroup>
{reports}
    </crs:ReportingGroup>
  </crs:CrsBody>
</crs:CRS_OECD>"""


def build_new_file(code: str, token: str) -> tuple[str, list[str]]:
    """The CRS701 new-data document. Returns (xml, record DocRefIds)."""
    cast = _HOLDERS.get(code, _DEFAULT_CAST)
    ind, org, nfe, cp = cast
    currency = "GBP" if code == "GB" else "EUR"
    doc_refs = [f"NG{YEAR}-DEMO{token}-{code}-{n}" for n in (1, 2, 3)]
    fi_ref = f"NG{YEAR}-DEMO{token}-{code}-FI"

    reports = [
        # An individual depository account with interest.
        f"""      <crs:AccountReport>
{_doc_spec(doc_refs[0], "OECD1")}
        <crs:AccountNumber AcctNumberType="OECD601">{code}29DEMO{token}0001</crs:AccountNumber>
        <crs:AccountHolder>
{_individual(code, ind[0], ind[1], ind[2], ind[3], ind[4], ind[5], ind[6], ind[7])}
        </crs:AccountHolder>
        <crs:AccountBalance currCode="{currency}">8425000.50</crs:AccountBalance>
{_payment("CRS502", currency, "112300.25")}
      </crs:AccountReport>""",
        # A reportable entity (CRS102) custodial account with dividends.
        f"""      <crs:AccountReport>
{_doc_spec(doc_refs[1], "OECD1")}
        <crs:AccountNumber AcctNumberType="OECD605">DEMO-{token}-0002</crs:AccountNumber>
        <crs:AccountHolder>
{_organisation(code, org[0], org[1], org[2], org[3], org[4])}
          <crs:AcctHolderType>CRS102</crs:AcctHolderType>
        </crs:AccountHolder>
        <crs:AccountBalance currCode="{currency}">64100000.00</crs:AccountBalance>
{_payment("CRS501", currency, "1975000.00")}
      </crs:AccountReport>""",
        # A Passive NFE (CRS101) with a reportable controlling person.
        f"""      <crs:AccountReport>
{_doc_spec(doc_refs[2], "OECD1")}
        <crs:AccountNumber AcctNumberType="OECD605">DEMO-{token}-0003</crs:AccountNumber>
        <crs:AccountHolder>
{_organisation(code, nfe[0], nfe[1], nfe[2], nfe[3], nfe[4])}
          <crs:AcctHolderType>CRS101</crs:AcctHolderType>
        </crs:AccountHolder>
        <crs:ControllingPerson>
{_individual(code, cp[0], cp[1], cp[2], cp[3], cp[4], "5 Demo Close", "Demo Town", "DM1 1AA").replace("crs:Individual", "crs:Individual")}
          <crs:CtrlgPersonType>CRS801</crs:CtrlgPersonType>
        </crs:ControllingPerson>
        <crs:AccountBalance currCode="{currency}">23800000.00</crs:AccountBalance>
{_payment("CRS503", currency, "540000.00")}
      </crs:AccountReport>""",
    ]
    xml = _document(
        _message_spec(code, f"NG{YEAR}{code}DEMO{token}", "CRS701"),
        _reporting_fi(fi_ref, "OECD1"),
        reports,
    )
    return xml, doc_refs


def build_amended_file(code: str, token: str, new_doc_refs: list[str]) -> str:
    """The CRS702 amendment: corrects record 1, deletes record 3."""
    cast = _HOLDERS.get(code, _DEFAULT_CAST)
    ind, _, nfe, cp = cast
    currency = "GBP" if code == "GB" else "EUR"
    fi_ref = f"NG{YEAR}-DEMO{token}-{code}-FI"

    reports = [
        # Corrected balance and interest for the individual account.
        f"""      <crs:AccountReport>
{_doc_spec(f"NG{YEAR}-DEMO{token}-{code}-11", "OECD2", new_doc_refs[0])}
        <crs:AccountNumber AcctNumberType="OECD601">{code}29DEMO{token}0001</crs:AccountNumber>
        <crs:AccountHolder>
{_individual(code, ind[0], ind[1], ind[2], ind[3], ind[4], ind[5], ind[6], ind[7])}
        </crs:AccountHolder>
        <crs:AccountBalance currCode="{currency}">9126400.75</crs:AccountBalance>
{_payment("CRS502", currency, "148750.00")}
      </crs:AccountReport>""",
        # Deletion of the Passive NFE account, resent as reported.
        f"""      <crs:AccountReport>
{_doc_spec(f"NG{YEAR}-DEMO{token}-{code}-13", "OECD3", new_doc_refs[2])}
        <crs:AccountNumber AcctNumberType="OECD605">DEMO-{token}-0003</crs:AccountNumber>
        <crs:AccountHolder>
{_organisation(code, nfe[0], nfe[1], nfe[2], nfe[3], nfe[4])}
          <crs:AcctHolderType>CRS101</crs:AcctHolderType>
        </crs:AccountHolder>
        <crs:ControllingPerson>
{_individual(code, cp[0], cp[1], cp[2], cp[3], cp[4], "5 Demo Close", "Demo Town", "DM1 1AA")}
          <crs:CtrlgPersonType>CRS801</crs:CtrlgPersonType>
        </crs:ControllingPerson>
        <crs:AccountBalance currCode="{currency}">23800000.00</crs:AccountBalance>
{_payment("CRS503", currency, "540000.00")}
      </crs:AccountReport>""",
    ]
    return _document(
        _message_spec(code, f"NG{YEAR}{code}DEMO{token}A", "CRS702"),
        _reporting_fi(fi_ref, "OECD0"),
        reports,
    )


class Command(BaseCommand):
    help = "Generate fresh CRS demo XML files (new + amended per jurisdiction) with unused identifiers."

    def add_arguments(self, parser):
        parser.add_argument("--jurisdictions", default="GB,FR", help="Comma-separated receiving country codes")
        parser.add_argument("--out", default="demo_xml", help="Output directory")

    def handle(self, *args, **options):
        from portal.xml_ingest import parse_crs_upload

        codes = [c.strip().upper() for c in options["jurisdictions"].split(",") if c.strip()]
        activated = {code for code, *_ in config.PARTNER_JURISDICTIONS}
        for code in codes:
            if code not in activated:
                raise CommandError(f"{code} is not an activated partner jurisdiction.")

        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex[:8].upper()

        for code in codes:
            new_xml, doc_refs = build_new_file(code, token)
            amended_xml = build_amended_file(code, token, doc_refs)
            for label, xml in (("New", new_xml), ("Amended", amended_xml)):
                result = parse_crs_upload(xml.encode(), YEAR)
                if not result.ok:
                    raise CommandError(
                        f"Generated {label} {code} file failed its own validation: {result.errors[:3]}"
                    )
                path = out / f"DEMO_{YEAR}_{label}_CRS_XML_{code}_{token}.xml"
                path.write_text(xml, encoding="utf-8")
                self.stdout.write(
                    f"  {path}  ({len(result.records)} records"
                    f"{', ' + str(len(result.warnings)) + ' schema warnings' if result.warnings else ''})"
                )
        self.stdout.write(self.style.SUCCESS(
            f"Token {token}: upload the New files first, approve, then the Amended files."
        ))
