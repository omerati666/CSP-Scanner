"""Gmail SMTP ile mail gönderimi."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage


def send_mail(subject: str, text_body: str, html_body: str, user: str, app_password: str,
              to: str, host: str = "smtp.gmail.com", port: int = 465) -> None:
    recipients = [x.strip() for x in to.split(",") if x.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    # Google uygulama şifresi ekranda boşluklu gösterilir; boşlukları temizle.
    password = app_password.replace(" ", "")
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as server:
        server.login(user, password)
        server.send_message(msg)
