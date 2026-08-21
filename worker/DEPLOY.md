# Deploying to Cloudflare — free tier

This runs entirely on free plans, and **no payment card is needed anywhere** —
not on Cloudflare, not on Stripe, not on Google. Total time from a clean
account: about 30 minutes, most of it waiting on Google's OAuth consent screen.

Every command below is run from the `worker/` directory.

## What it costs: nothing

| Service | Free allowance | What this site uses |
| --- | --- | --- |
| **Workers** | 100,000 requests/day | A portfolio site sees a few hundred. |
| **Workers** CPU | 10ms per invocation | Measured ~0.5ms to render the heaviest page. |
| **Cron Triggers** | 5 per account | 1 — the analysis sweeper. |
| **D1** | 5GB, 5M rows read/day, 100k written/day | Kilobytes. Thousands of rows read per day at most. |
| **Workers KV** | 1 GB storage, 1,000 writes/day, 100k reads/day | ~50 clips at the 20MB cap, expiring after 30 days. |
| **Gemini 2.5 Flash** | ~250 requests/day | Capped at 40/day by `DAILY_ANALYSIS_CAP`. |
| **Stripe** | No monthly fee | Test mode moves no money at all. |
| **Resend** (optional) | 3,000 emails/month | A handful. |

Three things keep it there, and all are deliberate:

