"""Transactional email over HTTPS.

web.py used smtplib to talk to Gmail on port 465. Workers cannot open a raw
SMTP socket, so notifications go through an HTTP email API instead. Resend is
used here because its free tier covers a project at this size, but the shape is
the same for Postmark, SendGrid or Mailgun — swap the URL and the payload.

With no API key configured this logs the message instead of sending it, which
keeps local development working exactly like the Flask version's
`[SIMULATION]` branch did.
"""

import json

from httpjs import fetch_raw


async def send_email(env, to_email: str, subject: str, body: str) -> bool:
    api_key = getattr(env, "RESEND_API_KEY", None)
    sender = getattr(env, "EMAIL_FROM", None) or "Surfing AI <onboarding@resend.dev>"

    if not api_key:
        print(f"[SIMULATION] Email to {to_email}\nSubject: {subject}\n{body}\n")
        return False

    try:
        response = await fetch_raw(
            "https://api.resend.com/emails",
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            body=json.dumps(
                {"from": sender, "to": [to_email], "subject": subject, "text": body}
            ),
        )
        if not response.ok:
            # Logged, not raised: a missed notification should never roll back
            # an analysis the customer already paid for.
            print(f"Email to {to_email} failed: HTTP {response.status} {await response.text()}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"Email to {to_email} failed: {exc}")
        return False
