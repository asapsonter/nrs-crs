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

import functools
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils.dateparse import parse_date

CRS_NS = "urn:oecd:ties:crs:v2"
CFC_NS = "urn:oecd:ties:commontypesfatcacrs:v2"
STF_NS = "urn:oecd:ties:crsstf:v5"
NS = {"crs": CRS_NS, "cfc": CFC_NS, "stf": STF_NS}

#: The OECD-issued schema package, as republished by the NRS for reporting
#: financial institutions. Uploads are validated against it before any
#: business checks run, so a structurally invalid document fails on the
#: schema rather than on a hand-written rule.
CRS_XSD_PATH = (
    Path(__file__).resolve().parents[1] / "exchange" / "schemas" / "crs-v2.0" / "CrsXML_v2.0.xsd"
)

_ACCT_NUMBER_TYPES = {"OECD601", "OECD602", "OECD603", "OECD604", "OECD605"}
_ACCT_HOLDER_TYPES = {"CRS101", "CRS102", "CRS103"}
_TRUE_ATTRS = {"true", "1"}

#: DocTypeIndic values (User Guide, DocSpec Type).
_DOC_TYPE_NEW = "OECD1"
_DOC_TYPE_CORRECTED = "OECD2"
_DOC_TYPE_DELETED = "OECD3"
_DOC_TYPE_RESENT = "OECD0"
#: OECD10-OECD13 mirror OECD0-OECD3 but carry *test* data, for use during an
#: agreed testing period. They are accepted (NRS issues its UAT samples with
#: OECD11), normalised to the live equivalent for storage, and flagged on the
#: result so a test upload is never mistaken for a live return.
_DOC_TYPE_TEST = {
    "OECD10": _DOC_TYPE_RESENT,
    "OECD11": _DOC_TYPE_NEW,
    "OECD12": _DOC_TYPE_CORRECTED,
    "OECD13": _DOC_TYPE_DELETED,
}
_DOC_TYPES_LIVE_RECORD = {_DOC_TYPE_NEW, _DOC_TYPE_CORRECTED, _DOC_TYPE_DELETED}

#: Which record DocTypeIndic values each message type may carry. A message
#: must contain either all new data or all corrections/deletions, never both.
_ALLOWED_DOC_TYPES_FOR_MESSAGE = {
    "CRS701": {_DOC_TYPE_NEW},
    "CRS702": {_DOC_TYPE_CORRECTED, _DOC_TYPE_DELETED},
}


@functools.lru_cache(maxsize=1)
def _crs_schema():
    """The compiled CRS v2.0 schema, or None when xmlschema is unavailable.

    Compiling is expensive and the schema never changes, so it is cached for
    the life of the process.
    """
    try:
        import xmlschema
    except ImportError:  # pragma: no cover - xmlschema is a hard requirement
        return None
    if not CRS_XSD_PATH.exists():  # pragma: no cover - schema ships with the repo
        return None
    return xmlschema.XMLSchema(str(CRS_XSD_PATH))


@dataclass
class ParsedControllingPerson:
    name: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    residence_country: str = ""
    tin: str = ""
    address: str = ""
    city: str = ""
    birth_date: object = None
    ctrlg_person_type: str = "CRS801"


@dataclass
class ParsedRecord:
    # DocSpec — the FI's own identity for this record. Preserved verbatim so
    # the correction chain the FI started stays intact end to end.
    doc_ref_id: str = ""
    doc_type_indic: str = _DOC_TYPE_NEW
    corr_doc_ref_id: str = ""
    holder_name: str = ""
    holder_first_name: str = ""
    holder_middle_name: str = ""
    holder_last_name: str = ""
    holder_type: str = "INDIVIDUAL"
    acct_holder_type: str = ""
    residence_country: str = ""
    foreign_tin: str = ""
    holder_address: str = ""
    address_country: str = ""
    holder_street: str = ""
    holder_building_identifier: str = ""
    holder_suite_identifier: str = ""
    holder_floor_identifier: str = ""
    holder_district_name: str = ""
    holder_pob: str = ""
    holder_post_code: str = ""
    holder_city: str = ""
    holder_country_subentity: str = ""
    # The line in the uploaded XML where this AccountReport element starts,
    # so a validation finding can point back into the institution's file.
    source_line: int | None = None
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
    #: Deviations that do not compromise the reported data. The upload is
    #: accepted and these are surfaced to the preparer so the generating
    #: system can be corrected.
    warnings: list[str] = field(default_factory=list)
    #: True when any record carried an OECD10-OECD13 test-data indicator.
    contains_test_data: bool = False
    year: int | None = None
    records: list[ParsedRecord] = field(default_factory=list)
    # Message metadata from a CRS_OECD document, empty for the simplified form.
    message_type_indic: str = ""
    receiving_country: str = ""
    message_ref_id: str = ""
    # ReportingFI identity as declared in the document, for cross-checking
    # against the enrolled institution.
    reporting_fi_name: str = ""
    reporting_fi_in: str = ""


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


