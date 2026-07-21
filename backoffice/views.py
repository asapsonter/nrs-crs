"""Supervision Centre: authentication, dashboard, and audit views."""
from __future__ import annotations

from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.utils import timezone

from core import access, config
from core.decorators import require_internal, superadmin_or_cap
from core.models import AuditLog, IssuedCredential, OfficerProfile, Roles
from portal.models import Filing, ReportingFI


def login_view(request):
    """Credential sign in for officers, password sign in for the Super Admin."""
    error = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        # Strip the passcode: credentials are copied from the screen and often
        # arrive with surrounding whitespace, which must not fail the match.
        passcode = request.POST.get("passcode", "").strip()
        user = authenticate(request, username=username, password=passcode)
        if user is not None:
            login(request, user)
            credential: IssuedCredential | None = getattr(request, "_pending_credential", None)
            if credential is not None:
                # login() flushes any prior sign-in (for example the Super
                # Admin who issued this credential), which leaves the session
                # without a key. Allocate one before binding the credential.
                if not request.session.session_key:
                    request.session.save()
                request.session["credential_id"] = credential.pk
                credential.activate(request.session.session_key, user)
                AuditLog.record(
                    actor_name=credential.officer.name,
                    actor_role=credential.role_display,
                    action="CREDENTIAL_FIRST_USE",
                    target=credential.username,
                    detail=(
                        f"Credential activated. Session window fixed at {credential.session_hours} hours, "
                        f"ends {timezone.localtime(credential.session_expires_at):%Y-%m-%d %H:%M}."
                    ),
                    credential=credential,
                )
                return redirect("/backoffice/")
            if user.is_superuser:
                AuditLog.record(
                    actor_name="Super Admin",
                    actor_role=Roles.SUPER_ADMIN.label,
                    action="SUPER_ADMIN_LOGIN",
                    detail="Super Admin signed in to the admin console.",
                )
                return redirect("/backoffice/admin-console/")
            # A user authenticated by neither route has no place here.
            logout(request)
        # Distinguish refusal reasons for the presenter without weakening the
        # message shown for a plain wrong passcode.
        credential = IssuedCredential.objects.filter(username=username).first()
        if credential is not None:
            credential.lapse_if_unused()
            if credential.status == IssuedCredential.Status.CONSUMED:
                error = "This credential has been consumed. Each credential admits exactly one session. Contact the administrator for a new credential."
            elif credential.status == IssuedCredential.Status.ACTIVE:
                error = "This credential is bound to a live session and cannot be used again. The attempt has been recorded."
            elif credential.status == IssuedCredential.Status.REVOKED:
                error = "This credential has been revoked by the administrator."
            elif credential.status == IssuedCredential.Status.EXPIRED:
                error = "This credential lapsed unused past its issuance validity window."
            else:
                error = "Sign in failed. Check the credential username and passcode."
        else:
            error = "Sign in failed. Check the credential username and passcode."
    return render(request, "backoffice/login.html", {"error": error})


def logout_view(request):
    """Voluntary sign out consumes the credential: the session is over."""
    credential_id = request.session.get("credential_id")
    if credential_id:
        credential = IssuedCredential.objects.filter(
            pk=credential_id, status=IssuedCredential.Status.ACTIVE
        ).first()
        if credential:
            credential.consume("Officer signed out. Session ended.")
    logout(request)
    return redirect("/backoffice/login/")


@require_internal
def dashboard(request):
    """Landing after credential login: queue counts across the workflow."""
    from exchange.models import ExchangePackage, InboundFile, RecordError
    from exchange.services import days_to_exchange_deadline

    year = config.CURRENT_REPORTING_YEAR
    caps = access.capabilities_for(request.credential.role_list)
    days_left = days_to_exchange_deadline()
    # Each card names the capability that opens its queue. A card the user
    # cannot open stays visible as a figure but is not a link, so the
    # dashboard never routes a user to a page their roles would refuse.
    all_cards = [
        {
            "label": "Enrolments pending review",
            "value": ReportingFI.objects.filter(
                status__in=[ReportingFI.Status.SUBMITTED, ReportingFI.Status.UNDER_REVIEW]
            ).count(),
            "url": "/backoffice/registration/",
            "cap": access.VIEW_REGISTRATION,
        },
        {
            "label": "Filings awaiting validation",
            "value": Filing.objects.filter(
                status__in=[Filing.Status.SUBMITTED, Filing.Status.UNDER_VALIDATION]
            ).count(),
            "url": "/backoffice/returns/",
            "cap": access.VIEW_RETURNS,
        },
        {
            "label": "Filings awaiting approval",
            "value": Filing.objects.filter(
                status=Filing.Status.UNDER_VALIDATION, validated_at__isnull=False
            ).count(),
            "url": "/backoffice/returns/",
            "cap": access.VIEW_RETURNS,
        },
        {
            "label": "Packages ready to transmit",
            "value": ExchangePackage.objects.filter(status=ExchangePackage.Status.BUILT).count(),
            "url": "/backoffice/exchange/",
            "cap": access.VIEW_EXCHANGE,
        },
        {
            "label": "Inbound files awaiting processing",
            "value": InboundFile.objects.filter(
                status__in=[InboundFile.Status.RECEIVED, InboundFile.Status.ACCEPTED, InboundFile.Status.APPROVED]
            ).count(),
            "url": "/backoffice/exchange/inbound/",
            "cap": access.VIEW_EXCHANGE,
        },
        {
            "label": "Open correction items",
            "value": RecordError.objects.filter(resolved=False).count(),
            "url": "/backoffice/exchange/corrections/",
            "cap": access.VIEW_EXCHANGE,
        },
        {
            "label": "Days to 30 September exchange deadline",
            "value": days_left,
            "url": "/backoffice/exchange/",
            "alert": days_left < 30,
            "cap": access.VIEW_EXCHANGE,
        },
    ]
    for card in all_cards:
        card["clickable"] = card["cap"] in caps
    return render(request, "backoffice/dashboard.html", {"cards": all_cards, "nav": "dashboard", "year": year})


@superadmin_or_cap(access.VIEW_AUDIT)
def audit_log(request):
    """Complete append-only audit trail across both surfaces."""
    entries = AuditLog.objects.select_related("credential")[:500]
    return render(request, "backoffice/audit_log.html", {"entries": entries, "nav": "audit"})


@superadmin_or_cap(access.VIEW_AUDIT)
def credential_history(request):
    """Credential lifecycle history across all officers."""
    credentials = IssuedCredential.objects.select_related("officer")[:500]
    return render(
        request,
        "backoffice/credential_history.html",
        {"credentials": credentials, "nav": "audit-credentials"},
    )
