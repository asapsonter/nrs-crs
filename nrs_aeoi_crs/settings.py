"""Django settings for the NRS AEOI-CRS demonstration platform.

Demo-grade configuration: SQLite, console email backend, local file storage.
Not intended for production deployment.
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-demo-only-nrs-aeoi-crs-do-not-deploy"

DEBUG = True

ALLOWED_HOSTS = ["*"]

# Trust the domains handed out by the common localhost tunnelling services, so
# the demo can be shared with someone on another network without deploying.
# Forms are CSRF-protected (and session-based), so the tunnel domain must be a
# trusted origin or every login and POST would fail with a 403. Wildcards cover
# the random subdomain each provider mints per run.
CSRF_TRUSTED_ORIGINS = [
    "https://*.trycloudflare.com",   # cloudflared quick tunnel
    "https://*.ngrok-free.app",      # ngrok
    "https://*.ngrok.io",            # ngrok (legacy)
    "https://*.loca.lt",             # localtunnel
    "https://*.serveo.net",          # serveo
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
    "portal",
    "backoffice",
    "exchange",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.SurfaceSessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.CredentialSessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "nrs_aeoi_crs.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.surface_context",
            ],
        },
    },
]

WSGI_APPLICATION = "nrs_aeoi_crs.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

AUTHENTICATION_BACKENDS = [
    "core.auth_backends.IssuedCredentialBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Demo email: rendered to console and captured in the demo outbox page.
EMAIL_BACKEND = "core.email_backend.DemoOutboxEmailBackend"
DEFAULT_FROM_EMAIL = "aeoi-noreply@nrs.gov.ng"

# Surface separation: each surface carries its own session cookie so a
# portal session can never be presented to the backoffice and vice versa.
PORTAL_SESSION_COOKIE_NAME = "nrs_portal_sessionid"
BACKOFFICE_SESSION_COOKIE_NAME = "nrs_backoffice_sessionid"

# Store the CSRF token in the session rather than a shared cookie. Because the
# two surfaces already hold separate, path-scoped session cookies, this gives
# each surface an independent CSRF token: a login on one surface, which rotates
# that surface's token, can no longer invalidate a form open on the other.
CSRF_USE_SESSIONS = True

# Show a friendly, themed recovery page instead of the raw 403 when a form is
# submitted with a stale token (for example after a login rotates it).
CSRF_FAILURE_VIEW = "core.views.csrf_failure"

LOGIN_URL = "/portal/login/"
