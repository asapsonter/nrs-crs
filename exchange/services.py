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


def build_packages(year: int, message_type: str = "CRS701") -> list:
    """Sort approved records by residence jurisdiction and build one package
    per activated partner per year.

    Only records from Accepted filings, destined for activated partners, and
    not already packaged are included. Filings whose records are all packaged
    move to Included in Exchange.
    """
    from exchange.models import ExchangePackage, PartnerJurisdiction

    partners = {p.code: p for p in PartnerJurisdiction.objects.all()}
    eligible = AccountReport.objects.filter(
        filing__status=Filing.Status.ACCEPTED,
        filing__reporting_year=year,
        residence_country__in=partners.keys(),
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
            message_type=message_type,
        )
        package.records.set(records)
        package.xml_content = generate_crs_xml(package)
        package.save(update_fields=["xml_content"])
        packages.append(package)

    # Any accepted filing with every record now packaged is in exchange.
    for filing in Filing.objects.filter(status=Filing.Status.ACCEPTED, reporting_year=year):
        if filing.account_reports.exists() and not filing.account_reports.filter(packages__isnull=True).exists():
            filing.status = Filing.Status.IN_EXCHANGE
            filing.save(update_fields=["status"])

    return packages


def _address_block(indent: str, country: str, address_free: str, lines: list) -> None:
    """Emit a CRS Address element (CountryCode plus a free-form address)."""
    add = lines.append
    add(f"{indent}<crs:Address>")
    add(f"{indent}  <cfc:CountryCode>{country}</cfc:CountryCode>")
    add(f"{indent}  <cfc:AddressFree>{escape(address_free or 'Address not provided')}</cfc:AddressFree>")
    add(f"{indent}</crs:Address>")


def _birth_block(indent: str, birth_date, birth_city: str, lines: list) -> None:
    """Emit a CRS BirthInfo element when a date of birth is held."""
    if not birth_date:
        return
    add = lines.append
    add(f"{indent}<crs:BirthInfo>")
    add(f"{indent}  <crs:BirthDate>{birth_date:%Y-%m-%d}</crs:BirthDate>")
    if birth_city:
        add(f"{indent}  <crs:City>{escape(birth_city)}</crs:City>")
    add(f"{indent}</crs:BirthInfo>")


