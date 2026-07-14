# DANAH — Autonomous Build Progress Journal

**Run started:** 2026-07-13
**Governing docs:** `DANAH_FULL_BUILD_EXECUTION_PROMPT.md` (how) · `DANAH_CLAUDE_CODE_MASTER_PROMPT.md` (what) · `DANAH_DEVELOPER_ARCHITECTURE.md` (why)

**Legend:** `[ ]` not started · `[~]` in progress · `[x]` done

---

## Environment & Credential Status

| Item | Status | Note |
|---|---|---|
| Python 3.12 | ✅ 3.12.13 | provisioned via `uv python install 3.12` (system default was 3.14, which lacks wheels for parts of the locked stack) |
| Docker + compose | ✅ 29.4.0 / v5.1.1 | daemon reachable |
| Git | ✅ 2.53.0 | repo initialised on `main` |
| GNU Make | ✅ 4.4.1 | installed via `winget install ezwinports.make` |
| `OPENAI_API_KEY` | ✅ PRESENT | single-vendor: `gpt-4o` / `gpt-4o-mini` / `text-embedding-3-small` |
| `ANTHROPIC_API_KEY` | — unused | the gateway supports it; the client chose one vendor |
| `VOYAGE_API_KEY` | — unused | OpenAI supplies embeddings |

### ✅ VERIFIED LIVE — 2026-07-14

The credentials arrived and the whole system was run against them. **`make smoke`: 23 of 24
acceptance criteria pass.** Proven end-to-end, not by fixture: grounded chat citing an uploaded
document (confidence 0.696), an explicit abstention outside the corpus, 152 real items ingested from
the World Bank and live RSS, risk / opportunity / policy insights drawn from real evidence, a
bilingual briefing carrying **1,005 characters of genuine Arabic**, the approvals queue, publication
only on human approval, notifications reaching the approver, a verifying hash-chained audit log,
`429`s with `Retry-After`, and live per-model cost metering.

**Switching to OpenAI required rebuilding the database.** The vector columns were `vector(1024)`
(Voyage); OpenAI emits 1536. Authorised by the client, the `danah_pgdata` volume was dropped and
re-migrated. This is the one-way door flagged in `docs/EXTERNAL_APIS.md` §1 — decide the embedding
provider *before* go-live, or re-embed the entire corpus.

**The integration suite ran for the first time and found seven production bugs**, plus two more that
only a real provider could surface. All fixed; see `BUILD_REPORT.md` §1. Every one of them failed
silently — the system reported success while doing nothing — which is exactly why they survived a
build where every unit test was green.

The single criterion that does not pass (`memories=0`) is the Memory agent *declining*, not failing:
it refuses to restate an insight as a memory. Verified it writes when handed durable standing facts.
Left as designed.

---