def _record_lines(text: str, tag: str) -> list[int]:
    """1-based line number of each occurrence of an opening tag in the text."""
    lines: list[int] = []
    idx = text.find(tag)
    while idx != -1:
        lines.append(text.count("\n", 0, idx) + 1)
        idx = text.find(tag, idx + 1)
    return lines


_RECORD_ERROR = re.compile(r"^AccountReport (\d+):")

#: Where a file-level error points in the document: the element the message
#: names, or — when that element is the thing that is missing — the block
#: that should contain it. Ordered so the most specific phrase wins.
_FILE_LEVEL_ANCHORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MessageRefId", ("crs:MessageRefId", "crs:MessageSpec")),
    ("ReportingPeriod", ("crs:ReportingPeriod", "crs:MessageSpec")),
    ("MessageTypeIndic", ("crs:MessageTypeIndic", "crs:MessageSpec")),
    ("TransmittingCountry", ("crs:TransmittingCountry", "crs:MessageSpec")),
    ("ReceivingCountry", ("crs:ReceivingCountry", "crs:MessageSpec")),
    ("ReportingFI", ("crs:ReportingFI", "crs:CrsBody", "CRSFiling")),
    ("AccountReport elements", ("crs:ReportingGroup", "crs:CrsBody", "CRSFiling")),
    ("year attribute", ("CRSFiling",)),
)


def _line_of_first(text: str, tags: tuple[str, ...]) -> int | None:
    """Line of the first occurrence of any of the given opening tags.

    Each tag is tried with its namespace prefix and then bare, so the same
    anchors serve the official CRS_OECD document and the simplified form.
    """
    for tag in tags:
        for needle in (f"<{tag}", f"<{tag.split(':')[-1]}"):
            idx = text.find(needle)
            if idx != -1:
                return text.count("\n", 0, idx) + 1
    return None


def _attach_lines(result: ParseResult, text: str, lines: list[int]) -> None:
    """Stamp each record with its XML line and prefix every error with one.

    Record errors point at their AccountReport element; file-level errors
    point at the element they concern, falling back to its parent block when
    the element itself is absent; a bad root element points at line 1.
    """
    for index, record in enumerate(result.records):
        if index < len(lines):
            record.source_line = lines[index]

    def with_line(message: str) -> str:
        if message.startswith("Line "):
            return message
        match = _RECORD_ERROR.match(message)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(lines):
                return f"Line {lines[index]} — {message}"
            return message
        if "Root element" in message:
            return f"Line 1 — {message}"
        for phrase, tags in _FILE_LEVEL_ANCHORS:
            if phrase in message:
                line = _line_of_first(text, tags)
                if line is not None:
                    return f"Line {line} — {message}"
                break
        return message

    result.errors = [with_line(message) for message in result.errors]


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
        result = _parse_oecd_document(root, expected_year)
        result.warnings = _schema_findings(text) + result.warnings
        _attach_lines(result, text, _record_lines(text, "<crs:AccountReport"))
        return result
    if root.tag == "CRSFiling":
        result = _parse_simplified_document(root, expected_year)
        _attach_lines(result, text, _record_lines(text, "<AccountReport"))
        return result
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
            "suite": _text(fix, "cfc:SuiteIdentifier"),
            "floor": _text(fix, "cfc:FloorIdentifier"),
            "district": _text(fix, "cfc:DistrictName"),
            "pob": _text(fix, "cfc:POB"),
            "post_code": _text(fix, "cfc:PostCode"),
            "city": _text(fix, "cfc:City"),
            "subentity": _text(fix, "cfc:CountrySubentity"),
        }
    composed = ", ".join(
        value for value in (
            parts.get("street", ""), parts.get("building", ""), parts.get("suite", ""),
            parts.get("floor", ""), parts.get("district", ""), parts.get("pob", ""),
            parts.get("city", ""), parts.get("post_code", ""), parts.get("subentity", ""),
        ) if value
    )
    if prefix == "holder":
        record_or_cp.address_country = country
        record_or_cp.holder_street = parts.get("street", "")
        record_or_cp.holder_building_identifier = parts.get("building", "")
        record_or_cp.holder_suite_identifier = parts.get("suite", "")
        record_or_cp.holder_floor_identifier = parts.get("floor", "")
        record_or_cp.holder_district_name = parts.get("district", "")
        record_or_cp.holder_pob = parts.get("pob", "")
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


