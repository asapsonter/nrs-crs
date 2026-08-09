"""Reset and seed the full demonstration state.

Run: python manage.py demo_seed

Prints every credential the presenter needs. Safe to rerun; it wipes and
rebuilds all demo data.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import config
from core.models import AuditLog, DemoOutboxEmail, IssuedCredential, OfficerProfile, Roles
from exchange.models import (
    ExchangePackage,
    InboundFile,
    InboundRecord,
    PartnerJurisdiction,
    RecordError,
    StatusMessage,
    Taxpayer,
    TransmissionCertificate,
)
from exchange.services import generate_crs_xml, next_message_ref_id
from portal.models import AccountReport, Filing, PortalUser, ReportingFI, ValidationFinding

SUPERADMIN_PASSWORD = "NrsDemo#2025"
PORTAL_PASSWORD = "Portal#2025"

FAKE_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

class Command(BaseCommand):
    help = "Reset and seed the full AEOI-CRS demonstration state."

    def handle(self, *args, **options) -> None:
        self.stdout.write("Wiping existing demo data.")
        self._wipe()

        self.stdout.write("Seeding partners, institutions, filings, and exchange state.")
        self._seed_superadmin()
        self._seed_partners()
        self._seed_certificates()
        rfis = self._seed_rfis()
        self._seed_status_events()
        self._seed_filings(rfis)
        self._seed_exchange(rfis)
        self._seed_taxpayers()
        self._seed_inbound()
        self._seed_history()

        self.stdout.write(self.style.SUCCESS("\nDemo state ready.\n"))
        self.stdout.write("Supervision Centre (http://localhost:8000/backoffice/login/)")
        self.stdout.write(f"  Super Admin        superadmin / {SUPERADMIN_PASSWORD}")
        self.stdout.write("  No internal users are pre-created. Sign in as Super Admin, open Users to")
        self.stdout.write("  create users and assign roles (Internal Admin, Assistant Admin, Export Only,")
        self.stdout.write("  View Only), then issue each a credential from the Admin Console.")
        self.stdout.write("\nRFI Portal (http://localhost:8000/portal/login/)")
        self.stdout.write(f"  Checker (PU), First Bank of Wazobia    ngozi.bello@wazobiabank.ng / {PORTAL_PASSWORD}")
        self.stdout.write(f"  Maker, First Bank of Wazobia           tunde.ajayi@wazobiabank.ng / {PORTAL_PASSWORD}")
        self.stdout.write(f"  Checker (PU), Lagoon Capital           kemi.oladipo@lagooncapital.ng / {PORTAL_PASSWORD}")
        self.stdout.write(f"  Maker, Lagoon Capital                  ike.nwosu@lagooncapital.ng / {PORTAL_PASSWORD}")
        self.stdout.write(f"  Checker (PU), Sahel Life Assurance     amina.bako@sahellife.ng / {PORTAL_PASSWORD}")
        self.stdout.write("\nDemo email outbox: http://localhost:8000/demo/outbox/")

    def _wipe(self) -> None:
        for model in (
            RecordError,
            StatusMessage,
            InboundRecord,
            InboundFile,
            Taxpayer,
            TransmissionCertificate,
            ExchangePackage,
            ValidationFinding,
            AccountReport,
            Filing,
            PortalUser,
            ReportingFI,
            PartnerJurisdiction,
            AuditLog,
            IssuedCredential,
            OfficerProfile,
            DemoOutboxEmail,
            Session,
        ):
            model.objects.all().delete()
        User.objects.all().delete()

    def _seed_superadmin(self) -> User:
        user = User.objects.create_superuser(
            username="superadmin", email="superadmin@nrs.gov.ng", password=SUPERADMIN_PASSWORD
        )
        return user

    def _seed_partners(self) -> None:
        for code, name, since, fingerprint in config.PARTNER_JURISDICTIONS:
            PartnerJurisdiction.objects.create(
                code=code,
                name=name,
                activated_since=datetime.date.fromisoformat(since),
                key_fingerprint=fingerprint,
            )

    def _seed_certificates(self) -> None:
        """Seed the CTS certificate inventory: the NRS signing certificate and
        one public-key certificate per activated partner. A couple are set
        near or past expiry so the monitoring alert has something to show."""
        today = timezone.localdate()
        TransmissionCertificate.objects.create(
            owner=TransmissionCertificate.Owner.NRS,
            jurisdiction=None,
            subject="NRS AEOI CTS signing certificate",
            fingerprint="NG:5A:2C:81:44:E0:8C:71:2D:5A:B9:03:F4:66:1E:C8",
            valid_from=today - datetime.timedelta(days=400),
            valid_to=today + datetime.timedelta(days=210),
        )
        for index, partner in enumerate(PartnerJurisdiction.objects.all()):
            if index == 0:
                valid_to = today - datetime.timedelta(days=6)     # expired
            elif index == 1:
                valid_to = today + datetime.timedelta(days=18)    # expiring soon
            else:
                valid_to = today + datetime.timedelta(days=180 + (index % 10) * 20)
            TransmissionCertificate.objects.create(
                owner=TransmissionCertificate.Owner.PARTNER,
                jurisdiction=partner,
                subject=f"{partner.name} CTS public-key certificate",
                fingerprint=partner.key_fingerprint or "—",
                valid_from=today - datetime.timedelta(days=365),
                valid_to=valid_to,
            )

    def _make_rfi(self, index: int, **kwargs) -> ReportingFI:
        rfi = ReportingFI.objects.create(
            reference=f"NRS-RFI-{config.CURRENT_REPORTING_YEAR}-{index:04d}", **kwargs
        )
        rfi.ceo_letter.save(f"ceo_letter_{rfi.reference}.pdf", ContentFile(FAKE_PDF), save=True)
        rfi.id_document.save(f"pu_id_{rfi.reference}.pdf", ContentFile(FAKE_PDF), save=True)
        return rfi

    def _portal_user(
        self, rfi: ReportingFI, name: str, email: str, role: str, designation: str,
        is_primary: bool = False, status: str = PortalUser.Status.ACTIVE,
    ) -> PortalUser:
        user = User.objects.create_user(username=email, email=email, password=PORTAL_PASSWORD)
        return PortalUser.objects.create(
            user=user,
            rfi=rfi,
            display_name=name,
            designation=designation,
            role=role,
            is_primary_user=is_primary,
            status=status,
            must_change_password=False,
        )

    def _seed_rfis(self) -> dict[str, ReportingFI]:
        now = timezone.now()

        wazobia = self._make_rfi(
            1,
            legal_name="First Bank of Wazobia Plc",
            street="12 Marina Street",
            city="Lagos Island",
            state_province="Lagos",
            post_code="101001",
            email="info@wazobiabank.ng",
            phone_cc="+234",
            phone="12017001",
            tin="0450088801",
            category="DEPOSITORY_INSTITUTION",
            enrolment_type="FINANCIAL_ENTITY",
            status=ReportingFI.Status.ACTIVE,
            pu_surname="Bello",
            pu_first_name="Ngozi",
            pu_designation="Head, Regulatory Reporting",
            pu_email="ngozi.bello@wazobiabank.ng",
            pu_phone_cc="+234",
            pu_phone="8011112222",
            reviewer=None,
            reviewed_at=now - datetime.timedelta(days=220),
            check_tin=True, check_ceo_letter=True, check_licence=True, check_classification=True,
            review_notes="All checks satisfactory.",
            recommendation="APPROVE",
            decided_by=None,
            decided_at=now - datetime.timedelta(days=218),
        )
        self._portal_user(wazobia, "Ngozi Bello", "ngozi.bello@wazobiabank.ng", PortalUser.Role.CHECKER,
                          "Head, Regulatory Reporting", is_primary=True)
        self._portal_user(wazobia, "Tunde Ajayi", "tunde.ajayi@wazobiabank.ng", PortalUser.Role.MAKER,
                          "Regulatory Reporting Analyst")

        lagoon = self._make_rfi(
            2,
            legal_name="Lagoon Capital Asset Management Ltd",
            street="5 Ozumba Mbadiwe Avenue",
            city="Victoria Island",
            state_province="Lagos",
            post_code="101241",
            email="contact@lagooncapital.ng",
            phone_cc="+234",
            phone="12017002",
            tin="0450088802",
            category="INVESTMENT_ENTITY",
            enrolment_type="FINANCIAL_ENTITY",
            status=ReportingFI.Status.ACTIVE,
            pu_surname="Oladipo",
            pu_first_name="Kemi",
            pu_designation="Chief Compliance Officer",
            pu_email="kemi.oladipo@lagooncapital.ng",
            pu_phone_cc="+234",
            pu_phone="8022223333",
            reviewer=None,
            reviewed_at=now - datetime.timedelta(days=180),
            check_tin=True, check_ceo_letter=True, check_licence=True, check_classification=True,
            review_notes="SEC licence sighted.",
            recommendation="APPROVE",
            decided_by=None,
            decided_at=now - datetime.timedelta(days=178),
        )
        self._portal_user(lagoon, "Kemi Oladipo", "kemi.oladipo@lagooncapital.ng", PortalUser.Role.CHECKER,
                          "Chief Compliance Officer", is_primary=True)
        self._portal_user(lagoon, "Ike Nwosu", "ike.nwosu@lagooncapital.ng", PortalUser.Role.MAKER,
                          "Fund Operations Officer")
        self._portal_user(lagoon, "Bisi Falana", "bisi.falana@lagooncapital.ng", PortalUser.Role.MAKER,
                          "Operations Analyst", status=PortalUser.Status.PENDING)

        sahel = self._make_rfi(
            3,
            legal_name="Sahel Life Assurance Plc",
            street="8 Ahmadu Bello Way",
            city="Central Business District",
            state_province="Abuja (FCT)",
            post_code="900103",
            email="enquiries@sahellife.ng",
            phone_cc="+234",
            phone="94600030",
            tin="0450088803",
            category="SPECIFIED_INSURANCE",
            enrolment_type="FINANCIAL_ENTITY",
            status=ReportingFI.Status.ACTIVE,
            pu_surname="Bako",
            pu_first_name="Amina",
            pu_designation="Head of Compliance",
            pu_email="amina.bako@sahellife.ng",
            pu_phone_cc="+234",
            pu_phone="8033334444",
            reviewer=None,
            reviewed_at=now - datetime.timedelta(days=150),
            check_tin=True, check_ceo_letter=True, check_licence=True, check_classification=True,
            review_notes="NAICOM licence confirmed.",
            recommendation="APPROVE",
            decided_by=None,
            decided_at=now - datetime.timedelta(days=148),
        )
        self._portal_user(sahel, "Amina Bako", "amina.bako@sahellife.ng", PortalUser.Role.CHECKER,
                          "Head of Compliance", is_primary=True)

        self._make_rfi(
            4,
            legal_name="Harmattan Merchant Bank Ltd",
            street="20 Broad Street",
            city="Lagos Island",
            state_province="Lagos",
            post_code="101001",
            email="info@harmattanmb.ng",
            phone_cc="+234",
            phone="12017004",
            tin="0450088804",
            category="DEPOSITORY_INSTITUTION",
            enrolment_type="FINANCIAL_ENTITY",
            status=ReportingFI.Status.UNDER_REVIEW,
            pu_surname="Ogundare",
            pu_first_name="Sola",
            pu_designation="Executive Director, Operations",
            pu_email="sola.ogundare@harmattanmb.ng",
            pu_phone_cc="+234",
            pu_phone="8044445555",
            reviewer=None,
            reviewed_at=now - datetime.timedelta(days=2),
            check_tin=True, check_ceo_letter=True, check_licence=True, check_classification=True,
            review_notes="Documentation complete. Licence verified with the CBN register.",
            recommendation="APPROVE",
        )

        self._make_rfi(
            5,
            legal_name="Calabar Trustees Ltd",
            street="3 Marian Road",
            city="Calabar",
            state_province="Cross River",
            post_code="540281",
            email="info@calabartrustees.ng",
            phone_cc="+234",
            phone="87060050",
            tin="0450088805",
            category="CUSTODIAL_INSTITUTION",
            enrolment_type="FINANCIAL_ENTITY",
            status=ReportingFI.Status.SUBMITTED,
            pu_surname="Effiong",
            pu_first_name="Ekaette",
            pu_designation="Managing Director",
            pu_email="ekaette.effiong@calabartrustees.ng",
            pu_phone_cc="+234",
            pu_phone="8055556666",
        )

        self._make_rfi(
            6,
            legal_name="Delta Broker Partners Ltd",
            street="17 Nnebisi Road",
            city="Asaba",
            state_province="Delta",
            post_code="320242",
            email="info@deltabrokers.ng",
            phone_cc="+234",
            phone="80660060",
            tin="0450088806",
            category="CUSTODIAL_INSTITUTION",
            enrolment_type="FINANCIAL_ENTITY",
            status=ReportingFI.Status.REJECTED,
            pu_surname="Igbinedion",
            pu_first_name="Osaze",
            pu_designation="Principal Partner",
            pu_email="osaze.igbinedion@deltabrokers.ng",
            pu_phone_cc="+234",
            pu_phone="8066667777",
            reviewer=None,
            reviewed_at=now - datetime.timedelta(days=30),
            check_tin=True, check_ceo_letter=False, check_licence=False, check_classification=True,
            review_notes="CEO letter absent; SEC licence expired in the register.",
            recommendation="REJECT",
            decided_by=None,
            decided_at=now - datetime.timedelta(days=28),
            rejection_reason="The regulatory licence could not be confirmed and the CEO letter introducing the Primary User was not provided.",
        )
        return {"wazobia": wazobia, "lagoon": lagoon, "sahel": sahel}

    def _record(self, filing: Filing, sequence: int, **kwargs) -> AccountReport:
        defaults = dict(
            doc_ref_id=f"NG{filing.reporting_year}-{filing.rfi.reference}-{sequence:06d}",
            holder_type="INDIVIDUAL",
            currency="NGN",
        )
        defaults.update(kwargs)
        return AccountReport.objects.create(filing=filing, **defaults)

    def _seed_filings(self, rfis: dict[str, ReportingFI]) -> None:
        year = config.CURRENT_REPORTING_YEAR
        now = timezone.now()
        wazobia, lagoon, sahel = rfis["wazobia"], rfis["lagoon"], rfis["sahel"]
        wazobia_maker = wazobia.portal_users.get(role=PortalUser.Role.MAKER)
        wazobia_checker = wazobia.portal_users.get(is_primary_user=True)
        lagoon_maker = lagoon.portal_users.filter(role=PortalUser.Role.MAKER, status="ACTIVE").first()
        lagoon_checker = lagoon.portal_users.get(is_primary_user=True)
        sahel_checker = sahel.portal_users.get(is_primary_user=True)

        # Filing 1: accepted and destined for exchange (records GB, GB, FR, AE).
        accepted = Filing.objects.create(
            reference=f"FIL-{year}-00001",
            rfi=wazobia,
            reporting_year=year,
            kind=Filing.Kind.XML_UPLOAD,
            status=Filing.Status.ACCEPTED,
            created_by=wazobia_maker,
            checker=wazobia_checker,
            uploaded_filename="wazobia_crs_2025.xml",
            submitted_at=now - datetime.timedelta(days=60),
            validated_at=now - datetime.timedelta(days=58),
            validated_by=None,
            accepted_at=now - datetime.timedelta(days=55),
            approved_by=None,
        )
        self._record(accepted, 1, holder_name="Oliver Hartley", residence_country="GB",
                     foreign_tin="QQ123456C", account_number="0011223344",
                     balance=Decimal("48500000.00"), interest=Decimal("1250000.00"))
        self._record(accepted, 2, holder_name="Amelia Whitfield", residence_country="GB",
                     foreign_tin="QQ654321A", account_number="0011223355",
                     balance=Decimal("120000000.00"), dividends=Decimal("6400000.00"))
        self._record(accepted, 3, holder_name="Claire Moreau", residence_country="FR",
                     foreign_tin="1234567890123", account_number="0011224466",
                     balance=Decimal("74500000.00"), interest=Decimal("2100000.00"))
        self._record(accepted, 4, holder_name="Khalid Al Mansoori", residence_country="AE",
                     foreign_tin="784199012345678", account_number="0011225577",
                     balance=Decimal("310000000.00"), gross_proceeds=Decimal("42000000.00"))

        # Filing 2: submitted and auto-validated against the CRS schema at
        # submission (as the live flow does); its deliberate defects give the
        # approval demo findings to act on.
        submitted = Filing.objects.create(
            reference=f"FIL-{year}-00002",
            rfi=wazobia,
            reporting_year=year,
            kind=Filing.Kind.MANUAL,
            status=Filing.Status.UNDER_VALIDATION,
            created_by=wazobia_maker,
            checker=wazobia_checker,
            submitted_at=now - datetime.timedelta(days=1),
            validated_at=now - datetime.timedelta(days=1),
            validated_by=None,
        )
        self._record(submitted, 5, holder_name="Priya Raghunathan", residence_country="IN",
                     foreign_tin="ABCPR1234F", account_number="0022334455",
                     balance=Decimal("15200000.00"))
        self._record(submitted, 6, holder_name="Heinrich Vogel", residence_country="DE",
                     foreign_tin="", account_number="0022334466",
                     balance=Decimal("8700000.00"))
        self._record(submitted, 7, holder_name="Sipho Ndlovu", residence_country="US",
                     foreign_tin="123-45-6789", account_number="0022334477",
                     balance=Decimal("-500000.00"))
        # Findings come from the real validator so the demo matches production.
        from backoffice.validation import run_validation

        run_validation(submitted)

        # Filing 3: returned for correction with one record already amended,
        # so the correction lineage is visible immediately.
        returned = Filing.objects.create(
            reference=f"FIL-{year}-00003",
            rfi=lagoon,
            reporting_year=year,
            kind=Filing.Kind.MANUAL,
            status=Filing.Status.RETURNED,
            created_by=lagoon_maker,
            checker=lagoon_checker,
            submitted_at=now - datetime.timedelta(days=20),
            validated_at=now - datetime.timedelta(days=18),
            validated_by=None,
            returned_at=now - datetime.timedelta(days=17),
            return_reason="Record-level errors: one account holder without a TIN and one implausible TIN. Amend the flagged records and resubmit.",
        )
        original_a = self._record(returned, 1, holder_name="Margaret Chen", residence_country="CA",
                                  foreign_tin="", account_number="LC-9001",
                                  balance=Decimal("22800000.00"), superseded=True)
        corrected_a = self._record(returned, 3, holder_name="Margaret Chen", residence_country="CA",
                                   foreign_tin="946454286", account_number="LC-9001",
                                   balance=Decimal("22800000.00"),
                                   corr_doc_ref_id=original_a.doc_ref_id,
                                   doc_type_indic=AccountReport.DocTypeIndic.OECD2)
        flagged_b = self._record(returned, 2, holder_name="Rajesh Iyer", residence_country="IN",
                                 foreign_tin="@@", account_number="LC-9002",
                                 balance=Decimal("9400000.00"))
        ValidationFinding.objects.create(
            filing=returned, account_report=original_a, severity="WARNING", code="R-201",
            message="No TIN reported for the account holder. Partner jurisdictions may reject the record.",
        )
        ValidationFinding.objects.create(
            filing=returned, account_report=flagged_b, severity="ERROR", code="R-202",
            message="TIN '@@' fails the plausibility check.",
        )

        # Filing 4: nil return, accepted (CRS703).
        Filing.objects.create(
            reference=f"FIL-{year}-00004",
            rfi=sahel,
            reporting_year=year,
            kind=Filing.Kind.NIL,
            message_type=Filing.MessageType.CRS703,
            status=Filing.Status.ACCEPTED,
            created_by=sahel_checker,
            checker=sahel_checker,
            submitted_at=now - datetime.timedelta(days=40),
            validated_at=now - datetime.timedelta(days=39),
            validated_by=None,
            accepted_at=now - datetime.timedelta(days=39),
            approved_by=None,
        )

        # Filing 5: a maker draft sitting with the Checker.
        pending = Filing.objects.create(
            reference=f"FIL-{year}-00005",
            rfi=lagoon,
            reporting_year=year,
            kind=Filing.Kind.MANUAL,
            status=Filing.Status.PENDING_CHECKER,
            created_by=lagoon_maker,
        )
        self._record(pending, 4, holder_name="Beatrice Kamau", residence_country="GB",
                     foreign_tin="QQ998877B", account_number="LC-9010",
                     balance=Decimal("5400000.00"))

    def _seed_exchange(self, rfis: dict[str, ReportingFI]) -> None:
        year = config.CURRENT_REPORTING_YEAR
        now = timezone.now()
        accepted = Filing.objects.get(reference=f"FIL-{year}-00001")
        gb = PartnerJurisdiction.objects.get(code="GB")
        fr = PartnerJurisdiction.objects.get(code="FR")

        gb_records = accepted.account_reports.filter(residence_country="GB")
        fr_records = accepted.account_reports.filter(residence_country="FR")

        # Package 1: full cycle closed. Accepted by the partner and archived.
        package_gb = ExchangePackage.objects.create(
            jurisdiction=gb,
            reporting_year=year,
            message_ref_id=next_message_ref_id("GB", year),
            status=ExchangePackage.Status.ARCHIVED,
            pipeline_step=8,
            transmitted_at=now - datetime.timedelta(days=30),
            closed_at=now - datetime.timedelta(days=28),
        )
        package_gb.records.set(gb_records)
        package_gb.xml_content = generate_crs_xml(package_gb)
        package_gb.save()
        StatusMessage.objects.create(
            direction=StatusMessage.Direction.INBOUND,
            outcome=StatusMessage.Outcome.ACCEPTED,
            package=package_gb,
            detail="United Kingdom accepted the package. No errors reported.",
        )

        # Package 2: transmitted, record errors reported, correction open.
        package_fr = ExchangePackage.objects.create(
            jurisdiction=fr,
            reporting_year=year,
            message_ref_id=next_message_ref_id("FR", year),
            status=ExchangePackage.Status.RECORD_ERRORS,
            pipeline_step=8,
            transmitted_at=now - datetime.timedelta(days=12),
        )
        package_fr.records.set(fr_records)
        package_fr.xml_content = generate_crs_xml(package_fr)
        package_fr.save()
        error_message = StatusMessage.objects.create(
            direction=StatusMessage.Direction.INBOUND,
            outcome=StatusMessage.Outcome.RECORD_ERROR,
            package=package_fr,
            error_code="80008",
            detail="France reported 1 record error(s): 80008 TIN not supplied.",
        )
        RecordError.objects.create(
            status_message=error_message,
            doc_ref_id=fr_records.first().doc_ref_id,
            code="80008",
            detail="TIN not supplied",
        )

        # The accepted filing's records are all packaged: included in exchange.
        accepted.status = Filing.Status.IN_EXCHANGE
        accepted.save(update_fields=["status"])
        # The AE record is left unpackaged deliberately so the presenter can
        # click Build exchange packages live.

    def _seed_taxpayers(self) -> None:
        """Seed the domestic taxpayer register used by inbound matching.

        Covers most inbound account holders by TIN, but deliberately omits
        one (Ibrahim Danjuma) so the first matching pass leaves an unmatched
        record and the presenter can walk the reattempt loop (IB-09).
        """
        register = [
            ("10293847-0001", "Chukwuemeka Obi", "Lagos Island Tax Office"),
            ("10293847-0002", "Folake Adesina", "Ikeja Tax Office"),
            ("10293847-0004", "Adanna Okoli", "Enugu Tax Office"),
            ("10293847-0009", "Aisha Mohammed", "Abuja Central Tax Office"),
            ("10293847-0010", "Emeka Nwachukwu", "Port Harcourt Tax Office"),
        ]
        for tin, name, office in register:
            Taxpayer.objects.create(tin=tin, name=name, tax_office=office)

    def _seed_status_events(self) -> None:
        """Reconstruct a dated status history for each seeded institution."""
        from portal.models import EnrolmentStatusEvent

        S = ReportingFI.Status
        for rfi in ReportingFI.objects.all():
            # submitted_at is auto-stamped at seed time; backdate the Submitted
            # event to sit before the (backdated) review and decision events so
            # the history reads in chronological order.
            later = [t for t in (rfi.reviewed_at, rfi.decided_at) if t]
            submitted_at = min(later) - datetime.timedelta(days=2) if later else rfi.submitted_at
            events: list[tuple[str, object, str]] = [
                (S.SUBMITTED, submitted_at, "Application submitted.")
            ]
            if rfi.reviewed_at:
                events.append((S.UNDER_REVIEW, rfi.reviewed_at, "Assessment recorded by the NRS."))
            if rfi.decided_at:
                if rfi.status == S.REJECTED:
                    events.append((S.REJECTED, rfi.decided_at, "Enrolment rejected."))
                else:
                    events.append((S.APPROVED, rfi.decided_at, "Enrolment approved by the NRS."))
            if rfi.status in (S.ACTIVE, S.SUSPENDED, S.DEACTIVATED):
                base = rfi.decided_at or rfi.submitted_at
                events.append(
                    (S.ACTIVE, base + datetime.timedelta(days=1), "Primary User signed in. Institution active.")
                )
                if rfi.status in (S.SUSPENDED, S.DEACTIVATED):
                    events.append(
                        (rfi.status, base + datetime.timedelta(days=2), "Institution standing changed.")
                    )
            for status, at, note in events:
                EnrolmentStatusEvent.objects.create(rfi=rfi, status=status, changed_at=at, note=note)

    def _seed_inbound(self) -> None:
        year = config.CURRENT_REPORTING_YEAR
        now = timezone.now()
        gb = PartnerJurisdiction.objects.get(code="GB")
        ca = PartnerJurisdiction.objects.get(code="CA")

        # Approved and awaiting taxpayer matching, so the presenter runs the
        # match, reattempt and dissemination steps live (BPMN IB-08/09/10).
        clean = InboundFile.objects.create(
            jurisdiction=gb,
            reporting_year=year,
            message_ref_id=f"GB{year}NG000042",
            status=InboundFile.Status.APPROVED,
            file_checked_at=now - datetime.timedelta(days=10),
            records_checked_at=now - datetime.timedelta(days=10),
            approved_at=now - datetime.timedelta(days=9),
        )
        InboundRecord.objects.create(
            file=clean, doc_ref_id=f"GB{year}-HSBC-000101", holder_name="Chukwuemeka Obi",
            ng_tin="10293847-0001", account_number="GB-ACC-77001",
            balance=Decimal("85000.00"), currency="GBP",
        )
        InboundRecord.objects.create(
            file=clean, doc_ref_id=f"GB{year}-HSBC-000102", holder_name="Folake Adesina",
            ng_tin="10293847-0002", account_number="GB-ACC-77002",
            balance=Decimal("152400.00"), currency="GBP",
        )
        InboundRecord.objects.create(
            file=clean, doc_ref_id=f"GB{year}-BARC-000201", holder_name="Ibrahim Danjuma",
            ng_tin="10293847-0003", account_number="GB-ACC-88001",
            balance=Decimal("40100.00"), currency="GBP",
        )
        StatusMessage.objects.create(
            direction=StatusMessage.Direction.OUTBOUND,
            outcome=StatusMessage.Outcome.ACCEPTED,
            inbound_file=clean,
            detail="Accepted Status Message issued to United Kingdom: file processed with no errors.",
        )

        pending = InboundFile.objects.create(
            jurisdiction=ca,
            reporting_year=year,
            message_ref_id=f"CA{year}NG000007",
            status=InboundFile.Status.RECEIVED,
            simulate_file_error="50003",
        )
        InboundRecord.objects.create(
            file=pending, doc_ref_id=f"CA{year}-RBC-000301", holder_name="Adanna Okoli",
            ng_tin="10293847-0004", account_number="CA-ACC-55001",
            balance=Decimal("61200.00"), currency="CAD",
        )
        InboundRecord.objects.create(
            file=pending, doc_ref_id=f"CA{year}-RBC-000302", holder_name="Yusuf Garba",
            ng_tin="", account_number="CA-ACC-55002",
            balance=Decimal("23800.00"), currency="CAD",
            has_error=True, error_code="80008", error_detail="TIN not supplied",
        )

    def _seed_history(self) -> None:
        """Populate the audit log with generic unit actors, plus one flagged
        credential-reuse event, so the trail reads as populated."""
        year = config.CURRENT_REPORTING_YEAR
        AuditLog.record(
            actor_name="Internal user (unassigned)",
            actor_role=Roles.VIEW_ONLY.label,
            action="CREDENTIAL_REUSE_REFUSED",
            target="nrs-usr-XXXXX",
            detail="Login refused: credential already bound to a live session. Possible credential sharing.",
            flagged=True,
        )
        AuditLog.record(
            actor_name="CRS schema validator",
            actor_role="System",
            surface="portal",
            action="FILING_AUTO_VALIDATED",
            target=f"FIL-{year}-00001",
            before_state="SUBMITTED",
            after_state="UNDER_VALIDATION",
            detail="Automatic schema validation on submission: 0 file-level, 0 record-level findings.",
        )
        AuditLog.record(
            actor_name="NRS Returns Unit",
            actor_role=Roles.INTERNAL_ADMIN.label,
            action="FILING_APPROVED",
            target=f"FIL-{year}-00001",
            before_state="UNDER_VALIDATION",
            after_state="ACCEPTED",
            detail="Filing approved for exchange with 4 records.",
        )
        package = ExchangePackage.objects.filter(jurisdiction__code="GB").first()
        if package:
            AuditLog.record(
                actor_name="NRS Exchange Unit",
                actor_role=Roles.INTERNAL_ADMIN.label,
                action="PACKAGE_TRANSMITTED",
                target=package.message_ref_id,
                detail="Package uploaded to the CTS inbox of the United Kingdom.",
            )
