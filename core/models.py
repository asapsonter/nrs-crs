"""Shared models: backoffice officers, issued credentials, and the audit log."""
from __future__ import annotations

import secrets
import string

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone


class Roles(models.TextChoices):
    """Backoffice CRS internal user roles.

    Super Admin is a standing administrative account that manages access but
    does not process casework. The other four are internal access tiers a
    Super Admin assigns to a user, and a user may hold more than one.
    """

    SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
    INTERNAL_ADMIN = "INTERNAL_ADMIN", "Internal Admin User"
    ASSISTANT_ADMIN = "ASSISTANT_ADMIN", "Assistant Admin"
    EXPORT_ONLY = "EXPORT_ONLY", "Export Only User"
    VIEW_ONLY = "VIEW_ONLY", "View Only User"


# Roles a Super Admin may assign to an internal user (everything but the
# standing Super Admin account itself).
ASSIGNABLE_ROLES: list[str] = [
    Roles.INTERNAL_ADMIN,
    Roles.ASSISTANT_ADMIN,
    Roles.EXPORT_ONLY,
    Roles.VIEW_ONLY,
]


def roles_to_labels(codes) -> list[str]:
    """Map a list of role codes to their human labels, skipping unknowns."""
    labels = []
    for code in codes:
        try:
            labels.append(Roles(code).label)
        except ValueError:
            continue
    return labels


class RoleListMixin:
    """Shared helpers for models that store roles as a comma-separated list."""

    roles: str

    @property
    def role_list(self) -> list[str]:
        return [code for code in self.roles.split(",") if code]

    @property
    def role_labels(self) -> list[str]:
        return roles_to_labels(self.role_list)

    @property
    def role_display(self) -> str:
        return ", ".join(self.role_labels)


class OfficerProfile(RoleListMixin, models.Model):
    """An NRS internal user eligible to receive issued credentials.

    Users do not hold standing passwords. Access to the Supervision Centre is
    only through single-use credentials issued by the Super Admin. A user may
    be assigned several roles; their access is the union of those roles.
    """

    name = models.CharField(max_length=120)
    roles = models.CharField("Assigned roles", max_length=200, blank=True, default="")
    email = models.EmailField(unique=True)
    phone = models.CharField("Phone number", max_length=30, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.role_display})"


# Unambiguous alphabet for issued credentials: excludes characters that are
# easily confused when read from a screen and typed by hand (0/O, 1/I/L,
# 2/Z, 5/S, 8/B). This keeps manual entry of a credential reliable.
_CREDENTIAL_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"


def _random_block(length: int = 5) -> str:
    return "".join(secrets.choice(_CREDENTIAL_ALPHABET) for _ in range(length))


class IssuedCredentialManager(models.Manager):
    def issue(
        self,
        officer: OfficerProfile,
        session_hours: float,
    ) -> tuple["IssuedCredential", str]:
        """Create a credential and return it with the cleartext passcode.

        The credential inherits the user's assigned roles as a snapshot. The
        passcode is shown to the Super Admin exactly once at creation and
        stored hashed thereafter.
        """
        from core import config

        username = f"nrs-usr-{_random_block()}"
        while self.filter(username=username).exists():
            username = f"nrs-usr-{_random_block()}"
        passcode = "-".join(_random_block(4) for _ in range(3))
        credential = self.create(
            officer=officer,
            roles=officer.roles,
            username=username,
            passcode_hash=make_password(passcode),
            session_hours=session_hours,
            expires_if_unused_at=timezone.now()
            + timezone.timedelta(hours=config.CREDENTIAL_UNUSED_VALIDITY_HOURS),
        )
        return credential, passcode


