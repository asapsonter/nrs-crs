"""Smoke test: each signed-in portal menu page renders without error."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from portal.models import PortalUser, ReportingFI

pytestmark = pytest.mark.django_db

MENU_URLS = [
    "/portal/",
    "/portal/filings/drafts/",
    "/portal/submit/",
    "/portal/filings/",
    "/portal/documents/",
    "/portal/profile/",
    "/portal/me/",
    "/portal/help/",
]


@pytest.fixture
def portal_client() -> Client:
    rfi = ReportingFI.objects.create(
        reference="NRS-RFI-2025-0099",
        legal_name="Meridian Trust Bank Plc",
        tin="0450099901",
        category="DEPOSITORY_INSTITUTION",
        enrolment_type="FINANCIAL_ENTITY",
        status=ReportingFI.Status.ACTIVE,
        pu_surname="Bello",
        pu_first_name="Ngozi",
        pu_designation="Head, Regulatory Reporting",
        pu_email="pu@meridian.ng",
        pu_phone="8000000000",
    )
    email = "pu@meridian.ng"
    user = get_user_model().objects.create_user(username=email, password="Secret123!")
    PortalUser.objects.create(
        user=user,
        rfi=rfi,
        display_name="Ngozi Bello",
        designation="Head, Regulatory Reporting",
        role=PortalUser.Role.CHECKER,
        status=PortalUser.Status.ACTIVE,
        is_primary_user=True,
        must_change_password=False,
    )
    client = Client()
    resp = client.post("/portal/login/", {"email": email, "password": "Secret123!"})
    assert resp.status_code == 302, "login did not succeed"
    return client


@pytest.fixture
def maker_client(portal_client: Client) -> Client:
    """A Maker signed into the same institution as the portal_client fixture."""
    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    email = "maker@meridian.ng"
    user = get_user_model().objects.create_user(username=email, password="Secret123!")
    PortalUser.objects.create(
        user=user,
        rfi=rfi,
        display_name="Emeka Obi",
        designation="Reporting Analyst",
        role=PortalUser.Role.MAKER,
        status=PortalUser.Status.ACTIVE,
        must_change_password=False,
    )
    client = Client()
    resp = client.post("/portal/login/", {"email": email, "password": "Secret123!"})
    assert resp.status_code == 302, "maker login did not succeed"
    return client


@pytest.mark.parametrize("url", MENU_URLS)
def test_menu_page_renders(portal_client: Client, url: str) -> None:
    response = portal_client.get(url)
    assert response.status_code == 200, f"{url} returned {response.status_code}"


def test_shell_chrome_present(portal_client: Client) -> None:
    """The signed-in shell renders the grouped menu and topbar avatar chip."""
    html = portal_client.get("/portal/").content.decode()
    for marker in ["pt-menu-group", "pt-avatar", "pt-topbar-eyebrow", "Workspace"]:
        assert marker in html, f"missing shell marker {marker}"


def test_manage_filings_submenu_and_role_badge(portal_client: Client) -> None:
    """Manage Filings carries Create/Delete submenu; the header shows a role badge."""
    html = portal_client.get("/portal/").content.decode()
    assert "pt-submenu" in html
    assert "Create filing" in html and "Delete filing" in html
    assert "pt-role-badge" in html
    assert "Primary User" in html


def _make_filing(rfi, status):
    from portal.models import Filing

    return Filing.objects.create(
        reference=f"FIL-2024-{Filing.objects.count() + 90000}",
        rfi=rfi,
        reporting_year=2024,
        kind=Filing.Kind.MANUAL,
        status=status,
    )


def test_delete_mode_shows_controls(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    _make_filing(rfi, Filing.Status.DRAFT)
    html = portal_client.get("/portal/filings/?mode=delete").content.decode()
    assert "Delete mode" in html
    assert "link-danger" in html


def test_draft_filing_can_be_deleted(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = _make_filing(rfi, Filing.Status.DRAFT)
    resp = portal_client.post(f"/portal/filings/{filing.pk}/delete/")
    assert resp.status_code == 302
    assert not Filing.objects.filter(pk=filing.pk).exists()


def test_submitted_filing_cannot_be_deleted(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = _make_filing(rfi, Filing.Status.SUBMITTED)
    portal_client.post(f"/portal/filings/{filing.pk}/delete/")
    assert Filing.objects.filter(pk=filing.pk).exists()


def test_create_filing_page_lists_all_types(portal_client: Client) -> None:
    html = portal_client.get("/portal/filings/create/").content.decode()
    assert "Create Filing" in html
    for label in [
        "CRS Manual Entry Filing",
        "CRS XML upload Filing",
        "CRS Excel Upload Filing",
        "Primary User Change Notice",
        "Reporting Entity Deactivation",
        "Change of reporting entity information",
    ]:
        assert label in html, f"missing type {label}"
    assert 'name="period_end_date"' in html


def test_create_manual_filing_as_maker(maker_client: Client) -> None:
    from portal.models import Filing

    resp = maker_client.post(
        "/portal/filings/create/",
        {"name": "CRS annual return 2024", "filing_type": "MANUAL", "period_end_date": "2024-12-31"},
    )
    assert resp.status_code == 302
    filing = Filing.objects.get(name="CRS annual return 2024")
    assert filing.kind == Filing.Kind.MANUAL
    assert str(filing.period_end_date) == "2024-12-31"
    assert resp.url == f"/portal/filings/{filing.pk}/created/"


def test_create_notice_as_checker(portal_client: Client) -> None:
    from portal.models import Filing

    resp = portal_client.post(
        "/portal/filings/create/",
        {"name": "PU change for new head of tax", "filing_type": "PU_CHANGE", "period_end_date": "2024-12-31"},
    )
    assert resp.status_code == 302
    filing = Filing.objects.get(kind=Filing.Kind.PU_CHANGE, name="PU change for new head of tax")
    assert resp.url == f"/portal/filings/{filing.pk}/created/"


def test_created_screen_shows_name_and_reference(portal_client: Client) -> None:
    resp = portal_client.post(
        "/portal/filings/create/",
        {"name": "Deactivation notice", "filing_type": "ENTITY_DEACTIVATION", "period_end_date": "2024-12-31"},
        follow=True,
    )
    html = resp.content.decode()
    assert "New Filing created successfully" in html
    assert "Deactivation notice" in html
    assert "Go to Draft Filings" in html
    # The reference number is shown on the confirmation screen.
    assert "success-ref" in html


def test_primary_user_can_create_and_submit_filing(portal_client: Client, partners) -> None:
    """The Primary User files and submits directly; no maker-checker step."""
    from portal.models import Filing

    resp = portal_client.post(
        "/portal/filings/create/",
        {"name": "PU direct filing", "filing_type": "MANUAL", "period_end_date": "2024-12-31"},
    )
    assert resp.status_code == 302
    filing = Filing.objects.get(name="PU direct filing")
    # Add a complete record, then submit straight to the NRS.
    _crs_record(filing, doc_ref_id="NG2024-PU-000001", account_number="ACC-PU1",
                holder_type="INDIVIDUAL", acct_holder_type="")
    resp = portal_client.post(f"/portal/filings/{filing.pk}/stage/")
    assert resp.status_code == 302
    filing.refresh_from_db()
    # Submission auto-validates against the CRS schema on arrival.
    assert filing.status == Filing.Status.ACCEPTED  # auto-accepted on passing validation
    assert filing.validated_at is not None


def test_create_xml_carries_metadata_to_upload(maker_client: Client) -> None:
    resp = maker_client.post(
        "/portal/filings/create/",
        {"name": "XML batch A", "filing_type": "XML_UPLOAD", "period_end_date": "2024-12-31"},
    )
    assert resp.status_code == 302
    assert resp.url == "/portal/filings/upload/"


def test_submissions_lists_submitted_filings(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    Filing.objects.create(
        reference="FIL-2024-96001", name="Submitted return A", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.SUBMITTED,
    )
    # A draft should not appear in the submitted list.
    Filing.objects.create(
        reference="FIL-2024-96002", name="Still a draft", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    html = portal_client.get("/portal/submit/").content.decode()
    assert "Submitted return A" in html
    assert "Still a draft" not in html
    # The creation tiles have been removed; Submissions is view-only.
    assert "Upload CRS XML" not in html
    assert "Start manual entry" not in html


def test_users_page_ok_for_checker(portal_client: Client) -> None:
    resp = portal_client.get("/portal/users/")
    assert resp.status_code == 200


def test_invite_composes_name_from_three_fields(portal_client: Client) -> None:
    from portal.models import PortalUser

    portal_client.post(
        "/portal/users/",
        {"action": "invite", "surname": "Okafor", "middle_name": "Ngozi",
         "other_names": "Adaeze", "designation": "Analyst",
         "email": "adaeze@meridian.ng", "role": "MAKER"},
    )
    invited = PortalUser.objects.get(user__username="adaeze@meridian.ng")
    assert invited.display_name == "Adaeze Ngozi Okafor"


def test_invite_requires_surname_and_other_names(portal_client: Client) -> None:
    from portal.models import PortalUser

    before = PortalUser.objects.count()
    portal_client.post(
        "/portal/users/",
        {"action": "invite", "surname": "", "other_names": "OnlyOther",
         "email": "half@meridian.ng", "role": "MAKER"},
    )
    assert PortalUser.objects.count() == before


def test_officer_create_composes_name(portal_client: Client) -> None:
    from django.contrib.auth import get_user_model
    from django.test import Client as DjangoClient
    from core.models import OfficerProfile

    get_user_model().objects.create_superuser(username="root2", password="RootPass1!")
    client = DjangoClient()
    client.post("/backoffice/login/", {"username": "root2", "passcode": "RootPass1!"})
    client.post(
        "/backoffice/admin-console/officers/",
        {"surname": "Adewale", "middle_name": "Kunle", "other_names": "Tomi",
         "email": "tomi.adewale@nrs.gov.ng", "phone": "+234 801 234 5678",
         "roles": ["INTERNAL_ADMIN"]},
    )
    officer = OfficerProfile.objects.get(email="tomi.adewale@nrs.gov.ng")
    assert officer.name == "Tomi Kunle Adewale"
    assert officer.phone == "+234 801 234 5678"


def test_cp_add_composes_name_from_three_fields(maker_client: Client) -> None:
    from portal.models import ControllingPerson, Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98090", name="CP name filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    record = _crs_record(filing, doc_ref_id="NG2024-NM-000001")
    maker_client.post(
        f"/portal/filings/{filing.pk}/records/{record.pk}/cp/add/",
        {"cp_surname": "Eze", "cp_middle_name": "Obi", "cp_other_names": "Chika",
         "cp_residence": "GB", "cp_type": "CRS801"},
    )
    cp = ControllingPerson.objects.get(account_report=record)
    assert cp.name == "Chika Obi Eze"


def test_users_page_redirects_maker_not_404(maker_client: Client) -> None:
    resp = maker_client.get("/portal/users/")
    assert resp.status_code == 302
    assert resp.url == "/portal/"


def test_draft_list_shows_new_columns(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-95001",
        name="CRS annual return 2024",
        rfi=rfi,
        reporting_year=2024,
        kind=Filing.Kind.MANUAL,
        status=Filing.Status.DRAFT,
    )
    html = portal_client.get("/portal/filings/drafts/").content.decode()
    for header in ["Filing Name", "Revision", "Category", "Receiving Country", "Due Date"]:
        assert header in html, f"missing column {header}"
    assert "CRS annual return 2024" in html
    assert f"/portal/filings/{filing.pk}/view/" in html  # name opens the Form View, as in Vizor
    assert "Waiting" in html
    assert "No Data" in html


def test_crs_form_renders_general_information(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-95002", name="Batch A", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    html = portal_client.get(f"/portal/filings/{filing.pk}/crs/").content.decode()
    assert "CRS Filing" in html
    assert "General Information" in html
    for field in ["receiving_country", "sending_company_in", "message_type", "message_reference"]:
        assert f'name="{field}"' in html
    assert "Save as draft" in html and "Validate &amp; Save" in html
    # Sending Company IN defaults to the entity TIN.
    assert rfi.tin in html


def test_crs_form_save_as_draft(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-95003", name="Batch B", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    resp = portal_client.post(
        f"/portal/filings/{filing.pk}/crs/",
        {"action": "save", "receiving_country": "GB", "sending_company_in": "TIN123",
         "message_type": "CRS701", "message_reference": ""},
    )
    assert resp.status_code == 302
    assert resp.url == "/portal/filings/drafts/"
    filing.refresh_from_db()
    assert filing.receiving_country == "GB"
    assert filing.sending_company_in == "TIN123"


def test_crs_form_validate_requires_fields(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-95004", name="Batch C", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    # Missing receiving country and reference: validate should fail and stay.
    resp = portal_client.post(
        f"/portal/filings/{filing.pk}/crs/",
        {"action": "validate", "receiving_country": "", "sending_company_in": "TIN123",
         "message_type": "CRS701", "message_reference": ""},
    )
    assert resp.status_code == 200
    assert "Select a receiving country." in resp.content.decode()

    # Complete header: validate succeeds and moves to the filing.
    resp = portal_client.post(
        f"/portal/filings/{filing.pk}/crs/",
        {"action": "validate", "receiving_country": "GB", "sending_company_in": "TIN123",
         "message_type": "CRS701", "message_reference": "NG2024-001"},
    )
    assert resp.status_code == 302
    assert resp.url == f"/portal/filings/{filing.pk}/view/"


def test_filing_view_tree_renders(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-97001", name="Tree filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    html = portal_client.get(f"/portal/filings/{filing.pk}/view/").content.decode()
    assert "View Filing" in html
    assert "Please select a form to view" in html
    # Legend / key
    for legend in ["Form Set", "Repeatable Folder", "Add section", "Validated", "No Data", "Mandatory"]:
        assert legend in html, f"missing legend {legend}"
    # Tree nodes and codes
    for node in ["General Information", "CRS Report", "Reporting FI Information", "Account Information"]:
        assert node in html, f"missing node {node}"
    for code in [">GI<", ">CRS<", ">RFI<", ">AI<"]:
        assert code in html, f"missing code chip {code}"
    assert "View comments" in html


def _make_record(filing, i=1):
    from portal.models import AccountReport

    return AccountReport.objects.create(
        filing=filing, doc_ref_id=f"DR-{filing.pk}-{i}",
        holder_name=f"Holder {i}", residence_country="GB", account_number=f"ACC{i}",
    )


def test_crs_form_read_only_view(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98010", name="RO filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT, receiving_country="GB",
        sending_company_in="TIN9", message_reference="REF9",
    )
    html = portal_client.get(f"/portal/filings/{filing.pk}/crs/?mode=view").content.decode()
    assert "read only" in html.lower()
    assert "readonly" in html  # inputs disabled
    assert ">Edit<" in html


def test_gi_clear_action(maker_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98011", name="Clear GI", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT, receiving_country="GB",
        sending_company_in="TIN9", message_reference="REF9",
    )
    resp = maker_client.post(f"/portal/filings/{filing.pk}/crs/", {"action": "clear"})
    assert resp.status_code == 302
    filing.refresh_from_db()
    assert filing.receiving_country == "" and filing.sending_company_in == "" and filing.message_reference == ""


def test_record_delete_action(maker_client: Client) -> None:
    from portal.models import AccountReport, Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98012", name="Del rec", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    record = _make_record(filing)
    resp = maker_client.post(f"/portal/filings/{filing.pk}/records/{record.pk}/delete/")
    assert resp.status_code == 302
    assert not AccountReport.objects.filter(pk=record.pk).exists()


def test_records_clear_action(maker_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98013", name="Clear recs", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    _make_record(filing, 1)
    _make_record(filing, 2)
    resp = maker_client.post(f"/portal/filings/{filing.pk}/records/clear/")
    assert resp.status_code == 302
    assert filing.account_reports.count() == 0


def test_tree_shows_editable_actions_and_records_for_maker(maker_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98014", name="Maker tree", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    _make_record(filing, 1)
    html = maker_client.get(f"/portal/filings/{filing.pk}/view/").content.decode()
    assert ">Edit<" in html and ">Add Section<" in html and ">Delete<" in html
    assert "Holder 1" in html  # account record listed under AI
    assert "/records/" in html
    # RFI node has both Edit and View; CRS repeatable has Delete All.
    assert "/portal/profile/?edit=1" in html
    assert ">Delete All<" in html


def test_record_add_requires_currency(maker_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98020", name="Cur filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    base = {"holder_name": "Jane Doe", "holder_type": "INDIVIDUAL",
            "residence_country": "GB", "account_number": "ACC-1", "balance": "1000",
            "holder_address": "5 Oak Ave, London", "self_certification": "OBTAINED"}
    # No currency selected: the record is not created.
    maker_client.post(f"/portal/filings/{filing.pk}/records/add/", base)
    assert filing.account_reports.count() == 0
    # Currency selected: the record is created with that currency.
    maker_client.post(f"/portal/filings/{filing.pk}/records/add/", {**base, "currency": "USD"})
    record = filing.account_reports.get()
    assert record.currency == "USD"


def test_record_add_requires_address(maker_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98030", name="Addr filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    # Address is mandatory in CRS: without it, no record is created.
    maker_client.post(f"/portal/filings/{filing.pk}/records/add/", {
        "holder_name": "No Address", "holder_type": "INDIVIDUAL", "residence_country": "GB",
        "account_number": "ACC-9", "currency": "USD", "self_certification": "OBTAINED"})
    assert filing.account_reports.count() == 0


def _crs_record(filing, **kw):
    from portal.models import AccountReport
    defaults = dict(
        doc_ref_id=f"NG2024-{filing.rfi.reference}-{AccountReport.objects.count()+1:06d}",
        holder_name="Acme Ltd", holder_type="ORGANISATION", acct_holder_type="CRS101",
        residence_country="GB", foreign_tin="GB123456", holder_address="1 Market St, London",
        self_certification="OBTAINED", account_number="ACC-100", currency="USD",
    )
    defaults.update(kw)
    return AccountReport.objects.create(filing=filing, **defaults)


def test_generated_xml_includes_crs_holder_structure(maker_client: Client) -> None:
    """D1/C2/D4: outbound XML carries Address, entity type, controlling person,
    and the RFI's captured Sending Company IN and the CA sending identity."""
    from portal.models import ControllingPerson, Filing, ReportingFI
    from exchange.models import PartnerJurisdiction
    from exchange.services import generate_crs_xml, build_packages
    from core import config

    for code, name, since, fp in config.PARTNER_JURISDICTIONS:
        PartnerJurisdiction.objects.get_or_create(
            code=code, defaults={"name": name, "activated_since": since, "key_fingerprint": fp})

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98040", name="XML filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.ACCEPTED, sending_company_in="RFI-IN-777",
    )
    record = _crs_record(filing)
    ControllingPerson.objects.create(
        account_report=record, name="John Owner", residence_country="GB", tin="GB999",
        ctrlg_person_type="CRS801")

    packages = build_packages(2024)
    xml = packages[0].xml_content
    assert "<crs:Address>" in xml
    assert "<cfc:AddressFree>1 Market St, London</cfc:AddressFree>" in xml
    # AcctHolderType is a sibling element of Organisation, not an attribute.
    assert "<crs:AcctHolderType>CRS101</crs:AcctHolderType>" in xml
    assert "crsAcctHolderType" not in xml
    assert "<crs:ControllingPerson>" in xml
    assert "<crs:CtrlgPersonType>CRS801</crs:CtrlgPersonType>" in xml
    assert "John Owner" in xml
    # D4: RFI Sending Company IN and the CA-level sending identity both appear.
    assert "RFI-IN-777" in xml
    assert config.NRS_SENDING_COMPANY_IN in xml
    assert "<crs:Contact>" in xml


