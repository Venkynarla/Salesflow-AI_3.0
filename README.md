# SalesFlow AI Dashboard

GitHub-ready version extracted from the Colab notebook.

## What this app contains

- FastAPI backend
- Single-page dashboard in `frontend/index.html`
- Campaign and contact APIs
- Manual enrichment field support
- NVIDIA OpenAI-compatible AI drafting
- Email sending service
- Background follow-up scheduler
- Playwright-based enrichment attempt

## Project structure

```text
salesflow-ai/
├── backend/
│   ├── models/
│   ├── routers/
│   └── services/
├── frontend/
│   └── index.html
├── main.py
├── requirements.txt
├── Dockerfile
├── render.yaml
├── .env.example
└── .gitignore
```

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate   # Mac/Linux
# .venv\Scripts\activate  # Windows

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edit .env and add your NVIDIA_API_KEY

uvicorn main:app --reload --port 8000
```

Open:

```text
http://localhost:8000
```

Health check:

```text
http://localhost:8000/api/health
```

## Push to GitHub

```bash
cd salesflow-ai

git init
git add .
git commit -m "Initial SalesFlow AI app"

git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/salesflow-ai.git
git push -u origin main
```

## Deploy free on Render + Supabase (recommended for real use)

Render's free disk is ephemeral (wiped on every redeploy/restart), so SQLite
will lose your data. Supabase gives you a free, persistent Postgres database
instead — takes about 5 minutes to wire up.

**1. Create the Supabase project**
1. Go to https://supabase.com → New Project (free tier is fine).
2. Wait for it to finish provisioning (~2 minutes).
3. Go to **Project Settings → Database → Connection string** and copy the
   **URI** under "Connection pooling" (recommended for serverless/Render —
   it looks like `postgresql://postgres.xxxx:[PASSWORD]@aws-0-region.pooler.supabase.com:6543/postgres`).
4. Replace `[PASSWORD]` with your actual database password.

**2. Deploy on Render**
1. Push this repo to GitHub (see above).
2. Render → **New Web Service** → connect your repo → environment **Docker** → free plan.
3. Add these environment variables:
   - `DATABASE_URL` = the Supabase connection string from step 1
   - `NVIDIA_API_KEY` = your key (or leave blank for fallback templates)
   - `NVIDIA_MODEL=meta/llama-3.1-8b-instruct`
   - `EMAIL_DEV_MODE=true` (set to `false` once your SMTP settings are ready)
   - `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`
   - `SENDER_NAME` = your name
4. Deploy. The app auto-creates all tables (and auto-migrates any new
   columns) against Supabase on first boot — no manual SQL needed.
5. The **first account you register** on the live URL automatically becomes
   the platform admin.

Your app URL will look like `https://salesflow-ai.onrender.com` and your data
now survives redeploys, restarts, and sleep/wake cycles.

## Important notes

Render free tier gives a permanent URL, but the app may sleep after inactivity. When you open it again, it will wake up automatically (first request after sleep takes ~30-60s).

LinkedIn scraping may be blocked frequently. Use manual enrichment or a compliant enrichment API for reliable production results.

Multi-user notes: the first account ever registered becomes admin automatically. Admins can promote/demote other admins, restrict (block login for) any account, and monitor all user activity from the sidebar's **Admin Panel**. Everyone else only sees their own contacts and campaigns.

