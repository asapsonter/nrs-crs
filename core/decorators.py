"""Access control decorators for the two application surfaces.

Cross-surface requests return 404 rather than 403 so the existence of the
other surface's resources is never confirmed to the wrong audience. Within
the backoffice, a signed-in user who lacks access to a page is told why
(access restricted page) rather than shown a bare 404.
"""
from __future__ import annotations

from functools import wraps

from django.http import Http404
from django.shortcuts import redirect, render

from core.access import ANY_INTERNAL, SUPERADMIN_CAPS, capabilities_for
from core.models import Roles


def _access_restricted(request, reason: str, hint: str = ""):
    """Friendly in-surface denial for a signed-in backoffice user."""
    return render(
        request,
        "backoffice/access_restricted.html",
        {"reason": reason, "hint": hint},
        status=403,
    )


_SUPERADMIN_ACTION_REASON = (
    "The Super Admin can view the operational workspaces but does not process "
    "casework, so this action is reserved for credentialled officers."
)
_SUPERADMIN_ACTION_HINT = (
    "Issue an officer a credential with the appropriate role from the Admin "
    "Console; the officer signs in with it to take this action."
)


def superadmin_required(view_func):
    """Require the standing Super Admin account (administration only)."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if getattr(request, "surface", "") != "backoffice":
            raise Http404
        if not request.user.is_authenticated:
            return redirect("/backoffice/login/")
        if not request.user.is_superuser:
            raise Http404
        return view_func(request, *args, **kwargs)

    return wrapped


def require_internal(view_func):
    """Require the Super Admin or any credentialled internal user.

    The Super Admin views the operational workspaces read-only; casework is
    processed by credentialled officers.
    """

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if getattr(request, "surface", "") != "backoffice":
            raise Http404
        if not request.user.is_authenticated:
            return redirect("/backoffice/login/")
        if request.user.is_superuser:
            request.caps = set(SUPERADMIN_CAPS)
            return view_func(request, *args, **kwargs)
        credential = getattr(request, "credential", None)
        if credential is None:
            return redirect("/backoffice/login/")
        if not (set(credential.role_list) & ANY_INTERNAL):
            return _access_restricted(
                request, "Your credential does not carry an internal operational role."
            )
        request.caps = capabilities_for(credential.role_list)
        return view_func(request, *args, **kwargs)

    return wrapped


def require_cap(*needed: str):
    """Require the internal user to hold at least one of the given capabilities."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if getattr(request, "surface", "") != "backoffice":
                raise Http404
            if not request.user.is_authenticated:
                return redirect("/backoffice/login/")
            if request.user.is_superuser:
                caps = set(SUPERADMIN_CAPS)
                if not (set(needed) & caps):
                    return _access_restricted(
                        request, _SUPERADMIN_ACTION_REASON, _SUPERADMIN_ACTION_HINT
                    )
                request.caps = caps
                return view_func(request, *args, **kwargs)
            credential = getattr(request, "credential", None)
            if credential is None:
                return redirect("/backoffice/login/")
            caps = capabilities_for(credential.role_list)
            if not (set(needed) & caps):
                return _access_restricted(
                    request,
                    "Your current roles do not include the capability this page requires.",
                    "Ask the Super Admin to issue a credential with the appropriate role.",
                )
            request.caps = caps
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def superadmin_or_cap(*needed: str):
    """Allow the Super Admin, or an internal user holding one of the caps."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if getattr(request, "surface", "") != "backoffice":
                raise Http404
            if not request.user.is_authenticated:
                return redirect("/backoffice/login/")
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            credential = getattr(request, "credential", None)
            if credential is None:
                raise Http404
            if not (set(needed) & capabilities_for(credential.role_list)):
                raise Http404
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def portal_required(view_func):
    """Require an authenticated portal user with an RFI profile."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if getattr(request, "surface", "") != "portal":
            raise Http404
        if not request.user.is_authenticated:
            return redirect("/portal/login/")
        profile = getattr(request.user, "portal_profile", None)
        if profile is None:
            raise Http404
        request.portal_profile = profile
        return view_func(request, *args, **kwargs)

    return wrapped


def checker_required(view_func):
    """Require the portal user to hold the Checker role."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        profile = getattr(request, "portal_profile", None)
        if profile is None or profile.role != "CHECKER":
            raise Http404
        return view_func(request, *args, **kwargs)

    return portal_required(wrapped)