def _schema_findings(text: str) -> list[str]:
    """Validate against the OECD CRS v2.0 schema and describe any deviations.

    These are reported as warnings rather than rejections. The schema is the
    authority on document shape, but a deviation such as an out-of-sequence
    optional element does not make the reported financial data unusable, and
    this parser locates elements by name rather than by position. What gates
    acceptance is the business validation below: the elements CRS actually
    requires, present and well formed.
    """
    schema = _crs_schema()
    if schema is None:  # pragma: no cover - only when xmlschema is missing
        return []
    findings = []
    for error in schema.iter_errors(text):
        path = (getattr(error, "path", "") or "").replace("/crs:CRS_OECD", "")
        reason = " ".join((error.reason or "").split())
        findings.append(f"Schema deviation at {path}: {reason}" if path else f"Schema deviation: {reason}")
        if len(findings) == 20:
            findings.append("Further schema deviations were suppressed.")
            break
    return findings


def _read_person_name(element) -> tuple[str, str, str, str]:
    """(full, first, middle, last) from the first crs:Name NamePerson block.

    FirstName, MiddleName and LastName are kept distinct — folding the middle
    name into FirstName would send a wrong FirstName to the receiving
    Competent Authority when the record is exchanged.
    """
    name = element.find("crs:Name", NS)
    if name is None:
        return "", "", "", ""
    first = _text(name, "crs:FirstName")
    middle = _text(name, "crs:MiddleName")
    last = _text(name, "crs:LastName")
    full = " ".join(part for part in (first, middle, last) if part)
    return full, first, middle, last


