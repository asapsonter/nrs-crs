"""Enrolments workspace, in the Vizor AEOI structure.

Financial institutions submit the Institution & Primary User Enrolment form.
The authority reviews the enrolment and either approves it (the Primary User
receives credentials by email) or declines it with a reason. There is no
separate recorded-assessment step; that flow is retired to match the Vizor
enrolment structure (see `review` below, kept commented for reference).
"""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core import access
from core.decorators import require_cap
from core.models import AuditLog
from portal.models import PortalUser, ReportingFI
from portal.services import provision_portal_user, send_pu_welcome_email, send_rejection_email


@require_cap(access.VIEW_REGISTRATION)
def queue(request):
    """Enrolments: requests awaiting approval, and the decided register."""
    pending = ReportingFI.objects.filter(
        status__in=[ReportingFI.Status.SUBMITTED, ReportingFI.Status.UNDER_REVIEW]
    )
    decided = ReportingFI.objects.filter(
        status__in=[
            ReportingFI.Status.APPROVED,
            ReportingFI.Status.ACTIVE,
            ReportingFI.Status.REJECTED,
            ReportingFI.Status.SUSPENDED,
            ReportingFI.Status.DEACTIVATED,
        ]
    )
    return render(
        request,
        "backoffice/registration_queue.html",
        {
            "pending": pending,
            "decided": decided,
            "nav": "registration",
        },
    )


@require_cap(access.VIEW_REGISTRATION)
def detail(request, rfi_id: int):
    """Enrolment detail: the Institution & Primary User Enrolment as submitted,
    with Approve Enrolment / Decline Enrolment for authorised officers."""
    rfi = get_object_or_404(ReportingFI, pk=rfi_id)
    caps = request.caps
    can_authorise = access.DECIDE_REGISTRATION in caps
    # An enrolment is open for approval while submitted (or in the legacy
    # under-review state from the retired assessment flow).
    awaiting_decision = rfi.status in (
        ReportingFI.Status.SUBMITTED,
        ReportingFI.Status.UNDER_REVIEW,
    )
    can_decide = can_authorise and awaiting_decision
    can_manage_standing = can_authorise and rfi.status in (
        ReportingFI.Status.ACTIVE,
        ReportingFI.Status.APPROVED,
        ReportingFI.Status.SUSPENDED,
    )
    # A read-only viewer sees why the decision is not theirs to take.
    decision_block = "not_authoriser" if awaiting_decision and not can_authorise else ""
    return render(
        request,
        "backoffice/registration_detail.html",
        {
            "rfi": rfi,
            "can_decide": can_decide,
            "can_manage_standing": can_manage_standing,
            "awaiting_decision": awaiting_decision,
            "decision_block": decision_block,
            "nav": "registration",
        },
    )


# Retired: the recorded-assessment step (checklist, notes, recommendation) is
# not part of the Vizor AEOI enrolment structure, where the authority reviews
# the submitted enrolment form and approves or declines it directly. Kept
# commented for reference should a formal assessment stage be reinstated.
#
# @require_cap(access.ACT_REGISTRATION)
# def review(request, rfi_id: int):
#     """Assessment: four checks, notes, and a recommendation."""
#     rfi = get_object_or_404(
#         ReportingFI,
#         pk=rfi_id,
#         status__in=[ReportingFI.Status.SUBMITTED, ReportingFI.Status.UNDER_REVIEW],
#     )
#     if request.method != "POST":
#         return redirect(f"/backoffice/registration/{rfi.pk}/")
#     before = rfi.status
#     rfi.check_tin = bool(request.POST.get("check_tin"))
#     rfi.check_ceo_letter = bool(request.POST.get("check_ceo_letter"))
#     rfi.check_licence = bool(request.POST.get("check_licence"))
#     rfi.check_classification = bool(request.POST.get("check_classification"))
#     rfi.review_notes = request.POST.get("review_notes", "").strip()
#     recommendation = request.POST.get("recommendation", "")
#     if recommendation not in ReportingFI.Recommendation.values:
#         messages.error(request, "Select a recommendation.")
#         return redirect(f"/backoffice/registration/{rfi.pk}/")
#     rfi.recommendation = recommendation
#     rfi.reviewer = request.credential.officer
#     rfi.reviewed_at = timezone.now()
#     rfi.status = ReportingFI.Status.UNDER_REVIEW
#     rfi.save()
#     rfi.record_status_event(f"Assessment recorded. Decision proposed: {rfi.get_recommendation_display()}.")
#     messages.success(request, "Assessment recorded. You can now accept or reject the application.")
#     return redirect(f"/backoffice/registration/{rfi.pk}/")


