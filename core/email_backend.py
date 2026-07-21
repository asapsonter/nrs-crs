"""Demo email backend: writes to the console and to the demo outbox page."""
from __future__ import annotations

from django.core.mail.backends.console import EmailBackend as ConsoleBackend


class DemoOutboxEmailBackend(ConsoleBackend):
    """Console email backend that also captures messages in the database.

    The captured copies power the demo outbox page so the audience can see
    applicant and Primary User notifications without a mail server.
    """

    def send_messages(self, email_messages):
        from core.models import DemoOutboxEmail

        for message in email_messages:
            DemoOutboxEmail.objects.create(
                to_address=", ".join(message.to),
                subject=message.subject,
                body=message.body,
            )
        return super().send_messages(email_messages)