def test_tree_shows_cp_folder_and_record_status(maker_client: Client) -> None:
    """The folder tree surfaces the CRS data model: per-record Validated or
    In Draft status, self-certification flags, and a Controlling Persons
    sub-folder for Passive NFE (CRS101) records."""
    from portal.models import ControllingPerson, Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98060", name="Combined tree", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    # A complete CRS101 record with a controlling person.
    complete = _crs_record(filing, doc_ref_id="NG2024-CB-000001", account_number="ACC-A")
    ControllingPerson.objects.create(
        account_report=complete, name="Grace Holder", residence_country="GB",
        ctrlg_person_type="CRS803")
    # An incomplete individual record (no address).
    _crs_record(filing, doc_ref_id="NG2024-CB-000002", account_number="ACC-B",
                holder_type="INDIVIDUAL", acct_holder_type="", holder_address="",
                holder_name="Tunde Ade")

    html = maker_client.get(f"/portal/filings/{filing.pk}/view/").content.decode()
    # CP sub-folder with the controlling person listed.
    assert "Controlling Persons" in html
    assert "Grace Holder" in html
    assert ">CP<" in html
    # Per-record status chips.
    assert ">Validated<" in html and ">In Draft<" in html
    # Vizor-style Add Section label and the not-ready submit panel.
    assert "Add Section" in html
    assert "Not yet ready to submit" in html


