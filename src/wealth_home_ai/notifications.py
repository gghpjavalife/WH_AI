"""Session-configured WhatsApp and email notification delivery."""

from email.message import EmailMessage
from smtplib import SMTP, SMTP_SSL
from ssl import create_default_context

import requests


def send_whatsapp_alert(
    message: str,
    *,
    account_sid: str,
    auth_token: str,
    sender: str,
    recipient: str,
) -> None:
    """Send a WhatsApp message through Twilio's WhatsApp API."""
    credentials = (account_sid.strip(), auth_token.strip())
    source = sender.strip()
    destination = recipient.strip()
    if not all(credentials + (source, destination)):
        raise ValueError("Complete the WhatsApp notification settings first.")
    if not source.startswith("whatsapp:") or not destination.startswith("whatsapp:"):
        raise ValueError(
            "WhatsApp sender and recipient must use the whatsapp:+countrycode format."
        )

    response = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{credentials[0]}/Messages.json",
        data={"From": source, "To": destination, "Body": message},
        auth=credentials,
        timeout=(5, 20),
    )
    response.raise_for_status()


def send_email_alert(
    message: str,
    *,
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    sender: str,
    recipient: str,
) -> None:
    """Send an email using authenticated SMTP with TLS."""
    host = smtp_host.strip()
    username = username.strip()
    sender = sender.strip()
    recipient = recipient.strip()
    if not all((host, sender, recipient)):
        raise ValueError("Complete the email notification settings first.")
    if smtp_port not in (465, 587):
        raise ValueError("Use SMTP port 465 (SSL) or 587 (STARTTLS).")
    if bool(username) != bool(password):
        raise ValueError("Provide both SMTP username and password, or neither.")

    email = EmailMessage()
    email["Subject"] = "GGHP notification"
    email["From"] = sender
    email["To"] = recipient
    email.set_content(message)
    context = create_default_context()
    if smtp_port == 465:
        with SMTP_SSL(host, smtp_port, context=context, timeout=20) as server:
            if username:
                server.login(username, password)
            server.send_message(email)
    else:
        with SMTP(host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(email)
