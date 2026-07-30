"""Portal menu surfaces: drafts, submission, documents, profile, my details, help."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render

from core.decorators import portal_required
from portal.models import Filing
from portal.views_filing import _deadline_context

# Filing statuses that still need the institution's own action.
DRAFT_STATUSES = [
    Filing.Status.DRAFT,
    Filing.Status.PENDING_CHECKER,
    Filing.Status.RETURNED,
]

# Filing statuses that mean the return has been submitted to the NRS.
SUBMITTED_STATUSES = [
    Filing.Status.SUBMITTED,
    Filing.Status.UNDER_VALIDATION,
    Filing.Status.ACCEPTED,
    Filing.Status.IN_EXCHANGE,
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
    """Start a new submission and see filings already submitted to the NRS."""
    profile = request.portal_profile
    submitted = profile.rfi.filings.filter(status__in=SUBMITTED_STATUSES).order_by("-submitted_at", "-created_at")
    return render(
        request,
        "portal/submit.html",
        {
            "profile": profile,
            "is_maker": profile.role == "MAKER",
            "is_checker": profile.role == "CHECKER",
            "submitted_filings": submitted,
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
        docs.append({"name": "Letter of Authorisation", "category": "Enrolment", "file": rfi.ceo_letter})
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
    """The Reporting Entity's enrolment profile.

    Identity fields (legal name, TIN, enrolment type) are fixed at enrolment
    and shown read only. The institution may update its own contact details:
    email, telephone, and registered address.
    """
    profile = request.portal_profile
    rfi = profile.rfi
    if request.method == "POST":
        rfi.email = request.POST.get("email", "").strip()
        rfi.phone_cc = request.POST.get("phone_cc", rfi.phone_cc).strip() or rfi.phone_cc
        rfi.phone = request.POST.get("phone", "").strip()
        rfi.street = request.POST.get("street", "").strip()
        rfi.city = request.POST.get("city", "").strip()
        rfi.state_province = request.POST.get("state_province", "").strip()
        rfi.post_code = request.POST.get("post_code", "").strip()
        rfi.save(update_fields=["email", "phone_cc", "phone", "street", "city", "state_province", "post_code"])
        from core.models import AuditLog

        AuditLog.record(
            actor_name=profile.display_name,
            surface="portal",
            action="ENTITY_CONTACT_UPDATED",
            target=rfi.reference,
            detail=f"Reporting Entity contact details updated for {rfi.legal_name}.",
        )
        messages.success(request, "Reporting Entity contact details updated.")
        return redirect("/portal/profile/")
    return render(
        request,
        "portal/entity_profile.html",
        {"profile": profile, "rfi": rfi, "edit": request.GET.get("edit") == "1", "nav": "profile"},
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
