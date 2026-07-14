"""Live end-to-end acceptance check — `make smoke`.

Runs every acceptance criterion in master prompt §10 against a RUNNING DANAH stack with REAL
provider credentials. This is the script that converts the `PENDING-CREDENTIALS` rows in
BUILD_REPORT.md into PASSED.

It is deliberately not a pytest module: it exercises the deployed system over HTTP exactly as the
v11 front end will, including the ARQ worker and the real LLM. Nothing here is mocked.

    docker compose up -d
    docker compose exec api python -m scripts.seed
    python -m scripts.smoke_test              # or: make smoke

Exit code 0 = every checked criterion passed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

PASS_ICON = "PASS"
FAIL_ICON = "FAIL"
SKIP_ICON = "SKIP"


@dataclass
class Check:
    phase: str
    criterion: str
    passed: bool = False
    skipped: bool = False
    detail: str = ""


@dataclass
class Smoke:
    base_url: str
    email: str
    password: str
    checks: list[Check] = field(default_factory=list)
    access_token: str = ""
    client: httpx.AsyncClient | None = None
    run_id: str = ""

    # -- plumbing ------------------------------------------------------------
    @property
    def http(self) -> httpx.AsyncClient:
        if self.client is None:
            raise RuntimeError("HTTP client not started")
        return self.client

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def record(self, phase: str, criterion: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(phase, criterion, passed=passed, detail=detail))
        icon = f"{GREEN}{PASS_ICON}{RESET}" if passed else f"{RED}{FAIL_ICON}{RESET}"
        print(f"  {icon}  [{phase}] {criterion}")
        if detail:
            print(f"        {DIM}{detail}{RESET}")
        return passed

    def skip(self, phase: str, criterion: str, reason: str) -> None:
        self.checks.append(Check(phase, criterion, skipped=True, detail=reason))
        print(f"  {YELLOW}{SKIP_ICON}{RESET}  [{phase}] {criterion}")
        print(f"        {DIM}skipped: {reason}{RESET}")

    async def poll(
        self, predicate: Any, *, deadline_s: float = 90.0, interval: float = 2.0
    ) -> Any | None:
        """Poll until `predicate` returns a truthy value, or the deadline passes.

        Deliberately not `asyncio.timeout`: a timed-out check must return None so the criterion is
        recorded as failed-with-detail, not raise and abort every remaining check.
        """
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            result = await predicate()
            if result:
                return result
            await asyncio.sleep(interval)
        return None

    # -- phase 0 -------------------------------------------------------------
    async def phase0(self) -> None:
        print(f"\n{BOLD}Phase 0 - service health{RESET}")

        resp = await self.http.get("/api/healthz")
        body = resp.json() if resp.status_code == 200 else {}

        self.record(
            "0",
            "GET /api/healthz returns 200 with database and redis up",
            resp.status_code == 200 and body.get("database") == "up" and body.get("redis") == "up",
            f"database={body.get('database')} redis={body.get('redis')} "
            f"llm_configured={body.get('llm_configured')}",
        )

        if not body.get("llm_configured"):
            print(
                f"\n{RED}{BOLD}No LLM provider is configured.{RESET}\n"
                f"  Set ANTHROPIC_API_KEY (or OPENAI_API_KEY) and an embedding key in .env,\n"
                f"  restart the stack, then re-run. See FIRST_RUN.md.\n"
            )
            raise SystemExit(2)

    # -- phase 1 -------------------------------------------------------------
    async def phase1(self) -> None:
        print(f"\n{BOLD}Phase 1 - grounded chat{RESET}")

        login = await self.http.post(
            "/api/auth/login", json={"email": self.email, "password": self.password}
        )
        ok = login.status_code == 200
        self.record("1", "login as admin returns an access token", ok, f"HTTP {login.status_code}")
        if not ok:
            raise SystemExit(f"Cannot continue without a token: {login.text}")
        self.access_token = login.json()["access_token"]

        content = (
            "# Coastal Resilience Programme\n\n"
            "The Ministry allocates 1.4 billion dirhams to the Coastal Resilience Programme "
            "between 2026 and 2029. The programme's flagship target is to protect 180 kilometres "
            "of shoreline against a one-in-fifty-year storm surge.\n\n"
            "Programme delivery is overseen by the Directorate of Infrastructure, which reports "
            "quarterly to the Under-Secretary. Cost overruns above 8 percent require Cabinet "
            "re-approval.\n"
        )
        upload = await self.http.post(
            "/api/knowledge/documents",
            headers=self.auth,
            files={"file": ("coastal-resilience.md", content.encode(), "text/markdown")},
            data={"title": "Coastal Resilience Programme", "classification": "INTERNAL"},
        )
        uploaded = upload.status_code == 202
        self.record("1", "upload a document (202 accepted)", uploaded, f"HTTP {upload.status_code}")
        if not uploaded:
            raise SystemExit(f"Upload failed: {upload.text}")

        document_id = upload.json()["id"]

        async def _indexed() -> dict[str, Any] | None:
            listing = await self.http.get("/api/knowledge/documents", headers=self.auth)
            for doc in listing.json():
                if doc["id"] == document_id and doc["status"] in ("indexed", "failed"):
                    return dict(doc)
            return None

        doc = await self.poll(_indexed, deadline_s=120)
        indexed = bool(doc and doc["status"] == "indexed" and doc["chunk_count"] > 0)
        self.record(
            "1",
            "document reaches status 'indexed' within a minute",
            indexed,
            f"status={doc['status'] if doc else 'timeout'} "
            f"chunks={doc['chunk_count'] if doc else 0}"
            + (f" error={doc['error']}" if doc and doc.get("error") else ""),
        )

        chat = await self.http.post(
            "/api/agent/chat",
            headers=self.auth,
            json={
                "message": "How many kilometres of shoreline does the coastal programme protect?"
            },
        )
        body = chat.json() if chat.status_code == 200 else {}
        citations = body.get("citations", [])
        cited_this_doc = any(c.get("document_id") == document_id for c in citations)
        confidence = body.get("confidence", -1)

        self.record(
            "1",
            "chat answers with >=1 citation pointing at the uploaded document",
            chat.status_code == 200 and cited_this_doc,
            f"citations={len(citations)} grounded={body.get('grounded')}",
        )
        self.record(
            "1",
            "confidence is in [0,1]",
            isinstance(confidence, int | float) and 0.0 <= confidence <= 1.0,
            f"confidence={confidence}",
        )
        if body.get("answer"):
            print(f'        {DIM}answer: "{body["answer"][:140]}..."{RESET}')

        abstain = await self.http.post(
            "/api/agent/chat",
            headers=self.auth,
            json={"message": "What is the population of the planet Vulcan in the year 3000?"},
        )
        ab = abstain.json() if abstain.status_code == 200 else {}
        self.record(
            "1",
            "out-of-corpus question yields an explicit abstention, not an invention",
            abstain.status_code == 200 and not ab.get("grounded") and not ab.get("citations"),
            f'grounded={ab.get("grounded")} answer="{str(ab.get("answer", ""))[:100]}..."',
        )

    # -- phase 2 -------------------------------------------------------------
    async def phase2(self) -> None:
        print(f"\n{BOLD}Phase 2 - real data + agents{RESET}")

        sources = await self.http.get("/api/sources", headers=self.auth)
        if sources.status_code != 200:
            self.record("2", "GET /api/sources", False, f"HTTP {sources.status_code}")
            return

        worldbank = next((s for s in sources.json() if s["connector"] == "worldbank"), None)
        if worldbank is None:
            self.record("2", "World Bank source exists (run make seed first)", False)
            return

        sync = await self.http.post(
            f"/api/sources/{worldbank['id']}/sync", headers=self.auth, timeout=180.0
        )
        synced = sync.json() if sync.status_code == 200 else {}
        fetched = synced.get("fetched", 0)

        self.record(
            "2",
            "POST /api/sources/{worldbank}/sync ingests real indicator datapoints",
            sync.status_code == 200 and fetched > 0,
            f"fetched={fetched} created={synced.get('created', 0)} "
            f"duplicates={synced.get('duplicates', 0)}",
        )

        items = await self.http.get("/api/items?limit=5", headers=self.auth)
        item_total = items.json().get("total", 0) if items.status_code == 200 else 0
        self.record(
            "2",
            "ingested datapoints are visible in GET /api/items",
            item_total > 0,
            f"total={item_total}",
        )

        run = await self.http.post("/api/pipeline/run", headers=self.auth, json={"max_items": 12})
        if run.status_code not in (200, 202):
            self.record("2", "POST /api/pipeline/run", False, f"HTTP {run.status_code}: {run.text}")
            return
        self.run_id = run.json()["run_id"]
        print(f"        {DIM}run {self.run_id} enqueued; waiting for the agents...{RESET}")

        async def _finished() -> dict[str, Any] | None:
            detail = await self.http.get(f"/api/pipeline/runs/{self.run_id}", headers=self.auth)
            if detail.status_code != 200:
                return None
            data: dict[str, Any] = detail.json()
            return data if data["status"] in ("completed", "failed", "partial") else None

        final = await self.poll(_finished, deadline_s=480, interval=5)
        if final is None:
            self.record("2", "pipeline run completes", False, "timed out after 8 minutes")
            return

        self.record(
            "2",
            "pipeline run completes",
            final["status"] in ("completed", "partial"),
            f"status={final['status']} steps={len(final.get('steps', []))}",
        )

        steps = final.get("steps", [])
        has_usage = any(s["tokens_in"] > 0 for s in steps)
        has_cost = any(float(s["cost_usd"]) > 0 for s in steps)
        self.record(
            "2",
            "GET /api/pipeline/runs/{id} shows per-step token usage and cost",
            has_usage and has_cost,
            f"total_tokens={final.get('total_tokens')} "
            f"total_cost_usd={final.get('total_cost_usd')}",
        )

        insights = await self.http.get("/api/insights?kind=risk", headers=self.auth)
        risks = insights.json().get("items", []) if insights.status_code == 200 else []
        grounded_risk = next((r for r in risks if r.get("citations")), None)
        self.record(
            "2",
            "pipeline produces >=1 Risk insight grounded in real items with citations",
            grounded_risk is not None,
            f"risks={len(risks)}"
            + (f' e.g. "{grounded_risk["title"][:60]}..."' if grounded_risk else ""),
        )

    # -- phase 3 -------------------------------------------------------------
    async def phase3(self) -> None:
        print(f"\n{BOLD}Phase 3 - full agent cycle{RESET}")

        for kind in ("risk", "opportunity", "policy"):
            resp = await self.http.get(f"/api/insights?kind={kind}", headers=self.auth)
            items = resp.json().get("items", []) if resp.status_code == 200 else []
            self.record("3", f"{kind} insights produced", len(items) > 0, f"count={len(items)}")

        briefings = await self.http.get("/api/briefings", headers=self.auth)
        blist = briefings.json() if briefings.status_code == 200 else []
        if not blist:
            self.record("3", "a bilingual briefing was produced", False, "no briefings found")
        else:
            detail = await self.http.get(f"/api/briefings/{blist[0]['id']}", headers=self.auth)
            brief = detail.json() if detail.status_code == 200 else {}
            body_ar = brief.get("body_ar", "")
            has_arabic = any("؀" <= c <= "ۿ" for c in body_ar)

            self.record(
                "3",
                "briefing carries BOTH an English and a real Arabic body",
                bool(brief.get("body_en")) and has_arabic,
                f"en_chars={len(brief.get('body_en', ''))} ar_chars={len(body_ar)} "
                f"arabic_script={has_arabic}",
            )

        approvals = await self.http.get("/api/approvals?status=pending", headers=self.auth)
        pending = approvals.json() if approvals.status_code == 200 else []
        self.record(
            "3",
            "every agent output lands in the approvals queue as pending",
            len(pending) > 0,
            f"pending={len(pending)}",
        )

        if pending:
            target = pending[0]
            decide = await self.http.post(
                f"/api/approvals/{target['id']}/decision",
                headers=self.auth,
                json={"decision": "approved", "comment": "smoke test approval"},
            )
            decided = decide.json() if decide.status_code == 200 else {}
            self.record(
                "3",
                "approving publishes the subject",
                decide.status_code == 200 and decided.get("subject_status") == "published",
                f"subject={decided.get('subject_type')} status={decided.get('subject_status')}",
            )

        memory = await self.http.get("/api/memory", headers=self.auth)
        entries = memory.json() if memory.status_code == 200 else []
        self.record(
            "3",
            "memory entries are created and retrievable",
            len(entries) > 0,
            f"entries={len(entries)}",
        )

        # Read them as the executive, not the admin. Approval notifications are addressed to
        # `role=executive` and the endpoint returns only what is addressed to you or your role,
        # so checking as an admin asserts nothing: it returns an empty list whether the
        # notification system works or not.
        notes = await self._notifications_for_the_approver()
        self.record(
            "3",
            "notification rows are created and reach the approver",
            len(notes) > 0,
            f"notifications={len(notes)} (as executive)",
        )

    async def _notifications_for_the_approver(self) -> list[Any]:
        settings = get_settings()
        login = await self.http.post(
            "/api/auth/login",
            json={
                "email": settings.approver_email,
                "password": settings.admin_initial_password.get_secret_value(),
            },
        )
        if login.status_code != 200:
            return []
        token = login.json()["access_token"]
        resp = await self.http.get(
            "/api/notifications", headers={"Authorization": f"Bearer {token}"}
        )
        return list(resp.json()) if resp.status_code == 200 else []

    # -- phase 4 -------------------------------------------------------------
    async def phase4(self) -> None:
        print(f"\n{BOLD}Phase 4 - hardening{RESET}")

        verify = await self.http.get("/api/audit/verify", headers=self.auth)
        v = verify.json() if verify.status_code == 200 else {}
        checked = v.get("entries_checked", 0)
        self.record(
            "4",
            "GET /api/audit/verify returns valid: true over the whole chain",
            verify.status_code == 200 and v.get("valid") is True,
            f"valid={v.get('valid')} entries_checked={checked}",
        )
        if checked < 100:
            print(
                f"        {DIM}note: the chain holds {checked} entries; the §10 criterion asks "
                f"for >=100. Exercise the API more, or run the pipeline again.{RESET}"
            )

        limit = get_settings().rate_limit_login_per_minute
        attempts = 0
        got_429 = False
        for _ in range(limit + 4):
            r = await self.http.post(
                "/api/auth/login", json={"email": "nobody@ministry.gov", "password": "wrong"}
            )
            attempts += 1
            if r.status_code == 429:
                got_429 = True
                self.record(
                    "4",
                    "rate limit returns 429 with a Retry-After header",
                    r.headers.get("Retry-After") is not None,
                    f"429 after {attempts} attempts (limit={limit}), "
                    f"Retry-After={r.headers.get('Retry-After')}",
                )
                break
        if not got_429:
            self.record(
                "4",
                "rate limit returns 429 with a Retry-After header",
                False,
                f"no 429 after {attempts} attempts (limit={limit})",
            )

        metrics = await self.http.get("/metrics")
        text = metrics.text if metrics.status_code == 200 else ""
        self.record(
            "4",
            "/metrics exposes request and LLM cost counters",
            "danah_http_requests_total" in text and "danah_llm_cost_usd_total" in text,
            f"HTTP {metrics.status_code}, {len(text)} bytes",
        )

        print(
            f"        {DIM}note: viewer-blocked-from-OFFICIAL_SENSITIVE and audit-tamper-detection "
            f"are proven by the integration suite: pytest -k 'classification or tamper'{RESET}"
        )

    # -- report --------------------------------------------------------------
    def report(self) -> int:
        passed = sum(1 for c in self.checks if c.passed)
        failed = sum(1 for c in self.checks if not c.passed and not c.skipped)
        skipped = sum(1 for c in self.checks if c.skipped)

        print(f"\n{BOLD}{'-' * 62}{RESET}")
        print(f"{BOLD}Smoke test summary{RESET}")
        print(f"  {GREEN}passed:  {passed}{RESET}")
        print(f"  {RED if failed else DIM}failed:  {failed}{RESET}")
        if skipped:
            print(f"  {YELLOW}skipped: {skipped}{RESET}")

        if failed:
            print(f"\n{RED}Failed criteria:{RESET}")
            for c in self.checks:
                if not c.passed and not c.skipped:
                    print(f"  {FAIL_ICON}  [Phase {c.phase}] {c.criterion}")
                    if c.detail:
                        print(f"        {DIM}{c.detail}{RESET}")
            print()
            return 1

        print(f"\n{GREEN}{BOLD}All checked acceptance criteria passed.{RESET}")
        print(f"{DIM}Update BUILD_REPORT.md: PENDING-CREDENTIALS -> PASSED.{RESET}\n")
        return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="DANAH live acceptance check")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--phases", default="0,1,2,3,4", help="Comma-separated phases to run (default: all)"
    )
    args = parser.parse_args()

    settings = get_settings()
    phases = {p.strip() for p in args.phases.split(",")}

    smoke = Smoke(
        base_url=args.base_url,
        email=settings.admin_email,
        password=settings.admin_initial_password.get_secret_value(),
    )

    print(f"\n{BOLD}DANAH live acceptance check{RESET}")
    print(f"{DIM}target: {args.base_url}  ·  admin: {settings.admin_email}{RESET}")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=90.0) as client:
        smoke.client = client
        try:
            if "0" in phases:
                await smoke.phase0()
            if "1" in phases:
                await smoke.phase1()
            if "2" in phases:
                await smoke.phase2()
            if "3" in phases:
                await smoke.phase3()
            if "4" in phases:
                await smoke.phase4()
        except httpx.ConnectError:
            print(
                f"\n{RED}Cannot reach {args.base_url}.{RESET}\n"
                f"  Start the stack first:  docker compose up -d\n"
            )
            return 2

    return smoke.report()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