class IssuedCredential(RoleListMixin, models.Model):
    """A single-use, time-boxed credential for the Supervision Centre.

    Lifecycle: ISSUED at creation; ACTIVE at first successful login, when the
    session window is fixed; CONSUMED when that session ends for any reason;
    EXPIRED if never used within the issuance validity window; REVOKED by the
    Super Admin. A credential authenticates exactly one login and carries a
    snapshot of the user's roles.
    """

    class Status(models.TextChoices):
        ISSUED = "ISSUED", "Issued"
        ACTIVE = "ACTIVE", "Active"
        EXPIRED = "EXPIRED", "Expired"
        REVOKED = "REVOKED", "Revoked"
        CONSUMED = "CONSUMED", "Consumed"

    officer = models.ForeignKey(OfficerProfile, on_delete=models.PROTECT, related_name="credentials")
    roles = models.CharField("Roles", max_length=200, blank=True, default="")
    username = models.CharField(max_length=40, unique=True)
    passcode_hash = models.CharField(max_length=200)
    session_hours = models.DecimalField(max_digits=5, decimal_places=2)
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_if_unused_at = models.DateTimeField()
    first_used_at = models.DateTimeField(null=True, blank=True)
    session_expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ISSUED)
    bound_session_key = models.CharField(max_length=64, blank=True, default="")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="issued_credential",
    )

    objects = IssuedCredentialManager()

    class Meta:
        ordering = ["-issued_at"]

    def __str__(self) -> str:
        return f"{self.username} [{self.status}]"

    def check_passcode(self, raw: str) -> bool:
        return check_password(raw, self.passcode_hash)

    @property
    def remaining_seconds(self) -> int:
        if self.status != self.Status.ACTIVE or not self.session_expires_at:
            return 0
        return max(0, int((self.session_expires_at - timezone.now()).total_seconds()))

    def lapse_if_unused(self) -> None:
        """Mark an ISSUED credential EXPIRED once its issuance window passes."""
        if self.status == self.Status.ISSUED and timezone.now() > self.expires_if_unused_at:
            self.status = self.Status.EXPIRED
            self.save(update_fields=["status"])
            AuditLog.record(
                actor_name="system",
                action="CREDENTIAL_EXPIRED_UNUSED",
                target=self.username,
                detail="Credential lapsed unused past its issuance validity window.",
                credential=self,
            )

    def activate(self, session_key: str, user) -> None:
        """First successful login: bind to the session and fix the window."""
        now = timezone.now()
        self.first_used_at = now
        self.session_expires_at = now + timezone.timedelta(hours=float(self.session_hours))
        self.status = self.Status.ACTIVE
        # The field is stored for the audit trail; never allow a null value to
        # reach the database, which would raise an integrity error on save.
        self.bound_session_key = session_key or ""
        self.user = user
        self.save()

    def consume(self, reason: str) -> None:
        """End of the one permitted session. The credential can never log in again."""
        self.status = self.Status.CONSUMED
        self.save(update_fields=["status"])
        AuditLog.record(
            actor_name=self.officer.name,
            actor_role=self.role_display,
            action="CREDENTIAL_CONSUMED",
            target=self.username,
            detail=reason,
            credential=self,
        )

    def revoke(self, by_name: str) -> None:
        self.status = self.Status.REVOKED
        self.save(update_fields=["status"])
        AuditLog.record(
            actor_name=by_name,
            actor_role=Roles.SUPER_ADMIN.label,
            action="CREDENTIAL_REVOKED",
            target=self.username,
            detail=f"Credential revoked by {by_name}. Any live session ends on next request.",
            credential=self,
        )


class AuditLog(models.Model):
    """Append-only audit trail across both surfaces.

    Records actor, role, credential, timestamp, before/after state, and a
    free-text note for every state-changing action. Rows are never updated
    or deleted by application code.
    """

    timestamp = models.DateTimeField(auto_now_add=True)
    actor_name = models.CharField(max_length=120)
    actor_role = models.CharField(max_length=200, blank=True, default="")
    credential = models.ForeignKey(IssuedCredential, null=True, blank=True, on_delete=models.SET_NULL)
    surface = models.CharField(max_length=12, default="backoffice")
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=200, blank=True, default="")
    before_state = models.CharField(max_length=120, blank=True, default="")
    after_state = models.CharField(max_length=120, blank=True, default="")
    detail = models.TextField(blank=True, default="")
    flagged = models.BooleanField(default=False)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.actor_name} {self.action}"

    @classmethod
    def record(
        cls,
        *,
        actor_name: str,
        action: str,
        actor_role: str = "",
        credential: IssuedCredential | None = None,
        surface: str = "backoffice",
        target: str = "",
        before_state: str = "",
        after_state: str = "",
        detail: str = "",
        flagged: bool = False,
    ) -> "AuditLog":
        return cls.objects.create(
            actor_name=actor_name,
            actor_role=actor_role,
            credential=credential,
            surface=surface,
            action=action,
            target=target,
            before_state=before_state,
            after_state=after_state,
            detail=detail,
            flagged=flagged,
        )


class DemoOutboxEmail(models.Model):
    """Captured outbound email, shown on the demo outbox page.

    The demo stores messages here so the audience can see system
    notifications without a mail server.
    """

    sent_at = models.DateTimeField(auto_now_add=True)
    to_address = models.CharField(max_length=200)
    subject = models.CharField(max_length=200)
    body = models.TextField()

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self) -> str:
        return f"{self.to_address}: {self.subject}"
