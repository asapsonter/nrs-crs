"""CRS XML upload ingestion.

Two document shapes are accepted and auto-detected by root element:

  CRS_OECD  the official CRS XML Schema v2.0 document (urn:oecd:ties:crs:v2),
            as circulated to FIs in the 2026 sensitization samples. Holder
            identity, name parts, structured address, birth info, account
            attributes, payments, and controlling persons are all captured.

  CRSFiling the NRS simplified filing schema (see
            portal/schema/crs_filing_simplified.xsd), kept for continuity.

Structural validation is performed in code with per-record messages; parsing
is deliberately order-tolerant so near-miss documents fail on substance, not
sequence.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.utils.dateparse import parse_date

CRS_NS = "urn:oecd:ties:crs:v2"
CFC_NS = "urn:oecd:ties:commontypesfatcacrs:v2"
STF_NS = "urn:oecd:ties:crsstf:v5"
NS = {"crs": CRS_NS, "cfc": CFC_NS, "stf": STF_NS}

_ACCT_NUMBER_TYPES = {"OECD601", "OECD602", "OECD603", "OECD604", "OECD605"}
_ACCT_HOLDER_TYPES = {"CRS101", "CRS102", "CRS103"}
_TRUE_ATTRS = {"true", "1"}


@dataclass
class ParsedControllingPerson:
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    residence_country: str = ""
    tin: str = ""
    address: str = ""
    city: str = ""
    birth_date: object = None
    ctrlg_person_type: str = "CRS801"


@dataclass
class ParsedRecord:
    holder_name: str = ""
    holder_first_name: str = ""
    holder_last_name: str = ""
    holder_type: str = "INDIVIDUAL"
    acct_holder_type: str = ""
    residence_country: str = ""
    foreign_tin: str = ""
    holder_address: str = ""
    address_country: str = ""
    holder_street: str = ""
    holder_building_identifier: str = ""
    holder_post_code: str = ""
    holder_city: str = ""
    holder_country_subentity: str = ""
    birth_date: object = None
    birth_city: str = ""
    birth_city_subentity: str = ""
    birth_country_code: str = ""
    account_number: str = ""
    acct_number_type: str = ""
    closed_account: bool = False
    dormant_account: bool = False
    currency: str = ""
    balance: Decimal = Decimal("0")
    dividends: Decimal = Decimal("0")
    interest: Decimal = Decimal("0")
    gross_proceeds: Decimal = Decimal("0")
    other_income: Decimal = Decimal("0")
    controlling_persons: list[ParsedControllingPerson] = field(default_factory=list)


@dataclass
class ParseResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    year: int | None = None
    records: list[ParsedRecord] = field(default_factory=list)
    # Message metadata from a CRS_OECD document, empty for the simplified form.
    message_type_indic: str = ""
    receiving_country: str = ""
    message_ref_id: str = ""


REQUIRED_RECORD_ELEMENTS = ["HolderName", "ResCountry", "AccountNumber", "Balance"]
DECIMAL_ELEMENTS = {
    "Balance": "balance",
    "Dividends": "dividends",
    "Interest": "interest",
    "GrossProceeds": "gross_proceeds",
    "OtherIncome": "other_income",
}
# CRS Payment Type codes onto the record's income fields.
_PAYMENT_FIELDS = {
    "CRS501": "dividends",
    "CRS502": "interest",
    "CRS503": "gross_proceeds",
    "CRS504": "other_income",
}


def parse_crs_upload(content: bytes, expected_year: int) -> ParseResult:
    """Parse and structurally validate an uploaded CRS filing document."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ParseResult(ok=False, errors=["The file is not valid UTF-8 text."])
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        line, column = exc.position
        return ParseResult(
            ok=False,
            errors=[f"Line {line}, column {column}: the XML is not well formed ({exc.msg.split(':')[0]})."],
        )

    if root.tag == f"{{{CRS_NS}}}CRS_OECD":
        return _parse_oecd_document(root, expected_year)
    if root.tag == "CRSFiling":
        return _parse_simplified_document(root, expected_year)
    return ParseResult(
        ok=False,
        errors=[
            "Root element must be a CRS_OECD document (urn:oecd:ties:crs:v2) "
            f"or the simplified CRSFiling form; found {root.tag}."
        ],
    )


