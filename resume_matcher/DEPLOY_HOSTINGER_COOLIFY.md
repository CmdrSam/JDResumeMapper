# Deploy on Hostinger VPS with Coolify

This document provides production-ready deployment steps for the Streamlit app.

## 1) Prerequisites

- Hostinger VPS (Ubuntu 22.04+ recommended)
- Domain/subdomain for the app (optional but strongly recommended)
- Git repository containing this project
- DeepSeek API key

Recommended VPS size:
- Minimum: 2 vCPU, 4 GB RAM
- Preferred for concurrent usage: 4 vCPU, 8 GB RAM

## 2) Prepare VPS

SSH into the VPS and update packages:

```bash
sudo apt update && sudo apt upgrade -y
```

If using UFW:

```bash
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw enable
```

## 3) Install Coolify

Follow the official install guide:
- https://coolify.io/docs

After install:
- Open `http://<VPS_IP>:8000`
- Finish onboarding
- Add your Git provider in Coolify (GitHub/GitLab)

## 4) Create application in Coolify

1. Create **New Resource** -> **Application**
2. Select your repository
3. Set **Base Directory** to:
   - `resume_matcher`
4. Build method:
   - **Dockerfile**
   - Dockerfile path: `Dockerfile`
5. Exposed port:
   - `8501`

## 5) Environment variables

Set in Coolify application environment:

- `DEEPSEEK_API_KEY=<your_key>`
- `DEEPSEEK_MODEL=deepseek-chat` (or your chosen model)
- `PYTHONUNBUFFERED=1` (optional)

Do not commit `.env` to the repository.

## 6) Persistent storage (important)

Add a persistent volume in Coolify:

- Container path: `/app/outputs`

Why:
- Keeps generated PDFs/CSV/JSON across container restarts and deploys
- Works with per-session output folders created by the app

## 7) Domain and HTTPS

In Coolify:
1. Add your domain/subdomain (example: `matcher.example.com`)
2. Enable SSL (Let's Encrypt)
3. Deploy application

## 8) Verify deployment

After deployment:
1. Open the app URL
2. Upload one JD and one resume
3. Click **Run match**
4. Confirm:
   - Results table appears
   - Download buttons work (CSV, JSON, PDFs)
   - Output files are under `/app/outputs/sessions/...`

## 9) Concurrency + cleanup behavior (already implemented)

Current Streamlit protections:
- Per-session output directories under `outputs/sessions/...`
- Automatic cleanup of old run folders
- Throttling guard to limit concurrent active runs

If server is busy, users are asked to retry.

## 10) Operations checklist

- Use Coolify logs to monitor runtime failures and API errors
- Rotate API keys periodically
- Keep VPS clock/timezone sane (UTC recommended)
- Backup `/app/outputs` volume if required for audit/history
- Upgrade VPS resources before increasing concurrency

## 11) Rollback strategy

If a deploy fails:
1. In Coolify, redeploy the previous working commit/tag
2. Keep persistent volume mounted at `/app/outputs`
3. Validate with a quick JD + resume test

## 12) Queue + workers + horizontal scaling (Docker CLI)

This project now supports queue-based processing:
- Streamlit app is frontend-only (submit/poll)
- Redis stores queue/job state
- RQ workers process match jobs

Use `docker-compose.yml` in `resume_matcher/`.

Create/update `.env`:

```bash
DEEPSEEK_API_KEY=your_key_here
DEEPSEEK_MODEL=deepseek-chat
REDIS_URL=redis://redis:6379/0
```

Start stack:

```bash
docker compose up -d --build
```

Scale workers horizontally (example: 6 workers):

```bash
docker compose up -d --scale worker=6
```

Check services:

```bash
docker compose ps
docker compose logs -f app
docker compose logs -f worker
```

The Streamlit app listens on port `8501`.
