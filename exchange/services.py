"""Exchange domain services: deadlines, identifiers, packaging, XML, penalties."""
from __future__ import annotations

import datetime
from xml.sax.saxutils import escape

from django.utils import timezone

from core import config
from portal.models import AccountReport, Filing, ReportingFI


def domestic_deadline(year: int | None = None) -> datetime.date:
    """31 May of the year following the reporting year."""
    year = year or config.CURRENT_REPORTING_YEAR
    return datetime.date(year + 1, config.DOMESTIC_DEADLINE_MONTH, config.DOMESTIC_DEADLINE_DAY)


def exchange_deadline(year: int | None = None) -> datetime.date:
    """30 September of the year following the reporting year."""
    year = year or config.CURRENT_REPORTING_YEAR
    return datetime.date(year + 1, config.EXCHANGE_DEADLINE_MONTH, config.EXCHANGE_DEADLINE_DAY)


def days_to_domestic_deadline(year: int | None = None) -> int:
    """Days remaining to the domestic filing deadline. Negative when overdue."""
    return (domestic_deadline(year) - timezone.localdate()).days


def days_to_exchange_deadline(year: int | None = None) -> int:
    """Days remaining to the international exchange deadline."""
    return (exchange_deadline(year) - timezone.localdate()).days


def late_filing_penalty(deadline: datetime.date, today: datetime.date | None = None) -> int:
    """Administrative penalty for a return not filed by the deadline.

    Per the CRS Regulations 2019 penalty regime: N10,000,000 for the first
    month of default and N1,000,000 for each subsequent month (any part of a
    month counts as a month). Returns 0 when the deadline has not passed.
    """
    today = today or timezone.localdate()
    if today <= deadline:
        return 0
    months = 0
    marker = deadline
    while marker < today:
        months += 1
        # Advance one calendar month, clamping the day to the month end.
        month = marker.month % 12 + 1
        year = marker.year + (1 if marker.month == 12 else 0)
        try:
            marker = marker.replace(year=year, month=month)
        except ValueError:
            marker = datetime.date(year, month, 28)
    return 10_000_000 + (months - 1) * 1_000_000


def next_message_ref_id(jurisdiction_code: str, year: int) -> str:
    """Build a MessageRefID: sending country + year + receiving country + id.

    Example: NG2025GB000123. Uniqueness is guaranteed by a running sequence
    across all packages for that corridor and year.
    """
    from exchange.models import ExchangePackage

    prefix = f"{config.SENDING_JURISDICTION}{year}{jurisdiction_code}"
    existing = ExchangePackage.objects.filter(message_ref_id__startswith=prefix).count()
    sequence = existing + 1
    candidate = f"{prefix}{sequence:06d}"
    while ExchangePackage.objects.filter(message_ref_id=candidate).exists():
        sequence += 1
        candidate = f"{prefix}{sequence:06d}"
    return candidate


def next_doc_ref_id(rfi: ReportingFI, year: int) -> str:
    """Build a DocRefID unique across the platform.

    Format: NG + year + RFI reference + running sequence, per the CRS XML
    User Guide requirement that DocRefID be unique in space and time.
    """
    prefix = f"NG{year}-{rfi.reference}-"
    existing = AccountReport.objects.filter(doc_ref_id__startswith=prefix).count()
    sequence = existing + 1
    candidate = f"{prefix}{sequence:06d}"
    while AccountReport.objects.filter(doc_ref_id=candidate).exists():
        sequence += 1
        candidate = f"{prefix}{sequence:06d}"
    return candidate


