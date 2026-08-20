# 🏄 SurfBoard-Measurement

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Cloudflare](https://img.shields.io/badge/Cloudflare_Workers-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)
![D1](https://img.shields.io/badge/Cloudflare_D1-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)
![Free Tier](https://img.shields.io/badge/runs_on_free_tier-22c55e?style=for-the-badge)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Gemini](https://img.shields.io/badge/Google%20Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
![Stripe](https://img.shields.io/badge/Stripe-635BFF?style=for-the-badge&logo=stripe&logoColor=white)

**A full-stack web app that recommends surfboard dimensions from a surfer's video, using Google's Gemini multimodal AI — deployed on Cloudflare's edge, entirely on free tiers.**

> ⚠️ Software engineering **learning project**. Recommendations are AI-generated and are not professional sizing advice.
> Payments run in **Stripe test mode** — the full checkout flow works, no money moves.

---

## 🔍 Overview

A surfer signs in, uploads a video of themselves in the water, and enters their height, weight, and skill level. The app sends the footage to **Gemini 2.5 Flash**, which confirms the clip actually shows surfing, assesses technique, and recommends an ideal board **volume (liters)** and **length (feet/inches)**.

To keep recommendations grounded in real expertise, the app pulls an experienced coach's previous sizing decisions from the database and feeds them into the prompt as **few-shot examples**, so the model mirrors a human coach's logic rather than guessing from scratch.

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
| **Cloudflare R2** | Video files. A D1 row caps out around 1MB, so clips cannot live in the database. | Free — 10GB, egress always free |
| **Cron Trigger** | Sweeps queued analyses once a minute. | Free — 5 per account |

Video never enters Python memory on any leg — browser → R2 → Gemini → browser all stream, because a Worker has 128MB and one clip can exceed it.

**No paid plan required.** Cloudflare Queues was the first home for the async analysis and is the natural fit, but it is the one service here with no free tier — so the job lives in a D1 table swept by a Cron Trigger, with the same exactly-once claiming, retries, and dead-worker recovery. Rendering the heaviest page measures ~0.5ms against the free plan's 10ms CPU budget.

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
uv run pywrangler dev          # local D1 and R2; nothing touches production
```

📖 **[DEPLOY.md](worker/DEPLOY.md)** — the full free-tier allowance table, creating the D1 database and R2 bucket, every secret and where it comes from, and the Stripe test-mode webhook setup.

## ✅ Tests

```bash
cd worker
pip install fastapi jinja2 httpx python-multipart
python3 tests/run.py
```

102 tests, run off-platform. `tests/fakes.py` stubs the Worker runtime and backs the D1 binding with **real SQLite**, so `schema.sql` and every query in `db.py` and `payments.py` are genuinely executed — including webhook signature rejection, replay rejection, the idempotency behaviour that stops one payment becoming five bundles, and the sweeper's claim-and-retry semantics under two workers racing for the same job.

## 📂 What's in here

| Path | Status | What it is |
| --- | --- | --- |
| `worker/src/worker.py` | **Current** | Worker entrypoint. Streaming upload/playback, plus the cron sweeper. |
| `worker/src/app.py` | **Current** | Every route, as a FastAPI app. |
| `worker/src/db.py` | **Current** | D1 data access. Replaces the SQLAlchemy models. |
| `worker/src/auth.py` | **Current** | Signed-cookie sessions, CSRF, Google OIDC. |
| `worker/src/payments.py` | **Current** | Stripe Checkout and the webhook. |
| `worker/src/gemini.py` | **Current** | Gemini Files + generateContent over REST. |
| `worker/src/templates.py` | **Current** | The Jinja2 templates. |
| `worker/schema.sql` | **Current** | The D1 schema. |
| `SurfBoard-Measurement/web.py` | Reference | The original single-file Flask app. Kept as the reference implementation; not deployed. |
| `SurfBoard-Measurement/surf_data.csv` | Reference | Header-only template, one row per video, read by `jsonl.py`. |
| `SurfBoard-Measurement/jsonl.py` | Reference | Builds a fine-tuning dataset from the CSV. Unused — sizing comes from few-shot prompting at request time, not a fine-tuned model. |
| `SurfBoard-Measurement/seed_db.py` | **Does not run** | Written against an earlier `web.py`; imports functions that no longer exist. Kept for the dataset it carries. |

## 📌 Notes & Limitations

- This is a learning project — recommendations are AI-generated and **not** professional sizing advice.
- Runs entirely on **free tiers** — no Cloudflare, Stripe or Google subscription.
- Uploads are capped at **25MB** to keep several hundred clips inside R2's 10GB free tier; Cloudflare's own ceiling is 100MB, down from the Flask app's 500MB.
- Analyses are capped at **40/day** to stay inside Gemini's free quota, and start up to a minute late because that is the fastest a Cron Trigger fires.
- **Python Workers are in open beta**; the `python_workers` compatibility flag is required.
- Built and tested with the guidance of an Olympic surfing coach.
