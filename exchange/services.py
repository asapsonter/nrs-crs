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


def generate_crs_xml(package) -> str:
    """Render the CRS OECD XML body for a package.

    Simplified but structurally faithful to the CRS XML Schema v2.0: one
    MessageSpec, then per reporting FI a ReportingFI element with its
    AccountReport blocks. Correction records carry CorrDocRefID and their
    DocTypeIndic (OECD2 or OECD3)."""
    lines: list[str] = []
    add = lines.append
    add('<?xml version="1.0" encoding="UTF-8"?>')
    add('<crs:CRS_OECD version="2.0" xmlns:crs="urn:oecd:ties:crs:v2" xmlns:cfc="urn:oecd:ties:commontypesfatcacrs:v2">')
    add("  <crs:MessageSpec>")
    add(f"    <crs:SendingCompanyIN>NRS-NG</crs:SendingCompanyIN>")
    add(f"    <crs:TransmittingCountry>{config.SENDING_JURISDICTION}</crs:TransmittingCountry>")
    add(f"    <crs:ReceivingCountry>{package.jurisdiction.code}</crs:ReceivingCountry>")
    add("    <crs:MessageType>CRS</crs:MessageType>")
    add(f"    <crs:MessageRefId>{package.message_ref_id}</crs:MessageRefId>")
    message_type_indic = "CRS701" if package.message_type == "CRS701" else "CRS702"
    add(f"    <crs:MessageTypeIndic>{message_type_indic}</crs:MessageTypeIndic>")
    add(f"    <crs:ReportingPeriod>{package.reporting_year}-12-31</crs:ReportingPeriod>")
    add(f"    <crs:Timestamp>{timezone.now():%Y-%m-%dT%H:%M:%S}</crs:Timestamp>")
    add("  </crs:MessageSpec>")

    by_rfi: dict[int, list] = {}
    rfis: dict[int, ReportingFI] = {}
    for record in package.records.select_related("filing__rfi").order_by("doc_ref_id"):
        rfi = record.filing.rfi
        by_rfi.setdefault(rfi.pk, []).append(record)
        rfis[rfi.pk] = rfi

    for rfi_pk, records in by_rfi.items():
        rfi = rfis[rfi_pk]
        add("  <crs:CrsBody>")
        add("    <crs:ReportingFI>")
        add(f"      <crs:ResCountryCode>{config.SENDING_JURISDICTION}</crs:ResCountryCode>")
        add(f'      <crs:IN issuedBy="NG">{escape(rfi.tin)}</crs:IN>')
        add(f"      <crs:Name>{escape(rfi.legal_name)}</crs:Name>")
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
            add(f"        <crs:AccountNumber>{escape(record.account_number)}</crs:AccountNumber>")
            add("        <crs:AccountHolder>")
            if record.holder_type == "INDIVIDUAL":
                add("          <crs:Individual>")
                add(f"            <crs:ResCountryCode>{record.residence_country}</crs:ResCountryCode>")
                if record.foreign_tin:
                    add(f'            <crs:TIN issuedBy="{record.residence_country}">{escape(record.foreign_tin)}</crs:TIN>')
                add(f"            <crs:Name><crs:FullName>{escape(record.holder_name)}</crs:FullName></crs:Name>")
                add("          </crs:Individual>")
            else:
                add("          <crs:Organisation>")
                add(f"            <crs:ResCountryCode>{record.residence_country}</crs:ResCountryCode>")
                if record.foreign_tin:
                    add(f'            <crs:IN issuedBy="{record.residence_country}">{escape(record.foreign_tin)}</crs:IN>')
                add(f"            <crs:Name>{escape(record.holder_name)}</crs:Name>")
                add("          </crs:Organisation>")
            add("        </crs:AccountHolder>")
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
