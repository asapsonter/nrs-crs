"""Filing surface for RFIs: XML upload, manual entry, nil returns, corrections.

Both the Primary User and Secondary Users prepare and submit filings to the
NRS directly; the maker-checker review step is retired.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from core import config
from core.decorators import portal_required
from core.models import AuditLog
from exchange.services import days_to_domestic_deadline, domestic_deadline, next_doc_ref_id
from portal.models import AccountReport, ControllingPerson, Filing, PortalUser, ValidationFinding
from portal.services import auto_validate_submission, next_filing_reference
import re

from portal.xml_ingest import parse_crs_upload, resolution_hint


def _deadline_context() -> dict:
    days = days_to_domestic_deadline()
    return {
        "deadline_days": days,
        "deadline_date": domestic_deadline(),
        "deadline_overdue": days < 0,
    }


def _audit(profile: PortalUser, action: str, filing: Filing, detail: str, before: str = "", after: str = "") -> None:
    AuditLog.record(
        actor_name=profile.display_name,
        surface="portal",
        action=action,
        target=filing.reference,
        before_state=before,
        after_state=after,
        detail=detail,
    )


@portal_required
def filings(request):
    """All filings for the institution, with the three filing paths."""
    profile = request.portal_profile
    return render(
        request,
        "portal/filings.html",
        {
            "profile": profile,
            "filings": profile.rfi.filings.all(),
            "is_maker": profile.role == PortalUser.Role.MAKER,
            "is_checker": profile.role == PortalUser.Role.CHECKER,
            "delete_mode": request.GET.get("mode") == "delete",
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_delete(request, filing_id: int):
    """Delete a filing that has not yet been submitted to the NRS.

    Guarded to filings still in the institution's hands (draft, pending
    Checker, returned for correction). A filing already submitted to the NRS
    is part of the exchange record and cannot be removed.
    """
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    # Deletion is offered from both the filings list and Draft Filings;
    # return the user to the page they acted from.
    target = request.POST.get("next", "") or "/portal/filings/?mode=delete"
    if not target.startswith("/portal/"):
        target = "/portal/filings/?mode=delete"
    if request.method != "POST":
        return redirect("/portal/filings/?mode=delete")
    if not filing.is_deletable:
        messages.error(request, "A filing submitted to the NRS cannot be deleted.")
        return redirect(target)
    reference = filing.reference
    _audit(
        profile,
        "FILING_DELETED",
        filing,
        f"Filing {reference} deleted by {profile.get_role_display()} while in {filing.get_status_display()}.",
        before=filing.status,
    )
    filing.delete()
    messages.success(request, f"Filing {reference} was deleted.")
    return redirect(target)


# Statuses in which the institution may still edit a filing. PENDING_CHECKER
# is retained for legacy filings staged under the retired maker-checker flow.
_OPEN_STATUSES = (Filing.Status.DRAFT, Filing.Status.PENDING_CHECKER)

# Filing types offered on the Create Filing entry page, in display order.
FILING_TYPE_OPTIONS = [
    (Filing.Kind.MANUAL, "CRS Manual Entry Filing"),
    (Filing.Kind.XML_UPLOAD, "CRS XML upload Filing"),
    (Filing.Kind.EXCEL_UPLOAD, "CRS Excel Upload Filing"),
    (Filing.Kind.PU_CHANGE, "Primary User Change Notice"),
    (Filing.Kind.ENTITY_DEACTIVATION, "Reporting Entity Deactivation"),
    (Filing.Kind.ENTITY_INFO_CHANGE, "Change of reporting entity information"),
]
_VALID_FILING_TYPES = {value for value, _ in FILING_TYPE_OPTIONS}


@portal_required
def filing_create(request):
    """Create Filing entry page: capture a name, type, and period end date.

    The chosen type routes the user onward: a CRS Manual Entry Filing opens a
    draft to add records; a CRS XML upload Filing goes to the upload step
    carrying the name and period; the three administrative notices are recorded
    as draft filings for the institution to complete.
    """
    profile = request.portal_profile
    values = {"name": "", "filing_type": "", "period_end_date": ""}
    errors: list[str] = []
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        filing_type = request.POST.get("filing_type", "").strip()
        period_raw = request.POST.get("period_end_date", "").strip()
        values = {"name": name, "filing_type": filing_type, "period_end_date": period_raw}

        if not name:
            errors.append("Enter a filing name.")
        if filing_type not in _VALID_FILING_TYPES:
            errors.append("Select a filing type.")
        period_end = None
        if not period_raw:
            errors.append("Enter the period end date.")
        else:
            period_end = parse_date(period_raw)
            if period_end is None:
                errors.append("Enter the period end date as a valid date.")

        # Both the Primary User and Secondary Users prepare and submit filings.
        if not errors:
            if filing_type in (Filing.Kind.XML_UPLOAD, Filing.Kind.EXCEL_UPLOAD):
                # The upload step creates the filing; carry the metadata across.
                request.session["pending_filing_meta"] = {
                    "name": name,
                    "period_end_date": period_raw,
                }
                if filing_type == Filing.Kind.EXCEL_UPLOAD:
                    return redirect("/portal/filings/upload/excel/")
                return redirect("/portal/filings/upload/")

            filing = Filing.objects.create(
                reference=next_filing_reference(),
                name=name,
                rfi=profile.rfi,
                reporting_year=config.CURRENT_REPORTING_YEAR,
                period_end_date=period_end,
                kind=filing_type,
                created_by=profile,
            )
            label = dict(FILING_TYPE_OPTIONS)[filing_type]
            _audit(profile, "FILING_CREATED", filing, f"{label} '{name}' opened as draft.", after=filing.status)

            return redirect(f"/portal/filings/{filing.pk}/created/")

    return render(
        request,
        "portal/filing_create.html",
        {
            "filing_types": FILING_TYPE_OPTIONS,
            "values": values,
            "errors": errors,
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_crs_form(request, filing_id: int):
    """CRS Filing General Information form: the message header for a filing.

    Save as draft records what has been entered. Validate & Save requires the
    full message header before saving and moves on to the filing itself.
    """
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    read_only = request.GET.get("mode") == "view"
    errors: list[str] = []
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "clear":
            filing.receiving_country = ""
            filing.sending_company_in = ""
            filing.message_reference = ""
            filing.save(update_fields=["receiving_country", "sending_company_in", "message_reference"])
            _audit(profile, "FILING_HEADER_CLEARED", filing, "CRS message header cleared.")
            messages.success(request, "General Information cleared.")
            return redirect(f"/portal/filings/{filing.pk}/view/")
        filing.receiving_country = request.POST.get("receiving_country", "").strip().upper()
        filing.sending_company_in = request.POST.get("sending_company_in", "").strip()
        message_type = request.POST.get("message_type", "").strip()
        if message_type in Filing.MessageType.values:
            filing.message_type = message_type
        filing.message_reference = request.POST.get("message_reference", "").strip()

        if action == "validate":
            if not filing.receiving_country:
                errors.append("Select a receiving country.")
            if not filing.sending_company_in:
                errors.append("Enter the Sending Company IN.")
            if not filing.message_reference:
                errors.append("Enter a message reference.")

        if not errors:
            filing.save(
                update_fields=[
                    "receiving_country",
                    "sending_company_in",
                    "message_type",
                    "message_reference",
                ]
            )
            if action == "validate":
                _audit(profile, "FILING_HEADER_SAVED", filing, "CRS message header validated and saved.")
                messages.success(request, "General information validated and saved.")
                return redirect(f"/portal/filings/{filing.pk}/view/")
            _audit(profile, "FILING_HEADER_DRAFTED", filing, "CRS message header saved as draft.")
            messages.success(request, "Filing saved as draft.")
            return redirect("/portal/filings/drafts/")

    return render(
        request,
        "portal/filing_crs.html",
        {
            "filing": filing,
            "jurisdictions": _jurisdiction_choices(),
            "message_types": Filing.MessageType.choices,
            "errors": errors,
            "read_only": read_only,
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_notice_form(request, filing_id: int):
    """The administrative notice form: Primary User change, entity
    deactivation, or change of entity information.

    Mirrors the Vizor notice workflow: the form is edited from the Draft
    Filing screen, Save as Draft keeps progress, Validate & Save requires
    every mandatory field and marks the notice Ready to Submit.
    """
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if not filing.is_notice:
        return redirect(f"/portal/filings/{filing.pk}/")
    can_edit = filing.status in (*_OPEN_STATUSES, Filing.Status.RETURNED)
    read_only = request.GET.get("mode") == "view" or not can_edit
    required = Filing.NOTICE_REQUIRED_FIELDS[filing.kind]
    keys = [key for key, _ in required] + list(Filing.NOTICE_OPTIONAL_FIELDS[filing.kind])

    values = {key: filing.notice_payload.get(key, "") for key in keys}
    if filing.kind == Filing.Kind.ENTITY_INFO_CHANGE and not filing.notice_payload:
        # The change form starts from the entity's current registered
        # details, as the Vizor portal pre-populates it from the FI profile.
        rfi = profile.rfi
        values.update(
            legal_name=rfi.legal_name, street=rfi.street, city=rfi.city,
            state_province=rfi.state_province, post_code=rfi.post_code,
            email=rfi.email, phone=rfi.phone,
        )

    errors: list[str] = []
    if request.method == "POST" and not read_only:
        action = request.POST.get("action", "save")
        values = {key: request.POST.get(key, "").strip() for key in keys}
        if action == "validate":
            for key, label in required:
                if not values[key]:
                    errors.append(f"{label} is required.")
            email_key = "new_pu_email" if filing.kind == Filing.Kind.PU_CHANGE else "email"
            if values.get(email_key) and "@" not in values[email_key]:
                errors.append("Enter a valid email address.")
        if not errors:
            filing.notice_payload = {**values, "validated": action == "validate"}
            filing.save(update_fields=["notice_payload"])
            label = filing.get_kind_display()
            if action == "validate":
                _audit(profile, "NOTICE_VALIDATED", filing, f"{label} form validated and saved.")
                messages.success(request, f"{label} validated and saved. The notice is ready to submit.")
                return redirect(f"/portal/filings/{filing.pk}/view/")
            _audit(profile, "NOTICE_DRAFTED", filing, f"{label} form saved as draft.")
            messages.success(request, "Notice saved as draft.")
            return redirect("/portal/filings/drafts/")

    return render(
        request,
        "portal/filing_notice.html",
        {
            "filing": filing,
            "values": values,
            "errors": errors,
            "read_only": read_only,
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_view(request, filing_id: int):
    """Form-tree view of a filing: its CRS structure as an expandable folder
    hierarchy with per-node actions, in the manner of an AEOI filing console.

    Follows the Vizor-style workflow: each form validates individually
    (General Information, Reporting FI Information, and one Account
    Information form per account); when every form is Validated the filing
    becomes Ready to Submit and any portal user submits it to the NRS."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    records = list(
        filing.account_reports.filter(superseded=False).prefetch_related("controlling_persons")
    )
    header_done = bool(filing.receiving_country and filing.sending_company_in and filing.message_reference)
    records_complete = all(record.is_complete for record in records)
    incomplete_count = sum(1 for record in records if not record.is_complete)
    # Ready to Submit: a notice needs its form validated; a CRS return needs
    # the header validated and either a nil return or at least one account
    # form, all of them complete.
    if filing.is_notice:
        ready_to_submit = filing.notice_validated
    else:
        ready_to_submit = header_done and (
            filing.kind == Filing.Kind.NIL or (bool(records) and records_complete)
        )
    # Both Primary and Secondary Users prepare and submit open filings.
    can_edit = filing.status in (Filing.Status.DRAFT, Filing.Status.PENDING_CHECKER) and (
        filing.kind in (Filing.Kind.MANUAL, Filing.Kind.XML_UPLOAD, Filing.Kind.EXCEL_UPLOAD)
        or filing.is_notice
    )
    return render(
        request,
        "portal/filing_view.html",
        {
            "filing": filing,
            "records": records,
            "record_count": len(records),
            "header_done": header_done,
            "ready_to_submit": ready_to_submit,
            "incomplete_count": incomplete_count,
            "can_edit": can_edit,
            "can_submit": can_edit and ready_to_submit,
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def record_delete(request, filing_id: int, record_id: int):
    """Delete one account record from an open filing."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing)
    target = f"/portal/filings/{filing.pk}/view/"
    if request.method != "POST":
        return redirect(target)
    if filing.status not in _OPEN_STATUSES:
        messages.error(request, "Account records can only be deleted while the filing is open.")
        return redirect(target)
    ref = record.doc_ref_id
    record.delete()
    _audit(profile, "RECORD_DELETED", filing, f"Account record {ref} deleted.")
    messages.success(request, "Account record deleted.")
    return redirect(target)


@portal_required
def records_clear(request, filing_id: int):
    """Clear the CRS Report: remove every account record from an open filing."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    target = f"/portal/filings/{filing.pk}/view/"
    if request.method != "POST":
        return redirect(target)
    if filing.status not in _OPEN_STATUSES:
        messages.error(request, "The CRS Report can only be cleared while the filing is open.")
        return redirect(target)
    count = filing.account_reports.count()
    filing.account_reports.all().delete()
    _audit(profile, "RECORDS_CLEARED", filing, f"CRS Report cleared, {count} account records removed.")
    messages.success(request, f"CRS Report cleared. {count} account record{'' if count == 1 else 's'} removed.")
    return redirect(target)


@portal_required
def filing_created(request, filing_id: int):
    """Confirmation screen shown after a filing is created."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    return render(
        request,
        "portal/filing_created.html",
        {"filing": filing, "nav": "filings", **_deadline_context()},
    )


@portal_required
def filing_new_manual(request):
    """Open a manual-entry draft filing (any portal user)."""
    profile = request.portal_profile
    filing = Filing.objects.create(
        reference=next_filing_reference(),
        rfi=profile.rfi,
        reporting_year=config.CURRENT_REPORTING_YEAR,
        kind=Filing.Kind.MANUAL,
        created_by=profile,
    )
    _audit(profile, "FILING_CREATED", filing, "Manual entry filing opened as draft.", after=filing.status)
    messages.success(
        request,
        f"{profile.display_name} ({profile.get_role_display()}), a manual entry draft filing "
        f"({filing.reference}) has been created for {profile.rfi.legal_name}.",
    )
    return redirect(f"/portal/filings/{filing.pk}/")


_ERROR_LINE = re.compile(r"^Line (\d+)(?:, column \d+)?\s*[—:]?\s*(.*)$", re.S)


def _upload_error_rows(errors: list[str]) -> list[dict]:
    """Structure upload rejections for display: line, cause, resolution."""
    rows = []
    for message in errors:
        line, cause = None, message
        match = _ERROR_LINE.match(message)
        if match:
            line, cause = match.group(1), match.group(2)
        rows.append({"line": line, "cause": cause, "hint": resolution_hint(message)})
    return rows


@portal_required
def filing_upload(request):
    """CRS XML upload: the official CRS_OECD v2.0 document or the simplified
    CRSFiling form, auto-detected (any portal user)."""
    profile = request.portal_profile
    errors: list[str] = []
    if request.method == "POST":
        upload = request.FILES.get("crs_file")
        if upload is None:
            errors = ["Choose a CRS XML file."]
        else:
            result = parse_crs_upload(upload.read(), config.CURRENT_REPORTING_YEAR)
            _reject_already_filed(result)
            if not result.ok:
                errors = result.errors
            else:
                meta = request.session.pop("pending_filing_meta", None) or {}
                filing = _store_upload(
                    profile,
                    result,
                    upload.name,
                    name=meta.get("name", ""),
                    period_end_date=parse_date(meta.get("period_end_date", "") or "")
                    if meta.get("period_end_date")
                    else None,
                )
                _validate_and_send_upload(profile, filing)
                return redirect(f"/portal/filings/{filing.pk}/validation/")
    return render(
        request,
        "portal/filing_upload.html",
        {
            "errors": errors,
            "error_rows": _upload_error_rows(errors),
            "nav": "filings",
            **_deadline_context(),
        },
    )


def _reject_already_filed(result) -> None:
    """Fail a parse result whose DocRefIds are already on file.

    A DocRefId must be unique in space and time, so a document whose record
    identifiers are already on file is a resubmission rather than new data.
    Checked before anything is written so a rejected upload leaves no
    partial filing behind.
    """
    if not result.ok:
        return
    incoming = [record.doc_ref_id for record in result.records if record.doc_ref_id]
    already_filed = set(
        AccountReport.objects.filter(doc_ref_id__in=incoming).values_list("doc_ref_id", flat=True)
    )
    if already_filed:
        result.ok = False
        result.errors = [
            f"DocRefId '{doc_ref}' has already been filed. A corrected record must carry a "
            "new DocRefId with CorrDocRefId pointing at the record it replaces."
            for doc_ref in sorted(already_filed)[:10]
        ]


def _store_upload(
    profile: PortalUser,
    result,
    filename: str,
    *,
    name: str = "",
    period_end_date=None,
    reporting_year: int | None = None,
    corrects: Filing | None = None,
) -> Filing:
    """Create the filing and its records from a parsed CRS document."""
    filing = Filing.objects.create(
        reference=next_filing_reference(),
        name=name,
        rfi=profile.rfi,
        reporting_year=reporting_year or config.CURRENT_REPORTING_YEAR,
        period_end_date=period_end_date,
        kind=Filing.Kind.XML_UPLOAD,
        created_by=profile,
        uploaded_filename=filename,
        corrects=corrects,
    )
    # A CRS_OECD document carries its message header; keep it on
    # the filing so the preparer need not re-enter it.
    header_fields = []
    if result.message_type_indic in Filing.MessageType.values:
        filing.message_type = result.message_type_indic
        header_fields.append("message_type")
    if result.receiving_country:
        filing.receiving_country = result.receiving_country
        header_fields.append("receiving_country")
    if result.message_ref_id:
        filing.message_reference = result.message_ref_id
        header_fields.append("message_reference")
    if header_fields:
        filing.save(update_fields=header_fields)
    for parsed in result.records:
        record = AccountReport.objects.create(
            filing=filing,
                        # Keep the FI's own DocRefId so the correction chain it
                        # started stays resolvable; the simplified CRSFiling
                        # form carries none, so one is minted for it.
                        doc_ref_id=parsed.doc_ref_id or next_doc_ref_id(profile.rfi, filing.reporting_year),
                        doc_type_indic=parsed.doc_type_indic,
                        corr_doc_ref_id=parsed.corr_doc_ref_id,
                        source_line=parsed.source_line,
                        holder_name=parsed.holder_name,
                        holder_first_name=parsed.holder_first_name,
                        holder_middle_name=parsed.holder_middle_name,
                        holder_last_name=parsed.holder_last_name,
                        holder_type=parsed.holder_type,
                        acct_holder_type=parsed.acct_holder_type,
                        residence_country=parsed.residence_country,
                        foreign_tin=parsed.foreign_tin,
                        holder_address=parsed.holder_address,
                        address_country=parsed.address_country,
                        holder_street=parsed.holder_street,
                        holder_building_identifier=parsed.holder_building_identifier,
                        holder_suite_identifier=parsed.holder_suite_identifier,
                        holder_floor_identifier=parsed.holder_floor_identifier,
                        holder_district_name=parsed.holder_district_name,
                        holder_pob=parsed.holder_pob,
                        holder_post_code=parsed.holder_post_code,
                        holder_city=parsed.holder_city,
                        holder_country_subentity=parsed.holder_country_subentity,
                        birth_date=parsed.birth_date,
                        birth_city=parsed.birth_city,
                        birth_city_subentity=parsed.birth_city_subentity,
                        birth_country_code=parsed.birth_country_code,
                        account_number=parsed.account_number,
                        acct_number_type=parsed.acct_number_type,
                        closed_account=parsed.closed_account,
                        dormant_account=parsed.dormant_account,
                        currency=parsed.currency or "NGN",
                        balance=parsed.balance,
                        dividends=parsed.dividends,
                        interest=parsed.interest,
                        gross_proceeds=parsed.gross_proceeds,
                        other_income=parsed.other_income,
        )
        for cp in parsed.controlling_persons:
            ControllingPerson.objects.create(
                account_report=record,
                name=cp.name,
                first_name=cp.first_name,
                middle_name=cp.middle_name,
                last_name=cp.last_name,
                residence_country=cp.residence_country,
                tin=cp.tin,
                address=cp.address,
                city=cp.city,
                birth_date=cp.birth_date,
                ctrlg_person_type=cp.ctrlg_person_type,
            )
    detail = f"CRS XML file {filename} accepted with {len(result.records)} records."
    if corrects is not None:
        detail = f"CRS702 correction of {corrects.reference}: {detail}"
    if result.contains_test_data:
        detail += " Document carried OECD10-OECD13 test-data indicators."
    if result.warnings:
        detail += f" {len(result.warnings)} schema/consistency warning(s)."
    _audit(profile, "FILING_UPLOADED", filing, detail, after=filing.status)
    return filing


def _validate_and_send_upload(profile: PortalUser, filing: Filing) -> bool:
    """Validate an uploaded filing and send it on if it is clean.

    An uploaded document is a complete return — it carries its own CRS message
    header — so there is no separate submit step. Validation runs immediately
    and decides:

      errors present  the filing stays a Draft carrying its findings, for the
                      institution to correct and resubmit;
      no errors       it goes straight to the Supervision Centre.

    Only ERROR findings hold a filing back. Warnings travel with it: several
    (self-certification above all) describe due-diligence facts the CRS schema
    has no element for, so an institution filing valid XML could never clear
    them. They are for the Supervision Centre to weigh and override on the
    record, which is what its override machinery exists for.

    Returns True when the filing was sent.
    """
    # Imported here: backoffice.validation pulls in exchange models, which
    # must not load at portal app import time.
    from backoffice.validation import run_validation

    run_validation(filing)
    has_errors = filing.findings.filter(severity=ValidationFinding.Severity.ERROR).exists()
    if has_errors:
        _audit(
            profile,
            "FILING_VALIDATION_FAILED",
            filing,
            f"Validation found errors; filing held as a draft for correction "
            f"({filing.findings.filter(severity=ValidationFinding.Severity.ERROR).count()} errors).",
            after=filing.status,
        )
        return False

    before = filing.status
    filing.status = Filing.Status.SUBMITTED
    filing.submitted_at = timezone.now()
    filing.checker = profile
    filing.save(update_fields=["status", "submitted_at", "checker"])
    _audit(
        profile,
        "FILING_SUBMITTED",
        filing,
        "Validation passed on upload; filing sent to the Supervision Centre.",
        before=before,
        after=filing.status,
    )
    auto_validate_submission(filing)
    return True


@portal_required
def filing_validation_report(request, filing_id: int):
    """The validation outcome for an uploaded filing.

    Shown straight after upload, and reachable afterwards from the filing, so
    an institution can revisit exactly why a return was held back.
    """
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    findings = filing.findings.select_related("account_report")
    errors = [f for f in findings if f.severity == ValidationFinding.Severity.ERROR]
    warnings = [f for f in findings if f.severity == ValidationFinding.Severity.WARNING]
    return render(
        request,
        "portal/filing_validation.html",
        {
            "filing": filing,
            "errors": errors,
            "warnings": warnings,
            "file_errors": [f for f in errors if f.account_report_id is None],
            "record_errors": [f for f in errors if f.account_report_id is not None],
            "was_sent": filing.status != Filing.Status.DRAFT,
            "filed_by": filing.checker or filing.created_by or profile,
            # XML line references only exist for records ingested from an
            # uploaded document.
            "show_lines": filing.kind == Filing.Kind.XML_UPLOAD,
            "return_label": {
                Filing.Kind.XML_UPLOAD: "CRS XML return",
                Filing.Kind.EXCEL_UPLOAD: "CRS Excel return",
                Filing.Kind.MANUAL: "CRS return",
                Filing.Kind.NIL: "CRS nil return",
            }.get(filing.kind, "return"),
            "record_count": filing.account_reports.filter(superseded=False).count(),
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_upload_excel(request):
    """CRS filing prepared in the Excel or CSV template (any portal user).

    One account per row; optional controlling person columns are honoured for
    Passive NFE (CRS101) rows. The parsed rows become account reports on a new
    draft filing, exactly as with the XML upload path."""
    from portal.excel_ingest import ALL_COLUMNS, REQUIRED_COLUMNS, parse_excel_upload

    profile = request.portal_profile
    errors: list[str] = []
    if request.method == "POST":
        upload = request.FILES.get("crs_file")
        if upload is None:
            errors = ["Choose a .xlsx or .csv file prepared with the CRS template."]
        else:
            result = parse_excel_upload(upload.read(), upload.name)
            if not result.ok:
                errors = result.errors
            else:
                meta = request.session.pop("pending_filing_meta", None) or {}
                filing = Filing.objects.create(
                    reference=next_filing_reference(),
                    name=meta.get("name", ""),
                    rfi=profile.rfi,
                    reporting_year=config.CURRENT_REPORTING_YEAR,
                    period_end_date=parse_date(meta.get("period_end_date", "") or "") if meta.get("period_end_date") else None,
                    kind=Filing.Kind.EXCEL_UPLOAD,
                    created_by=profile,
                    uploaded_filename=upload.name,
                )
                for parsed in result.records:
                    record = AccountReport.objects.create(
                        filing=filing,
                        doc_ref_id=next_doc_ref_id(profile.rfi, filing.reporting_year),
                        holder_name=parsed.holder_name,
                        holder_first_name=parsed.holder_first_name,
                        holder_last_name=parsed.holder_last_name,
                        holder_type=parsed.holder_type,
                        acct_holder_type=parsed.acct_holder_type,
                        residence_country=parsed.residence_country,
                        foreign_tin=parsed.foreign_tin,
                        tin_unavailable_reason=parsed.tin_unavailable_reason,
                        holder_address=parsed.holder_address,
                        address_country=parsed.address_country,
                        holder_street=parsed.holder_street,
                        holder_building_identifier=parsed.holder_building_identifier,
                        holder_post_code=parsed.holder_post_code,
                        holder_city=parsed.holder_city,
                        holder_country_subentity=parsed.holder_country_subentity,
                        birth_date=parsed.birth_date,
                        birth_city=parsed.birth_city,
                        birth_city_subentity=parsed.birth_city_subentity,
                        birth_country_code=parsed.birth_country_code,
                        birth_former_country_name=parsed.birth_former_country_name,
                        self_certification=parsed.self_certification,
                        account_number=parsed.account_number,
                        acct_number_type=parsed.acct_number_type,
                        closed_account=parsed.closed_account,
                        dormant_account=parsed.dormant_account,
                        currency=parsed.currency,
                        balance=parsed.balance,
                        dividends=parsed.dividends,
                        interest=parsed.interest,
                        gross_proceeds=parsed.gross_proceeds,
                        other_income=parsed.other_income,
                    )
                    if parsed.cp_name:
                        ControllingPerson.objects.create(
                            account_report=record,
                            name=parsed.cp_name,
                            first_name=parsed.cp_first_name,
                            last_name=parsed.cp_last_name,
                            residence_country=parsed.cp_residence,
                            tin=parsed.cp_tin,
                            city=parsed.cp_city,
                            ctrlg_person_type=parsed.cp_type or ControllingPerson.CtrlgPersonType.CRS801,
                        )
                _audit(
                    profile,
                    "FILING_UPLOADED",
                    filing,
                    f"CRS Excel file {upload.name} accepted with {len(result.records)} records.",
                    after=filing.status,
                )
                return redirect(f"/portal/filings/{filing.pk}/created/")
    return render(
        request,
        "portal/filing_upload_excel.html",
        {
            "errors": errors,
            "required_columns": REQUIRED_COLUMNS,
            "all_columns": ALL_COLUMNS,
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_nil(request):
    """One-click nil return for the reporting year, message type CRS703."""
    profile = request.portal_profile
    if request.method != "POST":
        return redirect("/portal/filings/")
    year = config.CURRENT_REPORTING_YEAR
    existing = profile.rfi.filings.filter(
        reporting_year=year,
        status__in=[
            Filing.Status.SUBMITTED,
            Filing.Status.UNDER_VALIDATION,
            Filing.Status.ACCEPTED,
            Filing.Status.IN_EXCHANGE,
        ],
    )
    if existing.exists():
        messages.error(request, f"A live filing already exists for {year}. A nil return is not available.")
        return redirect("/portal/filings/")
    # Any portal user submits the nil return to the NRS directly.
    filing = Filing.objects.create(
        reference=next_filing_reference(),
        rfi=profile.rfi,
        reporting_year=year,
        kind=Filing.Kind.NIL,
        message_type=Filing.MessageType.CRS703,
        created_by=profile,
        status=Filing.Status.SUBMITTED,
        submitted_at=timezone.now(),
        checker=profile,
    )
    _audit(
        profile,
        "NIL_RETURN_FILED",
        filing,
        f"Nil return (CRS703) for {year} submitted to NRS.",
        after=filing.status,
    )
    auto_validate_submission(filing)
    messages.success(
        request,
        f"Nil return for {year} submitted to the NRS and passed automatic schema validation.",
    )
    return redirect("/portal/filings/")


def _jurisdiction_choices() -> list[dict]:
    """Activated partner jurisdictions offered for a record's residence.

    Sourced from the partner table so the options are exactly the set the
    validator accepts (see backoffice R-104), leaving no room to type a code
    that would later be rejected.
    """
    from exchange.models import PartnerJurisdiction

    return list(PartnerJurisdiction.objects.order_by("name").values("code", "name"))


def _record_form_context() -> dict:
    """Choice lists shared by the record add and edit forms."""
    return {
        "currencies": config.CURRENCIES,
        "acct_holder_types": AccountReport.AcctHolderType.choices,
        "acct_number_types": AccountReport.AcctNumberType.choices,
        "self_cert_choices": AccountReport.SelfCertification.choices,
        "ctrlg_person_types": ControllingPerson.CtrlgPersonType.choices,
    }


@portal_required
def filing_detail(request, filing_id: int):
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    # An administrative notice has no account records; its home is the Form
    # View, where the notice form is edited and submitted.
    if filing.is_notice:
        return redirect(f"/portal/filings/{filing.pk}/view/")
    records = filing.account_reports.all()
    flagged_ids = set(
        filing.findings.filter(account_report__isnull=False).values_list("account_report_id", flat=True)
    )
    # Any portal user (Primary or Secondary) prepares and submits filings.
    can_edit = filing.status in _OPEN_STATUSES and filing.kind in (
        Filing.Kind.MANUAL,
        Filing.Kind.XML_UPLOAD,
        Filing.Kind.EXCEL_UPLOAD,
    )
    can_correct = filing.status == Filing.Status.RETURNED
    # An accepted CRS data filing may be amended through a CRS702 correction
    # filing; a correction draft offers the original's records to pull in.
    can_open_correction = (
        filing.status in (Filing.Status.ACCEPTED, Filing.Status.IN_EXCHANGE)
        and filing.is_crs_data
        and filing.kind != Filing.Kind.NIL
    )
    correction_source = []
    if filing.corrects_id and filing.status in _OPEN_STATUSES:
        picked = set(
            filing.account_reports.exclude(corr_doc_ref_id="").values_list("corr_doc_ref_id", flat=True)
        )
        correction_source = [
            {"record": record, "picked": record.doc_ref_id in picked}
            for record in filing.corrects.account_reports.filter(superseded=False)
        ]
    return render(
        request,
        "portal/filing_detail.html",
        {
            "filing": filing,
            "records": records,
            "can_open_correction": can_open_correction,
            "correction_source": correction_source,
            "jurisdictions": _jurisdiction_choices(),
            **_record_form_context(),
            "flagged_ids": flagged_ids,
            "findings": filing.findings.select_related("account_report"),
            "file_findings": filing.findings.filter(account_report__isnull=True),
            "can_edit": can_edit,
            "can_correct": can_correct,
            "can_submit": filing.status in _OPEN_STATUSES
            and (filing.kind == Filing.Kind.NIL or records.exists()),
            "can_resubmit": can_correct and not filing.findings.filter(
                account_report__isnull=False, account_report__superseded=False
            ).exclude(account_report__doc_type_indic=AccountReport.DocTypeIndic.OECD2).exists(),
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def record_add(request, filing_id: int):
    """Add an account report block to an open manual filing."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if filing.message_type == Filing.MessageType.CRS702:
        messages.error(
            request,
            "A corrections filing cannot carry new data. Pull records in from the "
            "original filing; report new accounts in a separate CRS701 filing.",
        )
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.status not in _OPEN_STATUSES:
        messages.error(request, "Records can only be added while the filing is open.")
        return redirect(f"/portal/filings/{filing.pk}/")
    if request.method == "POST":
        error = _apply_record_form(request, filing, None, profile)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, "Account report added.")
    return redirect(f"/portal/filings/{filing.pk}/")


def _apply_record_form(request, filing: Filing, record: AccountReport | None, profile: PortalUser) -> str:
    """Create or amend a record from the posted form. Returns an error or empty."""
    holder_name = request.POST.get("holder_name", "").strip()
    residence_country = request.POST.get("residence_country", "").strip().upper()
    account_number = request.POST.get("account_number", "").strip()
    if not holder_name or not residence_country or not account_number:
        return "Account holder, residence jurisdiction, and account number are required."
    currency = request.POST.get("currency", "").strip().upper()
    if currency not in {code for code, _ in config.CURRENCIES}:
        return "Select the account currency."
    holder_address = request.POST.get("holder_address", "").strip()
    if not holder_address:
        return "Account holder address is required (mandatory in CRS)."
    holder_type = request.POST.get("holder_type", "INDIVIDUAL")
    acct_holder_type = request.POST.get("acct_holder_type", "").strip()
    if acct_holder_type not in dict(AccountReport.AcctHolderType.choices):
        acct_holder_type = ""
    # Entity holders always carry a classification; default to the first
    # option (CRS101) when none was selected.
    if holder_type == "ORGANISATION" and not acct_holder_type:
        acct_holder_type = AccountReport.AcctHolderType.CRS101
    self_certification = request.POST.get("self_certification", "").strip()
    if self_certification not in dict(AccountReport.SelfCertification.choices):
        self_certification = ""
    acct_number_type = request.POST.get("acct_number_type", "").strip().upper()
    if acct_number_type not in dict(AccountReport.AcctNumberType.choices):
        acct_number_type = ""
    amounts: dict[str, Decimal] = {}
    for field_name in ("balance", "dividends", "interest", "gross_proceeds", "other_income"):
        raw = request.POST.get(field_name, "0").strip() or "0"
        try:
            amounts[field_name] = Decimal(raw)
        except InvalidOperation:
            return f"The {field_name.replace('_', ' ')} amount is not a valid number."
    values = {
        "holder_name": holder_name,
        "holder_first_name": request.POST.get("holder_first_name", "").strip(),
        "holder_last_name": request.POST.get("holder_last_name", "").strip(),
        "holder_type": holder_type,
        "acct_holder_type": acct_holder_type if holder_type == "ORGANISATION" else "",
        "residence_country": residence_country,
        "foreign_tin": request.POST.get("foreign_tin", "").strip(),
        "tin_unavailable_reason": request.POST.get("tin_unavailable_reason", "").strip(),
        "holder_address": holder_address,
        "address_country": (request.POST.get("address_country", "").strip().upper() or residence_country),
        "holder_street": request.POST.get("holder_street", "").strip(),
        "holder_building_identifier": request.POST.get("holder_building_identifier", "").strip(),
        "holder_post_code": request.POST.get("holder_post_code", "").strip(),
        "holder_city": request.POST.get("holder_city", "").strip(),
        "holder_country_subentity": request.POST.get("holder_country_subentity", "").strip(),
        "birth_date": parse_date(request.POST.get("birth_date", "").strip() or "") or None,
        "birth_city": request.POST.get("birth_city", "").strip(),
        "birth_country_code": request.POST.get("birth_country_code", "").strip().upper(),
        "self_certification": self_certification,
        "account_number": account_number,
        "acct_number_type": acct_number_type,
        "closed_account": bool(request.POST.get("closed_account")),
        "dormant_account": bool(request.POST.get("dormant_account")),
        "currency": currency,
        **amounts,
    }
    if record is None:
        AccountReport.objects.create(
            filing=filing,
            doc_ref_id=next_doc_ref_id(filing.rfi, filing.reporting_year),
            **values,
        )
        return ""
    # Correction: the amended data becomes a new record carrying OECD2 and a
    # CorrDocRefID pointing at the original DocRefID. The original stays on
    # the filing, superseded, so the lineage is visible. The replacement
    # starts from the original's full payload so fields the form does not
    # expose (Excel-only columns such as birth city subentity) carry over
    # rather than resetting to defaults, and controlling persons are copied
    # so the corrected record reports the same persons as the one it replaces.
    if filing.status == Filing.Status.RETURNED:
        carried = {
            f.name: getattr(record, f.name)
            for f in AccountReport._meta.fields
            if f.name not in ("id", "filing", "doc_ref_id", "corr_doc_ref_id", "doc_type_indic", "superseded")
        }
        carried.update(values)
        replacement = AccountReport.objects.create(
            filing=filing,
            doc_ref_id=next_doc_ref_id(filing.rfi, filing.reporting_year),
            corr_doc_ref_id=record.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
            **carried,
        )
        for cp in record.controlling_persons.all():
            cp.pk = None
            cp.account_report = replacement
            cp.save()
        record.superseded = True
        record.save(update_fields=["superseded"])
        _audit(
            profile,
            "RECORD_CORRECTED",
            filing,
            f"Record {record.doc_ref_id} corrected by {replacement.doc_ref_id} (OECD2).",
        )
        return ""
    for key, value in values.items():
        setattr(record, key, value)
    record.save()
    return ""


@portal_required
def record_edit(request, filing_id: int, record_id: int):
    """Amend a record: freely while open, as an OECD2 correction when returned.

    On a returned filing every non-superseded record may be amended, not only
    the flagged ones — the NRS may return a filing with file-level findings
    or a narrative reason that touches records it did not individually flag.
    Each amendment supersedes its original and carries a CorrDocRefID.
    """
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing, superseded=False)
    if filing.status not in (*_OPEN_STATUSES, Filing.Status.RETURNED):
        messages.error(request, "This filing is not open for amendment.")
        return redirect(f"/portal/filings/{filing.pk}/")
    if request.method == "POST":
        error = _apply_record_form(request, filing, record, profile)
        if error:
            messages.error(request, error)
            return render(
                request,
                "portal/record_edit.html",
                {
                    "filing": filing,
                    "record": record,
                    "jurisdictions": _jurisdiction_choices(),
                    "controlling_persons": record.controlling_persons.all(),
                    **_record_form_context(),
                    "nav": "filings",
                    **_deadline_context(),
                },
            )
        messages.success(request, "Record amended.")
        return redirect(f"/portal/filings/{filing.pk}/")
    return render(
        request,
        "portal/record_edit.html",
        {
            "filing": filing,
            "record": record,
            "record_findings": filing.findings.filter(account_report=record),
            "jurisdictions": _jurisdiction_choices(),
            "controlling_persons": record.controlling_persons.all(),
            **_record_form_context(),
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def controlling_person_add(request, filing_id: int, record_id: int):
    """Add a controlling person to a Passive NFE account record."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing, superseded=False)
    target = f"/portal/filings/{filing.pk}/records/{record.pk}/"
    if request.method != "POST":
        return redirect(target)
    if filing.status not in (*_OPEN_STATUSES, Filing.Status.RETURNED):
        messages.error(request, "Controlling persons can only be edited while the filing is open.")
        return redirect(target)
    if not record.requires_controlling_persons:
        messages.error(
            request,
            "Controlling persons are only reported for a Passive NFE with controlling persons "
            "(entity account holder type CRS101).",
        )
        return redirect(target)
    surname = request.POST.get("cp_surname", "").strip()
    middle_name = request.POST.get("cp_middle_name", "").strip()
    other_names = request.POST.get("cp_other_names", "").strip()
    # Legacy single-field alias, and composition in natural order.
    name = request.POST.get("cp_name", "").strip() or " ".join(
        part for part in [other_names, middle_name, surname] if part
    )
    residence = request.POST.get("cp_residence", "").strip().upper()
    if not name or not residence:
        messages.error(
            request,
            "Controlling person surname, other names, and residence jurisdiction are required.",
        )
        return redirect(target)
    cp_type = request.POST.get("cp_type", "").strip()
    if cp_type not in dict(ControllingPerson.CtrlgPersonType.choices):
        cp_type = ControllingPerson.CtrlgPersonType.CRS801
    # The structured name parts feed the CRS FirstName/LastName pair directly,
    # so a form-entered controlling person is never reported under the NFN
    # fallback when a surname was captured.
    ControllingPerson.objects.create(
        account_report=record,
        name=name,
        first_name=" ".join(part for part in [other_names, middle_name] if part),
        last_name=surname,
        residence_country=residence,
        tin=request.POST.get("cp_tin", "").strip(),
        address=request.POST.get("cp_address", "").strip(),
        city=request.POST.get("cp_city", "").strip(),
        birth_date=parse_date(request.POST.get("cp_dob", "").strip() or "") or None,
        birth_city=request.POST.get("cp_birth_city", "").strip(),
        birth_country_code=request.POST.get("cp_birth_country", "").strip().upper(),
        ctrlg_person_type=cp_type,
    )
    _audit(profile, "CONTROLLING_PERSON_ADDED", filing, f"Controlling person added to {record.doc_ref_id}.")
    messages.success(request, "Controlling person added.")
    return redirect(target)


@portal_required
def controlling_person_delete(request, filing_id: int, record_id: int, cp_id: int):
    """Remove a controlling person from an account record."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing)
    cp = get_object_or_404(ControllingPerson, pk=cp_id, account_report=record)
    target = f"/portal/filings/{filing.pk}/records/{record.pk}/"
    if request.method == "POST" and filing.status in (*_OPEN_STATUSES, Filing.Status.RETURNED):
        cp.delete()
        _audit(profile, "CONTROLLING_PERSON_REMOVED", filing, f"Controlling person removed from {record.doc_ref_id}.")
        messages.success(request, "Controlling person removed.")
    return redirect(target)


@portal_required
def filing_stage(request, filing_id: int):
    """Submit a filing to the NRS directly.

    Any portal user (Primary or Secondary) submits; the maker-checker review
    step is retired. The endpoint keeps its /stage/ path for continuity.
    """
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if request.method != "POST":
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.status not in (*_OPEN_STATUSES, Filing.Status.RETURNED):
        messages.error(request, "This filing cannot be submitted from its current status.")
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.is_notice:
        if not filing.notice_validated:
            messages.error(request, "Complete and validate the notice form before submitting.")
            return redirect(f"/portal/filings/{filing.pk}/view/")
    elif filing.kind != Filing.Kind.NIL and not filing.account_reports.filter(superseded=False).exists():
        messages.error(request, "Add at least one account report before submitting.")
        return redirect(f"/portal/filings/{filing.pk}/")
    before = filing.status
    filing.status = Filing.Status.SUBMITTED
    filing.checker = profile
    filing.submitted_at = timezone.now()
    filing.save(update_fields=["status", "checker", "submitted_at"])
    _audit(
        profile,
        "FILING_SUBMITTED",
        filing,
        f"Filing submitted to the NRS by {profile.get_role_display()}.",
        before=before,
        after=filing.status,
    )
    auto_validate_submission(filing)
    if filing.is_notice:
        messages.success(request, f"{filing.reference} submitted to the NRS.")
        return redirect(f"/portal/filings/{filing.pk}/view/")
    # Errors hold the filing back as a draft carrying its findings — the same
    # gate the upload flow applies — so nothing with known errors reaches the
    # Supervision Centre.
    if filing.findings.filter(severity=ValidationFinding.Severity.ERROR).exists():
        filing.status = Filing.Status.DRAFT
        filing.submitted_at = None
        filing.save(update_fields=["status", "submitted_at"])
        _audit(
            profile,
            "FILING_VALIDATION_FAILED",
            filing,
            "Validation found errors at submission; filing held as a draft for correction.",
            before=Filing.Status.SUBMITTED,
            after=filing.status,
        )
    # The validation report shows either the filed confirmation or the
    # held-back report with each error's cause and resolution.
    return redirect(f"/portal/filings/{filing.pk}/validation/")


def _copy_record_fields(record: AccountReport) -> dict:
    """Every payload field of a record, excluding identity and lineage."""
    return {
        f.name: getattr(record, f.name)
        for f in AccountReport._meta.fields
        if f.name
        not in ("id", "filing", "doc_ref_id", "corr_doc_ref_id", "doc_type_indic", "superseded", "source_line")
    }


@portal_required
def filing_correct(request, filing_id: int):
    """Open a CRS702 correction filing for a return already with the NRS.

    Mirrors the Vizor workflow: an accepted filing is never edited in place —
    the institution creates a corrections filing that references the filed
    records by DocRefId. The draft starts empty; records are pulled in from
    the original as amendments (OECD2) or deletions (OECD3).
    """
    profile = request.portal_profile
    original = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if request.method != "POST":
        return redirect(f"/portal/filings/{original.pk}/")
    if original.status not in (Filing.Status.ACCEPTED, Filing.Status.IN_EXCHANGE):
        messages.error(request, "Only a filing accepted by the NRS can be corrected.")
        return redirect(f"/portal/filings/{original.pk}/")
    if not original.is_crs_data or original.kind == Filing.Kind.NIL:
        messages.error(request, "This filing carries no account records to correct.")
        return redirect(f"/portal/filings/{original.pk}/")
    existing = Filing.objects.filter(corrects=original, status__in=_OPEN_STATUSES).first()
    if existing:
        messages.info(request, f"Correction {existing.reference} is already open for this filing.")
        return redirect(f"/portal/filings/{existing.pk}/")

    reference = next_filing_reference()
    filing = Filing.objects.create(
        reference=reference,
        name=f"Correction of {original.reference}",
        rfi=profile.rfi,
        reporting_year=original.reporting_year,
        period_end_date=original.period_end_date,
        kind=Filing.Kind.MANUAL,
        message_type=Filing.MessageType.CRS702,
        status=Filing.Status.DRAFT,
        receiving_country=original.receiving_country,
        sending_company_in=original.sending_company_in or profile.rfi.tin,
        # A corrections message needs its own MessageRefId; minted from the
        # new filing reference, which is unique by construction.
        message_reference=f"NG{original.reporting_year}{original.receiving_country or 'NG'}-{reference}",
        created_by=profile,
        corrects=original,
    )
    _audit(
        profile,
        "CORRECTION_OPENED",
        filing,
        f"CRS702 correction opened against {original.reference}.",
        after=filing.status,
    )
    messages.success(
        request,
        f"Correction filing {filing.reference} opened. Pull in the records to amend or delete, "
        "then submit.",
    )
    return redirect(f"/portal/filings/{filing.pk}/")


@portal_required
def filing_correct_xml(request, filing_id: int):
    """Amend an XML-filed return in its native format.

    An uploaded document may carry many account reports, so the amendment is
    edited as XML rather than record by record: the editor opens pre-filled
    with a CRS702 document holding every filed record as an OECD2 template
    (new DocRefId, CorrDocRefId pointing at the filed version). The preparer
    amends values, removes the AccountReport blocks that need no change, or
    flips OECD2 to OECD3 to delete a record, then submits through the same
    validation pipeline as an upload. Manual filings keep the form-based
    correction flow.
    """
    from exchange.services import correction_xml_draft

    profile = request.portal_profile
    original = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if original.kind != Filing.Kind.XML_UPLOAD:
        return redirect(f"/portal/filings/{original.pk}/")
    if original.status not in (Filing.Status.ACCEPTED, Filing.Status.IN_EXCHANGE):
        messages.error(request, "Only a filing accepted by the NRS can be corrected.")
        return redirect(f"/portal/filings/{original.pk}/")

    errors: list[str] = []
    xml_text = ""
    if request.method == "POST":
        xml_text = request.POST.get("xml", "")
        result = parse_crs_upload(xml_text.encode("utf-8"), original.reporting_year)
        if result.ok and result.message_type_indic != Filing.MessageType.CRS702:
            result.ok = False
            result.errors = [
                "MessageTypeIndic must remain CRS702: an amendment carries corrections "
                "for previously sent information."
            ]
        _reject_already_filed(result)
        if not result.ok:
            errors = result.errors
        else:
            filing = _store_upload(
                profile,
                result,
                f"correction-{original.reference}.xml",
                name=f"Correction of {original.reference}",
                reporting_year=original.reporting_year,
                corrects=original,
            )
            _validate_and_send_upload(profile, filing)
            return redirect(f"/portal/filings/{filing.pk}/validation/")
    if not xml_text:
        xml_text = correction_xml_draft(original)
    return render(
        request,
        "portal/filing_correct_xml.html",
        {
            "original": original,
            "xml_text": xml_text,
            "errors": errors,
            "error_rows": _upload_error_rows(errors),
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def correction_pick_record(request, filing_id: int, record_id: int):
    """Pull one record of the original filing into the correction draft.

    An amendment copies the record as OECD2 with CorrDocRefId pointing at the
    filed DocRefId, then opens it for editing; a deletion copies it as OECD3,
    which instructs partner jurisdictions to remove the record.
    """
    profile = request.portal_profile
    filing = get_object_or_404(
        Filing, pk=filing_id, rfi=profile.rfi, corrects__isnull=False, status__in=_OPEN_STATUSES
    )
    original = get_object_or_404(
        AccountReport, pk=record_id, filing=filing.corrects, superseded=False
    )
    if request.method != "POST":
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.account_reports.filter(corr_doc_ref_id=original.doc_ref_id).exists():
        messages.error(request, "That record is already part of this correction.")
        return redirect(f"/portal/filings/{filing.pk}/")
    action = request.POST.get("action", "amend")
    replacement = AccountReport.objects.create(
        filing=filing,
        doc_ref_id=next_doc_ref_id(profile.rfi, filing.reporting_year),
        corr_doc_ref_id=original.doc_ref_id,
        doc_type_indic=(
            AccountReport.DocTypeIndic.OECD3 if action == "delete" else AccountReport.DocTypeIndic.OECD2
        ),
        **_copy_record_fields(original),
    )
    for cp in original.controlling_persons.all():
        cp.pk = None
        cp.account_report = replacement
        cp.save()
    if action == "delete":
        _audit(
            profile, "CORRECTION_DELETION", filing,
            f"Deletion of {original.doc_ref_id} staged as {replacement.doc_ref_id} (OECD3).",
        )
        messages.success(request, f"Deletion of record {original.doc_ref_id} added to the correction.")
        return redirect(f"/portal/filings/{filing.pk}/")
    _audit(
        profile, "CORRECTION_AMENDMENT", filing,
        f"Amendment of {original.doc_ref_id} staged as {replacement.doc_ref_id} (OECD2).",
    )
    messages.success(request, "Record pulled into the correction. Amend the fields that change, then save.")
    return redirect(f"/portal/filings/{filing.pk}/records/{replacement.pk}/")


# Retired: the maker-checker review step no longer exists. Both the Primary
# User and Secondary Users submit filings to the NRS directly (see
# filing_stage above). Kept commented for reference.
#
# @portal_required
# def filing_check(request, filing_id: int):
#     """Checker submits to NRS, or rejects back to the Maker with comments."""
#     profile = request.portal_profile
#     filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
#     if request.method != "POST" or profile.role != PortalUser.Role.CHECKER:
#         return redirect(f"/portal/filings/{filing.pk}/")
#     if filing.status != Filing.Status.PENDING_CHECKER:
#         messages.error(request, "This filing is not awaiting Checker action.")
#         return redirect(f"/portal/filings/{filing.pk}/")
#     action = request.POST.get("action", "")
#     if action == "submit":
#         ... submitted to the NRS ...
#     elif action == "reject":
#         ... returned to the Maker with comments ...


@portal_required
def filing_resubmit(request, filing_id: int):
    """Resubmit a corrected filing to the NRS; the lineage travels with it."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if request.method != "POST":
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.status != Filing.Status.RETURNED:
        messages.error(request, "Only a filing returned for correction can be resubmitted.")
        return redirect(f"/portal/filings/{filing.pk}/")
    # Every record the NRS flagged must carry a correction before the filing
    # goes back. A filing returned on file-level findings alone (no flagged
    # records) may be resubmitted once the preparer has made their fixes.
    outstanding = (
        filing.findings.filter(account_report__isnull=False, account_report__superseded=False)
        .exclude(account_report__doc_type_indic=AccountReport.DocTypeIndic.OECD2)
        .count()
    )
    if outstanding:
        messages.error(request, "Amend the flagged records before resubmitting.")
        return redirect(f"/portal/filings/{filing.pk}/")
    corrected = filing.account_reports.filter(
        doc_type_indic=AccountReport.DocTypeIndic.OECD2, superseded=False
    ).count()
    before = filing.status
    filing.status = Filing.Status.SUBMITTED
    filing.checker = profile
    filing.submitted_at = timezone.now()
    filing.save(update_fields=["status", "checker", "submitted_at"])
    _audit(
        profile,
        "FILING_CORRECTION_SUBMITTED",
        filing,
        f"Correction resubmitted to the NRS with {corrected} amended record{'s' if corrected != 1 else ''} "
        "carrying CorrDocRefID references.",
        before=before,
        after=filing.status,
    )
    file_count, record_count = auto_validate_submission(filing)
    findings = file_count + record_count
    if findings:
        messages.success(
            request,
            "Corrections resubmitted to the NRS. Automatic schema validation recorded "
            f"{findings} finding{'s' if findings != 1 else ''} for NRS review.",
        )
    else:
        messages.success(
            request,
            "Corrections resubmitted to the NRS and passed automatic schema validation.",
        )
    return redirect(f"/portal/filings/{filing.pk}/")