# --------------------------------------------------------------------------
# Official CRS XML Schema v2.0 documents.


def _text(element, path: str) -> str:
    return (element.findtext(path, "", NS) or "").strip()


def _flag(element, attr: str) -> bool:
    return (element.get(attr) or "").strip().lower() in _TRUE_ATTRS


def _amount(raw: str, label: str, errors: list[str]) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        errors.append(f"{label} value '{raw}' is not a valid amount.")
        return Decimal("0")


def _read_address(record_or_cp, holder, errors: list[str], prefix: str) -> None:
    """First crs:Address: country, AddressFix components, and free text."""
    address = holder.find("crs:Address", NS)
    if address is None:
        return
    country = _text(address, "cfc:CountryCode").upper()
    free = _text(address, "cfc:AddressFree")
    fix = address.find("cfc:AddressFix", NS)
    parts = {}
    if fix is not None:
        parts = {
            "street": _text(fix, "cfc:Street"),
            "building": _text(fix, "cfc:BuildingIdentifier"),
            "post_code": _text(fix, "cfc:PostCode"),
            "city": _text(fix, "cfc:City"),
            "subentity": _text(fix, "cfc:CountrySubentity"),
        }
    composed = ", ".join(
        value for value in (
            parts.get("street", ""), parts.get("building", ""), parts.get("city", ""),
            parts.get("post_code", ""), parts.get("subentity", ""),
        ) if value
    )
    if prefix == "holder":
        record_or_cp.address_country = country
        record_or_cp.holder_street = parts.get("street", "")
        record_or_cp.holder_building_identifier = parts.get("building", "")
        record_or_cp.holder_post_code = parts.get("post_code", "")
        record_or_cp.holder_city = parts.get("city", "")
        record_or_cp.holder_country_subentity = parts.get("subentity", "")
        record_or_cp.holder_address = free or composed
    else:
        record_or_cp.city = parts.get("city", "")
        record_or_cp.address = free or composed


def _read_birth(record, individual) -> None:
    birth = individual.find("crs:BirthInfo", NS)
    if birth is None:
        return
    raw = _text(birth, "crs:BirthDate")
    if raw:
        record.birth_date = parse_date(raw[:10])
    record.birth_city = _text(birth, "crs:City")
    record.birth_city_subentity = _text(birth, "crs:CitySubentity")
    record.birth_country_code = _text(birth, "crs:CountryInfo/crs:CountryCode").upper()


def _read_person_name(element) -> tuple[str, str, str]:
    """(full, first, last) from the first crs:Name NamePerson block."""
    name = element.find("crs:Name", NS)
    if name is None:
        return "", "", ""
    first = _text(name, "crs:FirstName")
    middle = _text(name, "crs:MiddleName")
    last = _text(name, "crs:LastName")
    full = " ".join(part for part in (first, middle, last) if part)
    return full, " ".join(p for p in (first, middle) if p), last


