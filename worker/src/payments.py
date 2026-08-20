"""Stripe Checkout — how the money actually moves.

The shape of the flow, and why it is this shape:

  1. The surfer clicks Buy. Our Worker calls Stripe to create a *Checkout
     Session* and gets back a URL.
  2. We redirect the browser to that URL. The card form is Stripe's page on
     Stripe's domain. Card numbers never touch this Worker, which is the single
     biggest reason to do it this way: PCI scope stays with Stripe.
  3. The surfer pays. Stripe redirects them back to our success URL.
  4. Separately, Stripe POSTs a `checkout.session.completed` webhook to us.
     **This is the only event that grants bundles.**

Step 4 is the part that is easy to get wrong. The obvious-looking shortcut is to
credit the account on the success-URL redirect in step 3 — but that URL is just
a link. Anyone can visit it without paying, the browser may never arrive at it
(closed tab, dead battery, flaky network), and some payment methods settle
minutes after the redirect. The webhook is the authoritative signal because it
comes from Stripe, is signed, and is retried until we acknowledge it.

Retried until acknowledged is also why `processed_stripe_event` exists. Stripe
will deliver the same event more than once; without a record of what has already
been actioned, one payment becomes two bundles.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import db as dbx
from httpjs import fetch_json

STRIPE_API = "https://api.stripe.com/v1"

# The catalogue. Prices are in the smallest currency unit — cents — because
# floats and money must never meet: 0.1 + 0.2 != 0.3 in binary floating point,
# and a rounding error in a ledger is a bug you find months later.
BUNDLES = {
    "ai": {
        "label": "AI Analysis",
        "description": "Instant AI equipment sizing from your video",
        "amount_cents": 1000,   # $10.00
        "column": "ai_bundles",
    },
    "coach": {
        "label": "Olympic Coach Review",
        "description": "Manual video review and sizing by the head coach",
        "amount_cents": 3500,   # $35.00
        "column": "coach_bundles",
    },
    "zoom": {
        "label": "1-Hour Zoom Session",
        "description": "Live one-on-one meeting with the coach",
        "amount_cents": 9900,   # $99.00
        "column": "zoom_bundles",
    },
}

CURRENCY = "usd"


def _form(payload: dict) -> str:
    """Stripe's API takes form encoding, not JSON."""
    return urlencode(payload)


async def create_checkout_session(
    env, *, bundle_key: str, email: str, base_url: str
) -> str:
    """Ask Stripe for a hosted payment page and return its URL."""
    bundle = BUNDLES[bundle_key]
    secret_key = getattr(env, "STRIPE_SECRET_KEY", None)
    if not secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")

    payload = {
        "mode": "payment",
        "success_url": f"{base_url}/shop?paid={bundle_key}",
        "cancel_url": f"{base_url}/shop?cancelled=1",

        # Ties the payment to the signed-in account. Note that this comes from
        # our session, never from a form field — otherwise a user could credit
        # someone else's account, or their own, by editing the request.
        "client_reference_id": email,
        "customer_email": email,
        "metadata[bundle]": bundle_key,
        "metadata[email]": email,

        # Inline price data, so there is no Stripe dashboard product to keep in
        # sync with this file.
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": CURRENCY,
        "line_items[0][price_data][unit_amount]": str(bundle["amount_cents"]),
        "line_items[0][price_data][product_data][name]": bundle["label"],
        "line_items[0][price_data][product_data][description]": bundle["description"],
    }

    session = await fetch_json(
        f"{STRIPE_API}/checkout/sessions",
        method="POST",
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            # If this request is retried after a timeout, Stripe returns the
            # session it already created rather than creating a second one.
            "Idempotency-Key": f"{email}:{bundle_key}:{int(time.time() // 60)}",
        },
        body=_form(payload),
    )
    return session["url"]


# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------

def verify_signature(payload: str, signature_header: str, secret: str, tolerance: int = 300) -> bool:
    """Check that a webhook really came from Stripe.

    The `Stripe-Signature` header looks like `t=1690000000,v1=abc123...`. Stripe
    signs the string `"{timestamp}.{raw_body}"` with the endpoint's signing
    secret, so we recompute that HMAC and compare.

    Without this check the endpoint is an open door: anyone who learns the URL
    could POST a fake `checkout.session.completed` and mint free bundles.

    The timestamp check bounds replay — a signature captured today cannot be
    re-sent tomorrow. Note the body must be the *raw* bytes as received;
    re-serialising the parsed JSON changes whitespace and breaks the HMAC.
    """
    if not signature_header or not secret:
        return False

    timestamp = None
    signatures = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)

    if not timestamp or not signatures:
        return False

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False
    if age > tolerance:
        return False

    expected = hmac.new(
        secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256
    ).hexdigest()

    # Constant-time, and any of the v1 values may match during a secret rotation.
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


async def handle_event(database: "dbx.Database", event: dict) -> str:
    """Act on a verified Stripe event. Returns a short status for logging.

    Only ever called after verify_signature() has passed.
    """
    event_id = event.get("id")
    event_type = event.get("type")

    if not event_id:
        return "ignored: no event id"

    # Idempotency gate. INSERT is the check *and* the claim in one statement: if
    # this event has been seen before the primary key rejects the row, and we
    # know not to credit again. Doing a SELECT first and then an INSERT would
    # leave a gap where two concurrent deliveries both pass the check.
    meta = await database.execute(
        "INSERT OR IGNORE INTO processed_stripe_event (event_id, event_type) VALUES (?, ?)",
        event_id,
        event_type,
    )
    if int(meta.get("changes") or 0) == 0:
        return f"duplicate: {event_id} already processed"

    if event_type != "checkout.session.completed":
        # Acknowledged so Stripe stops retrying, but nothing to do.
        return f"ignored: {event_type}"

    session = event.get("data", {}).get("object", {}) or {}

    # A session can complete while the payment is still pending (some bank
    # debits settle later). Only `paid` means the money is actually there.
    if session.get("payment_status") != "paid":
        return f"not paid yet: {session.get('payment_status')}"

    metadata = session.get("metadata") or {}
    bundle_key = metadata.get("bundle")
    email = session.get("client_reference_id") or metadata.get("email")

    if bundle_key not in BUNDLES or not email:
        return f"ignored: unrecognised bundle {bundle_key!r} or missing email"

    column = BUNDLES[bundle_key]["column"]

    # Grant the credit and record the sale together, so we can never end up with
    # a bundle that has no purchase behind it.
    await dbx.get_inventory(database, email)
    await database.batch([
        (
            f"UPDATE inventory SET {column} = {column} + 1 WHERE user_email = ?",
            (email,),
        ),
        (
            "INSERT OR IGNORE INTO purchase"
            " (user_email, bundle, quantity, amount_total_cents, currency,"
            "  stripe_session_id, stripe_payment_intent)"
            " VALUES (?, ?, 1, ?, ?, ?, ?)",
            (
                email,
                bundle_key,
                int(session.get("amount_total") or BUNDLES[bundle_key]["amount_cents"]),
                session.get("currency") or CURRENCY,
                session.get("id") or event_id,
                session.get("payment_intent"),
            ),
        ),
    ])

    return f"credited 1 {bundle_key} bundle to {email}"


def parse_event(raw_body: str) -> dict:
    try:
        return json.loads(raw_body)
    except ValueError:
        return {}
