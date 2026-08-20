# Deploying to Cloudflare

Every command below is run from the `worker/` directory. Total time from a clean
account: about 30 minutes, most of it waiting on Google's OAuth consent screen.

## 0. Prerequisites

```bash
# uv drives pywrangler, the CLI for Python Workers
curl -LsSf https://astral.sh/uv/install.sh | sh
npm install -g wrangler
wrangler login
```

You need the **Workers Paid plan ($5/month)**. Two reasons, both hard limits
rather than preferences:

- The free plan allows **10ms of CPU per request**. Rendering a page with Jinja2
  and verifying an HMAC costs more than that, so pages would intermittently
  return `Error 1102`.
- **Queues is not on the free plan at all**, and the queue is what replaced the
  background thread.

## 1. Create the two services

```bash
# Service 1 — D1, the dataset
npx wrangler d1 create surfboard-db
```

Copy the printed `database_id` into `wrangler.jsonc`, replacing
`REPLACE_WITH_YOUR_D1_DATABASE_ID`. Then create the tables:

```bash
npx wrangler d1 execute surfboard-db --remote --file=./schema.sql
```

Service 2 is the Worker itself, deployed in step 4.

## 2. Create the supporting bindings

D1 rows cap out around 1MB, so video cannot live in the database, and a Worker
cannot keep a thread alive after it responds. These two cover both:

```bash
npx wrangler r2 bucket create surfboard-videos
npx wrangler queues create surfboard-analysis
npx wrangler queues create surfboard-analysis-dlq
```

## 3. Configure

Edit the `vars` block in `wrangler.jsonc`:

| Variable | Set it to |
| --- | --- |
| `APP_BASE_URL` | Your final Worker URL. Leave the placeholder for now; fix it after step 4. |
| `SUPER_ADMIN_EMAIL` | The Google account that gets the admin dashboard. |
| `EMAIL_FROM` | A verified Resend sender, or leave the default to log emails instead of sending. |

Then set the secrets. These are encrypted at rest and never appear in
`wrangler.jsonc`:

```bash
# Signs the session cookie. Any 32+ random bytes.
python3 -c "import secrets; print(secrets.token_hex(32))" | npx wrangler secret put SESSION_SECRET

npx wrangler secret put GOOGLE_CLIENT_ID
npx wrangler secret put GOOGLE_CLIENT_SECRET
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put STRIPE_SECRET_KEY
npx wrangler secret put STRIPE_WEBHOOK_SECRET
npx wrangler secret put RESEND_API_KEY        # optional
```

Where each one comes from:

- **Google OAuth** — [console.cloud.google.com](https://console.cloud.google.com/apis/credentials)
  → Create Credentials → OAuth client ID → Web application. Add
  `https://<your-worker>.workers.dev/authorize` as an authorised redirect URI.
  The Flask app read these from a `client_secret.json` on disk; a Worker has no
  filesystem, so they are secrets.
- **Gemini** — [aistudio.google.com](https://aistudio.google.com/) → Get API key.
- **Stripe** — [dashboard.stripe.com/apikeys](https://dashboard.stripe.com/apikeys).
  Use the `sk_test_…` key until you have tested the flow end to end.
- **Resend** — [resend.com/api-keys](https://resend.com/api-keys).

## 4. Deploy

```bash
uv run pywrangler deploy
```

Note the URL it prints, put it in `APP_BASE_URL`, and deploy once more so the
OAuth and Stripe redirect URLs point at the right host.

## 5. Point Stripe at the webhook

In [dashboard.stripe.com/webhooks](https://dashboard.stripe.com/webhooks), add an
endpoint:

- **URL**: `https://<your-worker>.workers.dev/webhooks/stripe`
- **Event**: `checkout.session.completed`

Stripe shows a signing secret starting with `whsec_`. That is
`STRIPE_WEBHOOK_SECRET` — set it and redeploy.

Test it before taking real money:

```bash
stripe listen --forward-to https://<your-worker>.workers.dev/webhooks/stripe
stripe trigger checkout.session.completed
```

Then buy a bundle yourself with Stripe's test card `4242 4242 4242 4242`, any
future expiry, any CVC.

## Local development

```bash
cp .dev.vars.example .dev.vars     # fill it in; it is gitignored
uv run pywrangler dev
```

`pywrangler dev` runs a local D1 (SQLite on disk), a local R2, and a local
queue, so nothing touches production. Apply the schema locally once:

```bash
npx wrangler d1 execute surfboard-db --local --file=./schema.sql
```

For webhooks against your local server, `stripe listen --forward-to
http://localhost:8787/webhooks/stripe` prints a different `whsec_` — use that
one in `.dev.vars`.

## Running the tests

```bash
pip install fastapi jinja2 httpx python-multipart
python3 tests/run.py
```

These run off-platform. `tests/fakes.py` stubs the Worker runtime and backs the
D1 binding with real SQLite, so `schema.sql` and every query in `db.py` and
`payments.py` are actually executed — including the webhook signature checks and
the idempotency behaviour.

## Useful commands

```bash
npx wrangler tail                                    # live logs
npx wrangler d1 execute surfboard-db --remote --command "SELECT * FROM purchase"
npx wrangler queues consumer list surfboard-analysis
npx wrangler d1 execute surfboard-db --remote --command \
  "SELECT bundle, COUNT(*), SUM(amount_total_cents)/100.0 FROM purchase GROUP BY bundle"
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Error 1102: Worker exceeded resource limits` | Still on the free plan's 10ms CPU limit. |
| Login bounces to "That sign-in link did not match" | `APP_BASE_URL` does not match the redirect URI registered with Google. |
| Webhook returns 400 | `STRIPE_WEBHOOK_SECRET` belongs to a different endpoint. The CLI and the dashboard issue different secrets. |
| Paid but no bundle appeared | Check `wrangler tail` during the payment. The webhook is the only thing that grants bundles; the success-page redirect deliberately grants nothing. |
| Analysis stuck on "Processing" | `npx wrangler tail` and look for queue-consumer errors. After 3 retries the job lands on `surfboard-analysis-dlq`. |
