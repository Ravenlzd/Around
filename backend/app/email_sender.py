"""
Minimal, provider-agnostic transactional email sender.

No email service existed anywhere in this codebase before this pass —
no verification tokens, no password-reset tokens, no email client
library, nothing. Rather than pick one vendor's REST API (SendGrid,
Postmark, Resend, SES) and add a new dependency for it, this speaks
plain SMTP via the standard library, which every one of those providers
also exposes as an SMTP relay — the choice of provider becomes a Render
environment-variable decision, not a code change.

If SMTP_HOST isn't configured (the default), send_email() logs instead
of attempting to send. In development/testing this logs the full body
(useful for exercising the OTP flow without a mail provider); in
production it deliberately does NOT — this module's callers now
include the 6-digit signup OTP, and "log the code in production
because there's no mail provider" is exactly the silent, insecure
fallback this was told not to do. In production with no provider
configured, signup genuinely cannot complete (no verification code
ever reaches the user, anywhere) until SMTP_* is set on Render.
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
        if settings.ENV == "production":
            logger.warning(
                "Email NOT sent (no SMTP provider configured) to=%s subject=%s — "
                "set SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM_EMAIL on Render to enable real delivery.",
                to, subject,
            )
        else:
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


def send_signup_otp_email(to: str, code: str) -> None:
    """
    The code is deliberately only ever in the email BODY, never a URL
    (a link carries the risk of being pre-fetched/scanned by mail
    security scanners, proxies, or link-preview bots, which would burn
    a single-use code before the real user ever sees it) — this is why
    signup moved from a clickable link to a typed 6-digit code at all.
    """
    send_email(
        to=to,
        subject="Your Around verification code",
        body=(
            "Use this code to verify your email address:\n\n"
            f"{code}\n\n"
            "This code expires in 10 minutes. If you didn't try to sign up for Around, you can ignore this email.\n"
        ),
    )