def test_tree_ready_to_submit_flow(maker_client: Client, partners) -> None:
    """When GI is validated and every account form is complete, the tree
    shows Ready to Submit and Validate & Submit stages the filing."""
    from portal.models import ControllingPerson, Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98061", name="Ready tree", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
        receiving_country="GB", sending_company_in="TIN1", message_reference="MR-1",
    )
    record = _crs_record(filing, doc_ref_id="NG2024-RD-000001", account_number="ACC-R")
    ControllingPerson.objects.create(
        account_report=record, name="Owner One", residence_country="GB",
        ctrlg_person_type="CRS801")

    html = maker_client.get(f"/portal/filings/{filing.pk}/view/").content.decode()
    assert "Ready to Submit" in html
    assert "Validate &amp; Submit" in html

    resp = maker_client.post(f"/portal/filings/{filing.pk}/stage/")
    assert resp.status_code == 302
    filing.refresh_from_db()
    # No maker-checker step: submission goes straight to the NRS, where it is
    # auto-validated against the CRS schema.
    assert filing.status == Filing.Status.ACCEPTED  # auto-accepted on passing validation
    assert filing.submitted_at is not None
    assert filing.validated_at is not None


CSV_HEADER = (
    "HolderName,HolderType,AcctHolderType,ResidenceCountry,TIN,TINMissingReason,"
    "Address,AddressCountry,BirthDate,SelfCertification,AccountNumber,Currency,"
    "Balance,CPName,CPResidence,CPTIN,CPType"
)


