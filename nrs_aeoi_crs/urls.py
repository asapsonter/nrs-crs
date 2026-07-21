"""URL configuration. Two hard-separated surfaces plus a small demo landing."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from core import views as core_views

urlpatterns = [
    path("", core_views.landing, name="landing"),
    path("demo/outbox/", core_views.demo_outbox, name="demo_outbox"),
    path("portal/", include("portal.urls")),
    path("backoffice/", include("backoffice.urls")),
    path("django-admin/", admin.site.urls),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
