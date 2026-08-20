"""Sessions, CSRF and Google sign-in, without Flask or Authlib.

Flask kept `session` in a signed cookie and Authlib drove the OAuth dance.
Neither library runs on Workers, but the underlying mechanisms are small enough
to write out directly, and doing so makes the security properties visible.

The session is a signed — not encrypted — cookie: the payload is readable by the
client, but it cannot be modified without the signing key. That is fine here,
because all it holds is an email address, a CSRF token, and an expiry.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

SESSION_COOKIE = "sb_session"
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60  # two weeks

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


# ---------------------------------------------------------------------------
# base64url helpers (no padding, per RFC 7515)
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# Signed cookie sessions
# ---------------------------------------------------------------------------

def sign_session(data: dict, secret: str) -> str:
    """Serialise a session dict into `payload.signature`."""
    body = dict(data)
    body["exp"] = int(time.time()) + SESSION_TTL_SECONDS
    payload = _b64e(json.dumps(body, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64e(signature)}"


def read_session(token: str | None, secret: str) -> dict:
    """Verify and decode a session cookie, returning {} if it is not usable."""
    if not token or "." not in token:
        return {}

    payload, _, signature = token.partition(".")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()

    # compare_digest, not ==. A plain comparison returns as soon as two bytes
    # differ, and that timing difference is enough to forge a signature one byte
    # at a time.
    if not hmac.compare_digest(_b64e(expected), signature):
        return {}

    try:
        data = json.loads(_b64d(payload))
    except Exception:
        return {}

    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return {}
    return data


def session_cookie_header(token: str, secure: bool = True, max_age: int | None = None) -> str:
    """Build the Set-Cookie value.

    HttpOnly keeps JavaScript (and therefore any XSS) away from the session.
    SameSite=Lax stops another site's form POST from riding on the cookie, which
    is the same protection the Flask app's SameSite setting gave.
    """
    age = SESSION_TTL_SECONDS if max_age is None else max_age
    parts = [
        f"{SESSION_COOKIE}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={age}",
    ]
    if secure:
        # Unlike the Flask app, which hardcoded SESSION_COOKIE_SECURE=False,
        # this is on by default: Workers are always served over HTTPS.
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header(secure: bool = True) -> str:
    return session_cookie_header("", secure=secure, max_age=0)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def ensure_csrf(session: dict) -> str:
    """Return the session's CSRF token, minting one if it has none."""
    token = session.get("csrf")
    if not token:
        token = secrets.token_hex(16)
        session["csrf"] = token
    return token


def csrf_ok(session: dict, submitted: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


# ---------------------------------------------------------------------------
# Google OAuth 2.0 / OpenID Connect, authorization-code flow
# ---------------------------------------------------------------------------

def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Step 1: where to send the browser to sign in.

    `state` is a random value we also store in the session. Google echoes it
    back, and refusing a mismatch is what stops an attacker from feeding us a
    login they started themselves.
    """
    return GOOGLE_AUTH_ENDPOINT + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
            "access_type": "online",
        }
    )


async def exchange_code(
    fetch_json, *, code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict:
    """Step 2: trade the one-time code for tokens, server-to-server.

    `fetch_json` is injected so this stays testable and free of Worker globals.
    """
    return await fetch_json(
        GOOGLE_TOKEN_ENDPOINT,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode(
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        ),
    )


def claims_from_id_token(id_token: str) -> dict:
    """Read the claims out of Google's ID token.

    The signature is deliberately not checked here, and that is safe *only*
    because of where this token came from: we received it directly from
    Google's token endpoint over a TLS connection we opened ourselves, in
    exchange for a client secret. Google's own documentation calls out this
    case as one where validation can be skipped.

    If an ID token ever reaches this app by any other route — posted by a
    browser, forwarded by another service — this function is not enough, and
    the signature must be verified against Google's JWKS first.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        return json.loads(_b64d(parts[1]))
    except Exception:
        return {}


def is_admin(email: str | None, super_admin_email: str) -> bool:
    if not email or not super_admin_email:
        return False
    return email.lower().strip() == super_admin_email.lower().strip()
