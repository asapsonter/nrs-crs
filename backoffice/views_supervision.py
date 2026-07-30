"""FI Management and Compliance monitoring workspaces.

The two authority-side surfaces of the Vizor AEOI structure not covered by
the registration and returns queues: a register of the enrolled Financial
Institution community, and a per-institution compliance monitor for the
reporting cycle (filed, nil, outstanding, overdue with penalty exposure).
"""
from __future__ import annotations

from django.db.models import Count
from django.shortcuts import render

from core import access, config
from core.decorators import require_cap
from exchange.services import days_to_domestic_deadline, domestic_deadline, late_filing_penalty
from portal.models import Filing, ReportingFI

# Filing statuses that count as filed with the NRS.
_FILED_STATUSES = [
    Filing.Status.SUBMITTED,
    Filing.Status.UNDER_VALIDATION,
    Filing.Status.ACCEPTED,
    Filing.Status.IN_EXCHANGE,
]


@require_cap(access.VIEW_REGISTRATION)
def institutions(request):
    """FI Management: the register of enrolled Financial Institutions."""
    rfis = (
        ReportingFI.objects.annotate(
            user_count=Count("portal_users", distinct=True),
            filing_count=Count("filings", distinct=True),
        )
        .order_by("legal_name")
    )
    status_filter = request.GET.get("status", "")
    if status_filter in ReportingFI.Status.values:
        rfis = rfis.filter(status=status_filter)
    counts = dict(
        ReportingFI.objects.values_list("status").annotate(total=Count("id")).values_list("status", "total")
    )
    return render(
        request,
        "backoffice/institutions.html",
        {
            "rfis": rfis,
            "status_filter": status_filter,
            "status_choices": ReportingFI.Status.choices,
            "status_counts": counts,
            "total": ReportingFI.objects.count(),
            "nav": "institutions",
        },
    )


@require_cap(access.VIEW_RETURNS)
def compliance(request):
    """Compliance monitor: each operational FI's filing position for the year.

    Mirrors the Vizor AEOI compliance view of the FI community: who has filed
    a data return, who has filed nil, who is still in progress, and who has
    filed nothing, with penalty exposure once the domestic deadline passes.
    """
    year = config.CURRENT_REPORTING_YEAR
    deadline = domestic_deadline(year)
    days_left = days_to_domestic_deadline(year)
    overdue = days_left < 0
    penalty = late_filing_penalty(deadline)

    rows = []
    summary = {"filed": 0, "nil": 0, "in_progress": 0, "not_filed": 0}
    operational = ReportingFI.objects.filter(
        status__in=[ReportingFI.Status.APPROVED, ReportingFI.Status.ACTIVE]
    ).order_by("legal_name")
    for rfi in operational:
        filings = list(rfi.filings.filter(reporting_year=year))
        filed = [f for f in filings if f.status in _FILED_STATUSES]
        nil_filed = any(f.kind == Filing.Kind.NIL for f in filed)
        data_filed = any(f.kind != Filing.Kind.NIL and f.is_crs_data for f in filed)
        drafting = any(
            f.status in (Filing.Status.DRAFT, Filing.Status.PENDING_CHECKER, Filing.Status.RETURNED)
            for f in filings
        )
        if data_filed:
            position, key = "Data return filed", "filed"
        elif nil_filed:
            position, key = "Nil return filed", "nil"
        elif drafting:
            position, key = "In progress", "in_progress"
        else:
            position, key = "Not filed", "not_filed"
        summary[key] += 1
        rows.append(
            {
                "rfi": rfi,
                "position": position,
                "key": key,
                "filings": len(filings),
                "records": sum(f.record_count for f in filed if f.is_crs_data),
                "penalty": penalty if overdue and key in ("not_filed", "in_progress") else 0,
            }
        )

    return render(
        request,
        "backoffice/compliance.html",
        {
            "year": year,
            "deadline": deadline,
            "days_left": days_left,
            "overdue_days": abs(days_left) if overdue else 0,
            "overdue": overdue,
            "penalty": penalty,
            "rows": rows,
            "summary": summary,
            "total_operational": len(rows),
            "nav": "compliance",
        },
    )
