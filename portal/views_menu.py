"""Portal menu surfaces: drafts, submission, documents, profile, my details, help."""
from __future__ import annotations

from django.shortcuts import render

from core.decorators import portal_required
from portal.models import Filing
from portal.views_filing import _deadline_context

# Filing statuses that still need the institution's own action.
DRAFT_STATUSES = [
    Filing.Status.DRAFT,
    Filing.Status.PENDING_CHECKER,
    Filing.Status.RETURNED,
]


@portal_required
def filings_drafts(request):
    """Filings still in the institution's hands: drafts, pending checker, returned."""
    profile = request.portal_profile
    drafts = profile.rfi.filings.filter(status__in=DRAFT_STATUSES)
    return render(
        request,
        "portal/filings_drafts.html",
        {"profile": profile, "filings": drafts, "nav": "drafts", **_deadline_context()},
    )


@portal_required
def submit(request):
    """Start a new submission: XML upload, manual entry, or a nil return."""
    profile = request.portal_profile
    return render(
        request,
        "portal/submit.html",
        {
            "profile": profile,
            "is_maker": profile.role == "MAKER",
            "is_checker": profile.role == "CHECKER",
            "nav": "submit",
            **_deadline_context(),
        },
    )


@portal_required
def documents(request):
    """Every document the institution holds: enrolment files and filing uploads."""
    profile = request.portal_profile
    rfi = profile.rfi
    docs = []
    if rfi.ceo_letter:
        docs.append({"name": "CEO letter clearance form", "category": "Enrolment", "file": rfi.ceo_letter})
    if rfi.id_document:
        docs.append({"name": "Primary User identification", "category": "Enrolment", "file": rfi.id_document})
    uploads = rfi.filings.exclude(uploaded_filename="").order_by("-created_at")
    return render(
        request,
        "portal/documents.html",
        {"profile": profile, "rfi": rfi, "docs": docs, "uploads": uploads, "nav": "documents"},
    )


@portal_required
def entity_profile(request):
    """The Reporting Entity's enrolment profile."""
    profile = request.portal_profile
    return render(
        request,
        "portal/entity_profile.html",
        {"profile": profile, "rfi": profile.rfi, "nav": "profile"},
    )


@portal_required
def my_details(request):
    """The signed-in user's own account details."""
    profile = request.portal_profile
    return render(
        request,
        "portal/my_details.html",
        {"profile": profile, "nav": "me"},
    )


@portal_required
def help_page(request):
    """Guidance for using the Reporting Entity Portal."""
    return render(request, "portal/help.html", {"profile": request.portal_profile, "nav": "help"})