def test_create_excel_filing_redirects_to_excel_upload(maker_client: Client) -> None:
    resp = maker_client.post(
        "/portal/filings/create/",
        {"name": "Excel batch", "filing_type": "EXCEL_UPLOAD", "period_end_date": "2024-12-31"},
    )
    assert resp.status_code == 302
    assert resp.url == "/portal/filings/upload/excel/"
    page = maker_client.get("/portal/filings/upload/excel/").content.decode()
    assert "CRS Excel Upload Filing" in page
    assert "Template columns" in page
    assert "HolderName" in page


def test_excel_csv_upload_creates_records_and_cp(maker_client: Client) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile
    from portal.models import Filing

    rows = [
        CSV_HEADER,
        "Jane Doe,INDIVIDUAL,,GB,QQ123456C,,1 Oak Ave London,GB,1980-02-01,OBTAINED,ACC-1,GBP,1500.50,,,,",
        "Acme Ltd,ORGANISATION,CRS101,FR,,No IN issued,9 Rue Cler Paris,FR,,OBTAINED,ACC-2,EUR,90000,Marc Owner,FR,FR-TIN-1,CRS801",
    ]
    upload = SimpleUploadedFile("crs_batch.csv", "\n".join(rows).encode("utf-8"), content_type="text/csv")
    resp = maker_client.post("/portal/filings/upload/excel/", {"crs_file": upload})
    assert resp.status_code == 302, resp.content
    filing = Filing.objects.get(kind=Filing.Kind.EXCEL_UPLOAD)
    assert filing.account_reports.count() == 2
    jane = filing.account_reports.get(account_number="ACC-1")
    assert jane.holder_address == "1 Oak Ave London"
    assert str(jane.birth_date) == "1980-02-01"
    assert jane.currency == "GBP"
    acme = filing.account_reports.get(account_number="ACC-2")
    assert acme.acct_holder_type == "CRS101"
    cp = acme.controlling_persons.get()
    assert cp.name == "Marc Owner" and cp.ctrlg_person_type == "CRS801"


