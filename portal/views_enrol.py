"""Public enrolment: application form and status check, pre-login."""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from core import config
from core.models import AuditLog
from portal.models import ReportingFI
from portal.services import next_rfi_reference

# Required model fields with the label used in error messages.
REQUIRED_FIELDS: list[tuple[str, str]] = [
    ("Enrolment type", "enrolment_type"),
    ("Financial Institution Name", "legal_name"),
    ("TIN", "tin"),
    ("Financial Institution Category", "category"),
    ("Financial Institution email", "email"),
    ("Financial Institution telephone", "phone"),
    ("Street", "street"),
    ("City or town", "city"),
    ("State or province", "state_province"),
    ("Post code", "post_code"),
    ("Primary User surname", "pu_surname"),
    ("Primary User firstname", "pu_first_name"),
    ("Date of birth", "pu_dob"),
    ("Place of birth", "pu_place_of_birth"),
    ("Primary User position", "pu_designation"),
    ("Primary User email", "pu_email"),
    ("Primary User telephone", "pu_phone"),
]


def enrol(request):
    """Public enrolment application for a Reporting Financial Institution."""
    errors: list[str] = []
    if request.method == "POST":
        valid_codes = {code for code, _ in config.DIAL_CODES}
        phone_cc = request.POST.get("phone_cc", "+234").strip()
        pu_phone_cc = request.POST.get("pu_phone_cc", "+234").strip()
        enrolment_type = request.POST.get("enrolment_type", "")
        valid_types = {code for code, _ in config.ENROLMENT_TYPES}
        fields = {
            "enrolment_type": enrolment_type if enrolment_type in valid_types else "",
            "legal_name": request.POST.get("legal_name", "").strip(),
            "tin": request.POST.get("tin", "").strip(),
            "category": request.POST.get("category", ""),
            "email": request.POST.get("email", "").strip().lower(),
            "phone_cc": phone_cc if phone_cc in valid_codes else "+234",
            "phone": request.POST.get("phone", "").strip(),
            "street": request.POST.get("street", "").strip(),
            "city": request.POST.get("city", "").strip(),
            "state_province": request.POST.get("state_province", "").strip(),
            "post_code": request.POST.get("post_code", "").strip(),
            "pu_surname": request.POST.get("pu_surname", "").strip(),
            "pu_middle_name": request.POST.get("pu_middle_name", "").strip(),
            "pu_first_name": request.POST.get("pu_first_name", "").strip(),
            "pu_dob": parse_date(request.POST.get("pu_dob", "").strip() or "") or None,
            "pu_place_of_birth": request.POST.get("pu_place_of_birth", "").strip(),
            "pu_designation": request.POST.get("pu_designation", "").strip(),
            "pu_email": request.POST.get("pu_email", "").strip().lower(),
            "pu_phone_cc": pu_phone_cc if pu_phone_cc in valid_codes else "+234",
            "pu_phone": request.POST.get("pu_phone", "").strip(),
        }
        email_confirm = request.POST.get("email_confirm", "").strip().lower()

        for label, key in REQUIRED_FIELDS:
            if not fields[key]:
                errors.append(f"{label} is required.")

        # The institution email must be a valid address, and the confirmation
        # must match it exactly.
        if fields["email"]:
            try:
                validate_email(fields["email"])
            except ValidationError:
                errors.append("The Financial Institution email is not a valid email address.")
            if email_confirm != fields["email"]:
                errors.append("The email and confirmation email do not match.")
        if fields["pu_email"]:
            try:
                validate_email(fields["pu_email"])
            except ValidationError:
                errors.append("The Primary User email is not a valid email address.")

        if fields["pu_dob"] and fields["pu_dob"] > timezone.now().date():
            errors.append("The date of birth cannot be in the future.")

        id_document = request.FILES.get("id_document")
        if id_document is None:
            errors.append("A Primary User means of identification is required.")

        ceo_letter = request.FILES.get("ceo_letter")
        if ceo_letter is None:
            errors.append("The Letter of Authorisation is required.")
        elif not ceo_letter.name.lower().endswith(".pdf"):
            errors.append("The Letter of Authorisation must be a PDF document.")

        # The applicant must tick the accuracy confirmation; the browser
        # enforces it too, but the server is the authority.
        if request.POST.get("confirm_accuracy") != "yes":
            errors.append(
                "Tick the confirmation that the information provided is accurate and complete."
            )

        if ReportingFI.objects.filter(tin=fields["tin"]).exclude(status=ReportingFI.Status.REJECTED).exists():
            errors.append("An application or enrolment already exists for this TIN.")

        if not errors:
            rfi = ReportingFI.objects.create(
                reference=next_rfi_reference(),
                status=ReportingFI.Status.SUBMITTED,
                ceo_letter=ceo_letter,
                id_document=id_document,
                **fields,
            )
            rfi.record_status_event("Application submitted.", at=rfi.submitted_at)
            AuditLog.record(
                actor_name=rfi.pu_name,
                surface="portal",
                action="ENROLMENT_SUBMITTED",
                target=rfi.reference,
                after_state=ReportingFI.Status.SUBMITTED,
                detail=f"Enrolment application submitted for {rfi.legal_name}.",
            )
            return render(request, "portal/enrol_submitted.html", {"rfi": rfi, "nav": "enrol"})
    return render(
        request,
        "portal/enrol.html",
        {
            "errors": errors,
            "categories": config.FI_CATEGORIES,
            "enrolment_types": config.ENROLMENT_TYPES,
            "dial_codes": config.DIAL_CODES,
            "values": request.POST if request.method == "POST" else {},
            "nav": "enrol",
        },
    )


def enrol_status(request):
    """Reference number lookup for applicants."""
    rfi = None
    searched = False
    if request.method == "POST":
        searched = True
        reference = request.POST.get("reference", "").strip().upper()
        rfi = ReportingFI.objects.filter(reference=reference).first()
    return render(
        request,
        "portal/enrol_status.html",
        {"rfi": rfi, "searched": searched, "nav": "status"},
    )
