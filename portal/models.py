"""Reporting Financial Institution models: enrolment, portal users, filings."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from core import config
from core.models import OfficerProfile


class ReportingFI(models.Model):
    """An RFI and its enrolment application.

    The enrolment state machine follows competent authority practice:
    Draft > Submitted > Under Review > Approved or Rejected > Active >
    Suspended or Deactivated. Approval is four-eyes: a Registration Officer
    reviews and recommends, a Registration Supervisor decides.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        UNDER_REVIEW = "UNDER_REVIEW", "Under Review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        DEACTIVATED = "DEACTIVATED", "Deactivated"

    class Recommendation(models.TextChoices):
        APPROVE = "APPROVE", "Approval"
        REJECT = "REJECT", "Rejection"

    reference = models.CharField(max_length=24, unique=True)
    enrolment_type = models.CharField(
        max_length=20, choices=config.ENROLMENT_TYPES, default="FINANCIAL_ENTITY"
    )
    legal_name = models.CharField(max_length=200)
    tin = models.CharField(max_length=20)
    category = models.CharField(max_length=30, choices=config.FI_CATEGORIES)

    # Registered office address, captured as structured components.
    street = models.CharField("Street", max_length=200, blank=True, default="")
    city = models.CharField("City or town", max_length=100, blank=True, default="")
    state_province = models.CharField("State or province", max_length=100, blank=True, default="")
    post_code = models.CharField("Post code", max_length=20, blank=True, default="")

    email = models.EmailField("Financial Institution email", blank=True, default="")
    phone_cc = models.CharField("Phone country code", max_length=6, blank=True, default="+234")
    phone = models.CharField("Financial Institution phone", max_length=30, blank=True, default="")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.SUBMITTED)

    # Primary User. Name is captured as surname and first name. For a
    # stakeholder or individual enrolment this is the natural person enrolling.
    pu_surname = models.CharField("Primary User surname", max_length=80, blank=True, default="")
    pu_first_name = models.CharField("Primary User first name", max_length=80, blank=True, default="")
    pu_dob = models.DateField("Date of birth", null=True, blank=True)
    pu_designation = models.CharField("Primary User position", max_length=120)
    pu_email = models.EmailField("Primary User email")
    pu_phone_cc = models.CharField("Primary User phone country code", max_length=6, blank=True, default="+234")
    pu_phone = models.CharField("Primary User phone", max_length=30)
    id_document = models.FileField("Primary User identification", upload_to="id_documents/", blank=True, null=True)
    ceo_letter = models.FileField(upload_to="ceo_letters/", blank=True, null=True)

    submitted_at = models.DateTimeField(auto_now_add=True)

    # Four-eyes review trail.
    reviewer = models.ForeignKey(
        OfficerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_enrolments"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    check_tin = models.BooleanField("TIN verified against the taxpayer register", default=False)
    check_ceo_letter = models.BooleanField("CEO letter introducing the PU sighted", default=False)
    check_licence = models.BooleanField("Regulatory licence confirmed", default=False)
    check_classification = models.BooleanField("CRS classification confirmed", default=False)
    review_notes = models.TextField(blank=True, default="")
    recommendation = models.CharField(max_length=10, choices=Recommendation.choices, blank=True, default="")

    decided_by = models.ForeignKey(
        OfficerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="decided_enrolments"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-submitted_at"]
        verbose_name = "Reporting Financial Institution"

    def __str__(self) -> str:
        return f"{self.reference} {self.legal_name}"

    @property
    def is_operational(self) -> bool:
        """Whether the RFI may sign in and file."""
        return self.status in (self.Status.APPROVED, self.Status.ACTIVE)

    @property
    def pu_name(self) -> str:
        """Primary User full name, first name then surname."""
        return f"{self.pu_first_name} {self.pu_surname}".strip()

    @property
    def full_phone(self) -> str:
        return f"{self.phone_cc} {self.phone}".strip()

    @property
    def pu_full_phone(self) -> str:
        return f"{self.pu_phone_cc} {self.pu_phone}".strip()

    @property
    def full_address(self) -> str:
        parts = [self.street, self.city, self.state_province, self.post_code]
        return ", ".join(part for part in parts if part)

    def record_status_event(self, note: str = "", at=None) -> "EnrolmentStatusEvent":
        """Log the current status as a dated event in the enrolment history."""
        return EnrolmentStatusEvent.objects.create(
            rfi=self, status=self.status, note=note, changed_at=at or timezone.now()
        )


class EnrolmentStatusEvent(models.Model):
    """A dated record of one enrolment status change.

    The application's status history is the ordered list of these events, so
    an applicant can see when the status changed to each value.
    """

    rfi = models.ForeignKey(ReportingFI, on_delete=models.CASCADE, related_name="status_events")
    status = models.CharField(max_length=15, choices=ReportingFI.Status.choices)
    changed_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["changed_at", "id"]

    def __str__(self) -> str:
        return f"{self.rfi.reference} -> {self.status} @ {self.changed_at:%Y-%m-%d}"

    @property
    def status_label(self) -> str:
        return ReportingFI.Status(self.status).label


class PortalUser(models.Model):
    """A portal account within an approved RFI, holding Maker or Checker duty.

    The Primary User is the first Checker. Makers prepare filings; Checkers
    review and submit them. Maker registrations require Checker approval.
    """

    class Role(models.TextChoices):
        MAKER = "MAKER", "Maker"
        CHECKER = "CHECKER", "Checker"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending Checker approval"
        ACTIVE = "ACTIVE", "Active"
        DISABLED = "DISABLED", "Disabled"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="portal_profile")
    rfi = models.ForeignKey(ReportingFI, on_delete=models.CASCADE, related_name="portal_users")
    display_name = models.CharField(max_length=120)
    designation = models.CharField(max_length=120, blank=True, default="")
    role = models.CharField(max_length=10, choices=Role.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    is_primary_user = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=True)
    failed_logins = models.PositiveIntegerField(default=0)
    is_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rfi", "display_name"]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.get_role_display()}, {self.rfi.legal_name})"


class Filing(models.Model):
    """A CRS filing by an RFI for a reporting year.

    Message types follow the CRS schema: CRS701 new data, CRS702 corrections,
    CRS703 nil return. The maker and checker split is enforced in views: a
    Maker stages, a Checker submits.
    """

    class Kind(models.TextChoices):
        XML_UPLOAD = "XML_UPLOAD", "CRS XML upload Filing"
        MANUAL = "MANUAL", "CRS Manual Entry Filing"
        NIL = "NIL", "Nil return"
        PU_CHANGE = "PU_CHANGE", "Primary User Change Notice"
        ENTITY_DEACTIVATION = "ENTITY_DEACTIVATION", "Reporting Entity Deactivation"
        ENTITY_INFO_CHANGE = "ENTITY_INFO_CHANGE", "Change of reporting entity information"

    # The three CRS data filing kinds carry account reports and a CRS message
    # type; the remaining kinds are administrative notices.
    CRS_DATA_KINDS = (Kind.XML_UPLOAD, Kind.MANUAL, Kind.NIL)
    NOTICE_KINDS = (Kind.PU_CHANGE, Kind.ENTITY_DEACTIVATION, Kind.ENTITY_INFO_CHANGE)

    class MessageType(models.TextChoices):
        CRS701 = "CRS701", "CRS701 New data"
        CRS702 = "CRS702", "CRS702 Corrections"
        CRS703 = "CRS703", "CRS703 Nil return"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_CHECKER = "PENDING_CHECKER", "Pending Checker"
        SUBMITTED = "SUBMITTED", "Submitted"
        UNDER_VALIDATION = "UNDER_VALIDATION", "Under Validation"
        RETURNED = "RETURNED", "Returned for Correction"
        ACCEPTED = "ACCEPTED", "Accepted"
        IN_EXCHANGE = "IN_EXCHANGE", "Included in Exchange"

    reference = models.CharField(max_length=24, unique=True)
    name = models.CharField("Filing name", max_length=200, blank=True, default="")
    revision = models.PositiveIntegerField(default=1)
    rfi = models.ForeignKey(ReportingFI, on_delete=models.CASCADE, related_name="filings")
    reporting_year = models.PositiveIntegerField(default=config.CURRENT_REPORTING_YEAR)
    period_end_date = models.DateField("Period end date", null=True, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    message_type = models.CharField(max_length=8, choices=MessageType.choices, default=MessageType.CRS701)
    status = models.CharField(max_length=18, choices=Status.choices, default=Status.DRAFT)

    # CRS message header, captured on the General Information form.
    receiving_country = models.CharField("Receiving country", max_length=2, blank=True, default="")
    sending_company_in = models.CharField("Sending Company IN", max_length=40, blank=True, default="")
    message_reference = models.CharField("Message reference", max_length=80, blank=True, default="")

    created_by = models.ForeignKey(PortalUser, null=True, on_delete=models.SET_NULL, related_name="filings_created")
    checker = models.ForeignKey(PortalUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="filings_checked")
    checker_comment = models.TextField(blank=True, default="")
    uploaded_filename = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    return_reason = models.TextField(blank=True, default="")
    accepted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        OfficerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="approved_filings"
    )
    validated_by = models.ForeignKey(
        OfficerProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="validated_filings"
    )

    # Correction lineage: a resubmission after Returned for Correction points
    # back at the filing it corrects.
    corrects = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="corrected_by"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.reference} ({self.rfi.legal_name}, {self.reporting_year})"

    @property
    def record_count(self) -> int:
        return self.account_reports.count()

    @property
    def is_crs_data(self) -> bool:
        """Whether this filing is a CRS data return (carries account reports)."""
        return self.kind in self.CRS_DATA_KINDS

    @property
    def data_status(self) -> str:
        """Whether the filing yet holds reportable data, for the draft list."""
        if self.kind == self.Kind.NIL:
            return "No Data"
        return "Data" if self.record_count else "No Data"

    @property
    def category_label(self) -> str:
        """A short workflow category shown in the draft filing list."""
        return {
            self.Status.DRAFT: "Waiting",
            self.Status.PENDING_CHECKER: "Awaiting Checker",
            self.Status.RETURNED: "Correction",
        }.get(self.status, self.get_status_display())

    @property
    def is_deletable(self) -> bool:
        """Whether the institution may delete this filing.

        Only filings still in the institution's hands can be deleted. Once a
        filing has been submitted to the NRS it is part of the exchange record
        and cannot be removed from the Portal.
        """
        return self.status in (
            self.Status.DRAFT,
            self.Status.PENDING_CHECKER,
            self.Status.RETURNED,
        )


