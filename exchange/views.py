"""Exchange workspace: packaging, simulated CTS, status messages, corrections.

The transmit pipeline is presentation theatre; the CRS XML and MessageRefID
underneath are real and correct. The act-as-partner controls let the
presenter drive both directions of the exchange without a live CTS.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core import access, config
from core.decorators import require_cap
from core.models import AuditLog
from exchange.matching import match_inbound_file, resolve_unmatched_identities
from exchange.models import (
    PIPELINE_STEPS,
    ExchangePackage,
    InboundFile,
    InboundRecord,
    PartnerJurisdiction,
    RecordError,
    StatusMessage,
)
from exchange.services import (
    build_nil_packages,
    build_packages,
    days_to_exchange_deadline,
    generate_crs_xml,
    next_doc_ref_id,
    next_message_ref_id,
)
from portal.models import AccountReport

# Indicative CTS error codes, styled on the CRS Status Message conventions.
FILE_ERROR_CHOICES: list[tuple[str, str]] = [
    ("50003", "Failed decryption"),
    ("50008", "Invalid MessageRefID"),
    ("50010", "Duplicate MessageRefID"),
]
RECORD_ERROR_CHOICES: list[tuple[str, str]] = [
    ("80008", "TIN not supplied"),
    ("80003", "Invalid country code"),
]


def _audit(request, action: str, target: str, detail: str, before: str = "", after: str = "") -> None:
    AuditLog.record(
        actor_name=request.credential.officer.name,
        actor_role=request.credential.role_display,
        credential=request.credential,
        action=action,
        target=target,
        before_state=before,
        after_state=after,
        detail=detail,
    )


@require_cap(access.VIEW_EXCHANGE)
def workspace(request):
    """Partners, outgoing packages, and the package builder."""
    eligible = AccountReport.objects.filter(
        filing__status="ACCEPTED",
        filing__reporting_year=config.CURRENT_REPORTING_YEAR,
        superseded=False,
        packages__isnull=True,
    ).count()
    return render(
        request,
        "backoffice/exchange_workspace.html",
        {
            "partners": PartnerJurisdiction.objects.all(),
            "packages": ExchangePackage.objects.select_related("jurisdiction"),
            "eligible_records": eligible,
            "deadline_days": days_to_exchange_deadline(),
            "nav": "exchange",
        },
    )


@require_cap(access.OPERATE_EXCHANGE)
def build(request):
    """Sort approved records by residence jurisdiction into per-partner packages."""
    if request.method != "POST":
        return redirect("/backoffice/exchange/")
    packages = build_packages(config.CURRENT_REPORTING_YEAR)
    if packages:
        officer = request.credential.officer
        for package in packages:
            package.responsible_officer = officer
            package.save(update_fields=["responsible_officer"])
            _audit(
                request,
                "PACKAGE_BUILT",
                package.message_ref_id,
                f"Package built for {package.jurisdiction.name} with {package.records.count()} records.",
                after=package.status,
            )
        messages.success(request, f"{len(packages)} exchange package(s) built by partner jurisdiction.")
    else:
        messages.error(request, "No unpackaged approved records are available for activated partners.")
    return redirect("/backoffice/exchange/")


@require_cap(access.OPERATE_EXCHANGE)
def build_nil(request):
    """Build CRS703 nil returns for partners with nothing to exchange this year."""
    if request.method != "POST":
        return redirect("/backoffice/exchange/")
    packages = build_nil_packages(config.CURRENT_REPORTING_YEAR)
    if packages:
        officer = request.credential.officer
        for package in packages:
            package.responsible_officer = officer
            package.save(update_fields=["responsible_officer"])
            _audit(
                request,
                "NIL_RETURN_BUILT",
                package.message_ref_id,
                f"CRS703 nil return built for {package.jurisdiction.name}.",
                after=package.status,
            )
        messages.success(
            request,
            f"{len(packages)} nil return(s) built for partners with nothing to exchange this year.",
        )
    else:
        messages.error(
            request,
            "Every activated partner already has a package this year or has records awaiting packaging.",
        )
    return redirect("/backoffice/exchange/")


@require_cap(access.VIEW_EXCHANGE)
def package_detail(request, package_id: int):
    package = get_object_or_404(ExchangePackage, pk=package_id)
    open_errors = RecordError.objects.filter(
        status_message__package=package, resolved=False
    ).select_related("status_message")
    return render(
        request,
        "backoffice/exchange_package.html",
        {
            "package": package,
            "records": package.records.select_related("filing__rfi"),
            "pipeline": package.pipeline_display(),
            "status_messages": package.status_messages.all(),
            "open_errors": open_errors,
            "file_error_choices": FILE_ERROR_CHOICES,
            "record_error_choices": RECORD_ERROR_CHOICES,
            "can_transmit": package.status in (ExchangePackage.Status.BUILT, ExchangePackage.Status.TRANSMITTING),
            "awaiting_partner": package.status == ExchangePackage.Status.TRANSMITTED,
            "nav": "exchange",
        },
    )


@require_cap(access.OPERATE_EXCHANGE)
def package_advance(request, package_id: int):
    """Advance the simulated encrypt-and-transmit pipeline one step."""
    package = get_object_or_404(ExchangePackage, pk=package_id)
    if request.method != "POST" or package.status not in (
        ExchangePackage.Status.BUILT,
        ExchangePackage.Status.TRANSMITTING,
    ):
        return redirect(f"/backoffice/exchange/packages/{package.pk}/")
    package.status = ExchangePackage.Status.TRANSMITTING
    package.pipeline_step = min(package.pipeline_step + 1, len(PIPELINE_STEPS) + 1)
    if package.pipeline_step > len(PIPELINE_STEPS):
        package.status = ExchangePackage.Status.TRANSMITTED
        package.transmitted_at = timezone.now()
        _audit(
            request,
            "PACKAGE_TRANSMITTED",
            package.message_ref_id,
            f"Package uploaded to the CTS inbox of {package.jurisdiction.name}.",
            after=package.status,
        )
        messages.success(request, f"{package.message_ref_id} uploaded to the CTS inbox of {package.jurisdiction.name}.")
    package.save()
    return redirect(f"/backoffice/exchange/packages/{package.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def package_partner_response(request, package_id: int):
    """Demo control: act as the partner jurisdiction on a transmitted package."""
    package = get_object_or_404(ExchangePackage, pk=package_id, status=ExchangePackage.Status.TRANSMITTED)
    if request.method != "POST":
        return redirect(f"/backoffice/exchange/packages/{package.pk}/")
    outcome = request.POST.get("outcome", "")
    before = package.status
    if outcome == "accept":
        StatusMessage.objects.create(
            direction=StatusMessage.Direction.INBOUND,
            outcome=StatusMessage.Outcome.ACCEPTED,
            package=package,
            detail=f"{package.jurisdiction.name} accepted {package.message_ref_id}. No errors reported.",
        )
        package.status = ExchangePackage.Status.ARCHIVED
        package.closed_at = timezone.now()
        package.save()
        _audit(
            request,
            "PACKAGE_ACCEPTED",
            package.message_ref_id,
            "Inbound Status Message: Accepted. Exchange cycle closed and archived.",
            before=before,
            after=package.status,
        )
        messages.success(request, "Accepted Status Message received. Package archived.")
    elif outcome == "accept_warning":
        note = request.POST.get("warning_detail", "").strip() or "Non-blocking data-quality warnings noted."
        StatusMessage.objects.create(
            direction=StatusMessage.Direction.INBOUND,
            outcome=StatusMessage.Outcome.WARNING,
            package=package,
            detail=f"{package.jurisdiction.name} accepted {package.message_ref_id} with warnings: {note}",
        )
        package.status = ExchangePackage.Status.ARCHIVED
        package.closed_at = timezone.now()
        package.save()
        _audit(
            request,
            "PACKAGE_ACCEPTED_WARNING",
            package.message_ref_id,
            f"Inbound Status Message: accepted with warnings. {note} Exchange cycle closed and archived.",
            before=before,
            after=package.status,
        )
        messages.success(request, "Accepted-with-warnings Status Message received. Package archived.")
    elif outcome == "file_error":
        code = request.POST.get("file_error_code", "50003")
        label = dict(FILE_ERROR_CHOICES).get(code, "File error")
        StatusMessage.objects.create(
            direction=StatusMessage.Direction.INBOUND,
            outcome=StatusMessage.Outcome.FILE_ERROR,
            package=package,
            error_code=code,
            detail=f"{package.jurisdiction.name} rejected the whole file: {code} {label}.",
        )
        package.status = ExchangePackage.Status.FILE_ERROR
        package.save()
        _audit(
            request,
            "PACKAGE_FILE_ERROR",
            package.message_ref_id,
            f"Inbound Status Message: file error {code} {label}. Package reopened for resubmission.",
            before=before,
            after=package.status,
        )
        messages.error(request, f"File error {code} received. The whole package must be resubmitted.")
    elif outcome == "record_errors":
        record_ids = request.POST.getlist("record_ids")
        code = request.POST.get("record_error_code", "80008")
        label = dict(RECORD_ERROR_CHOICES).get(code, "Record error")
        selected = package.records.filter(pk__in=record_ids)
        if not selected.exists():
            messages.error(request, "Select at least one record for the record-error response.")
            return redirect(f"/backoffice/exchange/packages/{package.pk}/")
        status_message = StatusMessage.objects.create(
            direction=StatusMessage.Direction.INBOUND,
            outcome=StatusMessage.Outcome.RECORD_ERROR,
            package=package,
            error_code=code,
            detail=f"{package.jurisdiction.name} reported {selected.count()} record error(s): {code} {label}.",
        )
        for record in selected:
            RecordError.objects.create(
                status_message=status_message,
                doc_ref_id=record.doc_ref_id,
                code=code,
                detail=label,
            )
        package.status = ExchangePackage.Status.RECORD_ERRORS
        package.save()
        _audit(
            request,
            "PACKAGE_RECORD_ERRORS",
            package.message_ref_id,
            f"Inbound Status Message: {selected.count()} record error(s) {code}. Correction queue opened.",
            before=before,
            after=package.status,
        )
        messages.error(request, f"Record errors received on {selected.count()} record(s). Work them in the correction queue.")
    return redirect(f"/backoffice/exchange/packages/{package.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def package_resubmit(request, package_id: int):
    """After a file error, rebuild the envelope under a fresh MessageRefID."""
    package = get_object_or_404(ExchangePackage, pk=package_id, status=ExchangePackage.Status.FILE_ERROR)
    if request.method != "POST":
        return redirect(f"/backoffice/exchange/packages/{package.pk}/")
    old_ref = package.message_ref_id
    package.message_ref_id = next_message_ref_id(package.jurisdiction.code, package.reporting_year)
    package.pipeline_step = 1
    package.status = ExchangePackage.Status.BUILT
    package.xml_content = generate_crs_xml(package)
    package.save()
    _audit(
        request,
        "PACKAGE_RESUBMISSION_PREPARED",
        package.message_ref_id,
        f"Package {old_ref} rebuilt for resubmission as {package.message_ref_id}.",
        before=ExchangePackage.Status.FILE_ERROR,
        after=package.status,
    )
    messages.success(request, f"Package rebuilt as {package.message_ref_id}. Run the pipeline to retransmit.")
    return redirect(f"/backoffice/exchange/packages/{package.pk}/")


@require_cap(access.VIEW_EXCHANGE)
def corrections(request):
    """Open record errors awaiting a CRS702 correction message."""
    open_errors = (
        RecordError.objects.filter(resolved=False)
        .select_related("status_message__package__jurisdiction")
        .order_by("status_message__package__message_ref_id", "doc_ref_id")
    )
    by_package: dict = {}
    for error in open_errors:
        package = error.status_message.package
        if package is not None:
            by_package.setdefault(package, []).append(error)
    groups = [
        {
            "package": package,
            "errors": errors,
            "records": AccountReport.objects.filter(doc_ref_id__in=[e.doc_ref_id for e in errors]),
        }
        for package, errors in by_package.items()
    ]
    return render(
        request,
        "backoffice/exchange_corrections.html",
        {"groups": groups, "nav": "corrections"},
    )


@require_cap(access.OPERATE_EXCHANGE)
def correct_package(request, package_id: int):
    """Prepare and stage the CRS702 correction message for one package.

    Each corrected record is a new AccountReport carrying CorrDocRefID to the
    original DocRefID and DocTypeIndic OECD2 (or OECD3 for a deletion).
    """
    package = get_object_or_404(ExchangePackage, pk=package_id, status=ExchangePackage.Status.RECORD_ERRORS)
    open_errors = RecordError.objects.filter(status_message__package=package, resolved=False)
    if request.method != "POST":
        return redirect("/backoffice/exchange/corrections/")
    corrected_records: list[AccountReport] = []
    for error in open_errors:
        original = AccountReport.objects.filter(doc_ref_id=error.doc_ref_id).first()
        if original is None:
            continue
        action = request.POST.get(f"action_{error.pk}", "correct")
        if action == "delete":
            replacement = AccountReport.objects.create(
                filing=original.filing,
                doc_ref_id=next_doc_ref_id(original.filing.rfi, package.reporting_year),
                corr_doc_ref_id=original.doc_ref_id,
                doc_type_indic=AccountReport.DocTypeIndic.OECD3,
                holder_name=original.holder_name,
                holder_type=original.holder_type,
                residence_country=original.residence_country,
                foreign_tin=original.foreign_tin,
                account_number=original.account_number,
                balance=original.balance,
            )
        else:
            new_tin = request.POST.get(f"tin_{error.pk}", original.foreign_tin).strip()
            new_country = request.POST.get(f"country_{error.pk}", original.residence_country).strip().upper()
            try:
                new_balance = Decimal(request.POST.get(f"balance_{error.pk}", str(original.balance)) or "0")
            except InvalidOperation:
                new_balance = original.balance
            replacement = AccountReport.objects.create(
                filing=original.filing,
                doc_ref_id=next_doc_ref_id(original.filing.rfi, package.reporting_year),
                corr_doc_ref_id=original.doc_ref_id,
                doc_type_indic=AccountReport.DocTypeIndic.OECD2,
                holder_name=original.holder_name,
                holder_type=original.holder_type,
                residence_country=new_country or original.residence_country,
                foreign_tin=new_tin,
                account_number=original.account_number,
                balance=new_balance,
                dividends=original.dividends,
                interest=original.interest,
                gross_proceeds=original.gross_proceeds,
                other_income=original.other_income,
            )
        original.superseded = True
        original.save(update_fields=["superseded"])
        error.resolved = True
        error.correction_record = replacement
        error.save(update_fields=["resolved", "correction_record"])
        corrected_records.append(replacement)
    if not corrected_records:
        messages.error(request, "No records were corrected.")
        return redirect("/backoffice/exchange/corrections/")
    correction = ExchangePackage.objects.create(
        jurisdiction=package.jurisdiction,
        regime=package.regime,
        reporting_year=package.reporting_year,
        message_ref_id=next_message_ref_id(package.jurisdiction.code, package.reporting_year),
        message_type="CRS702",
        file_version=package.file_version + 1,
        responsible_officer=request.credential.officer,
        corrects_package=package,
    )
    correction.records.set(corrected_records)
    correction.xml_content = generate_crs_xml(correction)
    correction.save()
    package.status = ExchangePackage.Status.ARCHIVED
    package.closed_at = timezone.now()
    package.save(update_fields=["status", "closed_at"])
    _audit(
        request,
        "CORRECTION_PACKAGE_BUILT",
        correction.message_ref_id,
        f"CRS702 correction built for {package.message_ref_id} with {len(corrected_records)} corrected record(s). "
        f"Each carries CorrDocRefID to the original DocRefID.",
        after=correction.status,
    )
    messages.success(
        request,
        f"CRS702 correction {correction.message_ref_id} built with {len(corrected_records)} record(s). Run the pipeline to retransmit.",
    )
    return redirect(f"/backoffice/exchange/packages/{correction.pk}/")


@require_cap(access.VIEW_EXCHANGE)
def inbound_list(request):
    return render(
        request,
        "backoffice/exchange_inbound.html",
        {
            "files": InboundFile.objects.select_related("jurisdiction"),
            "partners": PartnerJurisdiction.objects.all(),
            "nav": "inbound",
        },
    )


@require_cap(access.VIEW_EXCHANGE)
def inbound_detail(request, file_id: int):
    inbound = get_object_or_404(InboundFile, pk=file_id)
    records = inbound.records.select_related("matched_taxpayer")
    return render(
        request,
        "backoffice/exchange_inbound_detail.html",
        {
            "inbound": inbound,
            "records": records,
            "status_messages": inbound.status_messages.all(),
            "error_records": inbound.records.filter(has_error=True, corrected=False),
            "matched_count": records.filter(match_status=InboundRecord.MatchStatus.MATCHED).count(),
            "unmatched_count": records.filter(match_status=InboundRecord.MatchStatus.UNMATCHED).count(),
            "nav": "inbound",
        },
    )


@require_cap(access.OPERATE_EXCHANGE)
def inbound_file_check(request, file_id: int):
    """Stage one: simulated decrypt plus schema check on the whole file."""
    inbound = get_object_or_404(InboundFile, pk=file_id, status=InboundFile.Status.RECEIVED)
    if request.method != "POST":
        return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")
    # The officer who first processes the file owns its review thereafter.
    if inbound.assigned_reviewer_id is None:
        inbound.assigned_reviewer = request.credential.officer
    if inbound.simulate_file_error:
        label = dict(FILE_ERROR_CHOICES).get(inbound.simulate_file_error, "File error")
        inbound.status = InboundFile.Status.FILE_ERROR
        inbound.file_checked_at = timezone.now()
        inbound.save()
        StatusMessage.objects.create(
            direction=StatusMessage.Direction.OUTBOUND,
            outcome=StatusMessage.Outcome.FILE_ERROR,
            inbound_file=inbound,
            error_code=inbound.simulate_file_error,
            detail=(
                f"File-error Status Message issued to {inbound.jurisdiction.name}: "
                f"{inbound.simulate_file_error} {label}. The whole file is rejected."
            ),
        )
        _audit(
            request,
            "INBOUND_FILE_REJECTED",
            inbound.message_ref_id,
            f"Stage one failed: {inbound.simulate_file_error} {label}. Outbound file-error Status Message issued.",
            after=inbound.status,
        )
        messages.error(request, f"File-level check failed ({inbound.simulate_file_error} {label}). Rejection sent to the partner.")
    else:
        inbound.file_checked_at = timezone.now()
        inbound.save(update_fields=["file_checked_at", "assigned_reviewer"])
        _audit(
            request,
            "INBOUND_FILE_CHECK_PASSED",
            inbound.message_ref_id,
            "Stage one passed: payload decrypted and schema-valid. Proceed to record-level checks.",
        )
        messages.success(request, "File-level checks passed. Run the record-level checks.")
    return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def inbound_record_check(request, file_id: int):
    """Stage two: record-level checks; failures raise a record-error message."""
    inbound = get_object_or_404(
        InboundFile,
        pk=file_id,
        status__in=[InboundFile.Status.RECEIVED, InboundFile.Status.RECORD_ERRORS],
    )
    if request.method != "POST" or inbound.file_checked_at is None:
        return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")
    failing = inbound.records.filter(has_error=True, corrected=False)
    inbound.records_checked_at = timezone.now()
    if failing.exists():
        inbound.status = InboundFile.Status.RECORD_ERRORS
        inbound.save()
        status_message = StatusMessage.objects.create(
            direction=StatusMessage.Direction.OUTBOUND,
            outcome=StatusMessage.Outcome.RECORD_ERROR,
            inbound_file=inbound,
            detail=(
                f"Record-error Status Message issued to {inbound.jurisdiction.name} "
                f"listing {failing.count()} record(s)."
            ),
        )
        for record in failing:
            RecordError.objects.create(
                status_message=status_message,
                doc_ref_id=record.doc_ref_id,
                code=record.error_code or "80000",
                detail=record.error_detail or "Record failed validation.",
            )
        _audit(
            request,
            "INBOUND_RECORD_ERRORS",
            inbound.message_ref_id,
            f"Stage two: {failing.count()} record(s) failed. Outbound record-error Status Message issued.",
            after=inbound.status,
        )
        messages.error(request, f"{failing.count()} record(s) failed. Record-error Status Message sent to the partner.")
    else:
        inbound.status = InboundFile.Status.ACCEPTED
        inbound.save()
        StatusMessage.objects.create(
            direction=StatusMessage.Direction.OUTBOUND,
            outcome=StatusMessage.Outcome.ACCEPTED,
            inbound_file=inbound,
            detail=f"Accepted Status Message issued to {inbound.jurisdiction.name}: file processed with no errors.",
        )
        _audit(
            request,
            "INBOUND_FILE_ACCEPTED",
            inbound.message_ref_id,
            "Stage two passed. Explicit Accepted Status Message issued to the partner.",
            after=inbound.status,
        )
        messages.success(request, "All records passed. Accepted Status Message sent to the partner.")
    return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def inbound_partner_correction(request, file_id: int):
    """Demo control: the partner sends corrected records back."""
    inbound = get_object_or_404(InboundFile, pk=file_id, status=InboundFile.Status.RECORD_ERRORS)
    if request.method != "POST":
        return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")
    corrected = 0
    for record in inbound.records.filter(has_error=True, corrected=False):
        record.corrected = True
        if record.error_code == "80008" and not record.ng_tin:
            record.ng_tin = f"NGTIN{record.pk:07d}"
        record.error_code = ""
        record.error_detail = ""
        record.save()
        corrected += 1
    RecordError.objects.filter(
        status_message__inbound_file=inbound, resolved=False
    ).update(resolved=True)
    _audit(
        request,
        "INBOUND_PARTNER_CORRECTION",
        inbound.message_ref_id,
        f"Partner returned {corrected} corrected record(s). Rerun the record-level checks.",
    )
    messages.success(request, f"Partner correction received for {corrected} record(s). Rerun the record-level checks.")
    return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def inbound_approve(request, file_id: int):
    inbound = get_object_or_404(InboundFile, pk=file_id, status=InboundFile.Status.ACCEPTED)
    if request.method == "POST":
        inbound.status = InboundFile.Status.APPROVED
        inbound.approved_at = timezone.now()
        inbound.save()
        _audit(
            request,
            "INBOUND_APPROVED_DOMESTIC",
            inbound.message_ref_id,
            "File approved for domestic use.",
            before=InboundFile.Status.ACCEPTED,
            after=inbound.status,
        )
        messages.success(request, "Approved for domestic use. The file can now be disseminated.")
    return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def inbound_match(request, file_id: int):
    """IB-08/09: match the file's records to Nigerian taxpayers and risk-profile.

    Runs on an approved file and is safe to rerun while records remain
    unmatched (the reattempt loop). A file with at least one match advances
    to Matched; dissemination then carries the matched records forward.
    """
    inbound = get_object_or_404(
        InboundFile,
        pk=file_id,
        status__in=[InboundFile.Status.APPROVED, InboundFile.Status.MATCHED],
    )
    if request.method != "POST":
        return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")
    summary = match_inbound_file(inbound)
    inbound.matched_at = timezone.now()
    if summary["matched"]:
        inbound.status = InboundFile.Status.MATCHED
    inbound.save(update_fields=["status", "matched_at"])
    _audit(
        request,
        "INBOUND_MATCHED",
        inbound.message_ref_id,
        f"Taxpayer matching run: {summary['matched']} matched, {summary['unmatched']} unmatched.",
        after=inbound.status,
    )
    if summary["unmatched"]:
        messages.warning(
            request,
            f"{summary['matched']} record(s) matched and risk-profiled; "
            f"{summary['unmatched']} unmatched and held for reattempt.",
        )
    else:
        messages.success(
            request,
            f"All {summary['matched']} record(s) matched to taxpayers and risk-profiled.",
        )
    return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def inbound_resolve_identities(request, file_id: int):
    """Demo control: resolve unmatched identities so the reattempt loop closes."""
    inbound = get_object_or_404(InboundFile, pk=file_id, status=InboundFile.Status.MATCHED)
    if request.method != "POST":
        return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")
    added = resolve_unmatched_identities(inbound)
    _audit(
        request,
        "INBOUND_IDENTITY_RESOLVED",
        inbound.message_ref_id,
        f"{added} unmatched holder(s) added to the taxpayer register for reattempt.",
    )
    messages.success(
        request,
        f"{added} identity(ies) resolved and added to the register. Rerun taxpayer matching.",
    )
    return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")


@require_cap(access.OPERATE_EXCHANGE)
def inbound_disseminate(request, file_id: int):
    inbound = get_object_or_404(InboundFile, pk=file_id, status=InboundFile.Status.MATCHED)
    if request.method == "POST":
        disseminated = inbound.records.filter(
            match_status=InboundRecord.MatchStatus.MATCHED
        ).count()
        inbound.status = InboundFile.Status.DISSEMINATED
        inbound.disseminated_at = timezone.now()
        inbound.save()
        _audit(
            request,
            "INBOUND_DISSEMINATED",
            inbound.message_ref_id,
            f"{disseminated} matched record(s) disseminated to the Tax Authority View.",
            before=InboundFile.Status.MATCHED,
            after=inbound.status,
        )
        messages.success(
            request,
            f"{disseminated} matched record(s) disseminated to the Tax Authority View.",
        )
    return redirect(f"/backoffice/exchange/inbound/{inbound.pk}/")


@require_cap(access.VIEW_TAV)
def tax_authority_view(request):
    """Read-only register of disseminated, taxpayer-matched inbound records."""
    records = (
        InboundRecord.objects.filter(
            file__status=InboundFile.Status.DISSEMINATED,
            match_status=InboundRecord.MatchStatus.MATCHED,
        )
        .select_related("file__jurisdiction", "matched_taxpayer")
        .order_by("risk_rating", "holder_name")
    )
    order = {
        InboundRecord.RiskRating.ENHANCED: 0,
        InboundRecord.RiskRating.SPECIFIC: 1,
        InboundRecord.RiskRating.GENERAL: 2,
    }
    records = sorted(records, key=lambda r: order.get(r.risk_rating, 3))
    counts = {
        "enhanced": sum(1 for r in records if r.risk_rating == InboundRecord.RiskRating.ENHANCED),
        "specific": sum(1 for r in records if r.risk_rating == InboundRecord.RiskRating.SPECIFIC),
        "general": sum(1 for r in records if r.risk_rating == InboundRecord.RiskRating.GENERAL),
    }
    return render(
        request,
        "backoffice/tax_authority_view.html",
        {"records": records, "counts": counts, "nav": "tav"},
    )


@require_cap(access.VIEW_EXCHANGE)
def cts_monitoring(request):
    """CTS monitoring: operational tiles, exception report, certificate inventory.

    Serves RR-CTS-001 (dashboard), RR-CTS-003 (exceptions), FR-CTS-010 (alerts)
    and OR-CTS-006 / AC-CTS-006 (certificate inventory and expiry alerts).
    """
    from exchange.models import TransmissionCertificate
    from exchange.services import cts_exceptions

    ex = cts_exceptions()
    packages = ExchangePackage.objects.all()
    tiles = {
        "ready": packages.filter(status=ExchangePackage.Status.BUILT).count(),
        "transmitted": packages.filter(status=ExchangePackage.Status.TRANSMITTED).count(),
        "archived": packages.filter(status=ExchangePackage.Status.ARCHIVED).count(),
        "failed": ex["failed_transmissions"].count(),
        "pending_corrections": ex["pending_corrections"].count(),
        "received": InboundFile.objects.exclude(status=InboundFile.Status.DISSEMINATED).count(),
        "rejected_inbound": ex["rejected_inbound"].count(),
        "expiring_certs": ex["expiring_certificates"].count(),
    }
    exception_total = (
        tiles["failed"] + tiles["pending_corrections"] + tiles["rejected_inbound"]
        + ex["unresolved_record_errors"].count() + tiles["expiring_certs"]
    )
    return render(
        request,
        "backoffice/cts_monitoring.html",
        {
            "tiles": tiles,
            "exceptions": ex,
            "exception_total": exception_total,
            "certificates": TransmissionCertificate.objects.select_related("jurisdiction"),
            "warning_days": config.CERTIFICATE_EXPIRY_WARNING_DAYS,
            "nav": "cts-monitoring",
        },
    )
