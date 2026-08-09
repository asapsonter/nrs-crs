"""Portal domain services: references, user provisioning, notifications."""
from __future__ import annotations

import secrets

from django.contrib.auth.models import User
from django.core.mail import send_mail

from core import config
from portal.models import Filing, PortalUser, ReportingFI


def next_rfi_reference() -> str:
    """Enrolment reference: NRS-RFI-<year>-<sequence>."""
    year = config.CURRENT_REPORTING_YEAR
    sequence = ReportingFI.objects.count() + 1
    candidate = f"NRS-RFI-{year}-{sequence:04d}"
    while ReportingFI.objects.filter(reference=candidate).exists():
        sequence += 1
        candidate = f"NRS-RFI-{year}-{sequence:04d}"
    return candidate


def next_filing_reference() -> str:
    """Filing reference: FIL-<year>-<sequence>."""
    year = config.CURRENT_REPORTING_YEAR
    sequence = Filing.objects.count() + 1
    candidate = f"FIL-{year}-{sequence:05d}"
    while Filing.objects.filter(reference=candidate).exists():
        sequence += 1
        candidate = f"FIL-{year}-{sequence:05d}"
    return candidate


def auto_validate_submission(filing: Filing) -> tuple[int, int]:
    """Validate a filing against the CRS schema standard at submission.

    Validation is automatic: the moment an institution submits, the schema
    checks run and the filing moves straight to Under Validation with its
    findings recorded, so the Supervision Centre queue begins at approval.
    Returns (file_level_count, record_level_count).
    """
    # Imported here: backoffice.validation pulls in exchange models, which
    # must not load at portal app import time.
    from django.utils import timezone

    from backoffice.validation import run_validation
    from core.models import AuditLog

    file_count, record_count = run_validation(filing)
    filing.status = Filing.Status.UNDER_VALIDATION
    filing.validated_at = timezone.now()
    filing.save(update_fields=["status", "validated_at"])
    AuditLog.record(
        actor_name="CRS schema validator",
        actor_role="System",
        surface="portal",
        action="FILING_AUTO_VALIDATED",
        target=filing.reference,
        before_state=Filing.Status.SUBMITTED,
        after_state=filing.status,
        detail=(
            f"Automatic schema validation on submission: {file_count} file-level, "
            f"{record_count} record-level findings."
        ),
    )
    return file_count, record_count


def generate_temp_password() -> str:
    return secrets.token_urlsafe(9)


def provision_portal_user(
    rfi: ReportingFI,
    *,
    name: str,
    email: str,
    designation: str,
    role: str,
    is_primary: bool = False,
    status: str = PortalUser.Status.ACTIVE,
) -> tuple[PortalUser, str]:
    """Create the Django user and portal profile, returning the temp password."""
    temp_password = generate_temp_password()
    user = User.objects.create_user(username=email, email=email, password=temp_password)
    profile = PortalUser.objects.create(
        user=user,
        rfi=rfi,
        display_name=name,
        designation=designation,
        role=role,
        is_primary_user=is_primary,
        status=status,
        must_change_password=True,
    )
    return profile, temp_password


def send_pu_welcome_email(rfi: ReportingFI, temp_password: str) -> None:
    send_mail(
        subject=f"NRS AEOI-CRS Portal: enrolment approved for {rfi.legal_name}",
        message=(
            f"Dear {rfi.pu_name},\n\n"
            f"The enrolment of {rfi.legal_name} (reference {rfi.reference}) has been approved "
            f"by the Nigeria Revenue Service.\n\n"
            f"You have been registered as the Primary User and first Checker.\n\n"
            f"Sign in at /portal/login/ with:\n"
            f"  Email: {rfi.pu_email}\n"
            f"  Temporary password: {temp_password}\n\n"
            f"You will be required to set a new password at first sign in.\n\n"
            f"Nigeria Revenue Service\nAutomatic Exchange of Information"
        ),
        from_email=None,
        recipient_list=[rfi.pu_email],
    )