def build_packages(year: int) -> list:
    """Sort approved records by residence jurisdiction and build one CRS701
    package per activated partner per year.

    Only records from Accepted filings, destined for activated partners, and
    not already packaged are included. Filings whose records are all packaged
    move to Included in Exchange. Corrections (CRS702) are built by
    correct_package and nil returns (CRS703) by build_nil_packages.
    """
    from exchange.models import ExchangePackage, PartnerJurisdiction

    partners = {p.code: p for p in PartnerJurisdiction.objects.all()}
    eligible = AccountReport.objects.filter(
        filing__status=Filing.Status.ACCEPTED,
        filing__reporting_year=year,
        residence_country__in=partners.keys(),
        superseded=False,
        packages__isnull=True,
    ).select_related("filing__rfi")

    by_country: dict[str, list[AccountReport]] = {}
    for record in eligible:
        by_country.setdefault(record.residence_country, []).append(record)

    packages = []
    for code, records in sorted(by_country.items()):
        package = ExchangePackage.objects.create(
            jurisdiction=partners[code],
            reporting_year=year,
            message_ref_id=next_message_ref_id(code, year),
            message_type="CRS701",
        )
        package.records.set(records)
        package.xml_content = generate_crs_xml(package)
        package.save(update_fields=["xml_content"])
        packages.append(package)

    # Any accepted filing with every live (non-superseded) record now
    # packaged is in exchange.
    for filing in Filing.objects.filter(status=Filing.Status.ACCEPTED, reporting_year=year):
        live = filing.account_reports.filter(superseded=False)
        if live.exists() and not live.filter(packages__isnull=True).exists():
            filing.status = Filing.Status.IN_EXCHANGE
            filing.save(update_fields=["status"])

    return packages


def build_nil_packages(year: int) -> list:
    """Build a CRS703 nil return for each activated partner with nothing to
    exchange for the year.

    A corridor qualifies when it has no package of any type for the year and
    no accepted records awaiting packaging. The nil message is MessageSpec
    only — the generator omits SendingCompanyIN and CrsBody, which is what
    marks a CA-level nil return. Accepted nil filings for the year move to
    Included in Exchange once the year's nil messages are built.
    """
    from exchange.models import ExchangePackage, PartnerJurisdiction

    awaiting = set(
        AccountReport.objects.filter(
            filing__status=Filing.Status.ACCEPTED,
            filing__reporting_year=year,
            superseded=False,
            packages__isnull=True,
        ).values_list("residence_country", flat=True)
    )
    packages = []
    for partner in PartnerJurisdiction.objects.order_by("code"):
        if partner.code in awaiting:
            continue
        if ExchangePackage.objects.filter(jurisdiction=partner, reporting_year=year).exists():
            continue
        package = ExchangePackage.objects.create(
            jurisdiction=partner,
            reporting_year=year,
            message_ref_id=next_message_ref_id(partner.code, year),
            message_type="CRS703",
        )
        package.xml_content = generate_crs_xml(package)
        package.save(update_fields=["xml_content"])
        packages.append(package)

    if packages:
        Filing.objects.filter(
            status=Filing.Status.ACCEPTED, reporting_year=year, kind=Filing.Kind.NIL
        ).update(status=Filing.Status.IN_EXCHANGE)
    return packages


# CRS XML Schema v2.0 namespaces, per the "Schema version" section of the CRS
# XML Schema User Guide (v3.0, June 2019). Note that User Guide version 3.0
# documents *schema* version 2.0 — there is no urn:oecd:ties:crs:v3.
NS_CRS = "urn:oecd:ties:crs:v2"
NS_CFC = "urn:oecd:ties:commontypesfatcacrs:v2"
NS_STF = "urn:oecd:ties:crsstf:v5"
NS_ISO = "urn:oecd:ties:isocrstypes:v1"

# DocSpec_Type and its children live in the stf namespace, not crs.
#
# CASING, settled: the User Guide prose writes "DocRefID"/"MessageRefID" but
# the published schema files (exchange/schemas/crs-v2.0/) declare DocRefId and
# CorrDocRefId (oecdcrstypes_v5.0.xsd) and MessageRefId (CrsXML_v2.0.xsd).
# The emitter follows the XSD; test_crs_conformance validates the generated
# XML against those schema files.


def _address_block(
    indent: str,
    country: str,
    address_free: str,
    lines: list,
    *,
    street: str = "",
    building: str = "",
    post_code: str = "",
    city: str = "",
    country_subentity: str = "",
) -> None:
    """Emit a CRS Address element.

    AddressFix is used whenever the address components are known, because the
    User Guide (IId) directs that it "should be used for all CRS reporting
    unless the reporting FI ... cannot define the various parts of the account
    holder's address". City is the sole Validation element inside AddressFix,
    so its absence forces the AddressFree fallback.
    """
    add = lines.append
    add(f"{indent}<crs:Address>")
    add(f"{indent}  <cfc:CountryCode>{country}</cfc:CountryCode>")
    if city.strip():
        add(f"{indent}  <cfc:AddressFix>")
        if street.strip():
            add(f"{indent}    <cfc:Street>{escape(street)}</cfc:Street>")
        if building.strip():
            add(f"{indent}    <cfc:BuildingIdentifier>{escape(building)}</cfc:BuildingIdentifier>")
        if post_code.strip():
            add(f"{indent}    <cfc:PostCode>{escape(post_code)}</cfc:PostCode>")
        add(f"{indent}    <cfc:City>{escape(city)}</cfc:City>")
        if country_subentity.strip():
            add(f"{indent}    <cfc:CountrySubentity>{escape(country_subentity)}</cfc:CountrySubentity>")
        add(f"{indent}  </cfc:AddressFix>")
    else:
        add(f"{indent}  <cfc:AddressFree>{escape(address_free or 'Address not provided')}</cfc:AddressFree>")
    add(f"{indent}</crs:Address>")


