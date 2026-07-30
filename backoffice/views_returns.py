"""Returns workspace: validation by processing users, approval by Internal Admin.

Four-eyes on the same filing: whoever validated cannot be the person who
approves it for exchange.
"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from backoffice.validation import blocking_findings, run_validation
from core import access
from core.decorators import require_cap
from core.models import AuditLog
from portal.models import Filing, ValidationFinding


@require_cap(access.VIEW_RETURNS)
def queue(request):
    """Filings by processing stage, including the nil return register."""
    awaiting_validation = Filing.objects.filter(status=Filing.Status.SUBMITTED).select_related("rfi")
    under_validation = Filing.objects.filter(status=Filing.Status.UNDER_VALIDATION).select_related("rfi")
    returned = Filing.objects.filter(status=Filing.Status.RETURNED).select_related("rfi")
    accepted = Filing.objects.filter(
        status__in=[Filing.Status.ACCEPTED, Filing.Status.IN_EXCHANGE]
    ).select_related("rfi")
    nil_returns = Filing.objects.filter(kind=Filing.Kind.NIL).select_related("rfi")
    return render(
        request,
        "backoffice/returns_queue.html",
        {
            "awaiting_validation": awaiting_validation,
            "under_validation": under_validation,
            "returned": returned,
            "accepted": accepted,
            "nil_returns": nil_returns,
            "nav": "returns",
        },
    )


@require_cap(access.VIEW_RETURNS)
def detail(request, filing_id: int):
    filing = get_object_or_404(Filing, pk=filing_id)
    caps = request.caps
    credential = getattr(request, "credential", None)
    officer = credential.officer if credential else None
    file_findings = filing.findings.filter(account_report__isnull=True)
    record_findings = filing.findings.filter(account_report__isnull=False).select_related("account_report")
    blocking = blocking_findings(filing)
    validated = filing.validated_at is not None
    can_validate = access.ACT_RETURNS in caps and filing.status in (
        Filing.Status.SUBMITTED,
        Filing.Status.UNDER_VALIDATION,
    )
    four_eyes_blocked = (
        access.APPROVE_RETURNS in caps
        and filing.status == Filing.Status.UNDER_VALIDATION
        and officer is not None
        and filing.validated_by_id == officer.pk
    )
    can_approve = (
        access.APPROVE_RETURNS in caps
        and filing.status == Filing.Status.UNDER_VALIDATION
        and validated
        and not blocking.exists()
        and not four_eyes_blocked
    )
    can_override = access.APPROVE_RETURNS in caps and filing.status == Filing.Status.UNDER_VALIDATION
    return render(
        request,
        "backoffice/returns_detail.html",
        {
            "filing": filing,
            "records": filing.account_reports.prefetch_related("controlling_persons"),
            "header_done": bool(
                filing.receiving_country and filing.sending_company_in and filing.message_reference
            ),
            "file_findings": file_findings,
            "record_findings": record_findings,
            "blocking_count": blocking.count(),
            "can_validate": can_validate,
            "can_return": access.ACT_RETURNS in caps
            and filing.status == Filing.Status.UNDER_VALIDATION
            and filing.findings.exists(),
            "can_approve": can_approve,
            "can_override": can_override,
            "four_eyes_blocked": four_eyes_blocked,
            "validated": validated,
            "nav": "returns",
        },
    )


@require_cap(access.ACT_RETURNS)
def validate(request, filing_id: int):
    """Run or rerun the data-quality checks."""
    filing = get_object_or_404(
        Filing, pk=filing_id, status__in=[Filing.Status.SUBMITTED, Filing.Status.UNDER_VALIDATION]
    )
    if request.method != "POST":
        return redirect(f"/backoffice/returns/{filing.pk}/")
    before = filing.status
    file_count, record_count = run_validation(filing)
    filing.status = Filing.Status.UNDER_VALIDATION
    filing.validated_at = timezone.now()
    filing.validated_by = request.credential.officer
    filing.save()
    AuditLog.record(
        actor_name=request.credential.officer.name,
        actor_role=request.credential.role_display,
        credential=request.credential,
        action="FILING_VALIDATED",
        target=filing.reference,
        before_state=before,
        after_state=filing.status,
        detail=f"Validation run: {file_count} file-level, {record_count} record-level findings.",
    )
    if file_count or record_count:
        messages.error(request, f"Validation found {file_count} file-level and {record_count} record-level findings.")
    else:
        messages.success(request, "Validation passed with no findings. The filing awaits Internal Admin approval.")
    return redirect(f"/backoffice/returns/{filing.pk}/")


@require_cap(access.ACT_RETURNS)
def return_to_rfi(request, filing_id: int):
    """Send the filing back to the institution with the recorded errors."""
    filing = get_object_or_404(Filing, pk=filing_id, status=Filing.Status.UNDER_VALIDATION)
    if request.method != "POST":
        return redirect(f"/backoffice/returns/{filing.pk}/")
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Returning a filing requires a reason for the institution.")
        return redirect(f"/backoffice/returns/{filing.pk}/")
    before = filing.status
    filing.status = Filing.Status.RETURNED
    filing.returned_at = timezone.now()
    filing.return_reason = reason
    filing.save()
    AuditLog.record(
        actor_name=request.credential.officer.name,
        actor_role=request.credential.role_display,
        credential=request.credential,
        action="FILING_RETURNED_TO_RFI",
        target=filing.reference,
        before_state=before,
        after_state=filing.status,
        detail=f"Returned to {filing.rfi.legal_name} for correction. Reason: {reason}",
    )
    messages.success(request, f"{filing.reference} returned to {filing.rfi.legal_name} for correction.")
    return redirect("/backoffice/returns/")


@require_cap(access.APPROVE_RETURNS)
def override_warning(request, filing_id: int, finding_id: int):
    """Override a single warning with a recorded justification."""
    filing = get_object_or_404(Filing, pk=filing_id, status=Filing.Status.UNDER_VALIDATION)
    finding = get_object_or_404(
        ValidationFinding, pk=finding_id, filing=filing, severity=ValidationFinding.Severity.WARNING
    )
    if request.method != "POST":
        return redirect(f"/backoffice/returns/{filing.pk}/")
    justification = request.POST.get("justification", "").strip()
    if not justification:
        messages.error(request, "An override requires a recorded justification.")
        return redirect(f"/backoffice/returns/{filing.pk}/")
    finding.overridden = True
    finding.override_justification = justification
    finding.overridden_by = request.credential.officer
    finding.save()
    AuditLog.record(
        actor_name=request.credential.officer.name,
        actor_role=request.credential.role_display,
        credential=request.credential,
        action="VALIDATION_WARNING_OVERRIDDEN",
        target=f"{filing.reference} / {finding.code}",
        detail=f"Warning overridden: {finding.message} Justification: {justification}",
    )
    messages.success(request, f"Warning {finding.code} overridden with justification.")
    return redirect(f"/backoffice/returns/{filing.pk}/")


@require_cap(access.APPROVE_RETURNS)
def approve(request, filing_id: int):
    """Approve a validated filing for exchange. Four-eyes with the validator."""
    filing = get_object_or_404(Filing, pk=filing_id, status=Filing.Status.UNDER_VALIDATION)
    if request.method != "POST":
        return redirect(f"/backoffice/returns/{filing.pk}/")
    officer = request.credential.officer
    if filing.validated_at is None:
        messages.error(request, "The filing has not been validated.")
        return redirect(f"/backoffice/returns/{filing.pk}/")
    if filing.validated_by_id == officer.pk:
        messages.error(request, "Four-eyes control: you validated this filing and cannot also approve it.")
        return redirect(f"/backoffice/returns/{filing.pk}/")
    if blocking_findings(filing).exists():
        messages.error(request, "Blocking findings remain. Return the filing or record overrides first.")
        return redirect(f"/backoffice/returns/{filing.pk}/")
    before = filing.status
    filing.status = Filing.Status.ACCEPTED
    filing.accepted_at = timezone.now()
    filing.approved_by = officer
    filing.save()
    AuditLog.record(
        actor_name=officer.name,
        actor_role=request.credential.role_display,
        credential=request.credential,
        action="FILING_APPROVED",
        target=filing.reference,
        before_state=before,
        after_state=filing.status,
        detail=f"Filing approved for exchange with {filing.account_reports.filter(superseded=False).count()} records.",
    )
    messages.success(request, f"{filing.reference} accepted and approved for exchange.")
    return redirect("/backoffice/returns/")
