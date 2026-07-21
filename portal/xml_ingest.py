"""CRS XML upload ingestion against the bundled simplified schema.

The demo accepts a simplified CRS filing document (see
portal/schema/crs_filing_simplified.xsd for the reference structure) and
rejects malformed files with line-level messages. Structural validation is
performed in code because the standard library cannot evaluate XSD.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass
class ParsedRecord:
    holder_name: str = ""
    holder_type: str = "INDIVIDUAL"
    residence_country: str = ""
    foreign_tin: str = ""
    account_number: str = ""
    balance: Decimal = Decimal("0")
    dividends: Decimal = Decimal("0")
    interest: Decimal = Decimal("0")
    gross_proceeds: Decimal = Decimal("0")
    other_income: Decimal = Decimal("0")


@dataclass
class ParseResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    year: int | None = None
    records: list[ParsedRecord] = field(default_factory=list)


REQUIRED_RECORD_ELEMENTS = ["HolderName", "ResCountry", "AccountNumber", "Balance"]
DECIMAL_ELEMENTS = {
    "Balance": "balance",
    "Dividends": "dividends",
    "Interest": "interest",
    "GrossProceeds": "gross_proceeds",
    "OtherIncome": "other_income",
}


def parse_crs_upload(content: bytes, expected_year: int) -> ParseResult:
    """Parse and structurally validate an uploaded CRS filing document."""
    errors: list[str] = []
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

    if root.tag != "CRSFiling":
        return ParseResult(ok=False, errors=[f"Root element must be CRSFiling, found {root.tag}."])

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
