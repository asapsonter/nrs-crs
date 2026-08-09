"""Exchange models: partner jurisdictions, CTS packages, status messages."""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils import timezone

from core import config
from core.models import OfficerProfile
from portal.models import AccountReport


class Regime(models.TextChoices):
    """Reporting regime an exchange file belongs to.

    CRS is live today; CARF is declared so the register, status-message and
    correction machinery is regime-aware and reusable, per the OECD design
    where the CARF Status Message schema mirrors the CRS one.
    """

    CRS = "CRS", "CRS"
    CARF = "CARF", "CARF"


class PartnerJurisdiction(models.Model):
    """An activated CRS MCAA exchange relationship."""

    code = models.CharField(max_length=2, unique=True)
    name = models.CharField(max_length=80)
    activated_since = models.DateField()
    key_fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


# Simulated CTS pipeline steps, in order. Presentation theatre; the XML and
# MessageRefID underneath are real.
PIPELINE_STEPS: list[str] = [
    "XML generated",
    "Digitally signed",
    "AES key generated",
    "Payload encrypted",
    "Key wrapped with recipient public key",
    "Metadata file created",
    "Uploaded to CTS inbox",
]


class ExchangePackage(models.Model):
    """A per-jurisdiction, per-year CRS data package sent through the CTS.

    MessageRefID format: sending country + year + receiving country + unique
    id, for example NG2025GB000123. A package rejected at file level is
    resubmitted whole; record errors open a CRS702 correction cycle.
    """

    class Status(models.TextChoices):
        BUILT = "BUILT", "Built"
        TRANSMITTING = "TRANSMITTING", "Transmitting"
        TRANSMITTED = "TRANSMITTED", "Transmitted"
        ACCEPTED = "ACCEPTED", "Accepted"
        FILE_ERROR = "FILE_ERROR", "Rejected, file error"
        RECORD_ERRORS = "RECORD_ERRORS", "Record errors reported"
        ARCHIVED = "ARCHIVED", "Archived"

    jurisdiction = models.ForeignKey(PartnerJurisdiction, on_delete=models.PROTECT, related_name="packages")
    regime = models.CharField(max_length=4, choices=Regime.choices, default=Regime.CRS)
    reporting_year = models.PositiveIntegerField()
    message_ref_id = models.CharField(max_length=40, unique=True)
    # File version within a correction lineage: 1 for the original, incremented
    # on each CRS702 correction so retransmissions stay individually traceable.
    file_version = models.PositiveSmallIntegerField(default=1)
    responsible_officer = models.ForeignKey(
        OfficerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="exchange_packages"
    )
    message_type = models.CharField(
        max_length=8,
        choices=[
            ("CRS701", "CRS701 New data"),
            ("CRS702", "CRS702 Corrections"),
            ("CRS703", "CRS703 Nil return"),
        ],
        default="CRS701",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.BUILT)
    pipeline_step = models.PositiveSmallIntegerField(default=1)
    xml_content = models.TextField(blank=True, default="")
    records = models.ManyToManyField(AccountReport, related_name="packages")
    corrects_package = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="correction_packages"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    transmitted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.message_ref_id

    @property
    def pipeline_complete(self) -> bool:
        return self.pipeline_step >= len(PIPELINE_STEPS)

    def pipeline_display(self) -> list[dict]:
        """Step list with done/current flags for the pipeline template."""
        steps = []
        for index, label in enumerate(PIPELINE_STEPS, start=1):
            steps.append(
                {
                    "label": label,
                    "done": index < self.pipeline_step
                    or (self.pipeline_complete and index <= self.pipeline_step),
                    "current": index == self.pipeline_step and not self.pipeline_complete,
                }
            )
        return steps


class StatusMessage(models.Model):
    """A CRS Status Message, either received on our outgoing package or
    issued by the NRS on a partner's inbound file."""

    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Received from partner"
        OUTBOUND = "OUTBOUND", "Issued to partner"

    class Outcome(models.TextChoices):
        ACCEPTED = "ACCEPTED", "Accepted"
        WARNING = "WARNING", "Accepted with warnings"
        FILE_ERROR = "FILE_ERROR", "File error"
        RECORD_ERROR = "RECORD_ERROR", "Record errors"

    direction = models.CharField(max_length=10, choices=Direction.choices)
    outcome = models.CharField(max_length=14, choices=Outcome.choices)
    package = models.ForeignKey(
        ExchangePackage, null=True, blank=True, on_delete=models.CASCADE, related_name="status_messages"
    )
    inbound_file = models.ForeignKey(
        "InboundFile", null=True, blank=True, on_delete=models.CASCADE, related_name="status_messages"
    )
    error_code = models.CharField(max_length=10, blank=True, default="")
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_direction_display()}: {self.get_outcome_display()}"


class RecordError(models.Model):
    """A record-level error named in a Status Message, worked in the
    correction queue until a CRS702 correction resolves it."""

    status_message = models.ForeignKey(StatusMessage, on_delete=models.CASCADE, related_name="record_errors")
    doc_ref_id = models.CharField(max_length=80)
    code = models.CharField(max_length=10)
    detail = models.CharField(max_length=200)
    resolved = models.BooleanField(default=False)
    correction_record = models.ForeignKey(
        AccountReport, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolves_errors"
    )

    class Meta:
        ordering = ["doc_ref_id"]

    def __str__(self) -> str:
        return f"{self.doc_ref_id}: {self.code}"


