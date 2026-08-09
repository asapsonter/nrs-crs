"""Data-quality validation for submitted CRS filings.

Findings split into file-level failures and record-level errors, matching
the two-gateway model in the approved BPMN. Warnings may be overridden by a
Returns Supervisor with a recorded justification; errors may not.
"""
from __future__ import annotations

import re

from django.db.models import Q

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

    # An administrative notice carries a form payload, not account reports;
    # its validation is completeness of that form.
    if filing.is_notice:
        for key, label in Filing.NOTICE_REQUIRED_FIELDS[filing.kind]:
            if not str(filing.notice_payload.get(key, "")).strip():
                file_finding("ERROR", "N-001", f"{label} is missing from the notice.")
        ValidationFinding.objects.bulk_create(findings)
        return len(findings), 0

    if filing.kind != Filing.Kind.NIL and not records:
        file_finding("ERROR", "F-001", "The filing contains no account reports.")

    # A MessageRefId identifies one CRS message for all time, so a reference
    # already carried by another filing can never be sent again.
    if filing.message_reference.strip():
        clash = (
            Filing.objects.exclude(pk=filing.pk)
            .filter(message_reference=filing.message_reference)
            .first()
        )
        if clash:
            file_finding(
                "ERROR",
                "F-002",
                f"MessageRefId '{filing.message_reference}' is already in use by filing "
                f"{clash.reference}. Every CRS message must carry its own unique MessageRefId.",
            )

    # Correction-chain integrity. A CorrDocRefId must point at the latest
    # previously filed version of a record, and each version may be corrected
    # or deleted only once — a second correction must reference the DocRefId
    # of the record that superseded it, or the chain becomes unresolvable.
    seen_corr: dict[str, AccountReport] = {}
    for record in records:
        corr = record.corr_doc_ref_id.strip()
        if not corr:
            continue
        if corr in seen_corr:
            record_finding(
                record,
                "ERROR",
                "R-501",
                f"CorrDocRefId '{corr}' is used more than once in this filing. A record "
                "cannot be corrected or deleted twice in the same filing.",
            )
            continue
        seen_corr[corr] = record
        # The referenced record must exist for this institution: either in a
        # filing already sent to the NRS, or superseded within this filing by
        # the returned-for-correction amendment flow.
        target_exists = (
            AccountReport.objects.filter(doc_ref_id=corr, filing__rfi=filing.rfi)
            .filter(Q(filing=filing) | ~Q(filing__status=Filing.Status.DRAFT))
            .exclude(pk=record.pk)
            .exists()
        )
        if not target_exists:
            record_finding(
                record,
                "ERROR",
                "R-502",
                f"CorrDocRefId '{corr}' does not match the DocRefId of any previously "
                "filed record for this institution.",
            )
        elif (
            AccountReport.objects.filter(corr_doc_ref_id=corr)
            .exclude(filing=filing)
            .exclude(filing__status=Filing.Status.DRAFT)
            .exists()
        ):
            record_finding(
                record,
                "ERROR",
                "R-503",
                f"The record with DocRefId '{corr}' has already been corrected by a "
                "previously submitted filing, which supersedes it. Point CorrDocRefId "
                "at the DocRefId of the latest version of the record.",
            )

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
            if record.tin_unavailable_reason.strip():
                record_finding(
                    record,
                    "WARNING",
                    "R-201",
                    "No TIN reported; a reason was provided. Partner jurisdictions may still query the record.",
                )
            else:
                record_finding(
                    record,
                    "ERROR",
                    "R-204",
                    "No TIN reported and no reason given. CRS 2.0 requires a reason when a TIN is unavailable.",
                )
        elif not TIN_PATTERN.match(record.foreign_tin.strip()):
            record_finding(
                record,
                "ERROR",
                "R-202",
                f"TIN '{record.foreign_tin}' fails the plausibility check.",
            )

        # Address is mandatory in the CRS AccountHolder element.
        if not record.holder_address.strip():
            record_finding(record, "ERROR", "R-106", "Account holder address is missing (mandatory in CRS).")

        # Self-certification / due diligence.
        if not record.self_certification:
            record_finding(record, "WARNING", "R-205", "No self-certification status recorded for the account.")
        elif record.is_undocumented:
            record_finding(
                record,
                "WARNING",
                "R-206",
                "Account is undocumented: holder self-certification was not obtained.",
            )

        # Entity account holders: classification and controlling persons.
        if record.holder_type == "ORGANISATION":
            if not record.acct_holder_type:
                record_finding(record, "WARNING", "R-107", "Entity account holder type (CRS101/102/103) is not set.")
            if record.requires_controlling_persons and not record.controlling_persons.exists():
                record_finding(
                    record,
                    "ERROR",
                    "R-108",
                    "Passive NFE (CRS101) reports no controlling persons.",
                )

        if record.balance < 0:
            record_finding(record, "ERROR", "R-301", f"Account balance {record.balance} is negative.")
        # The CRS User Guide (IVg) directs that a closed account is reported
        # with a zero balance alongside the ClosedAccount attribute.
        if record.closed_account and record.balance != 0:
            record_finding(
                record,
                "WARNING",
                "R-302",
                f"Account is flagged closed but reports a balance of {record.balance}; "
                "CRS expects a zero balance for a closed account.",
            )
        # NOTE: a missing structured last name is deliberately *not* a finding.
        # The emitter falls back to FirstName 'NFN' with the full name as
        # LastName, which the CRS User Guide (IIc) expressly permits, and
        # warnings block submission here — so flagging it would halt every
        # filing carrying only a free-text name.
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
