# DANAH — Strategic Intelligence Platform

A government ministry's strategic intelligence platform: continuous ingestion of external signals,
a pipeline of six specialised AI agents, retrieval-grounded chat with citations, a human approval
gate on every publication, and a hash-chained audit trail — behind the v11 command centre.

> **Grounded or silent · Human in the loop · Sovereign by default · Audit everything · Bilingual first-class**

**This is the full product, not a backend in isolation.** The v11 command centre is wired to the
real API and served by it at `/`. What was a simulation is now real:

| | Prototype (v11) | Now |
|---|---|---|
| Sign-in | a role dropdown, "Dummy SSO" | argon2 + JWT, enforced server-side |
| Live Agent | keyword matching → canned text | a real model, real citations, real confidence — and it **abstains** rather than invent |
| Pipeline | 14 strings on a 420 ms timer | six real AI agents over real ingested data, polled live |
| Approvals | splicing a local array | a real human gate; approval **publishes** and is written to the audit chain |
| Data | synthetic, lost on refresh | PostgreSQL — World Bank, GDELT, RSS, ReliefWeb |


---

## Quick start — the whole product in one command

```bash
cp .env.example .env          # set JWT_SECRET_KEY, ADMIN_INITIAL_PASSWORD, OPENAI_API_KEY
docker compose up -d          # api, worker, scheduler, postgres+pgvector, redis
docker compose exec api python -m scripts.seed
```

Then open **<http://localhost:8000>** and sign in.

| | |
|---|---|
| **Command centre** | <http://localhost:8000> |
| Sign in as | `admin@ministry.gov` (admin) or `executive@ministry.gov` (approver) |
| Password | whatever you set as `ADMIN_INITIAL_PASSWORD` — **change both after first login** |
| API docs | <http://localhost:8000/docs> |
| Metrics | <http://localhost:8000/metrics> |

A green **LIVE — REAL AI BACKEND** badge sits bottom-right. If it says **DEMO** in orange, the
backend is unreachable and the screens are showing the prototype's synthetic data — nothing is ever
passed off as live.

**No API keys yet?** The stack still builds, boots and passes its full test suite — LLM-backed routes
return a clear `503 llm_not_configured` rather than faking an answer. See [`FIRST_RUN.md`](FIRST_RUN.md).

### Rebuilding the UI after editing the prototype

```bash
python -m scripts.build_ui    # re-wires DANAH_..._v11.html -> web/index.html
```

---

## Architecture at a glance

| Layer | Technology | Responsibility |
|---|---|---|
| API | FastAPI + Pydantic v2 | The only entry point. Enforces auth, RBAC, classification. |
| Services | auth · RAG · LLM gateway · 6 agents · orchestrator · ingestion · approvals · memory · audit · notifications | All business logic; the API layer stays thin. |
| Workers | ARQ worker + cron scheduler | Source polling, embeddings, pipeline runs, daily briefing. |
| Data | PostgreSQL 16 + pgvector + FTS, Redis 7 | One relational source of truth; vectors co-located for sovereignty. |

**The six agents:** Signal (triage) → Risk ∥ Opportunity ∥ Policy (parallel analysis) →
Briefing (EN + AR) → Memory (durable lessons). Every output lands in the approvals queue as
`pending_approval`; only a human decision publishes it.

Full technical reference: [`DANAH_DEVELOPER_ARCHITECTURE.md`](DANAH_DEVELOPER_ARCHITECTURE.md) ·
endpoint contract: [`docs/API.md`](docs/API.md) · operations: [`docs/RUNBOOK.md`](docs/RUNBOOK.md) ·
engineering decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Development

Requires Python 3.12 and Docker.

```bash
make venv install     # 3.12 virtualenv + dependencies
docker compose up -d postgres redis
make migrate seed
make dev              # uvicorn with autoreload
make worker           # ARQ worker (separate shell)
```

Windows without GNU Make: `./make.ps1 <target>` exposes identical targets.

### Quality gates

```bash
make check            # ruff + mypy --strict + pytest — all must be green
```

`mypy --strict` passes over `app/`. No LLM is ever called in tests: the fake gateway fixture in
`tests/conftest.py` substitutes at the gateway interface, so the real code paths are exercised.