def _parse_oecd_document(root, expected_year: int) -> ParseResult:
    errors: list[str] = []
    spec = root.find("crs:MessageSpec", NS)
    if spec is None:
        return ParseResult(ok=False, errors=["The document has no MessageSpec header."])

    year: int | None = None
    period = _text(spec, "crs:ReportingPeriod")
    if len(period) >= 4 and period[:4].isdigit():
        year = int(period[:4])
        if year != expected_year:
            errors.append(
                f"The document declares reporting period {period}; the current filing cycle is {expected_year}."
            )
    else:
        errors.append("MessageSpec/ReportingPeriod must be the last day of the reporting year (YYYY-12-31).")

    indic = _text(spec, "crs:MessageTypeIndic")
    if indic == "CRS703":
        errors.append(
            "A nil return (CRS703) is filed with the portal's Nil Return option rather than as an XML upload."
        )
    receiving = _text(spec, "crs:ReceivingCountry").upper()
    message_ref = _text(spec, "crs:MessageRefId")

    reports = root.findall("crs:CrsBody/crs:ReportingGroup/crs:AccountReport", NS)
    if not reports:
        errors.append("The document contains no AccountReport elements. Use a nil return for years with nothing to report.")

    records: list[ParsedRecord] = []
    for index, element in enumerate(reports, start=1):
        record = ParsedRecord()

        acct = element.find("crs:AccountNumber", NS)
        if acct is None or not (acct.text or "").strip():
            errors.append(f"AccountReport {index}: AccountNumber is missing or empty.")
        else:
            record.account_number = acct.text.strip()
            acct_type = (acct.get("AcctNumberType") or "").strip()
            if acct_type and acct_type not in _ACCT_NUMBER_TYPES:
                errors.append(f"AccountReport {index}: AcctNumberType '{acct_type}' is not an OECD601-OECD605 code.")
            else:
                record.acct_number_type = acct_type
            record.closed_account = _flag(acct, "ClosedAccount")
            record.dormant_account = _flag(acct, "DormantAccount")

        holder = element.find("crs:AccountHolder", NS)
        individual = holder.find("crs:Individual", NS) if holder is not None else None
        organisation = holder.find("crs:Organisation", NS) if holder is not None else None
        if individual is not None:
            record.holder_type = "INDIVIDUAL"
            record.residence_country = _text(individual, "crs:ResCountryCode").upper()
            record.foreign_tin = _text(individual, "crs:TIN")
            full, first, last = _read_person_name(individual)
            record.holder_name = full
            record.holder_first_name = first
            record.holder_last_name = last
            _read_address(record, individual, errors, "holder")
            _read_birth(record, individual)
        elif organisation is not None:
            record.holder_type = "ORGANISATION"
            record.residence_country = _text(organisation, "crs:ResCountryCode").upper()
            record.foreign_tin = _text(organisation, "crs:IN")
            record.holder_name = _text(organisation, "crs:Name")
            _read_address(record, organisation, errors, "holder")
            acct_holder_type = _text(holder, "crs:AcctHolderType")
            if acct_holder_type in _ACCT_HOLDER_TYPES:
                record.acct_holder_type = acct_holder_type
            else:
                errors.append(
                    f"AccountReport {index}: an Organisation holder requires AcctHolderType CRS101, CRS102 or CRS103."
                )
        else:
            errors.append(f"AccountReport {index}: AccountHolder must contain an Individual or an Organisation.")

        if not record.holder_name:
            errors.append(f"AccountReport {index}: the account holder name is missing.")
        if not record.residence_country:
            errors.append(f"AccountReport {index}: ResCountryCode is missing.")
        elif len(record.residence_country) != 2:
            errors.append(f"AccountReport {index}: ResCountryCode must be a 2-letter ISO country code.")

        balance = element.find("crs:AccountBalance", NS)
        if balance is None or not (balance.text or "").strip():
            errors.append(f"AccountReport {index}: AccountBalance is missing or empty.")
        else:
            record.balance = _amount(balance.text.strip(), f"AccountReport {index}: AccountBalance", errors)
            record.currency = (balance.get("currCode") or "").strip().upper()
            if not record.currency:
                errors.append(f"AccountReport {index}: AccountBalance requires a currCode attribute.")

        for payment in element.findall("crs:Payment", NS):
            code = _text(payment, "crs:Type")
            field_name = _PAYMENT_FIELDS.get(code)
            if field_name is None:
                errors.append(f"AccountReport {index}: Payment Type '{code}' is not a CRS501-CRS504 code.")
                continue
            amount_el = payment.find("crs:PaymentAmnt", NS)
            raw = (amount_el.text or "").strip() if amount_el is not None else ""
            if raw:
                current = getattr(record, field_name)
                setattr(
                    record,
                    field_name,
                    current + _amount(raw, f"AccountReport {index}: PaymentAmnt", errors),
                )

        for cp_el in element.findall("crs:ControllingPerson", NS):
            cp = ParsedControllingPerson()
            cp_individual = cp_el.find("crs:Individual", NS)
            if cp_individual is not None:
                cp.residence_country = _text(cp_individual, "crs:ResCountryCode").upper()
                cp.tin = _text(cp_individual, "crs:TIN")
                full, first, last = _read_person_name(cp_individual)
                cp.name, cp.first_name, cp.last_name = full, first, last
                _read_address(cp, cp_individual, errors, "cp")
                birth = cp_individual.find("crs:BirthInfo", NS)
                if birth is not None:
                    raw = _text(birth, "crs:BirthDate")
                    if raw:
                        cp.birth_date = parse_date(raw[:10])
            cp_type = _text(cp_el, "crs:CtrlgPersonType")
            if cp_type:
                cp.ctrlg_person_type = cp_type
            if not cp.name:
                errors.append(f"AccountReport {index}: a ControllingPerson carries no name.")
            record.controlling_persons.append(cp)
        if record.controlling_persons and record.acct_holder_type != "CRS101":
            errors.append(
                f"AccountReport {index}: controlling persons are only reported for a Passive NFE "
                "with controlling persons (AcctHolderType CRS101)."
            )

        records.append(record)

    if errors:
        return ParseResult(ok=False, errors=errors, year=year)
    return ParseResult(
        ok=True,
        year=year,
        records=records,
        message_type_indic=indic,
        receiving_country=receiving,
        message_ref_id=message_ref,
    )