def _name_block(indent: str, first_name: str, last_name: str, lines: list) -> None:
    """Emit a CRS NamePerson_Type.

    FirstName and LastName are both Validation elements (User Guide IIc), so
    both are always written; `crs_name_parts` guarantees non-empty values.
    """
    add = lines.append
    add(f"{indent}<crs:Name>")
    add(f"{indent}  <crs:FirstName>{escape(first_name)}</crs:FirstName>")
    add(f"{indent}  <crs:LastName>{escape(last_name)}</crs:LastName>")
    add(f"{indent}</crs:Name>")


def _birth_block(
    indent: str,
    birth_date,
    birth_city: str,
    lines: list,
    *,
    city_subentity: str = "",
    country_code: str = "",
    former_country_name: str = "",
) -> None:
    """Emit a CRS BirthInfo element when a date of birth is held.

    Where a place of birth is reported the User Guide (IIf) asks for
    CountryInfo — a choice of current jurisdiction code or former jurisdiction
    name — alongside the city.
    """
    if not birth_date:
        return
    add = lines.append
    add(f"{indent}<crs:BirthInfo>")
    add(f"{indent}  <crs:BirthDate>{birth_date:%Y-%m-%d}</crs:BirthDate>")
    if birth_city:
        add(f"{indent}  <crs:City>{escape(birth_city)}</crs:City>")
    if city_subentity:
        add(f"{indent}  <crs:CitySubentity>{escape(city_subentity)}</crs:CitySubentity>")
    if birth_city and (country_code or former_country_name):
        add(f"{indent}  <crs:CountryInfo>")
        if country_code:
            add(f"{indent}    <crs:CountryCode>{country_code}</crs:CountryCode>")
        else:
            add(f"{indent}    <crs:FormerCountryName>{escape(former_country_name)}</crs:FormerCountryName>")
        add(f"{indent}  </crs:CountryInfo>")
    add(f"{indent}</crs:BirthInfo>")


def reporting_fi_doc_ref_id(rfi: ReportingFI, year: int, receiving_country: str) -> str:
    """The DocRefID for a ReportingFI element in a given corridor and year.

    Deterministic so that a CRS702 correction can resend the ReportingFI with
    the *same* DocRefID under DocTypeIndic OECD0, as the correction guidance
    requires. The receiving country is part of the identifier so the same FI
    reported to two partners does not reuse one DocRefID.
    """
    return f"NG{year}{receiving_country}-{rfi.reference}-FI"


