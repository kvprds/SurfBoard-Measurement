# 🏄 SurfBoard-Measurement

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Cloudflare](https://img.shields.io/badge/Cloudflare_Workers-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)
![D1](https://img.shields.io/badge/Cloudflare_D1-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Gemini](https://img.shields.io/badge/Google%20Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
![Stripe](https://img.shields.io/badge/Stripe-635BFF?style=for-the-badge&logo=stripe&logoColor=white)

**A full-stack web app that recommends surfboard dimensions from a surfer's video, using Google's Gemini multimodal AI — deployed on Cloudflare's edge.**

> ⚠️ Software engineering **learning project**. Recommendations are AI-generated and are not professional sizing advice.

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

| Service | Role |
| --- | --- |
| **Cloudflare Python Workers** | Every route. FastAPI + Jinja2 running on Pyodide. |
| **Cloudflare D1** | The dataset — sessions, recommendations, inventory, chat, purchase ledger. |
| **Cloudflare R2** | Video files. A D1 row caps out around 1MB, so the clips cannot live in the database. |
| **Cloudflare Queues** | Async Gemini analysis. A Worker cannot keep a thread alive past its response. |

Video never enters Python memory on any leg — browser → R2 → Gemini → browser all stream, because a Worker has 128MB and one clip can exceed it.

📖 **[ARCHITECTURE.md](worker/ARCHITECTURE.md)** — every change from the Flask original and why it was forced.

## 💳 Payments

The shop always showed $10 / $35 / $99. The Buy buttons were `href="#"` — they never charged anyone. They now run Stripe Checkout, with the bundle granted by a signature-verified, idempotent webhook rather than by the success-page redirect.

📖 **[PAYMENTS.md](worker/PAYMENTS.md)** — the money flow end to end, why the webhook and not the redirect, why duplicate deliveries must not double-credit, and what one sale is actually worth after fees.

The short version of the economics:

| | AI — $10 | Coach — $35 | Zoom — $99 |
| --- | ---: | ---: | ---: |
| Stripe fee (2.9% + 30c) | −$0.59 | −$1.32 | −$3.17 |
| Gemini analysis | −$0.01 | −$0.01 | — |
| Cloudflare (marginal) | ~$0.00 | ~$0.00 | ~$0.00 |
| **You keep** | **$9.40** | **$33.67** | **$95.83** |

Fixed cost is the **$5/month Workers Paid plan**. Break-even is one AI bundle a month. Stripe's fee is roughly 150× the AI cost — the model is not the expensive part.

## 🚀 Getting Started

```bash
cd worker
uv run pywrangler dev          # local D1, R2 and queue; nothing touches production
```

📖 **[DEPLOY.md](worker/DEPLOY.md)** — creating the D1 database, R2 bucket and queues, every secret and where it comes from, and the Stripe webhook setup.

## ✅ Tests

```bash
cd worker
pip install fastapi jinja2 httpx python-multipart
python3 tests/run.py
```

77 tests, run off-platform. `tests/fakes.py` stubs the Worker runtime and backs the D1 binding with **real SQLite**, so `schema.sql` and every query in `db.py` and `payments.py` are genuinely executed — including webhook signature rejection, replay rejection, and the idempotency behaviour that stops one payment becoming five bundles.

## 📂 What's in here

| Path | Status | What it is |
| --- | --- | --- |
| `worker/src/worker.py` | **Current** | Worker entrypoint. Streaming upload/playback, plus the queue consumer. |
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
- Uploads are capped at **100MB** (Cloudflare's request body limit on Free and Pro plans), down from the Flask app's 500MB.
- **Python Workers are in open beta**; the `python_workers` compatibility flag is required.
- Built and tested with the guidance of an Olympic surfing coach.
