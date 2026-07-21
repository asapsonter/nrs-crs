"""Public demo helper pages: landing, the email outbox, and CSRF recovery."""
from __future__ import annotations

from django.shortcuts import render

from core.models import DemoOutboxEmail


def landing(request):
    """Demo landing page linking to both surfaces."""
    return render(request, "landing.html")


def demo_outbox(request):
    """All captured outbound email, for presentation without a mail server."""
    return render(request, "demo_outbox.html", {"emails": DemoOutboxEmail.objects.all()[:100]})


def csrf_failure(request, reason: str = ""):
    """Friendly recovery page for a stale CSRF token.

    The security token is refreshed on sign in, so a form rendered before a
    login (a back-button page, a second tab, or one left open a while) fails
    verification. Reloading the page fetches a fresh token. This turns the raw
    403 into a themed page with a one-click way back to the form.
    """
    path = request.path
    if path.startswith("/backoffice"):
        surface, css, home = "backoffice", "css/backoffice.css", "/backoffice/"
    elif path.startswith("/portal"):
        surface, css, home = "portal", "css/portal.css", "/portal/"
    else:
        surface, css, home = "public", "css/portal.css", "/"

    referer = request.META.get("HTTP_REFERER", "")
    host = request.get_host()
    back = referer if referer and ("//" + host + "/") in (referer + "/") else home

    return render(
        request,
        "csrf_failure.html",
        {"surface": surface, "css": css, "back": back, "home": home},
        status=403,
    )
