"""Authentication backend for Super Admin issued credentials.

Backoffice officers hold no standing passwords. Each login presents a
single-use credential; the backend admits it only while the credential is in
the ISSUED state. Reuse of an ACTIVE credential from a second session is
refused and flagged as a possible credential-sharing event.
"""
from __future__ import annotations

from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User

from core.models import AuditLog, IssuedCredential


class IssuedCredentialBackend(BaseBackend):
    """Authenticates backoffice credentials.

    Officers sign in with their registered email (preferred) or the legacy
    credential username. An email resolves to the officer's newest credential
    that is still usable (issued or active).
    """

    def authenticate(self, request, username: str | None = None, password: str | None = None, **kwargs):
        if not username or password is None:
            return None
        username = username.strip()
        if username.startswith("nrs-"):
            try:
                credential = IssuedCredential.objects.select_related("officer").get(username=username)
            except IssuedCredential.DoesNotExist:
                return None
        elif "@" in username:
            # Registered email: the officer's newest usable credential.
            credential = (
                IssuedCredential.objects.select_related("officer")
                .filter(
                    officer__email__iexact=username,
                    status__in=[IssuedCredential.Status.ISSUED, IssuedCredential.Status.ACTIVE],
                )
                .order_by("-issued_at")
                .first()
            )
            if credential is None:
                return None
        else:
            return None

        credential.lapse_if_unused()

        if credential.status == IssuedCredential.Status.ACTIVE:
            # A second login with a live credential indicates the passcode has
            # been shared. Refuse and flag for the auditor.
            if credential.check_passcode(password):
                AuditLog.record(
                    actor_name=credential.officer.name,
                    actor_role=credential.role_display,
                    action="CREDENTIAL_REUSE_REFUSED",
                    target=credential.username,
                    detail="Login refused: credential already bound to a live session. Possible credential sharing.",
                    credential=credential,
                    flagged=True,
                )
            return None

        if credential.status != IssuedCredential.Status.ISSUED:
            return None
        if not credential.check_passcode(password):
            return None

        user, created = User.objects.get_or_create(
            username=credential.username,
            defaults={"first_name": credential.officer.name.split(" ")[0], "email": credential.officer.email},
        )
        if created:
            user.set_unusable_password()
            user.save()
        if request is not None:
            request._pending_credential = credential
        return user

    def get_user(self, user_id: int):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
