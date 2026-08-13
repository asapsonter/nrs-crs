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
            credential: IssuedCredential | None = getattr(request, "_pending_credential", None)
            login(request, user)
            if credential is not None:
                # login() flushes any prior sign-in (for example the Super
                # Admin who issued this credential), which leaves the session
                # without a key. Allocate one before binding the credential.
                if not request.session.session_key:
                    request.session.save()
                request.session["credential_id"] = credential.pk
                first_use = credential.first_used_at is None
                credential.activate(request.session.session_key, user)
                if first_use:
                    if credential.is_unlimited:
                        detail = "Credential activated with unlimited validity: no expiry."
                    else:
                        detail = (
                            f"Credential activated. Validity fixed at {credential.session_months} "
                            f"month{'s' if credential.session_months != 1 else ''}, ends "
                            f"{timezone.localtime(credential.session_expires_at):%Y-%m-%d}."
                        )
                    AuditLog.record(
                        actor_name=credential.officer.name,
                        actor_role=credential.role_display,
                        action="CREDENTIAL_FIRST_USE",
                        target=credential.username,
                        detail=detail,
                        credential=credential,
                    )
                else:
                    AuditLog.record(
                        actor_name=credential.officer.name,
                        actor_role=credential.role_display,
                        action="SESSION_STARTED",
                        target=credential.username,
                        detail="Officer signed in within the credential validity window.",
                        credential=credential,
                    )
                return redirect("/backoffice/")
            if user.is_superuser:
                AuditLog.record(
                    actor_name="Super Admin",
                    actor_role=Roles.SUPER_ADMIN.label,
                    action="SUPER_ADMIN_LOGIN",
                    detail="Super Admin signed in to the Supervision Centre.",
                )
                return redirect("/backoffice/")
            # A user authenticated by neither route has no place here.
            logout(request)
        # Distinguish refusal reasons for the presenter without weakening the
        # message shown for a plain wrong passcode.
        credential = IssuedCredential.objects.filter(username=username).first()
        if credential is not None:
            credential.lapse_if_unused()
            if credential.status == IssuedCredential.Status.CONSUMED:
                error = "This credential has been consumed: its validity window has ended. Contact the administrator for a new credential."
            elif credential.status == IssuedCredential.Status.ACTIVE and credential.bound_session_key:
                error = "This credential is bound to a live session and cannot be used again until that session ends. The attempt has been recorded."
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
    """Voluntary sign out ends the day's session; the credential stays valid.

    The officer signs back in with the same credential on the next working
    day (or later the same day) until the validity window ends.
    """
    credential_id = request.session.get("credential_id")
    if credential_id:
        credential = IssuedCredential.objects.filter(
            pk=credential_id, status=IssuedCredential.Status.ACTIVE
        ).first()
        if credential:
            credential.unbind("Officer signed out. Session ended; credential remains valid.")
    logout(request)
    return redirect("/backoffice/login/")


@require_internal
def dashboard(request):
    """Landing after credential login: queue counts across the workflow."""
    from exchange.models import ExchangePackage, InboundFile, RecordError
    from exchange.services import days_to_exchange_deadline

    year = config.CURRENT_REPORTING_YEAR
    caps = request.caps
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
        # Filings validate automatically against the CRS schema on submission,
        # so the queue starts at approval: no "awaiting validation" figure.
        {
            "label": "Notices awaiting decision",
            "value": Filing.objects.filter(
                status=Filing.Status.UNDER_VALIDATION,
                kind__in=Filing.NOTICE_KINDS,
            ).count(),
            "url": "/backoffice/returns/",
            "cap": access.VIEW_RETURNS,
        },
        {
            "label": "Filings accepted this cycle",
            "value": Filing.objects.filter(
                status__in=[Filing.Status.ACCEPTED, Filing.Status.IN_EXCHANGE]
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
    context = {"cards": all_cards, "nav": "dashboard", "year": year, "show_reports": False}
    # Reports & Insights render on the dashboard for officers whose roles
    # carry the reports capability.
    if access.VIEW_REPORTS in caps:
        from backoffice.views_reports import reports_context

        context.update(reports_context())
        context["show_reports"] = True
    return render(request, "backoffice/dashboard.html", context)


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
