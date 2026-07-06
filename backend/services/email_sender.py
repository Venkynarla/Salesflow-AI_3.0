"""
Email sending service.
Supports Gmail with regular password (for testing) or App Password (for production).
If SMTP fails, falls back to DEV MODE which prints the email instead of sending it.
"""

import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
import aiosmtplib
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


async def send_email(to_email: str, to_name: str, subject: str, body: str) -> bool:
    smtp_host     = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port     = int(os.getenv("SMTP_PORT", "587"))
    smtp_user     = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    from_name     = os.getenv("EMAIL_FROM_NAME", smtp_user)

    if not smtp_user or not smtp_password:
        logger.warning("No SMTP credentials — running in DEV MODE (printing email)")
        _print_dev_email(to_name, to_email, subject, body)
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((from_name, smtp_user))
    msg["To"]      = formataddr((to_name, to_email))
    msg["Reply-To"] = smtp_user
    msg.attach(MIMEText(body, "plain", "utf-8"))
    html_body = body.replace("\\n", "<br>")
    msg.attach(MIMEText(f"<html><body style='font-family:Arial;font-size:15px;line-height:1.6'>{html_body}</body></html>", "html", "utf-8"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=smtp_user,
            password=smtp_password,
            use_tls=False,
            start_tls=True,
        )
        logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.warning(f"SMTP failed ({e}) — falling back to DEV MODE")
        _print_dev_email(to_name, to_email, subject, body)
        return True  # Return True so pipeline continues even if email fails


def _print_dev_email(to_name, to_email, subject, body):
    """Print email to logs in dev mode instead of sending."""
    print(f"\\n[DEV MODE - EMAIL NOT SENT - WOULD SEND TO: {to_email}]")
    print(f"Subject: {subject}")
    print(f"Body:\\n{body}")
    print("[END DEV EMAIL]\\n")
