"""Data-quality validation for submitted CRS filings.

Findings split into file-level failures and record-level errors, matching
the two-gateway model in the approved BPMN. Warnings may be overridden by a
Returns Supervisor with a recorded justification; errors may not.
"""
from __future__ import annotations

import re

from exchange.models import PartnerJurisdiction
from portal.models import AccountReport, Filing, ValidationFinding

# TIN plausibility: alphanumeric with common separators, 6 to 20 characters.
TIN_PATTERN = re.compile(r"^[A-Za-z0-9\-/. ]{6,20}$")


def run_validation(filing: Filing) -> tuple[int, int]:
    """Run all checks against a filing, replacing prior findings.

    Returns (file_level_count, record_level_count).
    """
    filing.findings.all().delete()
    findings: list[ValidationFinding] = []
    partner_codes = set(PartnerJurisdiction.objects.values_list("code", flat=True))
    records = list(filing.account_reports.filter(superseded=False))

    def file_finding(severity: str, code: str, message: str) -> None:
        findings.append(
            ValidationFinding(filing=filing, severity=severity, code=code, message=message)
        )

    def record_finding(record: AccountReport, severity: str, code: str, message: str) -> None:
        findings.append(
            ValidationFinding(
                filing=filing, account_report=record, severity=severity, code=code, message=message
            )
        )

    if filing.kind != Filing.Kind.NIL and not records:
        file_finding("ERROR", "F-001", "The filing contains no account reports.")

    # Duplicate account detection within the filing.
    seen: dict[str, AccountReport] = {}
    for record in records:
        key = record.account_number.strip().upper()
        if key and key in seen:
            record_finding(
                record,
                "ERROR",
                "R-105",
                f"Duplicate account number {record.account_number} within the filing "
                f"(first reported in {seen[key].doc_ref_id}).",
            )
        else:
            seen[key] = record

    for record in records:
        if not record.holder_name.strip():
            record_finding(record, "ERROR", "R-101", "Account holder name is missing.")
        if not record.account_number.strip():
            record_finding(record, "ERROR", "R-102", "Account number is missing.")
        if not record.residence_country.strip():
            record_finding(record, "ERROR", "R-103", "Residence jurisdiction is missing.")
        elif record.residence_country not in partner_codes:
            record_finding(
                record,
                "ERROR",
                "R-104",
                f"Residence jurisdiction {record.residence_country} is not on the activated partner list.",
            )
        if not record.foreign_tin.strip():
            record_finding(
                record,
                "WARNING",
                "R-201",
                "No TIN reported for the account holder. Partner jurisdictions may reject the record.",
            )
        elif not TIN_PATTERN.match(record.foreign_tin.strip()):
            record_finding(
                record,
                "ERROR",
                "R-202",
                f"TIN '{record.foreign_tin}' fails the plausibility check.",
            )
        if record.balance < 0:
            record_finding(record, "ERROR", "R-301", f"Account balance {record.balance} is negative.")
        if record.opened_date and record.opened_date.year > filing.reporting_year:
            record_finding(
                record,
                "ERROR",
                "R-401",
                f"Account opened {record.opened_date} is outside reporting year {filing.reporting_year}.",
            )

    ValidationFinding.objects.bulk_create(findings)
    file_level = sum(1 for finding in findings if finding.account_report_id is None)
    record_level = len(findings) - file_level
    return file_level, record_level


def blocking_findings(filing: Filing):
    """Findings that stop approval: all errors plus warnings not overridden."""
    return filing.findings.filter(severity="ERROR") | filing.findings.filter(
        severity="WARNING", overridden=False
    )
