"""FI-initiated corrections of accepted filings (CRS702).

An accepted filing is never edited in place: the institution opens a
correction filing, pulls filed records in as amendments (OECD2) or deletions
(OECD3) referencing them by DocRefId, and submits. Accepted corrections are
packaged as CRS702 messages and supersede the records they replace.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core import config
from portal.models import AccountReport, Filing

pytestmark = pytest.mark.django_db

YEAR = config.CURRENT_REPORTING_YEAR


@pytest.fixture
def accepted(rfi):
    filing = Filing.objects.create(
        reference=f"FIL-{YEAR}-80001", rfi=rfi, reporting_year=YEAR,
        kind=Filing.Kind.MANUAL, status=Filing.Status.ACCEPTED,
        receiving_country="GB", sending_company_in=rfi.tin,
        message_reference=f"NG{YEAR}GB-ORIG-1",
    )
    AccountReport.objects.create(
        filing=filing, doc_ref_id=f"NG{YEAR}-ORIG-000001",
        holder_name="Chukwu Emeka", residence_country="GB",
        foreign_tin="QQ123456C", holder_address="1 King Street, London",
        holder_city="London", self_certification="OBTAINED",
        account_number="ACC-8001", balance=Decimal("1000000.00"),
    )
    return filing


def open_correction(client, accepted) -> Filing:
    client.post(f"/portal/filings/{accepted.pk}/correct/")
    return Filing.objects.filter(corrects=accepted).latest("pk")


class TestOpeningACorrection:
    def test_creates_a_crs702_draft_referencing_the_original(self, rfi, portal_client, accepted):
        correction = open_correction(portal_client, accepted)
        assert correction.status == Filing.Status.DRAFT
        assert correction.message_type == Filing.MessageType.CRS702
        assert correction.corrects == accepted
        assert correction.receiving_country == "GB"
        assert correction.message_reference != accepted.message_reference
        assert correction.account_reports.count() == 0  # starts empty

    def test_only_accepted_filings_can_be_corrected(self, rfi, portal_client):
        draft = Filing.objects.create(
            reference=f"FIL-{YEAR}-80002", rfi=rfi, reporting_year=YEAR,
            kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
        )
        portal_client.post(f"/portal/filings/{draft.pk}/correct/")
        assert not Filing.objects.filter(corrects=draft).exists()

    def test_reuses_an_open_correction(self, rfi, portal_client, accepted):
        first = open_correction(portal_client, accepted)
        response = portal_client.post(f"/portal/filings/{accepted.pk}/correct/")
        assert response.url == f"/portal/filings/{first.pk}/"
        assert Filing.objects.filter(corrects=accepted).count() == 1

    def test_detail_page_offers_the_action(self, rfi, portal_client, accepted):
        html = portal_client.get(f"/portal/filings/{accepted.pk}/").content.decode()
        assert f"/portal/filings/{accepted.pk}/correct/" in html


class TestPullingRecordsIn:
    def test_amend_copies_record_as_oecd2_and_opens_editor(self, rfi, portal_client, accepted):
        correction = open_correction(portal_client, accepted)
        original = accepted.account_reports.get()
        response = portal_client.post(
            f"/portal/filings/{correction.pk}/correction/records/{original.pk}/pick/",
            {"action": "amend"},
        )
        replacement = correction.account_reports.get()
        assert response.url == f"/portal/filings/{correction.pk}/records/{replacement.pk}/"
        assert replacement.doc_type_indic == AccountReport.DocTypeIndic.OECD2
        assert replacement.corr_doc_ref_id == original.doc_ref_id
        assert replacement.doc_ref_id != original.doc_ref_id
        assert replacement.holder_name == original.holder_name
        assert replacement.balance == original.balance

    def test_delete_copies_record_as_oecd3(self, rfi, portal_client, accepted):
        correction = open_correction(portal_client, accepted)
        original = accepted.account_reports.get()
        portal_client.post(
            f"/portal/filings/{correction.pk}/correction/records/{original.pk}/pick/",
            {"action": "delete"},
        )
        assert correction.account_reports.get().doc_type_indic == AccountReport.DocTypeIndic.OECD3

    def test_a_record_is_pulled_only_once(self, rfi, portal_client, accepted):
        correction = open_correction(portal_client, accepted)
        original = accepted.account_reports.get()
        url = f"/portal/filings/{correction.pk}/correction/records/{original.pk}/pick/"
        portal_client.post(url, {"action": "amend"})
        portal_client.post(url, {"action": "delete"})
        assert correction.account_reports.count() == 1

    def test_new_data_cannot_be_added_to_a_correction(self, rfi, portal_client, accepted):
        correction = open_correction(portal_client, accepted)
        portal_client.post(
            f"/portal/filings/{correction.pk}/records/add/",
            {"holder_name": "Brand New Holder", "residence_country": "GB",
             "account_number": "NEW-1", "balance": "5"},
        )
        assert correction.account_reports.count() == 0


class TestSubmittingACorrection:
    def test_correction_submits_cleanly(self, rfi, partners, portal_client, accepted):
        correction = open_correction(portal_client, accepted)
        original = accepted.account_reports.get()
        portal_client.post(
            f"/portal/filings/{correction.pk}/correction/records/{original.pk}/pick/",
            {"action": "amend"},
        )
        response = portal_client.post(f"/portal/filings/{correction.pk}/stage/")
        assert response.status_code == 302
        correction.refresh_from_db()
        assert correction.status == Filing.Status.ACCEPTED  # auto-accepted on passing validation
        assert not correction.findings.filter(severity="ERROR").exists()

    def test_second_correction_of_same_record_is_held(self, rfi, partners, portal_client, accepted):
        first = open_correction(portal_client, accepted)
        original = accepted.account_reports.get()
        portal_client.post(
            f"/portal/filings/{first.pk}/correction/records/{original.pk}/pick/", {"action": "amend"}
        )
        portal_client.post(f"/portal/filings/{first.pk}/stage/")
        second = open_correction(portal_client, accepted)
        portal_client.post(
            f"/portal/filings/{second.pk}/correction/records/{original.pk}/pick/", {"action": "amend"}
        )
        portal_client.post(f"/portal/filings/{second.pk}/stage/")
        second.refresh_from_db()
        assert second.status == Filing.Status.DRAFT
        assert second.findings.filter(code="R-503", severity="ERROR").exists()


class TestCorrectionExchange:
    def test_accepted_correction_builds_a_crs702_package(self, rfi, partners, portal_client, accepted):
        import xmlschema

        from exchange.services import build_packages

        correction = open_correction(portal_client, accepted)
        original = accepted.account_reports.get()
        portal_client.post(
            f"/portal/filings/{correction.pk}/correction/records/{original.pk}/pick/",
            {"action": "amend"},
        )
        correction.status = Filing.Status.ACCEPTED
        correction.save(update_fields=["status"])
        # The original filing is already exchanged; only the correction packages.
        accepted.status = Filing.Status.IN_EXCHANGE
        accepted.save(update_fields=["status"])

        packages = build_packages(YEAR)
        assert len(packages) == 1
        package = packages[0]
        assert package.message_type == "CRS702"
        assert "OECD2" in package.xml_content
        assert f"<stf:CorrDocRefId>{original.doc_ref_id}</stf:CorrDocRefId>" in package.xml_content
        assert "<stf:DocTypeIndic>OECD0</stf:DocTypeIndic>" in package.xml_content  # resent ReportingFI
        xmlschema.XMLSchema("exchange/schemas/crs-v2.0/CrsXML_v2.0.xsd").validate(package.xml_content)
        original.refresh_from_db()
        assert original.superseded  # the filed version is now replaced


class TestXmlStyledAmendment:
    """XML-filed returns are amended in XML: the editor opens pre-filled with
    a CRS702 draft of every filed record; manual filings keep the form flow."""

    @pytest.fixture
    def accepted_xml(self, rfi, partners, portal_client):
        from pathlib import Path

        from django.core.files.uploadedfile import SimpleUploadedFile

        raw = (
            Path("portal/tests/samples") / "2026_New_Sample_UAT_CRS_XML_Test1_NG-GB.xml"
        ).read_bytes()
        portal_client.post(
            "/portal/filings/upload/",
            {"crs_file": SimpleUploadedFile("new.xml", raw, content_type="text/xml")},
        )
        filing = Filing.objects.latest("pk")
        filing.status = Filing.Status.ACCEPTED
        filing.save(update_fields=["status"])
        return filing

    def test_detail_routes_xml_filings_to_the_xml_editor(self, portal_client, accepted_xml):
        html = portal_client.get(f"/portal/filings/{accepted_xml.pk}/").content.decode()
        assert f"/portal/filings/{accepted_xml.pk}/correct/xml/" in html
        assert "Create correction filing" not in html  # the form flow is for manual filings

    def test_editor_prefills_a_crs702_draft(self, portal_client, accepted_xml):
        filed = accepted_xml.account_reports.get()
        html = portal_client.get(f"/portal/filings/{accepted_xml.pk}/correct/xml/").content.decode()
        assert "xml-editor" in html
        assert "CRS702" in html
        assert "OECD2" in html
        assert f"&lt;stf:CorrDocRefId&gt;{filed.doc_ref_id}&lt;/stf:CorrDocRefId&gt;" in html
        assert filed.holder_name in html

    def test_submitting_an_edited_draft_files_the_amendment(self, portal_client, accepted_xml):
        from decimal import Decimal

        from exchange.services import correction_xml_draft

        filed = accepted_xml.account_reports.get()
        edited = correction_xml_draft(accepted_xml).replace(str(filed.balance), "99999999.99")
        response = portal_client.post(
            f"/portal/filings/{accepted_xml.pk}/correct/xml/", {"xml": edited}
        )
        amendment = Filing.objects.latest("pk")
        assert response.url == f"/portal/filings/{amendment.pk}/validation/"
        assert amendment.corrects == accepted_xml
        assert amendment.kind == Filing.Kind.XML_UPLOAD
        assert amendment.message_type == Filing.MessageType.CRS702
        assert amendment.status == Filing.Status.ACCEPTED  # auto-accepted on passing validation
        record = amendment.account_reports.get()
        assert record.doc_type_indic == AccountReport.DocTypeIndic.OECD2
        assert record.corr_doc_ref_id == filed.doc_ref_id
        assert record.doc_ref_id != filed.doc_ref_id
        assert record.balance == Decimal("99999999.99")

    def test_flipping_oecd2_to_oecd3_stages_a_deletion(self, portal_client, accepted_xml):
        from exchange.services import correction_xml_draft

        draft = correction_xml_draft(accepted_xml)
        edited = draft.replace(
            "<stf:DocTypeIndic>OECD2</stf:DocTypeIndic>",
            "<stf:DocTypeIndic>OECD3</stf:DocTypeIndic>",
        )
        portal_client.post(f"/portal/filings/{accepted_xml.pk}/correct/xml/", {"xml": edited})
        amendment = Filing.objects.latest("pk")
        assert amendment.account_reports.get().doc_type_indic == AccountReport.DocTypeIndic.OECD3

    def test_dangling_corr_doc_ref_id_is_held_with_r502(self, portal_client, accepted_xml):
        from exchange.services import correction_xml_draft

        filed = accepted_xml.account_reports.get()
        edited = correction_xml_draft(accepted_xml).replace(filed.doc_ref_id, "NG9999-UNKNOWN-1")
        portal_client.post(f"/portal/filings/{accepted_xml.pk}/correct/xml/", {"xml": edited})
        amendment = Filing.objects.latest("pk")
        assert amendment.status == Filing.Status.DRAFT
        assert amendment.findings.filter(code="R-502", severity="ERROR").exists()

    def test_broken_xml_re_renders_the_editor_with_errors(self, portal_client, accepted_xml):
        response = portal_client.post(
            f"/portal/filings/{accepted_xml.pk}/correct/xml/", {"xml": "<crs:CRS_OECD>"}
        )
        assert response.status_code == 200
        assert b"was rejected" in response.content
        assert b"xml-editor" in response.content

    def test_manual_filings_keep_the_form_flow(self, rfi, portal_client, accepted):
        html = portal_client.get(f"/portal/filings/{accepted.pk}/").content.decode()
        assert "Create correction filing" in html
        assert "/correct/xml/" not in html
