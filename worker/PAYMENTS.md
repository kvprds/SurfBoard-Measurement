# How the money actually moves

The shop page always showed three prices. The Buy buttons were `href="#"`.
This is what fills that gap, and why it is built the way it is.

**This ships in Stripe test mode.** Card `4242 4242 4242 4242` walks the entire
flow — checkout, webhook, bundle credited — and no money moves. Test-mode keys
need no account activation and no bank details, and Stripe charges no monthly
fee either way, so the payment code costs nothing to keep. Going live is a key
swap, covered at the end.

Code: [`src/payments.py`](src/payments.py). Tests:
[`tests/test_money.py`](tests/test_money.py).

---

## The flow

```
  Surfer's browser              Your Worker                   Stripe
        │                            │                           │
        │  POST /buy/coach           │                           │
        ├───────────────────────────►│                           │
        │                            │  create Checkout Session  │
        │                            ├──────────────────────────►│
        │                            │◄──────────────────────────┤
        │   303 → checkout.stripe.com│         session.url       │
        │◄───────────────────────────┤                           │
        │                                                        │
        │  card details go HERE, never to your Worker            │
        ├───────────────────────────────────────────────────────►│
        │                                                        │
        │◄────────── 303 back to /shop?paid=coach ───────────────┤
        │                            │                           │
        │                            │◄── POST /webhooks/stripe ─┤
        │                            │    checkout.session.completed
        │                            │                           │
        │                     verify signature                   │
        │                     claim event id  ◄── the bundle is
        │                     credit bundle       granted HERE
        │                            ├── 200 ───────────────────►│
```

Two paths come back from Stripe, and they do different jobs. The **redirect**
returns the customer to your site. The **webhook** tells your server what
happened. Only the webhook grants anything.

---

## The five decisions that matter

### 1. Card details never touch this Worker

`POST /buy/{bundle}` creates a Checkout Session and returns a redirect to a page
Stripe hosts. The card number is typed on Stripe's domain, into Stripe's form.

This is not laziness. Handling raw card numbers puts you in scope for PCI-DSS
compliance — a real audit obligation with real paperwork. Redirecting to hosted
Checkout keeps that scope with Stripe, and it is why almost every small shop is
built this way.

### 2. The success URL grants nothing

The tempting shortcut:

```python
@app.get("/shop")            # ?paid=coach
async def shop(request):
    if request.query_params.get("paid"):
        await credit_bundle(...)   # ← never do this
```

Three ways that loses money or trust:

- **It is just a URL.** Anyone can visit `/shop?paid=zoom` and get a $99 bundle.
  Nothing in that request proves a payment happened.
- **The browser may never arrive.** Closed tab, dead battery, tunnel. The
  customer paid and got nothing.
- **Some payments settle after the redirect.** Bank debits can land minutes
  later, so "back on our site" and "money received" are different events.

The webhook has none of those problems: it comes from Stripe, it is signed, and
Stripe retries it until you return a 2xx.

### 3. The webhook is authenticated

An unauthenticated webhook endpoint is a URL that grants free product to anyone
who finds it.

Stripe signs each delivery. The `Stripe-Signature` header carries a timestamp
and an HMAC-SHA256 of `"{timestamp}.{raw_body}"`, keyed with the endpoint's
signing secret. `verify_signature()` recomputes it and compares:

```python
expected = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(),
                    hashlib.sha256).hexdigest()
return any(hmac.compare_digest(expected, c) for c in signatures)
```

Three details in those two lines:

- **`compare_digest`, not `==`.** A normal comparison returns as soon as two
  bytes differ. That timing difference is measurable over the network, and it
  lets an attacker discover a valid signature one byte at a time.
- **The raw body.** Parse the JSON first and re-serialise it and the whitespace
  changes, so the HMAC no longer matches. The route reads bytes before anything
  else touches them.
- **The timestamp.** Rejecting signatures older than five minutes bounds replay:
  a delivery captured today cannot be re-sent tomorrow.

### 4. The same event arrives more than once

Stripe retries until it gets a 2xx, and its delivery guarantee is *at least
once*. A slow response, a deploy mid-request, a transient 500 — any of these
produce a second delivery of an event you already handled.

Credit blindly and one payment becomes five bundles.

The fix is one statement:

```sql
INSERT OR IGNORE INTO processed_stripe_event (event_id, event_type) VALUES (?, ?)
```

If the row is new, `changes = 1` and this is the first time — proceed. If the
primary key rejects it, `changes = 0` and it is a repeat — skip. The check and
the claim are the same operation, so two concurrent deliveries cannot both pass.

A `SELECT` followed by an `INSERT` would leave a window between them where both
deliveries see "not processed yet". That window is small and it will eventually
be hit.

`tests/test_money.py` delivers the same event five times and asserts the balance
is 1.

### 5. `payment_status` must be `paid`

A Checkout Session can complete while the money is still in flight. The handler
checks:

```python
if session.get("payment_status") != "paid":
    return f"not paid yet: {session.get('payment_status')}"
```

Returning 200 there is deliberate — the event was understood, so Stripe should
stop retrying. When the payment does settle, a *different* event arrives.

---

## What one sale would be worth

None of this is charged in test mode. It is here because the numbers are the
reason to pick one price over another, and they are worth knowing before you
ever flip the switch.

Stripe's standard US card rate is **2.9% + $0.30** per successful charge. It is
charged on the transaction, not the profit, so the fixed 30c hurts small tickets
most.

