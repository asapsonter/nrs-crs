"""Super Admin console: user management and the credential lifecycle.

Separation of duties: the Super Admin administers access but does not
process enrolments, returns, or exchanges. The Super Admin creates internal
users, assigns each one or more roles, and issues single-use credentials.
"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.decorators import superadmin_required
from core.models import ASSIGNABLE_ROLES, AuditLog, IssuedCredential, OfficerProfile, Roles, roles_to_labels


def _role_choices() -> list[tuple[str, str]]:
    return [(code, Roles(code).label) for code in ASSIGNABLE_ROLES]


def _posted_roles(request) -> list[str]:
    """Selected roles from the form, limited to assignable roles."""
    return [code for code in request.POST.getlist("roles") if code in ASSIGNABLE_ROLES]


@superadmin_required
def console(request):
    """Issue credentials to existing users and review recent issuance."""
    issued: tuple[IssuedCredential, str] | None = None
    if request.method == "POST":
        officer = get_object_or_404(OfficerProfile, pk=request.POST.get("officer"))
        try:
            session_hours = float(request.POST.get("session_hours", "0"))
        except ValueError:
            session_hours = 0.0
        if session_hours <= 0 or session_hours > 24:
            messages.error(request, "Session duration must be between 0 and 24 hours.")
        elif not officer.role_list:
            messages.error(request, "Assign at least one role to this user before issuing a credential.")
        else:
            credential, passcode = IssuedCredential.objects.issue(officer, session_hours)
            AuditLog.record(
                actor_name="Super Admin",
                actor_role=Roles.SUPER_ADMIN.label,
                action="CREDENTIAL_ISSUED",
                target=credential.username,
                detail=(
                    f"Issued to {officer.name} with roles {credential.role_display} "
                    f"for a {session_hours} hour session window."
                ),
                credential=credential,
            )
            issued = (credential, passcode)
            messages.success(
                request,
                "Credential issued. The passcode below is shown once only. Convey it to the user securely.",
            )
    # Lapse any unused credentials whose issuance window has passed, so the
    # console always shows current statuses.
    for credential in IssuedCredential.objects.filter(status=IssuedCredential.Status.ISSUED):
        credential.lapse_if_unused()
    recent = IssuedCredential.objects.select_related("officer")[:25]
    officers = OfficerProfile.objects.filter(is_active=True)
    return render(
        request,
        "backoffice/admin_console.html",
        {
            "officers": officers,
            "recent": recent,
            "issued": issued,
            "nav": "admin",
        },
    )


@superadmin_required
def officers(request):
    """User register: create internal users with one or more roles."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        roles = _posted_roles(request)
        if not name or not email or not roles:
            messages.error(request, "Name, email, and at least one role are required.")
        elif OfficerProfile.objects.filter(email=email).exists():
            messages.error(request, "A user with that email already exists.")
        else:
            officer = OfficerProfile.objects.create(name=name, email=email, roles=",".join(roles))
            AuditLog.record(
                actor_name="Super Admin",
                actor_role=Roles.SUPER_ADMIN.label,
                action="USER_CREATED",
                target=name,
                detail=f"User created with roles {officer.role_display}.",
            )
            messages.success(request, f"User {name} created with roles {officer.role_display}.")
        return redirect("/backoffice/admin-console/officers/")
    return render(
        request,
        "backoffice/officers.html",
        {
            "officers": OfficerProfile.objects.all(),
            "role_choices": _role_choices(),
            "nav": "officers",
        },
    )


@superadmin_required
def officer_roles(request, officer_id: int):
    """Update the roles assigned to a user."""
    officer = get_object_or_404(OfficerProfile, pk=officer_id)
    if request.method == "POST":
        roles = _posted_roles(request)
        if not roles:
            messages.error(request, "A user must hold at least one role.")
        else:
            before = officer.role_display
            officer.roles = ",".join(roles)
            officer.save(update_fields=["roles"])
            AuditLog.record(
                actor_name="Super Admin",
                actor_role=Roles.SUPER_ADMIN.label,
                action="USER_ROLES_UPDATED",
                target=officer.name,
                before_state=before,
                after_state=officer.role_display,
                detail=f"Roles updated for {officer.name}.",
            )
            messages.success(request, f"Roles updated for {officer.name}.")
    return redirect(f"/backoffice/admin-console/officers/{officer.pk}/")


@superadmin_required
def officer_history(request, officer_id: int):
    """Full credential history for one user."""
    officer = get_object_or_404(OfficerProfile, pk=officer_id)
    return render(
        request,
        "backoffice/officer_history.html",
        {
            "officer": officer,
            "credentials": officer.credentials.all(),
            "role_choices": _role_choices(),
            "nav": "officers",
        },
    )


@superadmin_required
def live_sessions(request):
    """Live credential sessions with remaining time and immediate revocation."""
    now = timezone.now()
    active = (
        IssuedCredential.objects.filter(status=IssuedCredential.Status.ACTIVE)
        .select_related("officer")
        .order_by("session_expires_at")
    )
    # A session whose window has passed but which has not made a request since
    # is displayed as ended; it is consumed on its next request or here.
    for credential in active:
        if credential.session_expires_at and now >= credential.session_expires_at:
            credential.consume("Session window elapsed. Marked consumed during live session review.")
    active = (
        IssuedCredential.objects.filter(status=IssuedCredential.Status.ACTIVE)
        .select_related("officer")
        .order_by("session_expires_at")
    )
    pending = IssuedCredential.objects.filter(status=IssuedCredential.Status.ISSUED).select_related("officer")
    return render(
        request,
        "backoffice/live_sessions.html",
        {"active": active, "pending": pending, "nav": "sessions"},
    )


@superadmin_required
def revoke_credential(request, credential_id: int):
    """Revoke immediately. Any live session ends on its next request."""
    credential = get_object_or_404(IssuedCredential, pk=credential_id)
    if request.method == "POST" and credential.status in (
        IssuedCredential.Status.ISSUED,
        IssuedCredential.Status.ACTIVE,
    ):
        credential.revoke("Super Admin")
        messages.success(request, f"Credential {credential.username} revoked.")
    return redirect(request.POST.get("next", "/backoffice/admin-console/sessions/"))
