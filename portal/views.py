"""RFI Portal: authentication, overview, and user administration."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render

from core import config
from core.decorators import portal_required
from core.models import AuditLog
from exchange.services import days_to_domestic_deadline, domestic_deadline
from portal.models import Filing, PortalUser, ReportingFI
from portal.services import provision_portal_user


def _deadline_context() -> dict:
    days = days_to_domestic_deadline()
    return {
        "deadline_days": days,
        "deadline_date": domestic_deadline(),
        "deadline_overdue": days < 0,
    }


def login_view(request):
    """Email and password sign in for approved RFI users."""
    error = ""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        profile = PortalUser.objects.filter(user__username=email).select_related("rfi", "user").first()
        if profile is None:
            error = "Sign in failed. Check your email and password."
        elif profile.is_locked:
            error = "This account is locked after repeated failed sign in attempts. Contact the NRS AEOI desk."
        elif profile.status == PortalUser.Status.PENDING:
            error = "Your registration is awaiting Checker approval within your institution."
        elif profile.status == PortalUser.Status.DISABLED:
            error = "This account has been disabled."
        elif not profile.rfi.is_operational:
            error = "Your institution's enrolment is not currently active."
        else:
            user = authenticate(request, username=email, password=password)
            if user is None:
                profile.failed_logins += 1
                if profile.failed_logins >= config.PORTAL_MAX_FAILED_LOGINS:
                    profile.is_locked = True
                    AuditLog.record(
                        actor_name=profile.display_name,
                        surface="portal",
                        action="PORTAL_ACCOUNT_LOCKED",
                        target=email,
                        detail="Account locked after 5 failed sign in attempts.",
                        flagged=True,
                    )
                profile.save(update_fields=["failed_logins", "is_locked"])
                error = "Sign in failed. Check your email and password."
            else:
                profile.failed_logins = 0
                profile.save(update_fields=["failed_logins"])
                login(request, user)
                AuditLog.record(
                    actor_name=profile.display_name,
                    surface="portal",
                    action="PORTAL_LOGIN",
                    target=profile.rfi.reference,
                    detail=f"{profile.get_role_display()} signed in for {profile.rfi.legal_name}.",
                )
                if profile.must_change_password:
                    return redirect("/portal/change-password/")
                return redirect("/portal/")
    return render(request, "portal/login.html", {"error": error, "nav": "login"})


def logout_view(request):
    logout(request)
    return redirect("/portal/login/")


@portal_required
def change_password(request):
    """Forced at first sign in, available thereafter."""
    profile = request.portal_profile
    error = ""
    if request.method == "POST":
        new_password = request.POST.get("new_password", "")
        confirm = request.POST.get("confirm_password", "")
        if len(new_password) < 8:
            error = "The new password must be at least 8 characters."
        elif new_password != confirm:
            error = "The two entries do not match."
        else:
            request.user.set_password(new_password)
            request.user.save()
            update_session_auth_hash(request, request.user)
            profile.must_change_password = False
            profile.save(update_fields=["must_change_password"])
            # First password change by the Primary User activates the RFI.
            rfi = profile.rfi
            if profile.is_primary_user and rfi.status == ReportingFI.Status.APPROVED:
                rfi.status = ReportingFI.Status.ACTIVE
                rfi.save(update_fields=["status"])
                rfi.record_status_event("Primary User completed first sign in. Institution active.")
                AuditLog.record(
                    actor_name=profile.display_name,
                    surface="portal",
                    action="RFI_ACTIVATED",
                    target=rfi.reference,
                    before_state=ReportingFI.Status.APPROVED,
                    after_state=ReportingFI.Status.ACTIVE,
                    detail="Primary User completed first sign in. Institution is active.",
                )
            messages.success(request, "Password updated.")
            return redirect("/portal/")
    return render(
        request,
        "portal/change_password.html",
        {"error": error, "forced": profile.must_change_password, "nav": "home"},
    )


@portal_required
def home(request):
    """Portal overview: institution standing and filing position."""
    profile = request.portal_profile
    if profile.must_change_password:
        return redirect("/portal/change-password/")
    rfi = profile.rfi
    filings = rfi.filings.all()[:10]
    context = {
        "rfi": rfi,
        "filings": filings,
        "profile": profile,
        "nav": "home",
        **_deadline_context(),
    }
    return render(request, "portal/home.html", context)


@portal_required
def users(request):
    """Checker administration: invite users, approve Maker registrations.

    Restricted to Checkers. A Maker who reaches this URL is sent back to the
    portal home with an explanation rather than a bare 404.
    """
    profile = request.portal_profile
    if profile.role != PortalUser.Role.CHECKER:
        messages.info(
            request,
            "Managing users is available to the Primary User and other Checkers. "
            "Ask a Checker at your institution to add or approve users.",
        )
        return redirect("/portal/")
    rfi = profile.rfi
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "invite":
            name = request.POST.get("name", "").strip()
            email = request.POST.get("email", "").strip().lower()
            designation = request.POST.get("designation", "").strip()
            role = request.POST.get("role", PortalUser.Role.MAKER)
            if not name or not email or role not in PortalUser.Role.values:
                messages.error(request, "Name, email, and role are required.")
            elif User.objects.filter(username=email).exists():
                messages.error(request, "A portal account with that email already exists.")
            else:
                # Maker registrations require Checker approval; a Checker
                # invited by the PU is active immediately.
                status = PortalUser.Status.PENDING if role == PortalUser.Role.MAKER else PortalUser.Status.ACTIVE
                new_profile, temp_password = provision_portal_user(
                    rfi, name=name, email=email, designation=designation, role=role, status=status
                )
                send_mail(
                    subject=f"NRS AEOI-CRS Portal: account created for {rfi.legal_name}",
                    message=(
                        f"Dear {name},\n\nAn account has been created for you on the NRS AEOI-CRS Portal "
                        f"by {profile.display_name} ({rfi.legal_name}).\n\n"
                        f"Role: {new_profile.get_role_display()}\n"
                        f"Email: {email}\nTemporary password: {temp_password}\n\n"
                        + (
                            "Your registration takes effect once approved by a Checker.\n\n"
                            if status == PortalUser.Status.PENDING
                            else ""
                        )
                        + "You will be required to set a new password at first sign in.\n\n"
                        "Nigeria Revenue Service\nAutomatic Exchange of Information"
                    ),
                    from_email=None,
                    recipient_list=[email],
                )
                AuditLog.record(
                    actor_name=profile.display_name,
                    surface="portal",
                    action="PORTAL_USER_INVITED",
                    target=email,
                    detail=f"{new_profile.get_role_display()} invited at {rfi.legal_name}. Status {status}.",
                )
                messages.success(request, f"{new_profile.get_role_display()} account created. Credentials sent by email.")
        elif action == "approve":
            pending = get_object_or_404(
                PortalUser, pk=request.POST.get("user_id"), rfi=rfi, status=PortalUser.Status.PENDING
            )
            pending.status = PortalUser.Status.ACTIVE
            pending.save(update_fields=["status"])
            AuditLog.record(
                actor_name=profile.display_name,
                surface="portal",
                action="PORTAL_MAKER_APPROVED",
                target=pending.user.username,
                before_state=PortalUser.Status.PENDING,
                after_state=PortalUser.Status.ACTIVE,
                detail=f"Maker registration approved by Checker at {rfi.legal_name}.",
            )
            messages.success(request, f"{pending.display_name} approved as Maker.")
        return redirect("/portal/users/")
    return render(
        request,
        "portal/users.html",
        {
            "rfi": rfi,
            "portal_users": rfi.portal_users.select_related("user"),
            "role_choices": PortalUser.Role.choices,
            "nav": "users",
            **_deadline_context(),
        },
    )
