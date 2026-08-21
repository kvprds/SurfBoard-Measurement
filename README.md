# 🏄 SurfBoard-Measurement

[![Live demo](https://img.shields.io/badge/live_demo-open_the_app-0ea5e9?style=for-the-badge&logo=cloudflare&logoColor=white)](https://surfboard-measurement.tomer-berger08.workers.dev/dashboard)
![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Cloudflare](https://img.shields.io/badge/Cloudflare_Workers-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)
![D1](https://img.shields.io/badge/Cloudflare_D1-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)
![Free Tier](https://img.shields.io/badge/runs_on_free_tier-22c55e?style=for-the-badge)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Gemini](https://img.shields.io/badge/Google%20Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
![Stripe](https://img.shields.io/badge/Stripe-635BFF?style=for-the-badge&logo=stripe&logoColor=white)

**A full-stack web app that recommends surfboard dimensions from a surfer's video, using Google's Gemini multimodal AI — deployed on Cloudflare's edge, entirely on free tiers.**

🔗 **Live app:** <https://surfboard-measurement.tomer-berger08.workers.dev/dashboard>
▶️ **Explanation video:** <https://www.youtube.com/watch?v=mvt0JRw-Pr8>

> ⚠️ Software engineering **learning project**. Recommendations are AI-generated and are not professional sizing advice.
> Payments run in **Stripe test mode** — the full checkout flow works, no money moves.

---

## 🔍 Overview

A surfer signs in, uploads a video of themselves in the water, and enters their height, weight, and skill level. The app sends the footage to **Gemini 2.5 Flash**, which confirms the clip actually shows surfing, assesses technique, and recommends an ideal board **volume (liters)** and **length (feet/inches)**.

To keep recommendations grounded in real expertise, the app pulls an experienced coach's previous sizing decisions from the database and feeds them into the prompt as **few-shot examples**, so the model mirrors a human coach's logic rather than guessing from scratch. A fresh database starts with [`worker/seed_pro.sql`](worker/seed_pro.sql) — ten professional surfers with the height, weight, volume and length recorded for each — and the coach's own decisions accumulate on top as they review.

## ✨ Key Features

- **Gemini multimodal video analysis** — technique assessment + board sizing from raw footage
- **Few-shot prompting** from a coach's historical sizing decisions, read from D1 at request time
- **Video validation** — rejects clips that aren't actually surfing, and refunds the bundle
- **Google OAuth login** with HMAC-signed session cookies
- **Stripe Checkout** — real payments for the three bundles, credited by a signed, idempotent webhook
- **Tiered bundles** — instant AI analysis, manual coach review, or a live Zoom session
- **Admin dashboard** — manual review, sizing overrides, inventory, and user chat
- **Security** — CSRF protection, secure headers, HTTP-only cookies, per-user video access control

## 🏗️ Architecture

Two Cloudflare services do the work, plus two bindings that the app cannot function without:

| Service | Role | Cost |
| --- | --- | --- |
| **Cloudflare Python Workers** | Every route. FastAPI + Jinja2 on Pyodide. | Free — 100k req/day |
| **Cloudflare D1** | The dataset — sessions, recommendations, inventory, chat, purchase ledger, and the job queue. | Free — 5GB |
| **Workers KV** | Video files. A D1 row caps out around 1MB, so clips cannot live in the database. | Free — 1GB, no card |
| **Cron Trigger** | Sweeps queued analyses once a minute. | Free — 5 per account |

Video bytes never become Python objects — they stay on the JavaScript side of the Pyodide boundary from browser to storage to Gemini and back, because a Worker gets 128MB.

**No paid plan, and no payment card anywhere.** Two services were swapped out to get there, and both cost something real:

- **Cloudflare Queues → a D1 job table swept by a Cron Trigger.** Queues is the one service here with no free tier. The replacement keeps exactly-once claiming, retries, and dead-worker recovery; an analysis just waits up to a minute to start.
- **R2 → Workers KV.** R2 is the right tool for video, but enabling it puts a card on your Cloudflare account. KV caps a value at 25 MiB, holds 1GB free, has no range requests, and is eventually consistent — which meant teaching the sweeper that a clip it cannot read yet is a *retry*, not a verdict. [`src/storage.py`](worker/src/storage.py) is the seam; swapping to R2, Supabase or Cloudinary is a rewrite of that one file.

Rendering the heaviest page measures ~0.5ms against the free plan's 10ms CPU budget.

📖 **[ARCHITECTURE.md](worker/ARCHITECTURE.md)** — every change from the Flask original and why it was forced.

## 💳 Payments

The shop always showed $10 / $35 / $99. The Buy buttons were `href="#"` — they never charged anyone. They now run Stripe Checkout, with the bundle granted by a signature-verified, idempotent webhook rather than by the success-page redirect.

This runs in **test mode**: card `4242 4242 4242 4242` walks checkout → webhook → bundle credited, with no real money and no Stripe account activation. Stripe charges no monthly fee, so the code costs nothing to keep, and going live later is a key swap.

📖 **[PAYMENTS.md](worker/PAYMENTS.md)** — the money flow end to end, why the webhook and not the redirect, why duplicate deliveries must not double-credit, and what a sale would be worth after fees.

If it were live, per sale:

| | AI — $10 | Coach — $35 | Zoom — $99 |
| --- | ---: | ---: | ---: |
| Stripe fee (2.9% + 30c) | −$0.59 | −$1.32 | −$3.17 |
| Gemini analysis | −$0.01 | −$0.01 | — |
| Cloudflare | free tier | free tier | free tier |
| **You keep** | **$9.40** | **$33.67** | **$95.83** |

Stripe's fee is roughly 150× the AI cost. On a $10 sale, payment processing takes 5.9% and the AI takes 0.05% — the model is not the expensive part.

## 🚀 Getting Started

```bash
cd worker
uv run pywrangler dev          # local D1 and KV; nothing touches production
```

📖 **[DEPLOY.md](worker/DEPLOY.md)** — the full free-tier allowance table, creating the D1 database and KV namespace, every secret and where it comes from, and the Stripe test-mode webhook setup.

## ✅ Tests

```bash
cd worker
pip install fastapi jinja2 httpx
python3 tests/run.py
```

123 tests, run off-platform. `tests/fakes.py` stubs the Worker runtime and backs the D1 binding with **real SQLite**, so `schema.sql` and every query in `db.py` and `payments.py` are genuinely executed — including webhook signature rejection, replay rejection, the idempotency behaviour that stops one payment becoming five bundles, the sweeper's claim-and-retry semantics under two workers racing for the same job, and KV's eventual consistency not being mistaken for a missing video.

The suite deliberately runs **without** `python-multipart` installed, because the Worker bundle does not have it either — that mismatch is exactly how a 500 on every form POST once reached production while the tests stayed green.

## 📂 What's in here

| Path | Status | What it is |
| --- | --- | --- |
| `worker/src/worker.py` | **Current** | Worker entrypoint. Streaming upload/playback, plus the cron sweeper. |
| `worker/src/app.py` | **Current** | Every route, as a FastAPI app. |
| `worker/src/db.py` | **Current** | D1 data access. Replaces the SQLAlchemy models. |
| `worker/src/auth.py` | **Current** | Signed-cookie sessions, CSRF, Google OIDC. |
| `worker/src/payments.py` | **Current** | Stripe Checkout and the webhook. |
| `worker/src/gemini.py` | **Current** | Gemini Files + generateContent over REST. |
| `worker/src/storage.py` | **Current** | Where clips live. The one file to change to swap storage providers. |
| `worker/src/templates.py` | **Current** | The Jinja2 templates. |
| `worker/schema.sql` | **Current** | The D1 schema. |
| `worker/seed_pro.sql` | **Current** | Ten professional sizing decisions. Seeds the few-shot examples the prompt is grounded in. |

The original single-file Flask app that this was ported from is no longer in the
tree. It was the reference implementation, never the deployment, and keeping
1,200 lines of superseded code beside the thing that replaced it only invited
reading the wrong file. It stays in git history, and
[`ARCHITECTURE.md`](worker/ARCHITECTURE.md) walks through what changed and why:

```bash
git show 2bf383f:SurfBoard-Measurement/web.py
```

Two dataset scripts went with it: `jsonl.py`, which built a fine-tuning corpus
for an approach this app does not take, and `seed_db.py`, which imported
functions `web.py` had already stopped defining and so raised `ImportError`
before doing anything. The sizing data `seed_db.py` carried was the one part
worth keeping, and it now lives in `worker/seed_pro.sql`, where it actually
runs.

## 📌 Notes & Limitations

- This is a learning project — recommendations are AI-generated and **not** professional sizing advice.
- Runs entirely on **free tiers** — no Cloudflare, Stripe or Google subscription.
- Uploads are capped at **20MB**, under Workers KV's hard 25 MiB per-value ceiling — down from the Flask app's 500MB.
- Storage holds roughly **50 clips**, and clips expire after 30 days so it never grows past KV's 1GB free tier.
- No video seeking: KV cannot serve byte ranges, so the browser downloads a clip whole before it can scrub.
- Analyses are capped at **40/day** to stay inside Gemini's free quota, and start up to a minute late because that is the fastest a Cron Trigger fires.
- **Python Workers are in open beta**; the `python_workers` compatibility flag is required.
- Built and tested with the guidance of an Olympic surfing coach.
