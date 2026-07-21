"""Filing surface for RFIs: XML upload, manual entry, nil returns, corrections.

Maker and checker separation is enforced here: Makers prepare and amend,
Checkers review and submit. A Checker cannot edit a staged filing, only
approve, reject back with comments, or submit.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core import config
from core.decorators import portal_required
from core.models import AuditLog
from exchange.services import days_to_domestic_deadline, domestic_deadline, next_doc_ref_id
from portal.models import AccountReport, Filing, PortalUser
from portal.services import next_filing_reference
from portal.xml_ingest import parse_crs_upload


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
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_new_manual(request):
    """Maker path: open a manual-entry draft filing."""
    profile = request.portal_profile
    if profile.role != PortalUser.Role.MAKER:
        messages.error(request, "Only a Maker can prepare filings. Checkers review and submit.")
        return redirect("/portal/filings/")
    filing = Filing.objects.create(
        reference=next_filing_reference(),
        rfi=profile.rfi,
        reporting_year=config.CURRENT_REPORTING_YEAR,
        kind=Filing.Kind.MANUAL,
        created_by=profile,
    )
    _audit(profile, "FILING_CREATED", filing, "Manual entry filing opened as draft.", after=filing.status)
    return redirect(f"/portal/filings/{filing.pk}/")


@portal_required
def filing_upload(request):
    """Maker path: CRS XML upload validated against the simplified schema."""
    profile = request.portal_profile
    if profile.role != PortalUser.Role.MAKER:
        messages.error(request, "Only a Maker can prepare filings. Checkers review and submit.")
        return redirect("/portal/filings/")
    errors: list[str] = []
    if request.method == "POST":
        upload = request.FILES.get("crs_file")
        if upload is None:
            errors = ["Choose a CRS XML file."]
        else:
            result = parse_crs_upload(upload.read(), config.CURRENT_REPORTING_YEAR)
            if not result.ok:
                errors = result.errors
            else:
                filing = Filing.objects.create(
                    reference=next_filing_reference(),
                    rfi=profile.rfi,
                    reporting_year=config.CURRENT_REPORTING_YEAR,
                    kind=Filing.Kind.XML_UPLOAD,
                    created_by=profile,
                    uploaded_filename=upload.name,
                )
                for parsed in result.records:
                    AccountReport.objects.create(
                        filing=filing,
                        doc_ref_id=next_doc_ref_id(profile.rfi, filing.reporting_year),
                        holder_name=parsed.holder_name,
                        holder_type=parsed.holder_type,
                        residence_country=parsed.residence_country,
                        foreign_tin=parsed.foreign_tin,
                        account_number=parsed.account_number,
                        balance=parsed.balance,
                        dividends=parsed.dividends,
                        interest=parsed.interest,
                        gross_proceeds=parsed.gross_proceeds,
                        other_income=parsed.other_income,
                    )
                _audit(
                    profile,
                    "FILING_UPLOADED",
                    filing,
                    f"CRS XML file {upload.name} accepted with {len(result.records)} records.",
                    after=filing.status,
                )
                messages.success(request, f"File accepted. {len(result.records)} account reports staged as draft.")
                return redirect(f"/portal/filings/{filing.pk}/")
    return render(
        request,
        "portal/filing_upload.html",
        {"errors": errors, "nav": "filings", **_deadline_context()},
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
    is_checker = profile.role == PortalUser.Role.CHECKER
    filing = Filing.objects.create(
        reference=next_filing_reference(),
        rfi=profile.rfi,
        reporting_year=year,
        kind=Filing.Kind.NIL,
        message_type=Filing.MessageType.CRS703,
        created_by=profile,
        status=Filing.Status.SUBMITTED if is_checker else Filing.Status.PENDING_CHECKER,
        submitted_at=timezone.now() if is_checker else None,
        checker=profile if is_checker else None,
    )
    _audit(
        profile,
        "NIL_RETURN_FILED",
        filing,
        f"Nil return (CRS703) for {year} {'submitted to NRS' if is_checker else 'staged for Checker submission'}.",
        after=filing.status,
    )
    messages.success(
        request,
        f"Nil return for {year} {'submitted to the NRS' if is_checker else 'staged for Checker approval'}.",
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


@portal_required
def filing_detail(request, filing_id: int):
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    records = filing.account_reports.all()
    flagged_ids = set(
        filing.findings.filter(account_report__isnull=False).values_list("account_report_id", flat=True)
    )
    is_maker = profile.role == PortalUser.Role.MAKER
    is_checker = profile.role == PortalUser.Role.CHECKER
    can_edit = is_maker and filing.status == Filing.Status.DRAFT and filing.kind != Filing.Kind.NIL
    can_correct = is_maker and filing.status == Filing.Status.RETURNED
    return render(
        request,
        "portal/filing_detail.html",
        {
            "filing": filing,
            "records": records,
            "jurisdictions": _jurisdiction_choices(),
            "flagged_ids": flagged_ids,
            "findings": filing.findings.select_related("account_report"),
            "file_findings": filing.findings.filter(account_report__isnull=True),
            "can_edit": can_edit,
            "can_correct": can_correct,
            "can_stage": is_maker
            and filing.status == Filing.Status.DRAFT
            and (filing.kind == Filing.Kind.NIL or records.exists()),
            "can_check": is_checker and filing.status == Filing.Status.PENDING_CHECKER,
            "can_resubmit": can_correct and not filing.findings.filter(
                account_report__isnull=False, account_report__superseded=False
            ).exclude(account_report__doc_type_indic=AccountReport.DocTypeIndic.OECD2).exists(),
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def record_add(request, filing_id: int):
    """Maker adds an account report block to a draft manual filing."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if profile.role != PortalUser.Role.MAKER or filing.status != Filing.Status.DRAFT:
        messages.error(request, "Records can only be added by a Maker while the filing is in draft.")
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
    amounts: dict[str, Decimal] = {}
    for field_name in ("balance", "dividends", "interest", "gross_proceeds", "other_income"):
        raw = request.POST.get(field_name, "0").strip() or "0"
        try:
            amounts[field_name] = Decimal(raw)
        except InvalidOperation:
            return f"The {field_name.replace('_', ' ')} amount is not a valid number."
    values = {
        "holder_name": holder_name,
        "holder_type": request.POST.get("holder_type", "INDIVIDUAL"),
        "residence_country": residence_country,
        "foreign_tin": request.POST.get("foreign_tin", "").strip(),
        "account_number": account_number,
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
    # the filing, superseded, so the lineage is visible.
    if filing.status == Filing.Status.RETURNED:
        replacement = AccountReport.objects.create(
            filing=filing,
            doc_ref_id=next_doc_ref_id(filing.rfi, filing.reporting_year),
            corr_doc_ref_id=record.doc_ref_id,
            doc_type_indic=AccountReport.DocTypeIndic.OECD2,
            **values,
        )
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
    """Maker amends a record: freely in draft, flagged records only when returned."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing, superseded=False)
    if profile.role != PortalUser.Role.MAKER:
        messages.error(request, "Only a Maker can amend records.")
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.status == Filing.Status.RETURNED:
        if not filing.findings.filter(account_report=record).exists():
            messages.error(request, "Only records flagged by the NRS may be amended on a returned filing.")
            return redirect(f"/portal/filings/{filing.pk}/")
    elif filing.status != Filing.Status.DRAFT:
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
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def filing_stage(request, filing_id: int):
    """Maker sends a draft to the Checker."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if request.method != "POST" or profile.role != PortalUser.Role.MAKER:
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.status not in (Filing.Status.DRAFT, Filing.Status.RETURNED):
        messages.error(request, "This filing cannot be staged from its current status.")
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.kind != Filing.Kind.NIL and not filing.account_reports.filter(superseded=False).exists():
        messages.error(request, "Add at least one account report before staging.")
        return redirect(f"/portal/filings/{filing.pk}/")
    before = filing.status
    filing.status = Filing.Status.PENDING_CHECKER
    filing.save(update_fields=["status"])
    _audit(profile, "FILING_STAGED", filing, "Filing staged for Checker review.", before=before, after=filing.status)
    messages.success(request, "Filing sent to the Checker for review and submission.")
    return redirect(f"/portal/filings/{filing.pk}/")


@portal_required
def filing_check(request, filing_id: int):
    """Checker submits to NRS, or rejects back to the Maker with comments."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if request.method != "POST" or profile.role != PortalUser.Role.CHECKER:
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.status != Filing.Status.PENDING_CHECKER:
        messages.error(request, "This filing is not awaiting Checker action.")
        return redirect(f"/portal/filings/{filing.pk}/")
    action = request.POST.get("action", "")
    before = filing.status
    if action == "submit":
        filing.status = Filing.Status.SUBMITTED
        filing.checker = profile
        filing.submitted_at = timezone.now()
        filing.save()
        _audit(profile, "FILING_SUBMITTED", filing, "Checker submitted the filing to the NRS.", before=before, after=filing.status)
        messages.success(request, f"{filing.reference} submitted to the NRS.")
    elif action == "reject":
        comment = request.POST.get("comment", "").strip()
        if not comment:
            messages.error(request, "A rejection back to the Maker requires a comment.")
            return redirect(f"/portal/filings/{filing.pk}/")
        filing.status = Filing.Status.RETURNED if filing.returned_at else Filing.Status.DRAFT
        filing.checker_comment = comment
        filing.save()
        _audit(profile, "FILING_REJECTED_BY_CHECKER", filing, f"Checker returned the filing to the Maker: {comment}", before=before, after=filing.status)
        messages.success(request, "Filing returned to the Maker with comments.")
    return redirect(f"/portal/filings/{filing.pk}/")


@portal_required
def filing_resubmit(request, filing_id: int):
    """Maker resubmits a corrected filing; the lineage travels with it."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    if request.method != "POST" or profile.role != PortalUser.Role.MAKER:
        return redirect(f"/portal/filings/{filing.pk}/")
    if filing.status != Filing.Status.RETURNED:
        messages.error(request, "Only a filing returned for correction can be resubmitted.")
        return redirect(f"/portal/filings/{filing.pk}/")
    corrected = filing.account_reports.filter(
        doc_type_indic=AccountReport.DocTypeIndic.OECD2, superseded=False
    ).count()
    if corrected == 0:
        messages.error(request, "Amend the flagged records before resubmitting.")
        return redirect(f"/portal/filings/{filing.pk}/")
    before = filing.status
    filing.status = Filing.Status.PENDING_CHECKER
    filing.save(update_fields=["status"])
    _audit(
        profile,
        "FILING_CORRECTION_STAGED",
        filing,
        f"Correction staged for Checker with {corrected} amended records carrying CorrDocRefID references.",
        before=before,
        after=filing.status,
    )
    messages.success(request, "Corrections staged for Checker submission.")
    return redirect(f"/portal/filings/{filing.pk}/")
