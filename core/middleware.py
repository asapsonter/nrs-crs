"""Surface separation and credential session enforcement.

Two middlewares:

SurfaceSessionMiddleware issues a different session cookie per surface, each
scoped to its own URL path, so a portal session token is never presented to
the backoffice and vice versa.

CredentialSessionMiddleware enforces the time-boxed access window on every
backoffice request. The server clock is the source of truth; the header
countdown is presentation only.
"""
from __future__ import annotations

import time

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date

from core.models import IssuedCredential

PORTAL_PREFIX = "/portal/"
BACKOFFICE_PREFIX = "/backoffice/"


def surface_for_path(path: str) -> str:
    if path.startswith(BACKOFFICE_PREFIX) or path == "/backoffice":
        return "backoffice"
    if path.startswith(PORTAL_PREFIX) or path == "/portal":
        return "portal"
    return "public"


def _cookie_settings_for(surface: str) -> tuple[str, str]:
    """Return (cookie name, cookie path) for a surface."""
    if surface == "backoffice":
        return settings.BACKOFFICE_SESSION_COOKIE_NAME, "/backoffice"
    if surface == "portal":
        return settings.PORTAL_SESSION_COOKIE_NAME, "/portal"
    return settings.SESSION_COOKIE_NAME, "/"


class SurfaceSessionMiddleware(SessionMiddleware):
    """SessionMiddleware variant with a per-surface cookie name and path."""

    def process_request(self, request):
        surface = surface_for_path(request.path)
        cookie_name, cookie_path = _cookie_settings_for(surface)
        request.surface = surface
        request._session_cookie_name = cookie_name
        request._session_cookie_path = cookie_path
        session_key = request.COOKIES.get(cookie_name)
        request.session = self.SessionStore(session_key)

    def process_response(self, request, response):
        try:
            accessed = request.session.accessed
            modified = request.session.modified
            empty = request.session.is_empty()
        except AttributeError:
            return response
        cookie_name = getattr(request, "_session_cookie_name", settings.SESSION_COOKIE_NAME)
        cookie_path = getattr(request, "_session_cookie_path", settings.SESSION_COOKIE_PATH)
        if cookie_name in request.COOKIES and empty:
            response.delete_cookie(
                cookie_name,
                path=cookie_path,
                domain=settings.SESSION_COOKIE_DOMAIN,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )
            patch_vary_headers(response, ("Cookie",))
        else:
            if accessed:
                patch_vary_headers(response, ("Cookie",))
            if (modified or settings.SESSION_SAVE_EVERY_REQUEST) and not empty:
                if request.session.get_expire_at_browser_close():
                    max_age = None
                    expires = None
                else:
                    max_age = request.session.get_expiry_age()
                    expires = http_date(time.time() + max_age)
                if response.status_code < 500:
                    try:
                        request.session.save()
                    except UpdateError:
                        raise SessionInterrupted(
                            "The request's session was deleted before the request completed."
                        )
                    response.set_cookie(
                        cookie_name,
                        request.session.session_key,
                        max_age=max_age,
                        expires=expires,
                        domain=settings.SESSION_COOKIE_DOMAIN,
                        path=cookie_path,
                        secure=settings.SESSION_COOKIE_SECURE or None,
                        httponly=settings.SESSION_COOKIE_HTTPONLY or None,
                        samesite=settings.SESSION_COOKIE_SAMESITE,
                    )
        return response


# Backoffice paths reachable without an authenticated credential session.
BACKOFFICE_EXEMPT = (
    "/backoffice/login/",
    "/backoffice/access-ended/",
)


class CredentialSessionMiddleware:
    """Enforces the issued-credential access window on every backoffice request.

    On expiry the user is logged out, the credential is marked CONSUMED, and
    an access-ended page is shown. A revoked credential ends its session on
    the next request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(request, "surface", "") != "backoffice":
            return self.get_response(request)
        if request.path in BACKOFFICE_EXEMPT or not request.user.is_authenticated:
            return self.get_response(request)

        # The permanent Super Admin account is not credential-bound.
        if request.user.is_superuser:
            request.credential = None
            return self.get_response(request)

        credential_id = request.session.get("credential_id")
        credential = (
            IssuedCredential.objects.filter(pk=credential_id).select_related("officer").first()
            if credential_id
            else None
        )
        if credential is None:
            logout(request)
            return redirect("/backoffice/login/")

        if credential.status == IssuedCredential.Status.REVOKED:
            logout(request)
            return render(
                request,
                "backoffice/access_ended.html",
                {"reason": "Your credential has been revoked by the administrator."},
                status=403,
            )

        if credential.status == IssuedCredential.Status.ACTIVE and credential.session_expires_at:
            if timezone.now() >= credential.session_expires_at:
                credential.consume("Session window elapsed. Access ended by the platform.")
                logout(request)
                return render(
                    request,
                    "backoffice/access_ended.html",
                    {"reason": "Your access window has ended. Contact the administrator for a new credential."},
                    status=403,
                )
        elif credential.status != IssuedCredential.Status.ACTIVE:
            # CONSUMED or EXPIRED while a stale session cookie survives.
            logout(request)
            return render(
                request,
                "backoffice/access_ended.html",
                {"reason": "Your access window has ended. Contact the administrator for a new credential."},
                status=403,
            )

        request.credential = credential
        return self.get_response(request)
