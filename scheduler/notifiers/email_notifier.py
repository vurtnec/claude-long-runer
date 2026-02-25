"""
Email Notification via SMTP
===========================

Uses Python's built-in smtplib - no external dependencies needed.
"""

import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict

from .base import BaseNotifier


class EmailNotifier(BaseNotifier):
    """Send email notifications via SMTP."""

    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        email_config = self.global_config.get("email", {})

        to_addr = settings.get("to", "")
        subject = self.render_template(settings.get("subject", ""), context)
        body = self.render_template(settings.get("body_template", ""), context)

        if not to_addr:
            print("  Email recipient not configured")
            return False

        msg = MIMEMultipart()
        msg["From"] = email_config.get("from_address", "scheduler@localhost")
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            smtp_host = email_config.get("smtp_host", "localhost")
            smtp_port = email_config.get("smtp_port", 587)

            def _send():
                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    if email_config.get("use_tls", True):
                        server.starttls()
                    user = email_config.get("smtp_user")
                    password = email_config.get("smtp_password")
                    if user and password:
                        server.login(user, password)
                    server.send_message(msg)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _send)

            print(f"  Email sent to {to_addr}")
            return True
        except Exception as e:
            print(f"  Email send failed to {to_addr}: {e}")
            return False
