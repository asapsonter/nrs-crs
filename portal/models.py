"""Reporting Financial Institution models: enrolment, portal users, filings."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from core import config
from core.models import OfficerProfile


class ReportingFI(models.Model):
    """An RFI and its Institution & Primary User Enrolment.

    The enrolment state machine follows AEOI portal practice: Submitted >
    Approved or Declined > Active > Suspended or Deactivated. The authority
    reviews the submitted enrolment form and approves or declines it; on
    approval the Primary User receives credentials by email. (The Under
    Review state and assessment fields remain from the retired recorded-
    assessment flow.)
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        UNDER_REVIEW = "UNDER_REVIEW", "Under Review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Declined"
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

    # Primary User. Name is captured as surname, middle name, and other
    # names. For a stakeholder or individual enrolment this is the natural
    # person enrolling. (pu_first_name holds the other names; the column name
    # is retained for data continuity.)
    pu_surname = models.CharField("Primary User surname", max_length=80, blank=True, default="")
    pu_middle_name = models.CharField("Primary User middle name", max_length=80, blank=True, default="")
    pu_first_name = models.CharField("Primary User other names", max_length=80, blank=True, default="")
    pu_dob = models.DateField("Date of birth", null=True, blank=True)
    pu_place_of_birth = models.CharField("Place of birth", max_length=200, blank=True, default="")
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
        """Primary User full name: other names, middle name, then surname."""
        parts = [self.pu_first_name, self.pu_middle_name, self.pu_surname]
        return " ".join(part for part in parts if part).strip()

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
    """A portal account within an approved RFI: Primary or Secondary User.

    Vizor AEOI convention: the Primary User is created at enrolment approval
    and administers the institution's access, creating Secondary Users. Both
    prepare and submit filings directly; there is no maker-checker review
    step. (Role values retain their legacy codes for data continuity.)
    """

    class Role(models.TextChoices):
        MAKER = "MAKER", "Secondary User"
        CHECKER = "CHECKER", "Primary User"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
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
    CRS703 nil return. Any portal user (Primary or Secondary) prepares and
    submits filings directly; there is no maker-checker review step.
    """

    class Kind(models.TextChoices):
        XML_UPLOAD = "XML_UPLOAD", "CRS XML upload Filing"
        EXCEL_UPLOAD = "EXCEL_UPLOAD", "CRS Excel Upload Filing"
        MANUAL = "MANUAL", "CRS Manual Entry Filing"
        NIL = "NIL", "Nil return"
        PU_CHANGE = "PU_CHANGE", "Primary User Change Notice"
        ENTITY_DEACTIVATION = "ENTITY_DEACTIVATION", "Reporting Entity Deactivation"
        ENTITY_INFO_CHANGE = "ENTITY_INFO_CHANGE", "Change of reporting entity information"

    # The CRS data filing kinds carry account reports and a CRS message
    # type; the remaining kinds are administrative notices.
    CRS_DATA_KINDS = (Kind.XML_UPLOAD, Kind.EXCEL_UPLOAD, Kind.MANUAL, Kind.NIL)
    NOTICE_KINDS = (Kind.PU_CHANGE, Kind.ENTITY_DEACTIVATION, Kind.ENTITY_INFO_CHANGE)

    # The notice form fields per kind: required first, then optional. Shared
    # by the portal notice form and the validation engine so the two can
    # never disagree on what a complete notice contains.
    NOTICE_REQUIRED_FIELDS = {
        Kind.PU_CHANGE: (
            ("new_pu_surname", "New Primary User surname"),
            ("new_pu_first_name", "New Primary User other names"),
            ("new_pu_designation", "New Primary User position"),
            ("new_pu_email", "New Primary User email"),
            ("new_pu_phone", "New Primary User phone"),
        ),
        Kind.ENTITY_DEACTIVATION: (
            ("effective_date", "Effective date"),
            ("reason", "Reason for deactivation"),
        ),
        Kind.ENTITY_INFO_CHANGE: (
            ("legal_name", "Legal name"),
            ("street", "Street"),
            ("city", "City or town"),
            ("email", "Financial Institution email"),
            ("phone", "Financial Institution phone"),
        ),
    }
    NOTICE_OPTIONAL_FIELDS = {
        Kind.PU_CHANGE: ("new_pu_middle_name", "reason"),
        Kind.ENTITY_DEACTIVATION: (),
        Kind.ENTITY_INFO_CHANGE: ("state_province", "post_code"),
    }

    class MessageType(models.TextChoices):
        CRS701 = "CRS701", "CRS701 New data"
        CRS702 = "CRS702", "CRS702 Corrections"
        CRS703 = "CRS703", "CRS703 Nil return"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_CHECKER = "PENDING_CHECKER", "Pending Submission"
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

    # Administrative notices (Primary User change, entity deactivation,
    # change of entity information) carry their form data here rather than
    # in account reports. `validated` is set by the notice form's
    # Validate & Save and gates submission.
    notice_payload = models.JSONField(default=dict, blank=True)

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
    def is_notice(self) -> bool:
        """Whether this filing is an administrative notice (no account data)."""
        return self.kind in self.NOTICE_KINDS

    @property
    def fi_status_display(self) -> str:
        """The status as shown to the institution.

        Under Validation is the NRS's internal processing stage; from the
        institution's side the filing is simply Submitted until the NRS
        accepts it or returns it for correction.
        """
        if self.status == self.Status.UNDER_VALIDATION:
            return "Submitted"
        return self.get_status_display()

    @property
    def notice_validated(self) -> bool:
        """Whether the notice form passed Validate & Save."""
        return bool(self.notice_payload.get("validated"))

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
            self.Status.PENDING_CHECKER: "Awaiting submission",
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


#: Placeholder the CRS User Guide (IIc) sanctions when a Reporting FI does not
#: hold a first name for an individual.
NO_FIRST_NAME = "NFN"


def crs_name_parts(first_name: str, last_name: str, full_name: str) -> tuple[str, str]:
    """Resolve a CRS NamePerson_Type FirstName/LastName pair.

    ``FirstName`` and ``LastName`` are both Validation elements, so every
    individual must yield two non-empty values. Where the record only carries a
    single free-text name we do *not* guess at a split — the User Guide allows
    ``NFN`` for an unknown first name and allows LastName to carry a free-format
    name, which keeps the output truthful about what the FI actually reported.
    """
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if last:
        return first or NO_FIRST_NAME, last
    whole = (full_name or "").strip()
    return first or NO_FIRST_NAME, whole or NO_FIRST_NAME


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

    class AcctNumberType(models.TextChoices):
        """CRS AccountNumber @AcctNumberType (User Guide IVd)."""
        OECD601 = "OECD601", "OECD601 IBAN"
        OECD602 = "OECD602", "OECD602 Other Bank Account Number"
        OECD603 = "OECD603", "OECD603 ISIN"
        OECD604 = "OECD604", "OECD604 Other Securities Information Number"
        OECD605 = "OECD605", "OECD605 Other (e.g. insurance contract)"

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
    # CRS NamePerson_Type requires FirstName and LastName for individuals
    # (User Guide IIc). Both are Validation elements, so an individual record
    # cannot be rendered from `holder_name` alone. When these are blank the
    # emitter falls back to NFN / free-format last name, which the User Guide
    # expressly permits.
    holder_first_name = models.CharField("First name", max_length=200, blank=True, default="")
    holder_middle_name = models.CharField("Middle name", max_length=200, blank=True, default="")
    holder_last_name = models.CharField("Last name", max_length=200, blank=True, default="")
    holder_type = models.CharField(
        max_length=12,
        choices=[("INDIVIDUAL", "Individual"), ("ORGANISATION", "Entity")],
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
    # `holder_address` backs AddressFree; the components below back AddressFix,
    # which the User Guide (IId) says should be used for all CRS reporting
    # unless the parts of the address cannot be determined. `holder_city` is
    # the Validation element within AddressFix, so it gates the choice.
    holder_address = models.CharField("Account holder address", max_length=300, blank=True, default="")
    address_country = models.CharField("Address country", max_length=2, blank=True, default="")
    holder_street = models.CharField("Street", max_length=200, blank=True, default="")
    holder_building_identifier = models.CharField("Building", max_length=200, blank=True, default="")
    holder_suite_identifier = models.CharField("Suite", max_length=200, blank=True, default="")
    holder_floor_identifier = models.CharField("Floor", max_length=200, blank=True, default="")
    holder_district_name = models.CharField("District", max_length=200, blank=True, default="")
    holder_pob = models.CharField("P.O. box", max_length=200, blank=True, default="")
    holder_post_code = models.CharField("Post code", max_length=200, blank=True, default="")
    holder_city = models.CharField("City", max_length=200, blank=True, default="")
    holder_country_subentity = models.CharField("State / region", max_length=200, blank=True, default="")

    # CRS BirthInfo (individual holders). CountryInfo is a choice between a
    # current jurisdiction code and a former jurisdiction name; the User Guide
    # (IIf) asks for one of them whenever place of birth is reported.
    birth_date = models.DateField("Date of birth", null=True, blank=True)
    birth_city = models.CharField("City of birth", max_length=120, blank=True, default="")
    birth_city_subentity = models.CharField("Birth city subentity", max_length=200, blank=True, default="")
    birth_country_code = models.CharField("Country of birth", max_length=2, blank=True, default="")
    birth_former_country_name = models.CharField(
        "Former country of birth", max_length=200, blank=True, default=""
    )

    # Due-diligence: self-certification status per CRS Regulations 2019.
    self_certification = models.CharField(
        max_length=14, choices=SelfCertification.choices, blank=True, default=""
    )

    account_number = models.CharField(max_length=40)
    acct_number_type = models.CharField(
        "Account number type", max_length=7, choices=AcctNumberType.choices, blank=True, default=""
    )
    # CRS AccountNumber attributes. ClosedAccount is "(Optional) Mandatory" in
    # the User Guide (IVd) and pairs with a zero balance (IVg).
    closed_account = models.BooleanField("Account closed in the reporting period", default=False)
    dormant_account = models.BooleanField("Account dormant", default=False)
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
    # For records ingested from an XML upload: the line in the uploaded file
    # where this AccountReport element starts, so findings can point the
    # preparer back into their own document.
    source_line = models.PositiveIntegerField(null=True, blank=True)

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

    @property
    def crs_name(self) -> tuple[str, str]:
        """The CRS FirstName/LastName pair for an individual holder."""
        return crs_name_parts(self.holder_first_name, self.holder_last_name, self.holder_name)

    @property
    def is_complete(self) -> bool:
        """Whether this Account Information form is complete (Validated).

        Mirrors the per-form validation a Vizor-style AEOI portal applies
        before a filing becomes Ready to Submit: identity, mandatory address,
        self-certification, a TIN or a reason it is unavailable, and, for a
        Passive NFE (CRS101), at least one controlling person.
        """
        if not (
            self.holder_name.strip()
            and self.account_number.strip()
            and self.residence_country.strip()
            and self.holder_address.strip()
            and self.self_certification
        ):
            return False
        if not self.foreign_tin.strip() and not self.tin_unavailable_reason.strip():
            return False
        if self.requires_controlling_persons and not self.controlling_persons.exists():
            return False
        return True


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
    # A controlling person is reported as a PersonParty_Type, so the same
    # FirstName/LastName Validation pair applies as for individual holders.
    first_name = models.CharField("First name", max_length=200, blank=True, default="")
    middle_name = models.CharField("Middle name", max_length=200, blank=True, default="")
    last_name = models.CharField("Last name", max_length=200, blank=True, default="")
    residence_country = models.CharField("Residence jurisdiction", max_length=2)
    tin = models.CharField("TIN", max_length=40, blank=True, default="")
    address = models.CharField("Address", max_length=300, blank=True, default="")
    city = models.CharField("City", max_length=200, blank=True, default="")
    address_country = models.CharField("Address country", max_length=2, blank=True, default="")
    birth_date = models.DateField("Date of birth", null=True, blank=True)
    birth_city = models.CharField("City of birth", max_length=120, blank=True, default="")
    birth_country_code = models.CharField("Country of birth", max_length=2, blank=True, default="")
    ctrlg_person_type = models.CharField(
        "Controlling person type", max_length=6, choices=CtrlgPersonType.choices,
        default=CtrlgPersonType.CRS801,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_ctrlg_person_type_display()})"

    @property
    def crs_name(self) -> tuple[str, str]:
        """The CRS FirstName/LastName pair for this controlling person."""
        return crs_name_parts(self.first_name, self.last_name, self.name)

    @property
    def address_country_code(self) -> str:
        """Address country, falling back to residence for the CRS Address."""
        return self.address_country or self.residence_country


# How to resolve each validation finding, by code. Shown beside the finding
# on the institution's validation report.
FINDING_RESOLUTIONS = {
    "F-001": "Add at least one account report to the filing, or file a nil return for a year with nothing to report.",
    "F-002": "Give this message a new, unused MessageRefId; every CRS message must carry its own unique reference.",
    "R-101": "Provide the account holder's name.",
    "R-102": "Provide the account number the institution uses for the account (or NANUM where no numbering system exists).",
    "R-103": "Provide the holder's residence jurisdiction as a 2-letter ISO country code.",
    "R-104": "Report the account under a jurisdiction on the activated CRS partner list, or remove the record until the jurisdiction is activated.",
    "R-105": "Remove the duplicate: each account may appear once per filing. Report joint holders on a single record.",
    "R-106": "Provide the account holder's address; it is mandatory in the CRS AccountHolder element.",
    "R-107": "Classify the entity holder as CRS101 (Passive NFE with controlling persons), CRS102 (CRS Reportable Person) or CRS103 (Passive NFE that is itself reportable).",
    "R-108": "Add at least one controlling person to the CRS101 Passive NFE account.",
    "R-201": "Obtain and report the holder's TIN when the jurisdiction issues one; the reason recorded travels with the filing.",
    "R-202": "Correct the TIN: 6-20 characters, letters and digits with common separators only.",
    "R-204": "Report the holder's TIN, or record the reason it is unavailable (for example, the jurisdiction issues none).",
    "R-205": "Record the holder's self-certification status collected under CRS due diligence.",
    "R-206": "Pursue the outstanding self-certification; undocumented accounts are reported to the NRS as such.",
    "R-301": "Report a negative balance as 0.00, per the CRS User Guide.",
    "R-302": "Report a closed account with a zero balance alongside the ClosedAccount flag.",
    "R-401": "Correct the account opening date: it cannot fall after the reporting year.",
    "R-501": "Correct or delete a record only once per filing; merge the changes into a single corrected record.",
    "R-502": "Point CorrDocRefId at the DocRefId of a record previously filed by your institution.",
    "R-503": "Point CorrDocRefId at the DocRefId of the latest version of the record; the original was superseded by an earlier correction.",
    "N-001": "Complete the missing field on the notice form, then Validate & Save.",
}


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

    @property
    def resolution(self) -> str:
        """How the institution resolves this finding."""
        return FINDING_RESOLUTIONS.get(
            self.code, "Correct the item described and submit the filing again."
        )

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    @property
    def is_blocking(self) -> bool:
        return self.severity == self.Severity.ERROR or not self.overridden