- **No Cloudflare Queues.** Queues is the one service here with no free tier, so
  the analysis job lives in a D1 table swept by a Cron Trigger instead. See
  [ARCHITECTURE.md](ARCHITECTURE.md#the-sweeper-is-a-queue-you-can-read).
- **No R2.** R2 is the right tool for video, but enabling it puts a payment card
  on your Cloudflare account. Workers KV is included in the free plan with no
  card, so clips live there instead — with real limits, spelled out in
  [`src/storage.py`](src/storage.py) and
  [ARCHITECTURE.md](ARCHITECTURE.md#video-lives-in-kv-which-is-not-a-blob-store).
- **Stripe stays in test mode.** The full checkout runs, the webhook fires, the
  bundle is credited — with test cards. See [PAYMENTS.md](PAYMENTS.md).

## 0. Prerequisites

```bash
# uv drives pywrangler, the CLI for Python Workers
curl -LsSf https://astral.sh/uv/install.sh | sh
npm install -g wrangler
wrangler login
```

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

## 2. Create the video store

D1 rows cap out around 1MB, so the clips cannot live in the database:

```bash
npx wrangler kv namespace create VIDEOS
```

Copy the printed `id` into `wrangler.jsonc`, replacing
`REPLACE_WITH_YOUR_KV_NAMESPACE_ID`.

No card is required for this, which is the whole reason it is KV and not R2. No
queue to create either — the Cron Trigger is declared in `wrangler.jsonc` and
needs no setup.

## 3. Configure

Edit the `vars` block in `wrangler.jsonc`:

| Variable | Set it to |
| --- | --- |
| `APP_BASE_URL` | Your final Worker URL. Leave the placeholder for now; fix it after step 4. |
| `SUPER_ADMIN_EMAIL` | The Google account that gets the admin dashboard. |
| `EMAIL_FROM` | A verified Resend sender, or leave the default to log emails instead of sending. |
| `MAX_UPLOAD_BYTES` | `20971520` (20MB). Workers KV caps a value at 25 MiB, so do not go above about 24MB. |
| `VIDEO_TTL_DAYS` | `30`. Clips delete themselves after this, keeping you inside KV's 1 GB. |
| `DAILY_ANALYSIS_CAP` | `40`. Guards Gemini's free quota. `0` disables the cap. |

Then set the secrets. These are encrypted at rest and never appear in
`wrangler.jsonc`:

```bash
# Signs the session cookie. Any 32+ random bytes.
python3 -c "import secrets; print(secrets.token_hex(32))" | npx wrangler secret put SESSION_SECRET

npx wrangler secret put GOOGLE_CLIENT_ID
npx wrangler secret put GOOGLE_CLIENT_SECRET
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put STRIPE_SECRET_KEY        # the sk_test_... key
npx wrangler secret put STRIPE_WEBHOOK_SECRET
npx wrangler secret put RESEND_API_KEY           # optional
```

Where each one comes from:

- **Google OAuth** — [console.cloud.google.com](https://console.cloud.google.com/apis/credentials)
  → Create Credentials → OAuth client ID → Web application. Add
  `https://<your-worker>.workers.dev/authorize` as an authorised redirect URI.
  The Flask app read these from a `client_secret.json` on disk; a Worker has no
  filesystem, so they are secrets. Free.
- **Gemini** — [aistudio.google.com](https://aistudio.google.com/) → Get API key.
  Free tier, no billing account needed. Note that Google may use free-tier
  prompts to improve their models — do not point this at footage you would mind
  them seeing.
- **Stripe** — [dashboard.stripe.com/apikeys](https://dashboard.stripe.com/apikeys).
  Use the **`sk_test_…`** key. A test-mode key needs no account activation, no
  bank details, and charges nothing.
- **Resend** — [resend.com/api-keys](https://resend.com/api-keys). Optional;
  leave it unset and emails are logged instead of sent.

## 4. Deploy

```bash
uv run pywrangler deploy
```

Note the URL it prints, put it in `APP_BASE_URL`, and deploy once more so the
OAuth and Stripe redirect URLs point at the right host.

## 5. Point Stripe at the webhook

In [dashboard.stripe.com/webhooks](https://dashboard.stripe.com/webhooks) — with
the **Test mode** toggle on — add an endpoint:

- **URL**: `https://<your-worker>.workers.dev/webhooks/stripe`
- **Event**: `checkout.session.completed`

Stripe shows a signing secret starting with `whsec_`. That is
`STRIPE_WEBHOOK_SECRET` — set it and redeploy.

Then buy a bundle yourself with test card `4242 4242 4242 4242`, any future
expiry, any CVC. No money moves. The bundle should appear on your dashboard
within a second or two of the redirect.

## Local development

```bash
cp .dev.vars.example .dev.vars     # fill it in; it is gitignored
npx wrangler d1 execute surfboard-db --local --file=./schema.sql
uv run pywrangler dev
```

`pywrangler dev` runs a local D1 (SQLite on disk) and a local KV, so nothing
touches production.

**Cron Triggers do not fire on their own in `dev`.** Poke the sweeper by hand:

```bash
curl "http://localhost:8787/__scheduled?cron=*+*+*+*+*"
```

For webhooks against your local server, `stripe listen --forward-to
http://localhost:8787/webhooks/stripe` prints a different `whsec_` — use that
one in `.dev.vars`.

## Running the tests

```bash
pip install fastapi jinja2 httpx
python3 tests/run.py
```

These run off-platform. `tests/fakes.py` stubs the Worker runtime and backs the
D1 binding with real SQLite, so `schema.sql` and every query in `db.py` and
`payments.py` are actually executed — including the webhook signature checks,
the idempotency behaviour, and the sweeper's claim-and-retry semantics.

## Useful commands

```bash
npx wrangler tail                                    # live logs, sweeper included
npx wrangler d1 execute surfboard-db --remote --command \
  "SELECT id, status, attempts, error_message FROM surfer WHERE status != 'complete'"
npx wrangler d1 execute surfboard-db --remote --command \
  "SELECT bundle, COUNT(*), SUM(amount_total_cents)/100.0 FROM purchase GROUP BY bundle"
npx wrangler kv key list --binding VIDEOS            # what is using your 1 GB
```

## Staying inside the free tier

The cron fires every minute, which is 1,440 invocations a day against the
100,000/day request budget — about 1.5%. If you want that lower, widen the
schedule in `wrangler.jsonc` (`*/5 * * * *` for every five minutes); the only
cost is that a surfer waits longer for their analysis to start.

The allowance that runs out first is **KV storage**: 1 GB, which is about 50
clips at the 20MB cap. `VIDEO_TTL_DAYS` is what keeps that in check — every clip
carries an expiry and deletes itself, so storage plateaus instead of growing.
Deleting a session from the admin panel removes its clips immediately.

The other free-plan ceiling worth knowing is **1,000 KV writes per day**. One
upload is one write, so it is not a constraint for a portfolio site, but it is
not a number you can scale into.

### If you outgrow KV

[`src/storage.py`](src/storage.py) is the only file that knows where video
lives. Replacing it is the whole migration:

| Option | Free tier | Card required |
| --- | --- | --- |
| **Cloudflare R2** | 10 GB, free egress | Yes — the reason it is not used here |
| **Supabase Storage** | 1 GB | No |
| **Cloudinary** | Video-oriented, 25 credits/month | No |

R2 is the one to move to if you ever add a card: it is in the same runtime, it
supports range requests (so video seeking works properly), and it has no
25 MiB-per-object ceiling.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Login bounces to "That sign-in link did not match" | `APP_BASE_URL` does not match the redirect URI registered with Google. |
| Webhook returns 400 | `STRIPE_WEBHOOK_SECRET` belongs to a different endpoint. The CLI and the dashboard issue different secrets. |
| Paid but no bundle appeared | Check `wrangler tail` during the payment. The webhook is the only thing that grants bundles; the success redirect deliberately grants nothing. |
| Analysis stuck on "Processing" | The sweeper runs once a minute. If it is still stuck after a few minutes, check `wrangler tail` and the `status` / `error_message` columns. After 3 attempts a job is marked `failed` and the bundle is refunded. |
| Upload rejected at 413 | Over `MAX_UPLOAD_BYTES`. Workers KV will not store a value above 25 MiB, so this ceiling is not raisable much. |
| A clip stopped playing after a month | `VIDEO_TTL_DAYS` expired it. Raise it, or accept it — the alternative is storage that grows until it leaves the free tier. |
| `Error 1102: Worker exceeded resource limits` | A page exceeded the free plan's 10ms CPU. Isolates tolerate infrequent overages; if it is persistent, the admin page with a very large queue is the likely culprit. |
| Every form POST 500s (`python-multipart ... must be installed`) | An old build. Forms are parsed by `_form_fields()` in `app.py` now, with no such dependency — redeploy from `main`. |
| `TypeError: Default.scheduled() takes N positional arguments` once a minute | The cron handler's signature drifted from what the runtime passes. `scheduled(self, controller=None, env=None, ctx=None)` accepts every shape the beta runtime has used; redeploy from `main`. |
| Analyses refused with 429 | `DAILY_ANALYSIS_CAP` reached. Raise it, or set `0`, and check your Gemini quota first. |
