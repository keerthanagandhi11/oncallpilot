"""SMTP email adapter (for example Office 365 SMTP relay or an internal mail relay)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.integrations.base import IntegrationError


class SmtpEmail:
    def __init__(self, host: str, port: int, username: str, password: str, sender: str, starttls: bool = True):
        if not host:
            raise ValueError("SMTP_HOST is required for EMAIL_PROVIDER=smtp")
        self.host, self.port, self.username, self.password = host, port, username, password
        self.sender, self.starttls = sender, starttls

    def _send_sync(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            if self.starttls:
                s.starttls(context=ssl.create_default_context())
            if self.username:
                s.login(self.username, self.password)
            s.send_message(msg)

    async def send(self, to: str, subject: str, text: str, html: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg["X-Priority"] = "1"
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except (OSError, smtplib.SMTPException) as exc:
            raise IntegrationError(f"SMTP send failed: {exc}") from exc
