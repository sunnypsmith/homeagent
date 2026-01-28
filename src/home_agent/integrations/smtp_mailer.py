from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterable, Optional, Sequence

from home_agent.config import SmtpSettings


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content_type: str
    data: bytes


class SmtpMailer:
    def __init__(self, settings: SmtpSettings) -> None:
        self._s = settings

    @property
    def enabled(self) -> bool:
        return bool(self._s.enabled)

    def send(
        self,
        *,
        to_addrs: Sequence[str],
        subject: str,
        text: str,
        attachments: Optional[Iterable[EmailAttachment]] = None,
    ) -> None:
        if not self._s.enabled:
            raise RuntimeError("smtp_not_configured")

        to_list = [a.strip() for a in (to_addrs or []) if isinstance(a, str) and a.strip()]
        if not to_list:
            raise RuntimeError("missing_to_addrs")

        msg = EmailMessage()
        msg["From"] = self._s.from_addr
        msg["To"] = ", ".join(to_list)
        msg["Subject"] = subject
        msg.set_content(text or "")

        for att in attachments or []:
            if not att or not att.data:
                continue
            maintype, subtype = _split_content_type(att.content_type)
            msg.add_attachment(att.data, maintype=maintype, subtype=subtype, filename=att.filename)

        timeout = float(self._s.timeout_seconds or 20.0)

        # Choose transport:
        if self._s.use_ssl:
            context = ssl.create_default_context()
            server: smtplib.SMTP = smtplib.SMTP_SSL(self._s.host, int(self._s.port), timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(self._s.host, int(self._s.port), timeout=timeout)

        try:
            server.ehlo()
            if (not self._s.use_ssl) and self._s.use_starttls:
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()

            if self._s.username and self._s.password:
                server.login(self._s.username, self._s.password)

            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass


def _split_content_type(content_type: str) -> tuple[str, str]:
    ct = (content_type or "").strip().lower()
    if "/" in ct:
        left, right = ct.split("/", 1)
        left = left.strip() or "application"
        right = right.strip() or "octet-stream"
        return (left, right)
    return ("application", "octet-stream")