@require_cap(access.DECIDE_REGISTRATION)
def decide(request, rfi_id: int):
    """Approve or decline an enrolment, per the Vizor enrolment workflow."""
    rfi = get_object_or_404(
        ReportingFI,
        pk=rfi_id,
        status__in=[ReportingFI.Status.SUBMITTED, ReportingFI.Status.UNDER_REVIEW],
    )
    if request.method != "POST":
        return redirect(f"/backoffice/registration/{rfi.pk}/")
    officer = request.credential.officer
    decision = request.POST.get("decision", "")
    # "reject" is accepted as a legacy alias for "decline".
    if decision == "reject":
        decision = "decline"
    before = rfi.status
    if decision == "approve":
        rfi.status = ReportingFI.Status.APPROVED
        rfi.decided_by = officer
        rfi.decided_at = timezone.now()
        rfi.save()
        rfi.record_status_event("Enrolment approved by the NRS.")
        profile, temp_password = provision_portal_user(
            rfi,
            name=rfi.pu_name,
            email=rfi.pu_email,
            designation=rfi.pu_designation,
            role=PortalUser.Role.CHECKER,
            is_primary=True,
        )
        send_pu_welcome_email(rfi, temp_password)
        AuditLog.record(
            actor_name=officer.name,
            actor_role=request.credential.role_display,
            credential=request.credential,
            action="ENROLMENT_APPROVED",
            target=rfi.reference,
            before_state=before,
            after_state=rfi.status,
            detail=f"Enrolment approved. Primary User account issued to {rfi.pu_email}.",
        )
        messages.success(request, f"{rfi.legal_name} enrolment approved. The Primary User has been emailed their credentials.")
    elif decision == "decline":
        reason = request.POST.get("rejection_reason", "").strip()
        if not reason:
            messages.error(request, "Declining an enrolment requires a reason; it is sent to the applicant.")
            return redirect(f"/backoffice/registration/{rfi.pk}/")
        rfi.status = ReportingFI.Status.REJECTED
        rfi.rejection_reason = reason
        rfi.decided_by = officer
        rfi.decided_at = timezone.now()
        rfi.save()
        rfi.record_status_event(f"Enrolment declined. Reason: {reason}")
        send_rejection_email(rfi)
        AuditLog.record(
            actor_name=officer.name,
            actor_role=request.credential.role_display,
            credential=request.credential,
            action="ENROLMENT_DECLINED",
            target=rfi.reference,
            before_state=before,
            after_state=rfi.status,
            detail=f"Enrolment declined. Reason: {reason}",
        )
        messages.success(request, f"{rfi.legal_name} enrolment declined. The applicant has been notified.")
    return redirect("/backoffice/registration/")


@require_cap(access.DECIDE_REGISTRATION)
def standing(request, rfi_id: int):
    """Suspend, reinstate, or deactivate an operational RFI."""
    rfi = get_object_or_404(ReportingFI, pk=rfi_id)
    if request.method != "POST":
        return redirect(f"/backoffice/registration/{rfi.pk}/")
    action = request.POST.get("action", "")
    before = rfi.status
    transitions = {
        "suspend": (
            [ReportingFI.Status.ACTIVE, ReportingFI.Status.APPROVED],
            ReportingFI.Status.SUSPENDED,
        ),
        "reinstate": ([ReportingFI.Status.SUSPENDED], ReportingFI.Status.ACTIVE),
        "deactivate": (
            [ReportingFI.Status.ACTIVE, ReportingFI.Status.APPROVED, ReportingFI.Status.SUSPENDED],
            ReportingFI.Status.DEACTIVATED,
        ),
    }
    if action not in transitions or rfi.status not in transitions[action][0]:
        messages.error(request, "That standing change is not available from the current state.")
        return redirect(f"/backoffice/registration/{rfi.pk}/")
    rfi.status = transitions[action][1]
    rfi.save(update_fields=["status"])
    rfi.record_status_event(f"Institution standing changed: {action}.")
    AuditLog.record(
        actor_name=request.credential.officer.name,
        actor_role=request.credential.role_display,
        credential=request.credential,
        action=f"RFI_{action.upper()}",
        target=rfi.reference,
        before_state=before,
        after_state=rfi.status,
        detail=f"Institution standing changed: {action}.",
    )
    messages.success(request, f"{rfi.legal_name}: {before} changed to {rfi.status}.")
    return redirect(f"/backoffice/registration/{rfi.pk}/")
