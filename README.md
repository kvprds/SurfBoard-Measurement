# 🏄 SurfBoard-Measurement

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white)
![Gemini](https://img.shields.io/badge/Google%20Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)

**A full-stack web app that recommends surfboard dimensions from a surfer's video, using Google's Gemini multimodal AI.**

> ⚠️ Software engineering **learning project**. Built to explore full-stack development and applied AI — not a production service.

---

## 🔍 Overview

A surfer signs in, uploads a video of themselves in the water, and enters their height, weight, and skill level. The app sends the footage to **Gemini 2.5 Flash**, which confirms the clip actually shows surfing, assesses technique, and recommends an ideal board **volume (liters)** and **length (feet/inches)**.

To keep recommendations grounded in real expertise, the app pulls an experienced coach's previous sizing decisions from the database and feeds them into the prompt as **few-shot examples**, so the model mirrors a human coach's logic rather than guessing from scratch.

## ✨ Key Features

- **Gemini multimodal video analysis** — technique assessment + board sizing from raw footage
- **Few-shot prompting** from a coach's historical sizing decisions for grounded results
- **Video validation** — rejects (and refunds) clips that aren't actually surfing
- **Google OAuth login** with secure session handling
- **Tiered bundles** — instant AI analysis, manual coach review, or a live Zoom session
- **Admin dashboard** — manual review, sizing overrides, inventory, and user chat
- **Email notifications** when results are ready
- **Security** — CSRF protection, secure headers, HTTP-only cookies

## 💻 Tech Stack

| Component | Technology |
| --- | --- |
| Backend | Python, Flask |
| Database | SQLite (SQLAlchemy ORM) |
| AI | Google Gemini 2.5 Flash (`google-genai`) |
| Auth | Google OAuth (Authlib) |
| Email | SMTP (Gmail) |

## 🚀 Getting Started

**1. Install dependencies**

```bash
pip install flask flask-sqlalchemy authlib google-genai
```

**2. Configure credentials**

Open `web.py` and replace every `CHANGE ME` placeholder with your own values:

| Setting | What it is |
| --- | --- |
| `GEMINI_API_KEY` | Your Google Gemini API key ([AI Studio](https://aistudio.google.com/)) |
| `CREDENTIALS_PATH` | Path to your Google OAuth client-secret JSON |
| `SUPER_ADMIN_EMAIL` | The email that gets admin access |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` | A Gmail address + [app password](https://myaccount.google.com/apppasswords) for notifications |

> 🔒 Keep your real keys out of Git. Use a `.env` file or environment variables — never commit credentials. A `.gitignore` should exclude `.env`, `*.db`, and `uploads/`.

**3. Run**

```bash
python web.py
```

Then open **http://localhost:5000**.

## 📌 Notes & Limitations

- This is a learning project — recommendations are AI-generated and **not** professional sizing advice.
- Local dev runs with `OAUTHLIB_INSECURE_TRANSPORT` enabled; use HTTPS in any real deployment.
- Built and tested with the guidance of an Olympic surfing coach.