def test_excel_upload_rejects_bad_rows(maker_client: Client) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile
    from portal.models import Filing

    before = Filing.objects.count()
    rows = [
        CSV_HEADER,
        # Missing address; CP on a non-CRS101 row.
        "Bad Row,INDIVIDUAL,,GB,,,,GB,,OBTAINED,ACC-9,GBP,10,Someone,GB,,CRS801",
    ]
    upload = SimpleUploadedFile("bad.csv", "\n".join(rows).encode("utf-8"), content_type="text/csv")
    resp = maker_client.post("/portal/filings/upload/excel/", {"crs_file": upload})
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Address is required" in html
    assert "controlling persons are only reported" in html
    assert Filing.objects.count() == before


def test_excel_xlsx_upload_creates_records(maker_client: Client) -> None:
    import io
    from django.core.files.uploadedfile import SimpleUploadedFile
    from openpyxl import Workbook
    from portal.models import Filing

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(CSV_HEADER.split(","))
    sheet.append(["Chidi Obi", "INDIVIDUAL", "", "GB", "QQ999", "", "4 Long St London", "GB",
                  "1975-05-05", "OBTAINED", "ACC-X1", "GBP", "2500", "", "", "", ""])
    buffer = io.BytesIO()
    workbook.save(buffer)
    upload = SimpleUploadedFile(
        "batch.xlsx", buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp = maker_client.post("/portal/filings/upload/excel/", {"crs_file": upload})
    assert resp.status_code == 302, resp.content
    filing = Filing.objects.filter(kind=Filing.Kind.EXCEL_UPLOAD).latest("created_at")
    record = filing.account_reports.get()
    assert record.holder_name == "Chidi Obi"
    assert record.holder_address == "4 Long St London"


def test_record_forms_carry_conditional_rules(maker_client: Client) -> None:
    """Both record forms load the holder-type rules script that blurs fields
    not applicable to the selected option (individual vs entity)."""
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98070", name="Rules filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    add_form = maker_client.get(f"/portal/filings/{filing.pk}/").content.decode()
    assert "Conditional field rules" in add_form
    assert 'id="acct_holder_type"' in add_form and 'id="birth_date"' in add_form

    record = _crs_record(filing, doc_ref_id="NG2024-RL-000001")
    edit_form = maker_client.get(f"/portal/filings/{filing.pk}/records/{record.pk}/").content.decode()
    assert "Conditional field rules" in edit_form
    assert 'id="cp-disabled-note"' in edit_form


def test_cp_add_blocked_for_non_crs101(maker_client: Client) -> None:
    from portal.models import ControllingPerson, Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98071", name="CP guard", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    record = _crs_record(filing, doc_ref_id="NG2024-GD-000001", acct_holder_type="CRS102")
    maker_client.post(
        f"/portal/filings/{filing.pk}/records/{record.pk}/cp/add/",
        {"cp_name": "Should Not Exist", "cp_residence": "GB", "cp_type": "CRS801"},
    )
    assert not ControllingPerson.objects.filter(account_report=record).exists()


def _officer_client(name="Sup Officer", email="sup@nrs.gov.ng"):
    from django.test import Client as DjangoClient
    from core.models import IssuedCredential, OfficerProfile, Roles

    officer = OfficerProfile.objects.create(name=name, roles=Roles.INTERNAL_ADMIN, email=email)
    credential, passcode = IssuedCredential.objects.issue(officer, 2.0)
    client = DjangoClient()
    client.post("/backoffice/login/", {"username": credential.username, "passcode": passcode})
    return client


def test_late_filing_penalty_schedule() -> None:
    """N10m for the first month of default, N1m each further month."""
    import datetime
    from exchange.services import late_filing_penalty

    deadline = datetime.date(2026, 5, 31)
    assert late_filing_penalty(deadline, datetime.date(2026, 5, 31)) == 0
    assert late_filing_penalty(deadline, datetime.date(2026, 6, 15)) == 10_000_000
    assert late_filing_penalty(deadline, datetime.date(2026, 7, 15)) == 11_000_000
    assert late_filing_penalty(deadline, datetime.date(2026, 10, 1)) == 14_000_000


def test_officer_signs_in_with_registered_email(portal_client: Client) -> None:
    """Credentials authenticate with the officer's registered email."""
    from django.test import Client as DjangoClient
    from core.models import IssuedCredential, OfficerProfile, Roles

    officer = OfficerProfile.objects.create(
        name="Email Login", roles=Roles.INTERNAL_ADMIN, email="email.login@nrs.gov.ng")
    credential, passcode = IssuedCredential.objects.issue(officer, 2.0)
    client = DjangoClient()
    resp = client.post("/backoffice/login/", {"username": "email.login@nrs.gov.ng", "passcode": passcode})
    assert resp.status_code == 302
    html = client.get("/backoffice/").content.decode()
    assert "Email Login" in html


def test_balance_shown_with_thousand_separators(maker_client: Client) -> None:
    from decimal import Decimal
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98100", name="Comma filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    _crs_record(filing, doc_ref_id="NG2024-CM-000001", account_number="ACC-CM1",
                holder_type="INDIVIDUAL", acct_holder_type="")
    record = filing.account_reports.get()
    record.balance = Decimal("1000000.00")
    record.save(update_fields=["balance"])
    detail = maker_client.get(f"/portal/filings/{filing.pk}/").content.decode()
    assert "1,000,000" in detail
    tree = maker_client.get(f"/portal/filings/{filing.pk}/view/").content.decode()
    assert "1,000,000" in tree
    # The bare Account header is now Account Number.
    assert "<th>Account Number</th>" in detail


def test_backoffice_institutions_register(portal_client: Client) -> None:
    client = _officer_client(email="fi.reg@nrs.gov.ng")
    html = client.get("/backoffice/institutions/").content.decode()
    assert "Financial Institutions" in html
    assert "Meridian Trust Bank Plc" in html
    assert "Portal users" in html
    # Sidebar shows the Vizor-style module structure.
    assert "FI Management" in html
    assert "Exchange &middot; CTS" in html or "Exchange · CTS" in html
    assert "AEOI Analytics" in html


def test_backoffice_compliance_monitor(portal_client: Client) -> None:
    from portal.models import Filing, ReportingFI
    from core import config

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    # A submitted data return for the current year: this FI reads as filed.
    Filing.objects.create(
        reference="FIL-CMP-0001", name="Filed return", rfi=rfi,
        reporting_year=config.CURRENT_REPORTING_YEAR,
        kind=Filing.Kind.MANUAL, status=Filing.Status.SUBMITTED,
    )
    # A second operational FI with no filings: reads as not filed.
    ReportingFI.objects.create(
        reference="NRS-RFI-2025-0777", legal_name="Dormant Capital Ltd", tin="0450077701",
        category="INVESTMENT_ENTITY", enrolment_type="FINANCIAL_ENTITY",
        status=ReportingFI.Status.ACTIVE, pu_designation="MD", pu_email="md@dormant.ng",
        pu_phone="8000000001",
    )
    client = _officer_client(email="comp@nrs.gov.ng")
    html = client.get("/backoffice/compliance/").content.decode()
    assert "Compliance Monitor" in html
    assert "Data return filed" in html
    assert "Not filed" in html
    assert "Dormant Capital Ltd" in html
    # Deadline for the demo year has passed: penalty exposure appears.
    assert "&#8358;" in html or "₦" in html


def test_backoffice_returns_detail_shows_filing_structure(portal_client: Client) -> None:
    """The Supervision Centre returns page shows the filing structure tree
    with GI / RFI / AI / CP nodes and the new CRS due-diligence columns."""
    from django.test import Client as DjangoClient
    from core.models import IssuedCredential, OfficerProfile, Roles
    from portal.models import ControllingPerson, Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98080", name="BO tree", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.SUBMITTED,
        receiving_country="GB", sending_company_in="TIN-1", message_reference="MR-9",
    )
    record = _crs_record(filing, doc_ref_id="NG2024-BO-000001")
    ControllingPerson.objects.create(
        account_report=record, name="Bola Owner", residence_country="GB",
        ctrlg_person_type="CRS803")

    officer = OfficerProfile.objects.create(
        name="Returns Officer", roles=Roles.INTERNAL_ADMIN, email="returns@nrs.gov.ng")
    credential, passcode = IssuedCredential.objects.issue(officer, 2.0)
    client = DjangoClient()
    client.post("/backoffice/login/", {"username": credential.username, "passcode": passcode})

    html = client.get(f"/backoffice/returns/{filing.pk}/").content.decode()
    assert "Filing structure" in html
    assert "General Information" in html and "Reporting FI Information" in html
    assert "Bola Owner" in html          # CP node in the tree
    assert ">CP<" in html
    assert "Self-cert" in html           # new account-report column
    assert "Obtained" in html