---

## Security posture

- Argon2 password hashing; JWT access (15 min) + rotating refresh (14 d, hashed at rest).
- **Classification is enforced in SQL**, not in prompts — an over-classified chunk cannot reach the
  model's context at all (`PUBLIC < INTERNAL < OFFICIAL < OFFICIAL_SENSITIVE`).
- Role → clearance ceiling: `viewer → INTERNAL`, `analyst → OFFICIAL`, `executive`/`admin` → `OFFICIAL_SENSITIVE`.
- Append-only, hash-chained `audit_log` (`entry_hash = sha256(prev_hash + canonical_json(row))`);
  UPDATE/DELETE blocked by a database trigger. `GET /api/audit/verify` re-walks the chain.
- Redis sliding-window rate limits on login and chat; per-source HMAC on webhook ingestion.
- Document text is never logged at OFFICIAL or above.

⚠️ `CORS_ORIGINS` ships with `null` so the v11 HTML file can be opened from disk during development.
**Remove it in production** — the config layer refuses to start if `APP_ENV=production` and `null`
is still present.

---

## Production notes

**Before the first production deploy**

| | Check |
|---|---|
| ☐ | `APP_ENV=production`, `APP_DEBUG=false` — the config layer *refuses to boot* otherwise |
| ☐ | `JWT_SECRET_KEY` ≥ 32 chars, freshly generated (`openssl rand -hex 48`), never the dev value |
| ☐ | `null` removed from `CORS_ORIGINS`; exact https origins only |
| ☐ | `ADMIN_INITIAL_PASSWORD` rotated after the first login |
| ☐ | `POSTGRES_PASSWORD` not `danah` |
| ☐ | `WEBHOOK_HMAC_DEFAULT_SECRET` set; per-source secrets set for any live feed |
| ☐ | TLS terminated at the edge; `--proxy-headers` is already on (see `docker-compose.yml`) |
| ☐ | `data/documents/` backed up **alongside** the database — the DB stores paths, not bytes |
| ☐ | `/metrics` scraped; alert on `danah_llm_cost_usd_total` and `danah_http_errors_total` |
| ☐ | `GET /api/audit/verify` scheduled — a broken chain is a security incident, not a bug |

The startup validator enforces the first four for you. It fails loudly and names what is wrong;
that is the app refusing to run insecurely, not a bug to work around.

**Topology.** 2+ stateless API replicas behind a load balancer → managed/sovereign PostgreSQL with
pgvector → Redis → 1–2 workers → **exactly one scheduler** (a second one double-fires every cron).
Object storage (S3-compatible) for original documents; the interface is in
`app/services/rag/storage.py` and is the only place that changes.

**Cost control.** `PIPELINE_TOKEN_BUDGET` hard-caps a single run — a run that hits it stops and
reports `partial` rather than billing without limit. `DAILY_COST_ALERT_USD` notifies administrators
(it does not stop spending). The Signal Agent runs on the *fast* model tier and archives everything
below `SIGNAL_RELEVANCE_THRESHOLD`, so the expensive agents only ever see items that survived
triage. `api_usage` is a per-model, per-purpose, per-user ledger; `/metrics` exposes the same
figures.

**Known limitations** (deliberate, documented, not oversights):

- **S3 storage is not implemented.** `STORAGE_BACKEND=local` works; `s3` raises a clear error
  naming the seam. Object storage is production-topology work, not a phase deliverable.
- **OIDC/SSO is a documented stub** (`app/security/oidc.py`). The government IdP's issuer, claims
  and group names are client-side dependencies that do not exist yet. Half-implementing a flow
  against an imagined IdP would produce code that looks finished and must be discarded.
- **The rate limiter fails open.** If Redis is unreachable, requests are allowed and the failure is
  logged loudly. A rate limiter is a guardrail, not an authentication boundary — a cache outage
  must not lock a ministry out of its platform mid-incident.
- **Chunk sizing uses `tiktoken` as a provider-neutral estimator.** Anthropic ships no local
  tokenizer; chunk size is not a correctness boundary. See `docs/DECISIONS.md` #13.