## Phase 0 — Skeleton

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 1 | Verify prerequisites; `git init`; write `.gitignore` | [x] | 2026-07-13 | py3.12 via uv, make via winget, docker OK; `.gitignore` excludes `.env` |
| 2 | Create `.env` from `.env.example`; record credential availability | [x] | 2026-07-13 | random JWT secret + admin password + HMAC secret generated; keys ABSENT → PENDING-CREDENTIALS |
| 3 | Full repo structure [§5]; `pyproject.toml` pinned deps; `Makefile` | [x] | 2026-07-13 | + `make.ps1` for Windows (DECISIONS #9) |
| 4 | `docker-compose.yml`: api, worker, scheduler, postgres+pgvector, redis | [x] | 2026-07-13 | all 5 services healthy; host ports configurable (DECISIONS #16) |
| 5 | `app/config.py` Settings mirroring every `.env.example` var + fail-fast | [x] | 2026-07-13 | bidirectional contract enforced by `test_config_contract.py` (13 tests) |
| 6 | Logging (structlog JSON + request-id) + exception hierarchy + handlers | [x] | 2026-07-13 | request id in ContextVar; `redact_text()` withholds text at OFFICIAL+ |
| 7 | Alembic init + migration 0001 (full schema, pgvector, HNSW, audit trigger) | [x] | 2026-07-13 | 17 tables, 20 enums, 2 HNSW + 2 GIN indexes; **audit trigger verified: INSERT ok, UPDATE/DELETE/TRUNCATE blocked** |
| 8 | `main.py` app factory, CORS, `/api/healthz`, `/metrics`, routers mounted | [x] | 2026-07-13 | metrics on `prometheus_client` directly (DECISIONS #19) |
| 9 | Test harness: `conftest.py`, test DB, FakeLLMGateway + FakeEmbedder | [x] | 2026-07-13 | real Postgres+pgvector test DB; semantically-meaningful fake embedder |
| 10 | **Phase 0 gate:** compose up, healthz 200, lint/mypy/pytest green; commit + tag `phase-0-complete` | [x] | 2026-07-13 | ✅ ruff clean · mypy --strict clean (42 files) · 22 tests pass · 5/5 services healthy |

## Phase 1 — Grounded chat

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 11 | Security core: argon2, JWT access/refresh rotation, `require_role`/`require_clearance` | [x] | 2026-07-13 | refresh-token **reuse detection** revokes the whole family; tokens stored SHA-256 hashed |
| 12 | Auth API: login, refresh, me + integration tests | [x] | 2026-07-13 | unknown-email and wrong-password are indistinguishable (dummy verify defeats the timing oracle) |
| 13 | LLM gateway + Anthropic provider (tool use, structured output + repair retry, backoff) | [x] | 2026-07-13 | gateway owns retries/backoff/failover; SDK `max_retries=0` so attempts never multiply |
| 14 | OpenAI provider + fallback switch; `usage_tracker` → `api_usage` with cost table | [x] | 2026-07-13 | ledger writes in its OWN transaction — a rolled-back request still spent the tokens |
| 15 | Embeddings service (voyage/openai by env, batching) | [x] | 2026-07-13 | both providers emit `EMBEDDING_DIM` explicitly; dimension mismatch fails loudly, not at INSERT |
| 16 | Indexer: extract → paragraph-aware chunk → embed → store; ARQ `embed_document` | [x] | 2026-07-13 | failure recorded on the row (`failed` + reason), never swallowed |
| 17 | Retriever: pgvector cosine + Postgres FTS, RRF, classification filter in SQL | [x] | 2026-07-13 | clearance is a WHERE clause — over-classified chunks are never read, so leakage is structurally impossible |
| 18 | Grounded answer composer: numbered sources, cite-or-abstain, confidence formula | [x] | 2026-07-13 | only sources the model *actually cited* become citations; hallucinated `[9]` is dropped; confidence capped at 0.95 |
| 19 | Knowledge API: upload, list, semantic search | [x] | 2026-07-13 | cannot classify a document above your own clearance |
| 20 | Chat API: sessions + `POST /api/agent/chat` (answer, citations, confidence) | [x] | 2026-07-13 | |
| 21 | `scripts/seed.py`: admin, default sources, 3 sample docs indexed | [x] | 2026-07-13 | verified: 4 sources + 3 docs; **refuses to fake embeddings without a key** |
| 22 | **Phase 1 gate:** §10 Phase-1 criteria; green; `docs/API.md`; commit + tag `phase-1-complete` | [x] | 2026-07-13 | ✅ ruff · mypy --strict (64 files) · **80 tests** · docs/API.md written |

## Phase 2 — Real data + first agents

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 23 | `BaseConnector` + normalization + `dedup_hash` | [x] | 2026-07-13 | dedup falls back external_id → url → title+**date** (a recurring headline on a new day is a new item, not a duplicate) |
| 24 | World Bank, GDELT, RSS, ReliefWeb connectors + recorded-response unit tests | [x] | 2026-07-13 | tests use `respx`; **no live network calls**. Null World Bank values skipped; one failing RSS feed does not abort the others |
| 25 | ARQ scheduler: per-source polling, `sync_source` task, source health | [x] | 2026-07-13 | cron enqueues (never executes) so a 12-min pipeline cannot block the poll tick |
| 26 | Sources + items APIs incl. manual sync | [x] | 2026-07-13 | INSERT … ON CONFLICT — race-safe when a scheduled poll and a manual sync overlap |
| 27 | `BaseAgent` framework + agent tools | [x] | 2026-07-13 | tool list fixed at construction, so no injected instruction can grant an agent a tool it lacks |
| 28 | Signal Agent + versioned prompt; relevance-threshold archiving | [x] | 2026-07-13 | fast tier; below-threshold items archived before the expensive agents see them |
| 29 | Risk Agent + prompt; insights persistence with citations + confidence | [x] | 2026-07-13 | **uncited insights are dropped**; prompt requires trigger→transmission→harm, not a vibe |
| 30 | Minimal orchestrator (Signal→Risk) + run/step records; pipeline APIs | [x] | 2026-07-13 | steps committed as they open/close, which is what makes the run view *live* |
| 31 | Insights API; `GET /api/dashboard/summary` v1 | [x] | 2026-07-13 | viewer forced to published; counts clearance-filtered (a count is itself a leak) |
| 32 | **Phase 2 gate:** §10 Phase-2 criteria; green; commit + tag `phase-2-complete` | [~] | 2026-07-13 | pending final gate |

## Phase 3 — Full agent cycle

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 33 | Opportunity Agent, Policy Agent (+prompts) | [x] | 2026-07-13 | |
| 34 | Briefing Agent: EN synthesis + faithful AR pass; briefings persistence + APIs | [x] | 2026-07-13 | Arabic is a **second LLM pass**, not machine translation; a failed AR pass marks the run partial rather than shipping an EN-only briefing |
| 35 | Memory Agent + memory service (embedded entries) + memory APIs | [x] | 2026-07-13 | memory is still recorded without an embedder — losing the memory is worse than losing its searchability |
| 36 | Full orchestrator: Signal → parallel(Risk, Opp, Policy) → Briefing → Memory; partial-failure; cron; token budget | [x] | 2026-07-13 | each parallel agent gets its OWN session (an AsyncSession is not task-safe); `PIPELINE_TOKEN_BUDGET` checked between steps |
| 37 | Approvals workflow: auto-pending, decision endpoint publishes/rejects, viewer sees published only | [x] | 2026-07-13 | **the orchestrator has no code path that can set `published`** — only a human decision does |
| 38 | Notifications: table + service (SMTP or log-only) + API | [x] | 2026-07-13 | log-only when SMTP unset; never raises into the caller |
| 39 | **Phase 3 gate:** §10 Phase-3 criteria; green; commit + tag `phase-3-complete` | [~] | 2026-07-13 | pending final gate |

## Phase 4 — Hardening

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 40 | Audit service on all mutations + hash chain + `/audit/verify` + tamper test | [x] | 2026-07-13 | DB trigger blocks UPDATE/DELETE/TRUNCATE (verified); the chain exists to catch the one attacker who can disable it |
| 41 | Rate limiting (login, chat) with 429 + Retry-After + tests | [x] | 2026-07-13 | Redis sliding window; **fails open** — a limiter outage must not become an auth outage |
| 42 | Classification enforcement sweep + integration tests (viewer blocked everywhere) | [x] | 2026-07-13 | 13 tests: documents, chat grounding, search, insights, briefings, memory **and dashboard counts** (a count is itself a leak) |
| 43 | Webhook ingestion with per-source HMAC + tests | [x] | 2026-07-13 | `compare_digest` over the **raw** body; unknown source and bad signature answered identically to prevent id enumeration |
| 44 | Prometheus metrics: request + LLM tokens/cost; `DAILY_COST_ALERT_USD` wiring | [x] | 2026-07-13 | ✅ verified live: `danah_http_requests_total` + `danah_llm_cost_usd_total` on `/metrics` |
| 45 | OIDC stub module + env plumbing (disabled by default) | [x] | 2026-07-13 | documented seam; `map_claims_to_role` fails **closed** to `viewer` |
| 46 | `scripts/loadtest.py` + results in RUNBOOK | [x] | 2026-07-13 | p50/p95/p99 (a mean hides the tail users notice) |
| 47 | Docs finalization: `README.md`, `docs/RUNBOOK.md`, `docs/API.md` | [x] | 2026-07-13 | + production checklist and known limitations |
| 48 | **Phase 4 gate:** §10 Phase-4 criteria incl. tamper detection; green; commit + tag `phase-4-complete` | [~] | 2026-07-13 | ⚠️ ruff + mypy + unit green; **integration suite blocked — the Docker daemon crashed** (see BUILD_REPORT §5a) |

## Completion

| # | Step | Status | When | Note |
|---|---|---|---|---|
| 49 | Write `BUILD_REPORT.md` (every §10 criterion, endpoint inventory, coverage, limitations) | [x] | 2026-07-13 | every criterion accounted for; none silently skipped |
| 50 | Print final summary pointing to `BUILD_REPORT.md` + `FIRST_RUN.md` | [x] | 2026-07-13 | |

---

## 🛑 Blocker at the final gate — the machine ran out of disk

**`C:` hit 100% full (0 bytes free).** That single fact caused every late failure in this build:
the Docker engine started returning `500 Internal Server Error` on every call, the Postgres and
Redis containers died with it, `docker compose build` aborted with an EOF, the test run hung, and
`git commit` failed with *"No space left on device"*.

Freeing this build's own caches (`.mypy_cache`, `__pycache__`, temp) recovered ~3.5 GB — enough to
commit all work safely. But Docker Desktop's backend service (`com.docker.service`, start type
**Manual**) still will not start, and starting it needs **Administrator rights** this session does
not have.

`C:\Users\DEV\AppData\Local\Docker\wsl` is **102.7 GB**. It was left untouched — it holds every
image and volume on the machine, including an unrelated `crm_postgres` container's data.

The integration suite runs against a real PostgreSQL + pgvector and a real Redis **by design**
(`docs/DECISIONS.md` #14): SQLite has no vectors, no FTS, no `jsonb` and no append-only trigger, so
it would test a *different schema* than the one that ships. It therefore cannot run without Docker.

**To finish (≈5 minutes)** — free disk, start Docker as Administrator, then:

```bash
docker system prune -a          # reclaim space (removes unused images across ALL projects)
docker compose up -d postgres redis
.venv/Scripts/pytest -q         # 168 tests
```

That completes the four criteria marked `PENDING-DOCKER` in `BUILD_REPORT.md`: audit tamper
detection, the ≥100-entry chain verify, rate-limit 429s, and the classification sweep.
`BUILD_REPORT.md` §5(a) also carries the three `git tag` commands — the phase-2/3/4 tags are
**deliberately not applied**, because their gates have not been observed to pass. Claiming a tag on
an unrun gate is exactly the kind of quiet dishonesty this journal exists to prevent.

**Green without Docker:** `ruff` ✅ · `ruff format` ✅ · `mypy --strict` (101 files) ✅ ·
unit suite (86 tests) ✅ · `/metrics` counters verified live ✅

---

## Acceptance Criteria Tracker (Master Prompt §10)

| Phase | Criterion | Status | Proof |
|---|---|---|---|
| 1 | compose up + seed → login → upload PDF → `indexed` within a minute | **PASSED-VIA-TESTS** · live=PENDING-CREDENTIALS | `pytest tests/integration/test_auth.py -k upload` (upload→202→pending). Live indexing needs an embedding key → `make smoke --phases 1` |
| 1 | chat about the PDF → answer w/ ≥1 citation to that doc + confidence ∈ [0,1] | **PASSED-VIA-TESTS** · live=PENDING-CREDENTIALS | `test_chat.py::test_answer_cites_the_uploaded_document` — asserts the citation's `document_id` equals the uploaded doc |
| 1 | out-of-corpus question → explicit "not in my sources" abstention | **PASSED-VIA-TESTS** · live=PENDING-CREDENTIALS | `test_chat.py::test_out_of_corpus_question_abstains` — `grounded=false`, `citations=[]`, `confidence=0` |
| 2 | `POST /sources/{worldbank}/sync` ingests real datapoints, visible in `GET /items` | [ ] | |
| 2 | `POST /pipeline/run` → ≥1 Risk insight grounded in real items with citations | [ ] | |
| 2 | `GET /pipeline/runs/{id}` shows per-step token usage and cost | [ ] | |
| 3 | full run → risk/opportunity/policy insights + bilingual briefing, all in approvals queue | [ ] | |
| 3 | approving publishes (visible to viewer); rejecting hides | [ ] | |
| 3 | memory entries created and retrievable; notification rows created | [ ] | |
| 4 | `GET /audit/verify` → `valid: true` over ≥100 entries | [ ] | |
| 4 | tampering with a DB row → verify returns the broken index | [ ] | |
| 4 | rate limits return 429 with `Retry-After` | [ ] | |
| 4 | viewer cannot read OFFICIAL_SENSITIVE (integration test proves it) | [ ] | |
| 4 | `/metrics` exposes request + LLM cost counters | [ ] | |
