# Deploying DANAH to Railway

The whole product — API, worker, scheduler, Postgres+pgvector and Redis — on one platform,
served at one URL. About 20 minutes of clicks. The UI is served by the backend, so there is one
address and no CORS to configure.

> ⚠️ **Read this first.** Railway is convenient foreign cloud. It is fine for a **demo on the
> synthetic seed data** this repo ships. It is **not** where real OFFICIAL-SENSITIVE ministry data
> goes — that is the ministry's own approved/sovereign infrastructure, and that decision is theirs
> (`docs/EXTERNAL_APIS.md` §7). Nothing in the seed data is real, so a Railway demo is safe.

---

## Before you start

1. **Rotate the OpenAI key** if you have not — the old one went through chat. `platform.openai.com`.
2. Push the repo to GitHub (already done: `DANAH_v1`).
3. Have three secrets ready. Generate them now — in Git Bash or WSL:
   ```bash
   openssl rand -hex 48    # JWT_SECRET_KEY
   openssl rand -hex 32    # WEBHOOK_HMAC_DEFAULT_SECRET
   # and pick a strong ADMIN_INITIAL_PASSWORD
   ```
   Paste them somewhere for a minute. You will put them into Railway below.

---

## Step 1 — Create the project and add the databases

1. Go to **railway.com** → sign in with GitHub → **New Project**.
2. Click **+ Create** → **Database** → **Add PostgreSQL**.
   - ⚠️ **You need pgvector.** Railway's default Postgres has it available — after the DB is
     created, open it → **Data** tab and confirm you can run `CREATE EXTENSION IF NOT EXISTS
     vector;`. If your Postgres plugin does not allow it, delete it and instead **+ Create → Template →** search **"pgvector"** and deploy that Postgres-with-pgvector template. The app's
     first migration runs `CREATE EXTENSION vector`, so this must work or the deploy fails loudly.
3. Click **+ Create** → **Database** → **Add Redis**.

You now have `Postgres` and `Redis` services in the project canvas.

---

## Step 2 — Deploy the app (the `web` service)

1. **+ Create** → **GitHub Repo** → pick **`Tanzeel607/DANAH_v1`**.
2. Railway detects `railway.json` and the `Dockerfile` and starts a build. Let it finish the first
   build; it will fail to boot until you add the variables below — that is expected.
3. Open the new service → rename it to **`web`** (Settings → Service Name).
4. Go to the **Variables** tab and add these. Use the **Raw Editor** and paste the block below,
   replacing the four `__...__` placeholders with your real values:

   ```
   APP_ENV=production
   APP_DEBUG=false
   LOG_LEVEL=INFO

   DATABASE_URL=${{Postgres.DATABASE_URL}}
   REDIS_URL=${{Redis.REDIS_URL}}

   JWT_SECRET_KEY=__your openssl rand -hex 48__
   WEBHOOK_HMAC_DEFAULT_SECRET=__your openssl rand -hex 32__

   ADMIN_EMAIL=admin@ministry.gov
   ADMIN_INITIAL_PASSWORD=__a strong password__
   APPROVER_EMAIL=executive@ministry.gov

   LLM_PROVIDER=openai
   EMBEDDING_PROVIDER=openai
   OPENAI_API_KEY=__your rotated OpenAI key__
   OPENAI_MODEL_PRIMARY=gpt-4o
   OPENAI_MODEL_FAST=gpt-4o-mini
   OPENAI_EMBEDDING_MODEL=text-embedding-3-small
   EMBEDDING_DIM=1536

   STORAGE_BACKEND=local
   ```

   `${{Postgres.DATABASE_URL}}` and `${{Redis.REDIS_URL}}` are Railway references — it fills in the
   real connection strings. The app converts the `postgresql://` it gets into the async driver on
   its own, so no editing is needed.

5. Go to **Settings → Networking → Generate Domain**. Copy the URL (e.g.
   `danah-web-production.up.railway.app`).