def test_controlling_person_add_and_delete(maker_client: Client) -> None:
    from portal.models import ControllingPerson, Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98050", name="CP filing", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    record = _crs_record(filing, doc_ref_id="NG2024-CP-000001")
    resp = maker_client.post(
        f"/portal/filings/{filing.pk}/records/{record.pk}/cp/add/",
        {"cp_name": "Ada Owner", "cp_residence": "GB", "cp_type": "CRS801", "cp_tin": "GB55"},
    )
    assert resp.status_code == 302
    cp = ControllingPerson.objects.get(account_report=record)
    assert cp.name == "Ada Owner"
    resp = maker_client.post(
        f"/portal/filings/{filing.pk}/records/{record.pk}/cp/{cp.pk}/delete/")
    assert resp.status_code == 302
    assert not ControllingPerson.objects.filter(pk=cp.pk).exists()


def test_record_form_has_currency_select_no_default(maker_client: Client) -> None:
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-98021", name="Cur form", rfi=rfi, reporting_year=2024,
        kind=Filing.Kind.MANUAL, status=Filing.Status.DRAFT,
    )
    html = maker_client.get(f"/portal/filings/{filing.pk}/").content.decode()
    assert 'name="currency"' in html
    assert "Select currency" in html
    # No currency option is pre-selected on a new record form.
    assert "selected" not in html.split('name="currency"')[1].split("</select>")[0]


