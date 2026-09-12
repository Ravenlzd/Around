"""
Minimal, provider-agnostic transactional email sender.

No email service existed anywhere in this codebase before this pass —
no verification tokens, no password-reset tokens, no email client
library, nothing. Rather than pick one vendor's REST API (SendGrid,
Postmark, Resend, SES) and add a new dependency for it, this speaks
plain SMTP via the standard library, which every one of those providers
also exposes as an SMTP relay — the choice of provider becomes a Render
environment-variable decision, not a code change.

If SMTP_HOST isn't configured (the default), send_email() logs the
message instead of attempting to send it. This is deliberate: it lets
the rest of the verification flow (token creation, the verify/resend
endpoints, the frontend UI) be fully built and testable right now,
without a working mail provider, while making it obvious in the logs
that no real email went out.
"""
import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger("around.email")


def is_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM_EMAIL)


def send_email(to: str, subject: str, body: str) -> bool:
    """
    Returns True if an actual send was attempted (regardless of
    whether it succeeded — send failures are logged, not raised, since
    a flaky mail provider shouldn't turn into a 500 on signup). Returns
    False when running in the unconfigured/log-only mode.
    """
    if not is_configured():
        logger.info("[EMAIL - not sent, SMTP not configured] to=%s subject=%s\n%s", to, subject, body)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
        return True  # attempted — caller shouldn't treat this as "not configured"


def send_verification_email(to: str, display_name: str, token: str) -> None:
    link = f"{settings.PUBLIC_APP_URL.rstrip('/')}/?verify_email={token}"
    send_email(
        to=to,
        subject="Verify your Around account",
        body=(
            f"Hi {display_name},\n\n"
            f"Confirm your email to finish setting up Around:\n{link}\n\n"
            "This link expires in 24 hours and can only be used once. "
            "If you didn't create an Around account, you can ignore this email.\n"
        ),
    )