| | AI — $10 | Coach — $35 | Zoom — $99 |
| --- | ---: | ---: | ---: |
| Customer pays | $10.00 | $35.00 | $99.00 |
| Stripe fee | −$0.59 | −$1.32 | −$3.17 |
| Gemini analysis | −$0.01 | −$0.01 | — |
| Cloudflare (marginal) | −$0.00 | −$0.00 | −$0.00 |
| **You keep** | **$9.40** | **$33.67** | **$95.83** |
| Effective fee | 5.9% | 3.8% | 3.2% |

Note the effective fee on the $10 bundle. Selling ten $10 bundles costs $5.90 in
fees; selling one $99 bundle costs $3.17. If you ever discount, discount toward
larger tickets.

### Where the Gemini number comes from

Gemini tokenises video at roughly 300 tokens per second of footage (about 263
for the frames plus 32 for audio). Gemini 2.5 Flash is **$0.30 per million input
tokens** and **$2.50 per million output**.

A 30-second clip:

```
video     30s × 300 tok/s        ≈  9,000 input tokens
prompt + few-shot examples       ≈  1,000 input tokens
                                   ─────────────────
                                   10,000 × $0.30/M  = $0.0030
assessment paragraph  ~500 output tokens × $2.50/M   = $0.0013
                                                       ───────
                                                       ≈ $0.004
```

Round it to **half a cent per analysis**. Even at a 90-second clip it stays
under 2c. Against a $10 sale, the AI is not your cost problem — Stripe is, by a
factor of roughly 150.

### Where the Cloudflare number comes from

Per analysis, at published rates:

| | Usage | Cost |
| --- | --- | ---: |
| Workers requests | ~20 | $0.000006 |
| D1 rows read/written | ~30 | $0.00003 |
| R2 storage | 50MB for a month | $0.00075 |
| R2 Class A (the upload) | 1 | $0.0000045 |
| Queues | 3 operations | $0.0000012 |

Under a tenth of a cent, and at portfolio-site volume the **free allowances
absorb all of it** — there is no Cloudflare bill at all. See
[DEPLOY.md](DEPLOY.md#what-it-costs-nothing) for the allowance table. R2 egress
being free is what keeps video playback from ever appearing on this list.

### Break-even

There is no fixed monthly cost to recover, so the first sale is profitable.
Cloudflare only starts charging if you leave the free tier — realistically that
means exceeding R2's 10GB of stored video (roughly 400 clips at the 25MB cap) or
100,000 requests a day. Long before either, Gemini's free quota of a few hundred
requests per day would be the thing you outgrew, and paid Gemini is still under
a cent per analysis.

The number to keep in view is not the infrastructure. It is that **Stripe takes
5.9% of a $10 sale and the AI costs 0.05% of it.**

---

## Test mode, and going live if you ever want to

As shipped, this is test mode and stays there. What that gets you:

- Card `4242 4242 4242 4242`, any future expiry, any CVC. Stripe has a
  [longer list](https://docs.stripe.com/testing) including cards that decline,
  cards that require 3-D Secure, and cards that fail after authorisation.
- Every code path is byte-identical to live mode. Test mode is not a simulation
  of Stripe; it is Stripe, with a different ledger.
- No account activation, no business details, no bank account, no monthly fee.
- `stripe listen --forward-to .../webhooks/stripe` replays events at a local
  server, and `stripe trigger checkout.session.completed` fires one on demand.

Turning it into real money later, when there is a reason to:

1. Activate the Stripe account — business details and a bank account.
2. Swap in the `sk_live_…` key **and** the live endpoint's signing secret. They
   are different values from their test counterparts, and a mismatched secret
   shows up as a 400 on every webhook.
3. Watch `npx wrangler tail` during your first real payment.

Nothing in `src/payments.py` changes. That is the point of test mode.

Reconciliation, when you need it:

```bash
npx wrangler d1 execute surfboard-db --remote --command \
  "SELECT bundle, COUNT(*) AS sales, SUM(amount_total_cents)/100.0 AS gross
     FROM purchase GROUP BY bundle"
```

Stripe's dashboard remains the source of truth for money. The `purchase` table
is your own record, so questions like "what did we sell this month" do not need
an API call.

---

## Deliberately not built

Each of these is a real product decision, not an oversight:

- **Refunds.** Issue them from the Stripe dashboard. Doing it in-app means
  handling the `charge.refunded` webhook and deciding whether to claw back a
  bundle that may already have been spent.
- **Subscriptions.** `mode: "payment"` is a one-off charge. Recurring billing is
  `mode: "subscription"` plus the `invoice.*` and `customer.subscription.*`
  events, and a whole dunning story for failed renewals.
- **Sales tax / VAT.** Selling digital services across borders can create tax
  obligations from the first sale in some jurisdictions. Stripe Tax computes it;
  a merchant-of-record like Paddle or Lemon Squeezy becomes the legal seller and
  takes the obligation on entirely, for a higher fee. If you sell outside your
  own country, look at this before volume makes it expensive to fix.
- **SCA / 3-D Secure.** Already handled — hosted Checkout does the challenge
  flow for European cards on its own. Another reason not to build your own form.
- **Multi-quantity carts.** One bundle per checkout. `line_items[0][quantity]`
  is hardcoded to 1; the webhook would need to read the real quantity and credit
  that many.