def generate_crs_xml(package) -> str:
    """Render the CRS OECD XML body for a package.

    Structurally faithful to the CRS XML Schema v2.0: one MessageSpec, then per
    reporting FI a ReportingFI element with its AccountReport blocks. Each
    AccountHolder carries a mandatory Address; individuals carry BirthInfo,
    entities carry an AcctHolderType, and Passive NFEs carry ControllingPerson
    blocks. Correction records carry CorrDocRefID and their DocTypeIndic."""
    lines: list[str] = []
    add = lines.append
    add('<?xml version="1.0" encoding="UTF-8"?>')
    add('<crs:CRS_OECD version="2.0" xmlns:crs="urn:oecd:ties:crs:v2" xmlns:cfc="urn:oecd:ties:commontypesfatcacrs:v2">')
    add("  <crs:MessageSpec>")
    add(f"    <crs:SendingCompanyIN>{escape(config.NRS_SENDING_COMPANY_IN)}</crs:SendingCompanyIN>")
    add(f"    <crs:TransmittingCountry>{config.SENDING_JURISDICTION}</crs:TransmittingCountry>")
    add(f"    <crs:ReceivingCountry>{package.jurisdiction.code}</crs:ReceivingCountry>")
    add("    <crs:MessageType>CRS</crs:MessageType>")
    add(f"    <crs:Contact>{escape(config.NRS_CONTACT)}</crs:Contact>")
    add(f"    <crs:MessageRefId>{package.message_ref_id}</crs:MessageRefId>")
    message_type_indic = "CRS701" if package.message_type == "CRS701" else "CRS702"
    add(f"    <crs:MessageTypeIndic>{message_type_indic}</crs:MessageTypeIndic>")
    add(f"    <crs:ReportingPeriod>{package.reporting_year}-12-31</crs:ReportingPeriod>")
    add(f"    <crs:Timestamp>{timezone.now():%Y-%m-%dT%H:%M:%S}</crs:Timestamp>")
    add("  </crs:MessageSpec>")

    by_rfi: dict[int, list] = {}
    rfis: dict[int, ReportingFI] = {}
    fi_filing: dict[int, object] = {}
    for record in package.records.select_related("filing__rfi").order_by("doc_ref_id"):
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
        _address_block("      ", config.SENDING_JURISDICTION, rfi.full_address, lines)
        add("      <crs:DocSpec>")
        add("        <crs:DocTypeIndic>OECD1</crs:DocTypeIndic>")
        add(f"        <crs:DocRefId>NG{package.reporting_year}-{rfi.reference}-FI</crs:DocRefId>")
        add("      </crs:DocSpec>")
        add("    </crs:ReportingFI>")
        add("    <crs:ReportingGroup>")
        for record in records:
            add("      <crs:AccountReport>")
            add("        <crs:DocSpec>")
            add(f"          <crs:DocTypeIndic>{record.doc_type_indic}</crs:DocTypeIndic>")
            add(f"          <crs:DocRefId>{record.doc_ref_id}</crs:DocRefId>")
            if record.corr_doc_ref_id:
                add(f"          <crs:CorrDocRefId>{record.corr_doc_ref_id}</crs:CorrDocRefId>")
            add("        </crs:DocSpec>")
            undoc = ' UndocumentedAccount="true"' if record.is_undocumented else ""
            add(f'        <crs:AccountNumber{undoc}>{escape(record.account_number)}</crs:AccountNumber>')
            add("        <crs:AccountHolder>")
            if record.holder_type == "INDIVIDUAL":
                add("          <crs:Individual>")
                add(f"            <crs:ResCountryCode>{record.residence_country}</crs:ResCountryCode>")
                if record.foreign_tin:
                    add(f'            <crs:TIN issuedBy="{record.residence_country}">{escape(record.foreign_tin)}</crs:TIN>')
                add(f"            <crs:Name><crs:FullName>{escape(record.holder_name)}</crs:FullName></crs:Name>")
                _address_block("            ", record.address_country_code, record.holder_address, lines)
                _birth_block("            ", record.birth_date, record.birth_city, lines)
                add("          </crs:Individual>")
            else:
                holder_type_attr = f' crsAcctHolderType="{record.acct_holder_type}"' if record.acct_holder_type else ""
                add(f"          <crs:Organisation{holder_type_attr}>")
                add(f"            <crs:ResCountryCode>{record.residence_country}</crs:ResCountryCode>")
                if record.foreign_tin:
                    add(f'            <crs:IN issuedBy="{record.residence_country}">{escape(record.foreign_tin)}</crs:IN>')
                add(f"            <crs:Name>{escape(record.holder_name)}</crs:Name>")
                _address_block("            ", record.address_country_code, record.holder_address, lines)
                add("          </crs:Organisation>")
            add("        </crs:AccountHolder>")

            # Controlling persons for Passive NFEs.
            for cp in record.controlling_persons.all():
                add("        <crs:ControllingPerson>")
                add("          <crs:Individual>")
                add(f"            <crs:ResCountryCode>{cp.residence_country}</crs:ResCountryCode>")
                if cp.tin:
                    add(f'            <crs:TIN issuedBy="{cp.residence_country}">{escape(cp.tin)}</crs:TIN>')
                add(f"            <crs:Name><crs:FullName>{escape(cp.name)}</crs:FullName></crs:Name>")
                _address_block("            ", cp.residence_country, cp.address, lines)
                _birth_block("            ", cp.birth_date, "", lines)
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