class AccountReport(models.Model):
    """One reportable account within a filing.

    DocRefID uniquely identifies the record across the AEOI ecosystem.
    A correction record carries CorrDocRefID pointing at the DocRefID it
    amends, with DocTypeIndic OECD2 (correction) or OECD3 (deletion).
    """

    class DocTypeIndic(models.TextChoices):
        OECD1 = "OECD1", "OECD1 New data"
        OECD2 = "OECD2", "OECD2 Correction"
        OECD3 = "OECD3", "OECD3 Deletion"

    class AcctHolderType(models.TextChoices):
        """CRS entity account-holder type. Applies to Organisation holders."""
        CRS101 = "CRS101", "CRS101 Passive NFE with controlling person(s)"
        CRS102 = "CRS102", "CRS102 CRS Reportable Person"
        CRS103 = "CRS103", "CRS103 Passive NFE that is a CRS Reportable Person"

    class SelfCertification(models.TextChoices):
        """Due-diligence status of the account holder self-certification."""
        OBTAINED = "OBTAINED", "Obtained and validated"
        CURED = "CURED", "Cured after remediation"
        NOT_OBTAINED = "NOT_OBTAINED", "Not obtained (undocumented)"

    filing = models.ForeignKey(Filing, on_delete=models.CASCADE, related_name="account_reports")
    doc_ref_id = models.CharField(max_length=80, unique=True)
    corr_doc_ref_id = models.CharField(max_length=80, blank=True, default="")
    doc_type_indic = models.CharField(max_length=6, choices=DocTypeIndic.choices, default=DocTypeIndic.OECD1)

    holder_name = models.CharField("Account holder name", max_length=200)
    holder_type = models.CharField(
        max_length=12,
        choices=[("INDIVIDUAL", "Individual"), ("ORGANISATION", "Organisation")],
        default="INDIVIDUAL",
    )
    # Entity account-holder type (Organisation holders); blank for individuals.
    acct_holder_type = models.CharField(
        "Entity account holder type", max_length=6, choices=AcctHolderType.choices, blank=True, default=""
    )
    residence_country = models.CharField("Residence jurisdiction", max_length=2)
    foreign_tin = models.CharField("TIN issued by residence jurisdiction", max_length=40, blank=True, default="")
    tin_unavailable_reason = models.CharField(
        "Reason TIN not reported", max_length=200, blank=True, default=""
    )

    # CRS AccountHolder Address (mandatory in the CRS XML schema).
    holder_address = models.CharField("Account holder address", max_length=300, blank=True, default="")
    address_country = models.CharField("Address country", max_length=2, blank=True, default="")

    # CRS BirthInfo (individual holders).
    birth_date = models.DateField("Date of birth", null=True, blank=True)
    birth_city = models.CharField("City of birth", max_length=120, blank=True, default="")

    # Due-diligence: self-certification status per CRS Regulations 2019.
    self_certification = models.CharField(
        max_length=14, choices=SelfCertification.choices, blank=True, default=""
    )

    account_number = models.CharField(max_length=40)
    balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="NGN")
    dividends = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    interest = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    gross_proceeds = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    other_income = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    opened_date = models.DateField(null=True, blank=True)

    # A record replaced by a correction stays on the filing for lineage but
    # is excluded from validation and packaging.
    superseded = models.BooleanField(default=False)

    class Meta:
        ordering = ["doc_ref_id"]

    def __str__(self) -> str:
        return f"{self.doc_ref_id} {self.holder_name}"

    @property
    def is_undocumented(self) -> bool:
        """An account whose holder self-certification was never obtained."""
        return self.self_certification == self.SelfCertification.NOT_OBTAINED

    @property
    def requires_controlling_persons(self) -> bool:
        """Passive NFEs with controlling persons must report them (CRS101)."""
        return (
            self.holder_type == "ORGANISATION"
            and self.acct_holder_type == self.AcctHolderType.CRS101
        )

    @property
    def address_country_code(self) -> str:
        """Address country, falling back to residence for the CRS Address."""
        return self.address_country or self.residence_country


