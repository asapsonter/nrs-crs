"""Analyst reports: read-only dashboards and exchange statistics."""
from __future__ import annotations

from django.db.models import Count
from django.shortcuts import render

from core import access, config
from core.decorators import require_cap
from exchange.models import ExchangePackage, InboundFile, InboundRecord
from exchange.services import days_to_exchange_deadline, exchange_deadline
from portal.models import AccountReport, Filing, ReportingFI


def _bar_chart(rows: list[tuple[str, int]], width: int = 720) -> dict:
    """Prepare inline SVG bar chart geometry: label, value, bar width."""
    maximum = max((value for _, value in rows), default=0) or 1
    bar_area = width - 260
    return {
        "width": width,
        "height": 26 * len(rows) + 10,
        "bars": [
            {
                "label": label,
                "value": value,
                "y": 26 * index + 4,
                "w": max(2, int(bar_area * value / maximum)) if value else 0,
            }
            for index, (label, value) in enumerate(rows)
        ],
    }


@require_cap(access.VIEW_REPORTS)
def reports(request):
    year = config.CURRENT_REPORTING_YEAR

    funnel_order = [
        Filing.Status.DRAFT,
        Filing.Status.PENDING_CHECKER,
        Filing.Status.SUBMITTED,
        Filing.Status.UNDER_VALIDATION,
        Filing.Status.RETURNED,
        Filing.Status.ACCEPTED,
        Filing.Status.IN_EXCHANGE,
    ]
    funnel_counts = dict(
        Filing.objects.values_list("status").annotate(total=Count("id")).values_list("status", "total")
    )
    funnel = [(Filing.Status(status).label, funnel_counts.get(status, 0)) for status in funnel_order]

    outgoing = list(
        AccountReport.objects.filter(packages__isnull=False, superseded=False)
        .values_list("residence_country")
        .annotate(total=Count("id", distinct=True))
        .order_by("-total")
        .values_list("residence_country", "total")
    )
    incoming = list(
        InboundRecord.objects.filter(file__status=InboundFile.Status.DISSEMINATED)
        .values_list("file__jurisdiction__code")
        .annotate(total=Count("id"))
        .order_by("-total")
        .values_list("file__jurisdiction__code", "total")
    )

    correction_rates = []
    for rfi in ReportingFI.objects.filter(status__in=["ACTIVE", "APPROVED", "SUSPENDED"]):
        total = AccountReport.objects.filter(filing__rfi=rfi).count()
        corrected = AccountReport.objects.filter(filing__rfi=rfi, doc_type_indic="OECD2").count()
        if total:
            correction_rates.append(
                {"rfi": rfi, "total": total, "corrected": corrected, "rate": round(100 * corrected / total, 1)}
            )

    enrolment = dict(
        ReportingFI.objects.values_list("status").annotate(total=Count("id")).values_list("status", "total")
    )
    enrolment_rows = [
        (ReportingFI.Status(status).label, enrolment.get(status, 0))
        for status in ReportingFI.Status.values
        if enrolment.get(status, 0)
    ]

    packages = ExchangePackage.objects.select_related("jurisdiction")
    timeliness = {
        "deadline": exchange_deadline(year),
        "days_remaining": days_to_exchange_deadline(year),
        "transmitted": packages.exclude(transmitted_at=None).count(),
        "outstanding": packages.filter(transmitted_at=None).count(),
        "archived": packages.filter(status=ExchangePackage.Status.ARCHIVED).count(),
    }

    return render(
        request,
        "backoffice/reports.html",
        {
            "year": year,
            "funnel": funnel,
            "funnel_chart": _bar_chart(funnel),
            "outgoing": outgoing,
            "outgoing_chart": _bar_chart([(code, total) for code, total in outgoing]),
            "incoming": incoming,
            "correction_rates": correction_rates,
            "enrolment_rows": enrolment_rows,
            "timeliness": timeliness,
            "nav": "reports",
        },
    )