def send_rejection_email(rfi: ReportingFI) -> None:
    send_mail(
        subject=f"NRS AEOI-CRS Portal: enrolment decision for {rfi.legal_name}",
        message=(
            f"Dear {rfi.pu_name},\n\n"
            f"The Institution & Primary User Enrolment of {rfi.legal_name} (reference {rfi.reference}) "
            f"has been declined.\n\n"
            f"Reason: {rfi.rejection_reason}\n\n"
            f"A new enrolment may be submitted once the matter above is addressed.\n\n"
            f"Nigeria Revenue Service\nAutomatic Exchange of Information"
        ),
        from_email=None,
        recipient_list=[rfi.pu_email],
    )


def apply_notice(filing: Filing) -> str:
    """Apply an approved administrative notice to the institution.

    Returns a short description of what was applied, for the audit trail.
    The caller is responsible for the filing's own status transition.
    """
    rfi = filing.rfi
    payload = filing.notice_payload

    if filing.kind == Filing.Kind.PU_CHANGE:
        # The outgoing Primary User's access ends; the incoming officer is
        # recorded on the institution and provisioned portal credentials.
        old_pus = rfi.portal_users.filter(is_primary_user=True)
        for old in old_pus:
            old.is_primary_user = False
            old.status = PortalUser.Status.DISABLED
            old.save(update_fields=["is_primary_user", "status"])
            old.user.is_active = False
            old.user.save(update_fields=["is_active"])
        rfi.pu_surname = payload.get("new_pu_surname", "")
        rfi.pu_middle_name = payload.get("new_pu_middle_name", "")
        rfi.pu_first_name = payload.get("new_pu_first_name", "")
        rfi.pu_designation = payload.get("new_pu_designation", "")
        rfi.pu_email = payload.get("new_pu_email", "")
        rfi.pu_phone = payload.get("new_pu_phone", "")
        rfi.save(update_fields=[
            "pu_surname", "pu_middle_name", "pu_first_name",
            "pu_designation", "pu_email", "pu_phone",
        ])
        display_name = " ".join(
            part for part in (rfi.pu_first_name, rfi.pu_middle_name, rfi.pu_surname) if part
        )
        _, temp_password = provision_portal_user(
            rfi,
            name=display_name,
            email=rfi.pu_email,
            designation=rfi.pu_designation,
            role=PortalUser.Role.CHECKER,
            is_primary=True,
        )
        send_pu_welcome_email(rfi, temp_password)
        return f"Primary User changed to {display_name}; credentials issued to {rfi.pu_email}."

    if filing.kind == Filing.Kind.ENTITY_DEACTIVATION:
        rfi.status = ReportingFI.Status.DEACTIVATED
        rfi.save(update_fields=["status"])
        for profile in rfi.portal_users.exclude(status=PortalUser.Status.DISABLED):
            profile.status = PortalUser.Status.DISABLED
            profile.save(update_fields=["status"])
            profile.user.is_active = False
            profile.user.save(update_fields=["is_active"])
        return (
            f"Reporting entity deactivated effective {payload.get('effective_date', '')}; "
            "all portal users disabled."
        )

    if filing.kind == Filing.Kind.ENTITY_INFO_CHANGE:
        rfi.legal_name = payload.get("legal_name", rfi.legal_name)
        rfi.street = payload.get("street", rfi.street)
        rfi.city = payload.get("city", rfi.city)
        rfi.state_province = payload.get("state_province", rfi.state_province)
        rfi.post_code = payload.get("post_code", rfi.post_code)
        rfi.email = payload.get("email", rfi.email)
        rfi.phone = payload.get("phone", rfi.phone)
        rfi.save(update_fields=[
            "legal_name", "street", "city", "state_province", "post_code", "email", "phone",
        ])
        return f"Reporting entity information updated for {rfi.legal_name}."

    raise ValueError(f"Filing {filing.reference} is not an administrative notice.")
