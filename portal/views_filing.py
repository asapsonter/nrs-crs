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
from django.utils.dateparse import parse_date

from core import config
from core.decorators import portal_required
from core.models import AuditLog
from exchange.services import days_to_domestic_deadline, domestic_deadline, next_doc_ref_id
from portal.models import AccountReport, ControllingPerson, Filing, PortalUser
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
    if request.method != "POST":
        return redirect("/portal/filings/?mode=delete")
    if not filing.is_deletable:
        messages.error(request, "A filing submitted to the NRS cannot be deleted.")
        return redirect("/portal/filings/")
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
    return redirect("/portal/filings/?mode=delete")


# Filing types offered on the Create Filing entry page, in display order.
FILING_TYPE_OPTIONS = [
    (Filing.Kind.MANUAL, "CRS Manual Entry Filing"),
    (Filing.Kind.XML_UPLOAD, "CRS XML upload Filing"),
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

        is_crs = filing_type in (Filing.Kind.MANUAL, Filing.Kind.XML_UPLOAD)
        if not errors and is_crs and profile.role != PortalUser.Role.MAKER:
            errors.append("Only a Maker can prepare a CRS data filing. Checkers review and submit.")

        if not errors:
            if filing_type == Filing.Kind.XML_UPLOAD:
                # The upload step creates the filing; carry the metadata across.
                request.session["pending_filing_meta"] = {
                    "name": name,
                    "period_end_date": period_raw,
                }
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
def filing_view(request, filing_id: int):
    """Form-tree view of a filing: its CRS structure as an expandable folder
    hierarchy with per-node actions, in the manner of an AEOI filing console."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    records = filing.account_reports.filter(superseded=False)
    header_done = bool(filing.receiving_country and filing.sending_company_in and filing.message_reference)
    can_edit = (
        profile.role == PortalUser.Role.MAKER
        and filing.status == Filing.Status.DRAFT
        and filing.kind in (Filing.Kind.MANUAL, Filing.Kind.XML_UPLOAD)
    )
    return render(
        request,
        "portal/filing_view.html",
        {
            "filing": filing,
            "records": records,
            "record_count": records.count(),
            "header_done": header_done,
            "can_edit": can_edit,
            "nav": "filings",
            **_deadline_context(),
        },
    )


@portal_required
def record_delete(request, filing_id: int, record_id: int):
    """Maker deletes one account record from a draft filing."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing)
    target = f"/portal/filings/{filing.pk}/view/"
    if request.method != "POST" or profile.role != PortalUser.Role.MAKER:
        return redirect(target)
    if filing.status != Filing.Status.DRAFT:
        messages.error(request, "Account records can only be deleted while the filing is in draft.")
        return redirect(target)
    ref = record.doc_ref_id
    record.delete()
    _audit(profile, "RECORD_DELETED", filing, f"Account record {ref} deleted.")
    messages.success(request, "Account record deleted.")
    return redirect(target)


@portal_required
def records_clear(request, filing_id: int):
    """Maker clears the CRS Report: removes every account record from a draft."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    target = f"/portal/filings/{filing.pk}/view/"
    if request.method != "POST" or profile.role != PortalUser.Role.MAKER:
        return redirect(target)
    if filing.status != Filing.Status.DRAFT:
        messages.error(request, "The CRS Report can only be cleared while the filing is in draft.")
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
                meta = request.session.pop("pending_filing_meta", None) or {}
                filing = Filing.objects.create(
                    reference=next_filing_reference(),
                    name=meta.get("name", ""),
                    rfi=profile.rfi,
                    reporting_year=config.CURRENT_REPORTING_YEAR,
                    period_end_date=parse_date(meta.get("period_end_date", "") or "") if meta.get("period_end_date") else None,
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
                return redirect(f"/portal/filings/{filing.pk}/created/")
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


def _record_form_context() -> dict:
    """Choice lists shared by the record add and edit forms."""
    return {
        "currencies": config.CURRENCIES,
        "acct_holder_types": AccountReport.AcctHolderType.choices,
        "self_cert_choices": AccountReport.SelfCertification.choices,
        "ctrlg_person_types": ControllingPerson.CtrlgPersonType.choices,
    }


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
    can_edit = (
        is_maker
        and filing.status == Filing.Status.DRAFT
        and filing.kind in (Filing.Kind.MANUAL, Filing.Kind.XML_UPLOAD)
    )
    can_correct = is_maker and filing.status == Filing.Status.RETURNED
    return render(
        request,
        "portal/filing_detail.html",
        {
            "filing": filing,
            "records": records,
            "jurisdictions": _jurisdiction_choices(),
            **_record_form_context(),
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
    self_certification = request.POST.get("self_certification", "").strip()
    if self_certification not in dict(AccountReport.SelfCertification.choices):
        self_certification = ""
    amounts: dict[str, Decimal] = {}
    for field_name in ("balance", "dividends", "interest", "gross_proceeds", "other_income"):
        raw = request.POST.get(field_name, "0").strip() or "0"
        try:
            amounts[field_name] = Decimal(raw)
        except InvalidOperation:
            return f"The {field_name.replace('_', ' ')} amount is not a valid number."
    values = {
        "holder_name": holder_name,
        "holder_type": holder_type,
        "acct_holder_type": acct_holder_type if holder_type == "ORGANISATION" else "",
        "residence_country": residence_country,
        "foreign_tin": request.POST.get("foreign_tin", "").strip(),
        "tin_unavailable_reason": request.POST.get("tin_unavailable_reason", "").strip(),
        "holder_address": holder_address,
        "address_country": (request.POST.get("address_country", "").strip().upper() or residence_country),
        "birth_date": parse_date(request.POST.get("birth_date", "").strip() or "") or None,
        "birth_city": request.POST.get("birth_city", "").strip(),
        "self_certification": self_certification,
        "account_number": account_number,
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
    """Maker adds a controlling person to a Passive NFE account record."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing, superseded=False)
    target = f"/portal/filings/{filing.pk}/records/{record.pk}/"
    if request.method != "POST" or profile.role != PortalUser.Role.MAKER:
        return redirect(target)
    if filing.status not in (Filing.Status.DRAFT, Filing.Status.RETURNED):
        messages.error(request, "Controlling persons can only be edited while the filing is open.")
        return redirect(target)
    name = request.POST.get("cp_name", "").strip()
    residence = request.POST.get("cp_residence", "").strip().upper()
    if not name or not residence:
        messages.error(request, "Controlling person name and residence jurisdiction are required.")
        return redirect(target)
    cp_type = request.POST.get("cp_type", "").strip()
    if cp_type not in dict(ControllingPerson.CtrlgPersonType.choices):
        cp_type = ControllingPerson.CtrlgPersonType.CRS801
    ControllingPerson.objects.create(
        account_report=record,
        name=name,
        residence_country=residence,
        tin=request.POST.get("cp_tin", "").strip(),
        address=request.POST.get("cp_address", "").strip(),
        birth_date=parse_date(request.POST.get("cp_dob", "").strip() or "") or None,
        ctrlg_person_type=cp_type,
    )
    _audit(profile, "CONTROLLING_PERSON_ADDED", filing, f"Controlling person added to {record.doc_ref_id}.")
    messages.success(request, "Controlling person added.")
    return redirect(target)


@portal_required
def controlling_person_delete(request, filing_id: int, record_id: int, cp_id: int):
    """Maker removes a controlling person from an account record."""
    profile = request.portal_profile
    filing = get_object_or_404(Filing, pk=filing_id, rfi=profile.rfi)
    record = get_object_or_404(AccountReport, pk=record_id, filing=filing)
    cp = get_object_or_404(ControllingPerson, pk=cp_id, account_report=record)
    target = f"/portal/filings/{filing.pk}/records/{record.pk}/"
    if request.method == "POST" and profile.role == PortalUser.Role.MAKER and filing.status in (
        Filing.Status.DRAFT,
        Filing.Status.RETURNED,
    ):
        cp.delete()
        _audit(profile, "CONTROLLING_PERSON_REMOVED", filing, f"Controlling person removed from {record.doc_ref_id}.")
        messages.success(request, "Controlling person removed.")
    return redirect(target)


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
