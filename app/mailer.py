"""
Outbound email — password resets and two-factor codes.

SMTP is configured by environment variable. When it isn't configured the app
still works: emails are logged instead of sent, so a missing provider degrades
to "the admin reads the code out of the logs" rather than locking everyone out.
That matters because the alternative — silently failing to send a reset link —
looks identical to a broken account.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .config import settings

log = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text email. Returns False if it couldn't be sent."""
    if not is_configured():
        log.warning("SMTP not configured — email to %s not sent.\n"
                    "SUBJECT: %s\n%s", to, subject, body)
        return False

    msg = EmailMessage()
    # "The Engine <josh@ystickets.com>" rather than the bare address. The
    # address itself is whatever SMTP_FROM says -- most providers require it to
    # match the authenticated mailbox, so the name is the part worth setting.
    name = (settings.smtp_from_name or "").strip()
    msg["From"] = (formataddr((name, settings.smtp_from)) if name
                   else settings.smtp_from)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                  context=ssl.create_default_context(),
                                  timeout=20) as s:
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                              timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.send_message(msg)
        log.info("sent %r to %s", subject, to)
        return True
    except Exception:
        log.exception("failed sending email to %s", to)
        return False


def send_reset(to: str, link: str, minutes: int) -> bool:
    return send(
        to, "Reset your reconciliation password",
        f"Someone asked to reset the password for this account.\n\n"
        f"{link}\n\n"
        f"The link works once and expires in {minutes} minutes.\n"
        f"If this wasn't you, no action is needed — the password is unchanged.")


def send_twofa(to: str, code: str, minutes: int) -> bool:
    return send(
        to, f"{code} is your sign-in code",
        f"Your sign-in code is {code}\n\n"
        f"It expires in {minutes} minutes. This device won't be asked again "
        f"for 12 hours.\n\n"
        f"If you didn't try to sign in, change your password.")