def generate_crs_xml(package) -> str:
    """Render the CRS OECD XML body for a package.

    Conforms to the CRS XML Schema v2.0 as documented by the CRS XML Schema
    User Guide v3.0 (June 2019): one MessageSpec, then per reporting FI a
    ReportingFI element with its AccountReport blocks. Each AccountHolder
    carries a mandatory Address; individuals carry a FirstName/LastName pair
    and BirthInfo, entities carry a sibling AcctHolderType element, and Passive
    NFEs carry ControllingPerson blocks.

    Message types:
      CRS701  new data — ReportingFI DocTypeIndic OECD1
      CRS702  corrections — records carry CorrDocRefId under OECD2/OECD3 and
              the ReportingFI is resent under OECD0 with an unchanged DocRefId
      CRS703  nil return — MessageSpec only, with CrsBody and SendingCompanyIN
              both omitted
    """
    is_nil_return = package.message_type == "CRS703"
    is_correction = package.message_type == "CRS702"
    # A nil return is MessageSpec-only; rendering one with records attached
    # would silently drop their data while they count as packaged.
    if is_nil_return and package.records.exists():
        raise ValueError("A CRS703 nil return must not carry account records.")

    lines: list[str] = []
    add = lines.append
    add('<?xml version="1.0" encoding="UTF-8"?>')
    add(
        f'<crs:CRS_OECD version="2.0" xmlns:crs="{NS_CRS}" xmlns:cfc="{NS_CFC}"'
        f' xmlns:stf="{NS_STF}" xmlns:iso="{NS_ISO}">'
    )
    add("  <crs:MessageSpec>")
    # A nil return between Competent Authorities omits both SendingCompanyIN
    # and CrsBody; that pairing is what marks it as a CA-level nil return
    # rather than a domestic one.
    if not is_nil_return:
        add(f"    <crs:SendingCompanyIN>{escape(config.NRS_SENDING_COMPANY_IN)}</crs:SendingCompanyIN>")
    add(f"    <crs:TransmittingCountry>{config.SENDING_JURISDICTION}</crs:TransmittingCountry>")
    add(f"    <crs:ReceivingCountry>{package.jurisdiction.code}</crs:ReceivingCountry>")
    add("    <crs:MessageType>CRS</crs:MessageType>")
    add(f"    <crs:Contact>{escape(config.NRS_CONTACT)}</crs:Contact>")
    add(f"    <crs:MessageRefId>{package.message_ref_id}</crs:MessageRefId>")
    add(f"    <crs:MessageTypeIndic>{package.message_type}</crs:MessageTypeIndic>")
    add(f"    <crs:ReportingPeriod>{package.reporting_year}-12-31</crs:ReportingPeriod>")
    add(f"    <crs:Timestamp>{timezone.now():%Y-%m-%dT%H:%M:%S}</crs:Timestamp>")
    add("  </crs:MessageSpec>")

    if is_nil_return:
        add("</crs:CRS_OECD>")
        return "\n".join(lines)

    by_rfi: dict[int, list] = {}
    rfis: dict[int, ReportingFI] = {}
    fi_filing: dict[int, object] = {}
    records_qs = (
        package.records.select_related("filing__rfi")
        .prefetch_related("controlling_persons")
        .order_by("doc_ref_id")
    )
    for record in records_qs:
        rfi = record.filing.rfi
        by_rfi.setdefault(rfi.pk, []).append(record)
        rfis[rfi.pk] = rfi
        fi_filing.setdefault(rfi.pk, record.filing)

    for rfi_pk, records in by_rfi.items():
        rfi = rfis[rfi_pk]
        filing = fi_filing[rfi_pk]
        # The RFI's own Sending Company IN captured on the filing (falls back to
        # the RFI TIN) is carried at ReportingFI level.
        fi_in = (filing.sending_company_in or rfi.tin).strip() or rfi.tin
        add("  <crs:CrsBody>")
        add("    <crs:ReportingFI>")
        add(f"      <crs:ResCountryCode>{config.SENDING_JURISDICTION}</crs:ResCountryCode>")
        add(f'      <crs:IN issuedBy="NG">{escape(fi_in)}</crs:IN>')
        add(f"      <crs:Name>{escape(rfi.legal_name)}</crs:Name>")
        _address_block(
            "      ",
            config.SENDING_JURISDICTION,
            rfi.full_address,
            lines,
            street=rfi.street,
            post_code=rfi.post_code,
            city=rfi.city,
            country_subentity=rfi.state_province,
        )
        # In a correction message the ReportingFI must always be resent, even
        # when unmodified, under DocTypeIndic OECD0 and with the *same*
        # DocRefID as the immediately preceding version. OECD1 here would make
        # the message an invalid DocTypeIndic combination and be rejected.
        add("      <crs:DocSpec>")
        add(f"        <stf:DocTypeIndic>{'OECD0' if is_correction else 'OECD1'}</stf:DocTypeIndic>")
        add(
            "        <stf:DocRefId>"
            f"{reporting_fi_doc_ref_id(rfi, package.reporting_year, package.jurisdiction.code)}"
            "</stf:DocRefId>"
        )
        add("      </crs:DocSpec>")
        add("    </crs:ReportingFI>")
        add("    <crs:ReportingGroup>")
        for record in records:
            add("      <crs:AccountReport>")
            add("        <crs:DocSpec>")
            add(f"          <stf:DocTypeIndic>{record.doc_type_indic}</stf:DocTypeIndic>")
            add(f"          <stf:DocRefId>{record.doc_ref_id}</stf:DocRefId>")
            if record.corr_doc_ref_id:
                add(f"          <stf:CorrDocRefId>{record.corr_doc_ref_id}</stf:CorrDocRefId>")
            add("        </crs:DocSpec>")

            attrs = ""
            if record.acct_number_type:
                attrs += f' AcctNumberType="{record.acct_number_type}"'
            if record.is_undocumented:
                attrs += ' UndocumentedAccount="true"'
            if record.closed_account:
                attrs += ' ClosedAccount="true"'
            if record.dormant_account:
                attrs += ' DormantAccount="true"'
            add(f'        <crs:AccountNumber{attrs}>{escape(record.account_number)}</crs:AccountNumber>')

            add("        <crs:AccountHolder>")
            if record.holder_type == "INDIVIDUAL":
                first_name, last_name = record.crs_name
                add("          <crs:Individual>")
                add(f"            <crs:ResCountryCode>{record.residence_country}</crs:ResCountryCode>")
                if record.foreign_tin:
                    add(f'            <crs:TIN issuedBy="{record.residence_country}">{escape(record.foreign_tin)}</crs:TIN>')
                _name_block("            ", first_name, last_name, lines)
                _address_block(
                    "            ", record.address_country_code, record.holder_address, lines,
                    street=record.holder_street, building=record.holder_building_identifier,
                    post_code=record.holder_post_code, city=record.holder_city,
                    country_subentity=record.holder_country_subentity,
                )
                _birth_block(
                    "            ", record.birth_date, record.birth_city, lines,
                    city_subentity=record.birth_city_subentity,
                    country_code=record.birth_country_code,
                    former_country_name=record.birth_former_country_name,
                )
                add("          </crs:Individual>")
            else:
                add("          <crs:Organisation>")
                add(f"            <crs:ResCountryCode>{record.residence_country}</crs:ResCountryCode>")
                if record.foreign_tin:
                    add(f'            <crs:IN issuedBy="{record.residence_country}">{escape(record.foreign_tin)}</crs:IN>')
                add(f"            <crs:Name>{escape(record.holder_name)}</crs:Name>")
                _address_block(
                    "            ", record.address_country_code, record.holder_address, lines,
                    street=record.holder_street, building=record.holder_building_identifier,
                    post_code=record.holder_post_code, city=record.holder_city,
                    country_subentity=record.holder_country_subentity,
                )
                add("          </crs:Organisation>")
                # AcctHolderType is a sibling element of Organisation within
                # AccountHolder, not an attribute on it.
                if record.acct_holder_type:
                    add(f"          <crs:AcctHolderType>{record.acct_holder_type}</crs:AcctHolderType>")
            add("        </crs:AccountHolder>")

            # Controlling persons for Passive NFEs.
            for cp in record.controlling_persons.all():
                cp_first, cp_last = cp.crs_name
                add("        <crs:ControllingPerson>")
                add("          <crs:Individual>")
                add(f"            <crs:ResCountryCode>{cp.residence_country}</crs:ResCountryCode>")
                if cp.tin:
                    add(f'            <crs:TIN issuedBy="{cp.residence_country}">{escape(cp.tin)}</crs:TIN>')
                _name_block("            ", cp_first, cp_last, lines)
                _address_block(
                    "            ", cp.address_country_code, cp.address, lines, city=cp.city,
                )
                _birth_block(
                    "            ", cp.birth_date, cp.birth_city, lines,
                    country_code=cp.birth_country_code,
                )
                add("          </crs:Individual>")
                add(f"          <crs:CtrlgPersonType>{cp.ctrlg_person_type}</crs:CtrlgPersonType>")
                add("        </crs:ControllingPerson>")

            add(f'        <crs:AccountBalance currCode="{record.currency}">{record.balance}</crs:AccountBalance>')
            for element, value in (
                ("CRS501", record.dividends),
                ("CRS502", record.interest),
                ("CRS503", record.gross_proceeds),
                ("CRS504", record.other_income),
            ):
                if value:
                    add(f'        <crs:Payment><crs:Type>{element}</crs:Type><crs:PaymentAmnt currCode="{record.currency}">{value}</crs:PaymentAmnt></crs:Payment>')
            add("      </crs:AccountReport>")
        add("    </crs:ReportingGroup>")
        add("  </crs:CrsBody>")
    add("</crs:CRS_OECD>")
    return "\n".join(lines)