class ControllingPerson(models.Model):
    """A controlling person of a Passive NFE account holder (CRS).

    Reported inside the AccountReport when the entity holder is a Passive NFE
    with one or more controlling persons (AcctHolderType CRS101).
    """

    class CtrlgPersonType(models.TextChoices):
        CRS801 = "CRS801", "CRS801 Ownership of legal person"
        CRS802 = "CRS802", "CRS802 Other means of control of legal person"
        CRS803 = "CRS803", "CRS803 Senior managing official"
        CRS804 = "CRS804", "CRS804 Trust settlor"
        CRS805 = "CRS805", "CRS805 Trust trustee"
        CRS806 = "CRS806", "CRS806 Trust protector"
        CRS807 = "CRS807", "CRS807 Trust beneficiary"
        CRS808 = "CRS808", "CRS808 Trust other"
        CRS809 = "CRS809", "CRS809 Legal arrangement settlor-equivalent"
        CRS810 = "CRS810", "CRS810 Legal arrangement trustee-equivalent"
        CRS811 = "CRS811", "CRS811 Legal arrangement protector-equivalent"
        CRS812 = "CRS812", "CRS812 Legal arrangement beneficiary-equivalent"
        CRS813 = "CRS813", "CRS813 Legal arrangement other-equivalent"

    account_report = models.ForeignKey(
        AccountReport, on_delete=models.CASCADE, related_name="controlling_persons"
    )
    name = models.CharField("Controlling person name", max_length=200)
    residence_country = models.CharField("Residence jurisdiction", max_length=2)
    tin = models.CharField("TIN", max_length=40, blank=True, default="")
    address = models.CharField("Address", max_length=300, blank=True, default="")
    birth_date = models.DateField("Date of birth", null=True, blank=True)
    ctrlg_person_type = models.CharField(
        "Controlling person type", max_length=6, choices=CtrlgPersonType.choices,
        default=CtrlgPersonType.CRS801,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_ctrlg_person_type_display()})"


class ValidationFinding(models.Model):
    """A data-quality finding raised against a filing during validation.

    File-level findings have no account report; record-level findings point
    at the offending record. A Returns Supervisor may override a warning with
    a recorded justification.
    """

    class Severity(models.TextChoices):
        ERROR = "ERROR", "Error"
        WARNING = "WARNING", "Warning"

    filing = models.ForeignKey(Filing, on_delete=models.CASCADE, related_name="findings")
    account_report = models.ForeignKey(
        AccountReport, null=True, blank=True, on_delete=models.CASCADE, related_name="findings"
    )
    severity = models.CharField(max_length=8, choices=Severity.choices)
    code = models.CharField(max_length=20)
    message = models.CharField(max_length=300)
    overridden = models.BooleanField(default=False)
    override_justification = models.TextField(blank=True, default="")
    overridden_by = models.ForeignKey(OfficerProfile, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["account_report__doc_ref_id", "code"]

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    @property
    def is_blocking(self) -> bool:
        return self.severity == self.Severity.ERROR or not self.overridden