class Taxpayer(models.Model):
    """A Nigerian taxpayer on the domestic register.

    Inbound partner records are matched against this register on TIN and
    identity to route foreign account data to the right taxpayer for
    compliance use. A minimal register that stands in for the wider NRS
    taxpayer database in the demo.
    """

    tin = models.CharField("Nigerian TIN", max_length=40, unique=True)
    name = models.CharField(max_length=200)
    tax_office = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.tin})"


class InboundFile(models.Model):
    """A CRS data file received from a partner jurisdiction.

    Processing runs decrypt plus schema check (file level), then record-level
    checks. A clean file is accepted, approved for domestic use, matched to
    Nigerian taxpayers, and disseminated to the Tax Authority View.
    """

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        FILE_ERROR = "FILE_ERROR", "Rejected, file error"
        RECORD_ERRORS = "RECORD_ERRORS", "Record errors outstanding"
        ACCEPTED = "ACCEPTED", "Accepted"
        APPROVED = "APPROVED", "Approved for domestic use"
        MATCHED = "MATCHED", "Matched to taxpayers"
        DISSEMINATED = "DISSEMINATED", "Disseminated"

    jurisdiction = models.ForeignKey(PartnerJurisdiction, on_delete=models.PROTECT, related_name="inbound_files")
    regime = models.CharField(max_length=4, choices=Regime.choices, default=Regime.CRS)
    reporting_year = models.PositiveIntegerField()
    message_ref_id = models.CharField(max_length=40, unique=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.RECEIVED)
    assigned_reviewer = models.ForeignKey(
        OfficerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="inbound_files_reviewed"
    )
    simulate_file_error = models.CharField(max_length=10, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)
    file_checked_at = models.DateTimeField(null=True, blank=True)
    records_checked_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    matched_at = models.DateTimeField(null=True, blank=True)
    disseminated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return self.message_ref_id


class InboundRecord(models.Model):
    """One account record inside a partner's inbound file, concerning a
    Nigerian-resident account holder."""

    class MatchStatus(models.TextChoices):
        UNMATCHED = "UNMATCHED", "Unmatched"
        MATCHED = "MATCHED", "Matched"

    class RiskRating(models.TextChoices):
        GENERAL = "GENERAL", "General monitoring"
        SPECIFIC = "SPECIFIC", "Specific review"
        ENHANCED = "ENHANCED", "Enhanced review"

    file = models.ForeignKey(InboundFile, on_delete=models.CASCADE, related_name="records")
    doc_ref_id = models.CharField(max_length=80)
    holder_name = models.CharField(max_length=200)
    ng_tin = models.CharField("Nigerian TIN", max_length=40, blank=True, default="")
    account_number = models.CharField(max_length=40)
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="USD")
    has_error = models.BooleanField(default=False)
    error_code = models.CharField(max_length=10, blank=True, default="")
    error_detail = models.CharField(max_length=200, blank=True, default="")
    corrected = models.BooleanField(default=False)

    # Taxpayer matching (IB-08/09): a record is matched to a domestic taxpayer
    # on TIN or identity, then risk-profiled for the appropriate review track.
    match_status = models.CharField(
        max_length=10, choices=MatchStatus.choices, default=MatchStatus.UNMATCHED
    )
    match_basis = models.CharField(max_length=20, blank=True, default="")
    matched_taxpayer = models.ForeignKey(
        Taxpayer, null=True, blank=True, on_delete=models.SET_NULL, related_name="inbound_records"
    )
    risk_rating = models.CharField(max_length=8, choices=RiskRating.choices, blank=True, default="")
    match_attempts = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["doc_ref_id"]

    def __str__(self) -> str:
        return f"{self.doc_ref_id} {self.holder_name}"


class TransmissionCertificate(models.Model):
    """A cryptographic certificate in the CTS certificate inventory.

    Covers the NRS's own signing certificate and each partner jurisdiction's
    public-key certificate. Validity is monitored against a renewal schedule
    so certificates are renewed before they lapse (OR-CTS-006); a certificate
    within the warning window, or already expired, raises a CTS exception
    alert (FR-CTS-010, RR-CTS-003, AC-CTS-006).
    """

    class Owner(models.TextChoices):
        NRS = "NRS", "NRS own signing certificate"
        PARTNER = "PARTNER", "Partner jurisdiction"

    owner = models.CharField(max_length=8, choices=Owner.choices, default=Owner.PARTNER)
    jurisdiction = models.ForeignKey(
        PartnerJurisdiction, null=True, blank=True, on_delete=models.CASCADE, related_name="certificates"
    )
    subject = models.CharField(max_length=120)
    fingerprint = models.CharField(max_length=64)
    valid_from = models.DateField()
    valid_to = models.DateField()
    revoked = models.BooleanField(default=False)

    class Meta:
        ordering = ["valid_to"]

    def __str__(self) -> str:
        return f"{self.subject} (to {self.valid_to})"

    @property
    def days_to_expiry(self) -> int:
        return (self.valid_to - timezone.localdate()).days

    @property
    def is_expired(self) -> bool:
        return self.revoked or self.days_to_expiry < 0

    @property
    def is_expiring(self) -> bool:
        return not self.is_expired and self.days_to_expiry <= config.CERTIFICATE_EXPIRY_WARNING_DAYS

    @property
    def status_label(self) -> str:
        if self.revoked:
            return "Revoked"
        if self.is_expired:
            return "Expired"
        if self.is_expiring:
            return "Expiring"
        return "Valid"