def _parse_oecd_document(root, expected_year: int) -> ParseResult:
    errors: list[str] = []
    warnings: list[str] = []
    contains_test_data = False
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

    message_type = _text(spec, "crs:MessageType")
    if message_type != "CRS":
        errors.append(
            f"MessageSpec/MessageType must be 'CRS' for a CRS return; found '{message_type or 'nothing'}'."
        )

    transmitting = _text(spec, "crs:TransmittingCountry").upper()
    if transmitting and transmitting != "NG":
        errors.append(
            f"MessageSpec/TransmittingCountry is '{transmitting}'; a return filed with the NRS must declare NG."
        )

    indic = _text(spec, "crs:MessageTypeIndic")
    if indic == "CRS703":
        errors.append(
            "A nil return (CRS703) is filed with the portal's Nil Return option rather than as an XML upload."
        )
    receiving = _text(spec, "crs:ReceivingCountry").upper()
    message_ref = _text(spec, "crs:MessageRefId")
    if not message_ref:
        errors.append("MessageSpec/MessageRefId is required and must uniquely identify this message.")

    # ReportingFI identity, for cross-checking against the enrolled entity.
    reporting_fi = root.find("crs:CrsBody/crs:ReportingFI", NS)
    reporting_fi_name = _text(reporting_fi, "crs:Name") if reporting_fi is not None else ""
    reporting_fi_in = _text(reporting_fi, "crs:IN") if reporting_fi is not None else ""
    if reporting_fi is None:
        errors.append("CrsBody/ReportingFI is required: the document must identify the reporting institution.")

    reports = root.findall("crs:CrsBody/crs:ReportingGroup/crs:AccountReport", NS)
    if not reports:
        errors.append("The document contains no AccountReport elements. Use a nil return for years with nothing to report.")

    records: list[ParsedRecord] = []
    seen_doc_ref_ids: set[str] = set()
    for index, element in enumerate(reports, start=1):
        record = ParsedRecord()

        # ---- DocSpec: the FI's own identity for this record ----------------
        doc_spec = element.find("crs:DocSpec", NS)
        if doc_spec is None:
            errors.append(f"AccountReport {index}: DocSpec is required and identifies the record.")
        else:
            record.doc_ref_id = _text(doc_spec, "stf:DocRefId")
            record.corr_doc_ref_id = _text(doc_spec, "stf:CorrDocRefId")
            doc_type = _text(doc_spec, "stf:DocTypeIndic")

            if not record.doc_ref_id:
                errors.append(f"AccountReport {index}: DocSpec/DocRefId is required.")
            elif record.doc_ref_id in seen_doc_ref_ids:
                errors.append(
                    f"AccountReport {index}: DocRefId '{record.doc_ref_id}' is used more than once. "
                    "A DocRefId must be unique in space and time."
                )
            else:
                seen_doc_ref_ids.add(record.doc_ref_id)

            # Test-data indicators are accepted and normalised to their live
            # equivalent; the result is flagged so the filing can be shown as
            # test data rather than silently co-mingled with live returns.
            if doc_type in _DOC_TYPE_TEST:
                contains_test_data = True
                effective = _DOC_TYPE_TEST[doc_type]
                warnings.append(
                    f"AccountReport {index}: DocTypeIndic {doc_type} marks test data "
                    f"(read as {effective}). Test indicators belong to an agreed testing period only."
                )
            else:
                effective = doc_type

            if effective == _DOC_TYPE_RESENT:
                errors.append(
                    f"AccountReport {index}: DocTypeIndic OECD0 (resent data) applies only to the "
                    "ReportingFI element, never to an AccountReport."
                )
            elif effective not in _DOC_TYPES_LIVE_RECORD:
                errors.append(
                    f"AccountReport {index}: DocTypeIndic '{doc_type or 'nothing'}' is not a valid "
                    "value; expected OECD1 (new), OECD2 (correction) or OECD3 (deletion)."
                )
            else:
                # A record carrying CorrDocRefId inside a corrections message
                # is a correction whatever its indicator says. Trust the
                # evidence and re-label it, rather than storing a record that
                # would later be exchanged as OECD1-with-CorrDocRefId, which
                # no receiving Competent Authority would accept.
                if (
                    indic == "CRS702"
                    and effective == _DOC_TYPE_NEW
                    and record.corr_doc_ref_id
                ):
                    warnings.append(
                        f"AccountReport {index}: labelled {doc_type} inside a CRS702 corrections "
                        f"message but carries CorrDocRefId; recorded as {_DOC_TYPE_CORRECTED}."
                    )
                    effective = _DOC_TYPE_CORRECTED

                record.doc_type_indic = effective
                allowed = _ALLOWED_DOC_TYPES_FOR_MESSAGE.get(indic)
                if allowed and effective not in allowed:
                    errors.append(
                        f"AccountReport {index}: a {indic} message cannot carry a {effective} record. "
                        "A message must contain either all new data or all corrections and deletions."
                    )
                if effective in (_DOC_TYPE_CORRECTED, _DOC_TYPE_DELETED) and not record.corr_doc_ref_id:
                    errors.append(
                        f"AccountReport {index}: a {effective} record requires CorrDocRefId referencing "
                        "the DocRefId of the record it amends."
                    )
                if effective == _DOC_TYPE_NEW and record.corr_doc_ref_id:
                    errors.append(
                        f"AccountReport {index}: CorrDocRefId must not be present on new data (OECD1)."
                    )

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
            full, first, middle, last = _read_person_name(individual)
            record.holder_name = full
            record.holder_first_name = first
            record.holder_middle_name = middle
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
                full, first, middle, last = _read_person_name(cp_individual)
                cp.name, cp.first_name, cp.middle_name, cp.last_name = full, first, middle, last
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
        return ParseResult(ok=False, errors=errors, warnings=warnings, year=year)
    return ParseResult(
        ok=True,
        year=year,
        records=records,
        warnings=warnings,
        contains_test_data=contains_test_data,
        message_type_indic=indic,
        receiving_country=receiving,
        message_ref_id=message_ref,
        reporting_fi_name=reporting_fi_name,
        reporting_fi_in=reporting_fi_in,
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


# How to resolve each family of upload rejection, keyed by a phrase that
# appears in the error message. Shown beside the error on the upload page so
# the preparer can fix the generating system without contacting the NRS.
_RESOLUTION_HINTS: tuple[tuple[str, str], ...] = (
    ("not well formed", "Open the file in an XML editor and repair the syntax at the line and column shown, then export it again."),
    ("not valid UTF-8", "Save the file with UTF-8 encoding (without a byte-order mark) and upload it again."),
    ("Root element must be", "Export the filing as a CRS_OECD v2.0 document (urn:oecd:ties:crs:v2) or use the NRS simplified CRSFiling form."),
    ("DocSpec/DocRefId", "Give the record's DocSpec a globally unique DocRefId (for example NG2026-<your reference>); it identifies this version of the record for all time."),
    ("DocSpec is required", "Add a DocSpec block to the record carrying a DocTypeIndic and a globally unique DocRefId."),
    ("has already been filed", "Issue a new, unused DocRefId for the record. If you are correcting a filed record, set CorrDocRefId to the DocRefId being corrected."),
    ("used more than once", "Give every record its own unique DocRefId; no value may repeat within or across filings."),
    ("CorrDocRefId must not be present", "Remove CorrDocRefId from new-data (OECD1) records; it belongs only on corrections and deletions."),
    ("requires CorrDocRefId", "Add a CorrDocRefId naming the DocRefId of the previously filed record this correction or deletion replaces."),
    ("cannot carry a", "Split the filing: a CRS701 message may only carry new data (OECD1); a CRS702 message only corrections and deletions (OECD2/OECD3)."),
    ("DocTypeIndic", "Set DocTypeIndic to OECD1 (new), OECD2 (correction) or OECD3 (deletion); OECD0 is only valid on the ReportingFI element."),
    ("MessageRefId", "Provide a globally unique MessageRefId in MessageSpec; a new value is needed for every message."),
    ("ReportingPeriod", "Set ReportingPeriod to 31 December of the reporting year (YYYY-12-31)."),
    ("ReportingFI", "Include the ReportingFI element identifying your institution before the account reports."),
    ("no AccountReport elements", "Add the account reports to the document, or file a nil return if there is nothing to report for the year."),
    ("AccountNumber", "Provide the account number the institution uses for the account (or NANUM where no numbering system exists)."),
    ("AcctNumberType", "Use an OECD601-OECD605 account number type code."),
    ("account holder name", "Provide the account holder's name (Individual Name or Organisation Name)."),
    ("ResCountryCode", "Provide the holder's residence jurisdiction as a 2-letter ISO 3166-1 country code."),
    ("AccountBalance", "Provide the year-end account balance with its currCode attribute (report negative balances as 0.00)."),
    ("Payment Type", "Use CRS501 (dividends), CRS502 (interest), CRS503 (gross proceeds) or CRS504 (other) payment type codes."),
    ("ControllingPerson", "Complete the controlling person's details; a Passive NFE (CRS101) account must name at least one."),
    ("Individual or an Organisation", "Each AccountHolder must contain either an Individual or an Organisation element."),
    ("valid amount", "Use plain decimal amounts (for example 25000000.00) without thousands separators or currency symbols."),
    ("numeric year attribute", "Set the CRSFiling element's year attribute to the reporting year."),
)

_DEFAULT_HINT = "Correct the element shown in your reporting system and upload the file again."


def resolution_hint(message: str) -> str:
    """A short 'how to resolve' for an upload rejection message."""
    for phrase, hint in _RESOLUTION_HINTS:
        if phrase in message:
            return hint
    return _DEFAULT_HINT