def test_entity_profile_edit_saves_contact(portal_client: Client) -> None:
    from portal.models import ReportingFI

    form = portal_client.get("/portal/profile/?edit=1").content.decode()
    assert 'name="email"' in form and "Save contact details" in form
    resp = portal_client.post(
        "/portal/profile/",
        {"email": "newdesk@meridian.ng", "phone_cc": "+234", "phone": "8011112222",
         "street": "12 Marina", "city": "Lagos", "state_province": "Lagos", "post_code": "101001"},
    )
    assert resp.status_code == 302
    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    assert rfi.email == "newdesk@meridian.ng"
    assert rfi.city == "Lagos"
    # Identity fields are untouched.
    assert rfi.legal_name == "Meridian Trust Bank Plc"


def test_create_requires_name_and_period(portal_client: Client) -> None:
    from portal.models import Filing

    before = Filing.objects.count()
    resp = portal_client.post(
        "/portal/filings/create/",
        {"name": "", "filing_type": "PU_CHANGE", "period_end_date": ""},
    )
    assert resp.status_code == 200
    assert Filing.objects.count() == before


def test_filing_detail_meta_strip(portal_client: Client) -> None:
    """A filing detail page renders the meta strip of key facts."""
    from portal.models import Filing, ReportingFI

    rfi = ReportingFI.objects.get(reference="NRS-RFI-2025-0099")
    filing = Filing.objects.create(
        reference="FIL-2024-90001",
        rfi=rfi,
        reporting_year=2024,
        kind=Filing.Kind.MANUAL,
        status=Filing.Status.DRAFT,
    )
    html = portal_client.get(f"/portal/filings/{filing.pk}/").content.decode()
    assert "filing-meta" in html
    assert "Reporting year" in html
    assert "Prepared by" in html
