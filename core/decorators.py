"""Access control decorators for the two application surfaces.

Cross-surface requests return 404 rather than 403 so the existence of the
other surface's resources is never confirmed to the wrong audience.
"""
from __future__ import annotations

from functools import wraps

from django.http import Http404
from django.shortcuts import redirect

from core.access import ANY_INTERNAL, capabilities_for
from core.models import Roles


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
    """Require any credentialled internal user (dashboard-level access).

    The Super Admin administers access and does not process casework, so it is
    excluded from the operational surfaces.
    """

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if getattr(request, "surface", "") != "backoffice":
            raise Http404
        if not request.user.is_authenticated:
            return redirect("/backoffice/login/")
        credential = getattr(request, "credential", None)
        if credential is None:
            raise Http404
        if not (set(credential.role_list) & ANY_INTERNAL):
            raise Http404
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
            credential = getattr(request, "credential", None)
            if credential is None:
                raise Http404
            caps = capabilities_for(credential.role_list)
            if not (set(needed) & caps):
                raise Http404
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
