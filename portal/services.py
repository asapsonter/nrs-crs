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
            f"The enrolment application of {rfi.legal_name} (reference {rfi.reference}) "
            f"has been rejected.\n\n"
            f"Reason: {rfi.rejection_reason}\n\n"
            f"A new application may be submitted once the matter above is addressed.\n\n"
            f"Nigeria Revenue Service\nAutomatic Exchange of Information"
        ),
        from_email=None,
        recipient_list=[rfi.pu_email],
    )