6. Add one more variable, using that domain:
   ```
   CORS_ORIGINS=https://danah-web-production.up.railway.app
   ```
7. The service redeploys. When it boots, its start command runs `alembic upgrade head` first, so
   the schema (all 18 tables at `vector(1536)`) is created automatically. Watch **Deployments →
   View Logs** until you see uvicorn start and `/api/healthz` go green.

Visit `https://<your-domain>/api/healthz` — you should get `{"status":"ok", ... "database":"up",
"llm_configured":true}`.

---

## Step 3 — Add the worker and the scheduler

The pipeline and document indexing run on a background worker. Without it, "Run pipeline" would
enqueue a job that nothing processes. Add two more services from the **same repo**.

**Worker**
1. **+ Create** → **GitHub Repo** → `Tanzeel607/DANAH_v1` again.
2. Rename it **`worker`**.
3. **Settings → Deploy → Custom Start Command:**
   ```
   arq app.workers.worker.WorkerSettings
   ```
4. **Settings → Networking:** do *not* generate a domain (it serves no HTTP).
5. **Variables:** it needs the same variables as `web`. Quickest way — on the `web` service's
   Variables tab there is a **⋮ → Copy all**, then paste into the worker. (Or add a **Shared
   Variable** group in project settings and attach all three services to it.) The worker does not
   need `CORS_ORIGINS` but it does no harm.

**Scheduler**
1. Repeat: **+ Create → GitHub Repo →** same repo, rename **`scheduler`**.
2. **Custom Start Command:**
   ```
   arq app.workers.worker.SchedulerSettings
   ```
3. Same variables as the worker. No domain.

> The scheduler fires the daily pipeline and the 5-minute source-poll. For a live demo you don't
> strictly need it — you can trigger a run from the UI — but it is cheap to leave running.

---

## Step 4 — Seed the first users and sources

The database is migrated but empty. Seed it once.

**Easiest (Railway dashboard):** open the **`web`** service → the **⋮** menu → look for a shell /
"Run a command", and run:
```
python -m scripts.seed
```

**Or with the Railway CLI** on your machine:
```bash
npm i -g @railway/cli
railway login
railway link          # pick the DANAH project
railway run --service web python -m scripts.seed
```

You should see `admin user created` and `executive (approver) created`, plus 4 sources and 3
indexed documents.

---

## Step 5 — Open it

Go to `https://<your-web-domain>/` and sign in as `admin@ministry.gov` with the password you set.

Bottom-right should read **LIVE — REAL AI BACKEND** in green. If it is orange (DEMO), the browser
cannot reach the API — check that `web` is healthy and that `CORS_ORIGINS` matches the domain
exactly (`https://`, no trailing slash).

Then run through the demo in `README.md` / the walkthrough: ask the Live Agent a grounded question
and an out-of-corpus one, run the pipeline, approve an insight, check the audit log.

---

## Cost and the free trial

Railway gives a trial credit (about **$5**) that runs this small stack for weeks. When it runs low
you'll be asked to add a card; a hobby plan is roughly **$5/month**. For a submission the trial
credit is effectively free.

**To spend the least:** the three app services (`web`, `worker`, `scheduler`) each consume credit
while running. If you only need to demo, you can **pause** the `scheduler` (and even the `worker`
between demos) from its Settings, and resume when needed.

---

## When you move off the demo

- **Production hosting** (sovereign/on-prem) is the ministry's decision — see
  `docs/EXTERNAL_APIS.md` §7 and `docs/FRONTEND_REBUILD_RECOMMENDATION.md` §6.
- **Storage** is local disk here and is wiped on redeploy. Attach a Railway **Volume** to `web` at
  the documents path, or move to object storage, before anything but a demo.
- **Move the JWT into an httpOnly cookie** before real use — see the rebuild recommendation §6.1.
- **Change both seeded passwords** immediately after the first login.
