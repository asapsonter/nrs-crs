"""RFI Portal URL routes."""
from django.urls import path

from portal import views, views_enrol, views_filing, views_menu

urlpatterns = [
    path("filings/drafts/", views_menu.filings_drafts),
    path("submit/", views_menu.submit),
    path("documents/", views_menu.documents),
    path("profile/", views_menu.entity_profile),
    path("me/", views_menu.my_details),
    path("help/", views_menu.help_page),
    path("filings/", views_filing.filings),
    path("filings/new/", views_filing.filing_new_manual),
    path("filings/upload/", views_filing.filing_upload),
    path("filings/nil/", views_filing.filing_nil),
    path("filings/<int:filing_id>/", views_filing.filing_detail),
    path("filings/<int:filing_id>/records/add/", views_filing.record_add),
    path("filings/<int:filing_id>/records/<int:record_id>/", views_filing.record_edit),
    path("filings/<int:filing_id>/stage/", views_filing.filing_stage),
    path("filings/<int:filing_id>/check/", views_filing.filing_check),
    path("filings/<int:filing_id>/resubmit/", views_filing.filing_resubmit),
    path("enrol/", views_enrol.enrol),
    path("enrol/status/", views_enrol.enrol_status),
    path("login/", views.login_view),
    path("logout/", views.logout_view),
    path("change-password/", views.change_password),
    path("users/", views.users),
    path("", views.home),
]
