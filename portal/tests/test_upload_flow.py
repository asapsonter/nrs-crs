"""XML upload flow: validate on upload, then send or hold.

An uploaded CRS document is a complete return — it carries its own message
header — so there is no separate submit step. Validation runs immediately: a
clean filing goes straight to the Supervision Centre, one with errors is kept
as a draft carrying its findings.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from core import config
from portal.models import Filing, ValidationFinding

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR
SAMPLES = Path(__file__).resolve().parent / "samples"
CLEAN = "2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml"


def _sample(name: str = CLEAN) -> bytes:
    return (SAMPLES / name).read_bytes()


def _upload(client, payload: bytes, name: str = CLEAN):
    return client.post(
        "/portal/filings/upload/",
        {"crs_file": SimpleUploadedFile(name, payload, content_type="text/xml")},
    )


class TestCleanUploadIsSent:
    def test_redirects_to_the_validation_report(self, rfi, partners, portal_client):
        response = _upload(portal_client, _sample())
        assert response.status_code == 302
        filing = Filing.objects.latest("pk")
        assert response.url == f"/portal/filings/{filing.pk}/validation/"

    def test_filing_reaches_the_supervision_centre(self, rfi, partners, portal_client):
        _upload(portal_client, _sample())
        filing = Filing.objects.latest("pk")
        # Submitted and auto-validated in the same request.
        assert filing.status == Filing.Status.ACCEPTED  # auto-accepted on passing validation
        assert filing.submitted_at is not None
        assert filing.validated_at is not None

    def test_warnings_do_not_hold_a_filing_back(self, rfi, partners, portal_client):
        """Self-certification has no CRS schema element, so R-205 fires on
        every XML upload. It must not stop the return being filed."""
        _upload(portal_client, _sample())
        filing = Filing.objects.latest("pk")
        assert filing.findings.filter(severity=ValidationFinding.Severity.WARNING).exists()
        assert not filing.findings.filter(severity=ValidationFinding.Severity.ERROR).exists()
        assert filing.status == Filing.Status.ACCEPTED  # auto-accepted on passing validation

    def test_report_states_it_was_sent(self, rfi, partners, portal_client):
        _upload(portal_client, _sample())
        filing = Filing.objects.latest("pk")
        page = portal_client.get(f"/portal/filings/{filing.pk}/validation/")
        assert page.status_code == 200
        assert b"Sent to the Supervision Centre" in page.content


class TestUploadWithErrorsIsHeld:
    @staticmethod
    def _with_unactivated_residence() -> bytes:
        """R-104: a residence jurisdiction that is not an activated partner.

        US is used deliberately — it operates FATCA rather than CRS and so is
        absent from any CRS partner list, which keeps this test stable as
        `config.PARTNER_JURISDICTIONS` grows.
        """
        xml = _sample().decode()
        return xml.replace(
            "<crs:ResCountryCode>GB</crs:ResCountryCode>",
            "<crs:ResCountryCode>US</crs:ResCountryCode>",
        ).encode()

    def test_filing_is_kept_as_a_draft(self, rfi, partners, portal_client):
        _upload(portal_client, self._with_unactivated_residence())
        filing = Filing.objects.latest("pk")
        assert filing.status == Filing.Status.DRAFT
        assert filing.submitted_at is None

    def test_errors_are_attached_to_the_filing(self, rfi, partners, portal_client):
        _upload(portal_client, self._with_unactivated_residence())
        filing = Filing.objects.latest("pk")
        errors = filing.findings.filter(severity=ValidationFinding.Severity.ERROR)
        assert errors.exists()
        assert any(finding.code == "R-104" for finding in errors)

    def test_records_are_still_stored_for_correction(self, rfi, partners, portal_client):
        _upload(portal_client, self._with_unactivated_residence())
        filing = Filing.objects.latest("pk")
        assert filing.account_reports.exists()

    def test_report_states_it_was_not_sent(self, rfi, partners, portal_client):
        _upload(portal_client, self._with_unactivated_residence())
        filing = Filing.objects.latest("pk")
        page = portal_client.get(f"/portal/filings/{filing.pk}/validation/")
        assert page.status_code == 200
        assert b"Not sent" in page.content
        assert b"R-104" in page.content

    def test_report_details_line_cause_and_resolution(self, rfi, partners, portal_client):
        """The held-filing report gives the error count, the XML line of each
        record error, its cause, and how to resolve it."""
        _upload(portal_client, self._with_unactivated_residence())
        filing = Filing.objects.latest("pk")
        record = filing.account_reports.get()
        assert record.source_line is not None  # stamped from the uploaded XML
        html = portal_client.get(f"/portal/filings/{filing.pk}/validation/").content.decode()
        assert "1</strong> record error" in html  # error count stats
        assert "XML line" in html
        assert f">{record.source_line}<" in html
        assert "How to resolve it" in html
        assert "activated CRS partner list" in html  # the R-104 resolution

    def test_every_error_row_carries_a_line_number(self, rfi, partners, portal_client):
        """File-level errors anchor to the element they concern (or its
        parent block when the element is missing), so no row shows a dash."""
        import re as _re

        text = _sample().decode()
        # Remove MessageRefId entirely and blank the first DocRefId: the two
        # errors from the user-reported case.
        broken = _re.sub(r"[ \t]*<crs:MessageRefId>[^<]*</crs:MessageRefId>\r?\n", "", text)
        broken = _re.sub(
            r"<stf:DocRefId>[^<]*</stf:DocRefId>", "<stf:DocRefId></stf:DocRefId>", broken, count=2
        )
        from portal.xml_ingest import parse_crs_upload

        from core import config as _config

        result = parse_crs_upload(broken.encode(), _config.CURRENT_REPORTING_YEAR)
        assert not result.ok
        assert any("MessageRefId" in e for e in result.errors)
        # Every error is line-anchored — the MessageRefId one points at the
        # MessageSpec block it is missing from.
        for error in result.errors:
            assert error.startswith("Line "), error
        # And the DocRefId rejection carries its own resolution, not the
        # generic fallback.
        from portal.xml_ingest import resolution_hint

        docref_error = next(e for e in result.errors if "DocSpec/DocRefId" in e)
        assert "globally unique DocRefId" in resolution_hint(docref_error)

    def test_rejected_upload_lists_line_cause_and_resolution(self, rfi, partners, portal_client):
        """A schema-level rejection names the XML line, cause, and fix."""
        broken = _sample().decode().replace(
            "<crs:AccountBalance currCode=\"USD\">", "<crs:AccountBalance>", 1
        ).encode()
        response = _upload(portal_client, broken)
        assert response.status_code == 200  # re-renders the upload page
        html = response.content.decode()
        assert "error" in html and "was rejected" in html
        assert "What caused the error" in html and "How to resolve it" in html
        assert "currCode" in html  # the cause
        assert "Line" in html or "XML line" in html


class TestAmendedSampleSequence:
    """The FI sensitization pack's new → amended cycle.

    The amended sample corrects the new sample's records by CorrDocRefId, so
    it may only be filed after the new return is on file (R-502) and only
    once (R-503).
    """

    AMENDED = "2026_Ammended_Sample_UAT_CRS_XML_Test1_NG-GB.xml"

    def test_amendment_after_new_data_is_sent(self, rfi, partners, portal_client):
        _upload(portal_client, _sample())
        _upload(portal_client, _sample(self.AMENDED), name=self.AMENDED)
        amendment = Filing.objects.latest("pk")
        assert amendment.message_type == "CRS702"
        assert not amendment.findings.filter(
            code__in=["R-501", "R-502", "R-503"]
        ).exists()
        assert amendment.status == Filing.Status.ACCEPTED  # auto-accepted on passing validation

    def test_amendment_without_original_is_held(self, rfi, partners, portal_client):
        _upload(portal_client, _sample(self.AMENDED), name=self.AMENDED)
        amendment = Filing.objects.latest("pk")
        assert amendment.status == Filing.Status.DRAFT
        assert amendment.findings.filter(
            code="R-502", severity=ValidationFinding.Severity.ERROR
        ).exists()

    def test_amending_twice_is_held(self, rfi, partners, portal_client):
        _upload(portal_client, _sample())
        _upload(portal_client, _sample(self.AMENDED), name=self.AMENDED)
        # A second run of the same correction targets DocRefIds that the
        # first amendment already superseded — and reuses its DocRefIds,
        # which the upload gate rejects before validation even runs.
        response = _upload(portal_client, _sample(self.AMENDED), name=self.AMENDED)
        assert response.status_code == 200  # re-renders the upload form
        assert b"already been filed" in response.content


class TestParseFailureWritesNothing:
    """A document that cannot be parsed never becomes a filing at all."""

    def test_no_filing_is_created(self, rfi, partners, portal_client):
        before = Filing.objects.count()
        response = _upload(portal_client, b"<nonsense/>")
        assert response.status_code == 200  # re-renders the upload form
        assert Filing.objects.count() == before


class TestReportIsScopedToTheInstitution:
    def test_another_institutions_filing_is_not_visible(self, rfi, partners, portal_client):
        from portal.models import ReportingFI

        other = ReportingFI.objects.create(
            reference="NRS-RFI-2025-0999", legal_name="Other Bank Plc", tin="9999999999",
            category="DEPOSITORY_INSTITUTION", enrolment_type="FINANCIAL_ENTITY",
            status=ReportingFI.Status.ACTIVE, pu_surname="X", pu_first_name="Y",
            pu_designation="Head", pu_email="x@other.ng", pu_phone="800",
        )
        foreign = Filing.objects.create(
            reference="FIL-2025-09999", rfi=other, reporting_year=YEAR,
            kind=Filing.Kind.XML_UPLOAD, status=Filing.Status.DRAFT,
        )
        response = portal_client.get(f"/portal/filings/{foreign.pk}/validation/")
        assert response.status_code == 404
