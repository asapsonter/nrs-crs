"""Inbound taxpayer matching and risk profiling (BPMN IB-08/IB-09).

Inbound partner records concern Nigerian-resident account holders. Before the
data is disseminated to tax officials it is matched to the domestic taxpayer
register on TIN and, failing that, on identity (name), then risk-profiled by
account balance so each match lands on the right review track. Unmatched
records are stored and matching is reattempted once identity is resolved.
"""
from __future__ import annotations

from decimal import Decimal

from core import config
from exchange.models import InboundRecord, Taxpayer


def _normalise(value: str) -> str:
    return " ".join(value.strip().upper().split())


def risk_for_balance(balance: Decimal) -> str:
    """Route a matched record to a review track by account balance."""
    if balance >= config.RISK_ENHANCED_THRESHOLD:
        return InboundRecord.RiskRating.ENHANCED
    if balance >= config.RISK_SPECIFIC_THRESHOLD:
        return InboundRecord.RiskRating.SPECIFIC
    return InboundRecord.RiskRating.GENERAL


def _find_taxpayer(record: InboundRecord) -> tuple[Taxpayer | None, str]:
    """Match a record to a taxpayer, returning (taxpayer, basis)."""
    tin = record.ng_tin.strip()
    if tin:
        taxpayer = Taxpayer.objects.filter(tin__iexact=tin, is_active=True).first()
        if taxpayer:
            return taxpayer, "TIN"
    name = _normalise(record.holder_name)
    if name:
        for candidate in Taxpayer.objects.filter(is_active=True):
            if _normalise(candidate.name) == name:
                return candidate, "IDENTITY"
    return None, ""


def match_inbound_file(inbound) -> dict:
    """Match every clean record on a file and risk-profile the matches.

    Records already carrying an unresolved error are skipped. Returns a
    summary count for the caller to surface. Safe to rerun: an unmatched
    record is retried and its attempt counter advanced.
    """
    matched = 0
    unmatched = 0
    for record in inbound.records.all():
        if record.has_error and not record.corrected:
            continue
        if record.match_status == InboundRecord.MatchStatus.MATCHED:
            matched += 1
            continue
        taxpayer, basis = _find_taxpayer(record)
        record.match_attempts += 1
        if taxpayer is not None:
            record.match_status = InboundRecord.MatchStatus.MATCHED
            record.match_basis = basis
            record.matched_taxpayer = taxpayer
            record.risk_rating = risk_for_balance(record.balance)
            matched += 1
        else:
            record.match_status = InboundRecord.MatchStatus.UNMATCHED
            record.match_basis = ""
            record.matched_taxpayer = None
            record.risk_rating = ""
            unmatched += 1
        record.save(
            update_fields=[
                "match_status",
                "match_basis",
                "matched_taxpayer",
                "risk_rating",
                "match_attempts",
            ]
        )
    return {"matched": matched, "unmatched": unmatched}


def resolve_unmatched_identities(inbound) -> int:
    """Demo control: register the unmatched holders as taxpayers.

    Stands in for an analyst resolving identity from supporting data so the
    reattempt loop (IB-09) can close. Returns the number of holders added to
    the register.
    """
    added = 0
    for record in inbound.records.filter(match_status=InboundRecord.MatchStatus.UNMATCHED):
        tin = record.ng_tin.strip() or f"NGTIN{record.pk:07d}"
        if not record.ng_tin.strip():
            record.ng_tin = tin
            record.save(update_fields=["ng_tin"])
        if not Taxpayer.objects.filter(tin__iexact=tin).exists():
            Taxpayer.objects.create(
                tin=tin,
                name=record.holder_name,
                tax_office="Identity resolved from inbound record",
            )
            added += 1
    return added