# --------------------------------------------------------------------------
# The NRS simplified CRSFiling form.


def _parse_simplified_document(root, expected_year: int) -> ParseResult:
    errors: list[str] = []
    year_attr = root.get("year", "")
    year: int | None = None
    if not year_attr.isdigit():
        errors.append("CRSFiling requires a numeric year attribute.")
    else:
        year = int(year_attr)
        if year != expected_year:
            errors.append(
                f"The document declares reporting year {year}; the current filing cycle is {expected_year}."
            )

    if root.find("ReportingFI") is None:
        errors.append("The ReportingFI element is required before account reports.")

    report_elements = root.findall("AccountReport")
    if not report_elements:
        errors.append("The document contains no AccountReport elements. Use a nil return for years with nothing to report.")

    records: list[ParsedRecord] = []
    for index, element in enumerate(report_elements, start=1):
        record = ParsedRecord()
        for required in REQUIRED_RECORD_ELEMENTS:
            child = element.find(required)
            if child is None or not (child.text or "").strip():
                errors.append(f"AccountReport {index}: element {required} is missing or empty.")
        holder_type = (element.findtext("HolderType") or "INDIVIDUAL").strip().upper()
        if holder_type not in ("INDIVIDUAL", "ORGANISATION"):
            errors.append(f"AccountReport {index}: HolderType must be INDIVIDUAL or ORGANISATION.")
            holder_type = "INDIVIDUAL"
        record.holder_name = (element.findtext("HolderName") or "").strip()
        record.holder_type = holder_type
        record.residence_country = (element.findtext("ResCountry") or "").strip().upper()
        if record.residence_country and len(record.residence_country) != 2:
            errors.append(f"AccountReport {index}: ResCountry must be a 2-letter ISO country code.")
        record.foreign_tin = (element.findtext("TIN") or "").strip()
        record.account_number = (element.findtext("AccountNumber") or "").strip()
        for tag, attr in DECIMAL_ELEMENTS.items():
            raw = (element.findtext(tag) or "").strip()
            if raw:
                try:
                    setattr(record, attr, Decimal(raw))
                except InvalidOperation:
                    errors.append(f"AccountReport {index}: {tag} value '{raw}' is not a valid amount.")
        records.append(record)

    if errors:
        return ParseResult(ok=False, errors=errors, year=year)
    return ParseResult(ok=True, year=year, records=records)
