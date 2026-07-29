# Deploying the Survey QA Agent (container)

This app is **Streamlit + Playwright (headless Chromium)** and runs multi-minute survey
walks, so it needs a **persistent container** — not a serverless host like Vercel. The
`Dockerfile` bundles Chromium (via the official Playwright image), so any container platform
works. Two easy paths below.

## Before you deploy
- **Rotate your Anthropic key** (the earlier one was shared in chat) and set the new one as a
  **platform secret** named `ANTHROPIC_API_KEY`. It is never baked into the image (`.env` is
  git/dockerignored).
- **Access note:** clicking **Run live** drives the real Decipher survey and submits sessions.
  For a public URL, prefer demo mode or put the app behind auth.
- Needs **≥ 2 GB RAM** (Chromium). Free tiers (512 MB) will OOM during a walk.

## First-time git setup (this folder isn't a repo yet)
```bash
cd /home/nik/qa-agent
git init && git add -A && git commit -m "Survey QA Agent"
# push to a new GitHub repo:
git remote add origin git@github.com:<you>/survey-qa-agent.git
git push -u origin main
```

## Option 1 — Render (git-based, simplest)
1. Push the repo to GitHub (above).
2. In Render: **New → Blueprint**, pick the repo. It reads `render.yaml` and builds the
   Dockerfile.
3. Add the secret `ANTHROPIC_API_KEY` in the service's **Environment** tab.
4. Deploy. Render gives you `https://survey-qa-agent.onrender.com`.

## Option 2 — Fly.io (CLI)
```bash
cd /home/nik/qa-agent
flyctl launch --no-deploy          # uses fly.toml; pick an app name/region
flyctl secrets set ANTHROPIC_API_KEY=sk-ant-...   # your ROTATED key
flyctl deploy
```

## Local Docker smoke test (optional, if you have Docker)
```bash
docker build -t survey-qa-agent .
docker run -p 8501:8501 -e ANTHROPIC_API_KEY=sk-ant-... survey-qa-agent
# open http://localhost:8501
```

## Notes
- The image ships demo-mode caches + screenshots, so **Load last run** works instantly on a
  fresh deploy. **Run live** / routing walks Chromium in the container (that's why we need a
  real container host and 2 GB RAM).
- The bundled `spec/Survey_2.docx` is the default questionnaire; the UI's uploader overrides it.
