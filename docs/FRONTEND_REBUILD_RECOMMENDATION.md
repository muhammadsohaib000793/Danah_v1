# DANAH — Front-End Rebuild: Technical Recommendation

**Prepared for:** Ministry of Cabinet Affairs — DANAH Programme
**Date:** 14 July 2026
**Status:** Recommendation. No action is required to keep the current system running.

---

## 1. Executive summary

The v11 command centre is now connected to the real DANAH backend and is a **working product**.
Sign-in is real, the assistant answers from real documents with real citations, the pipeline runs
real AI agents over real data, and nothing is published without a named human approving it.

**Nothing here is urgent.** The wired v11 is fit to demonstrate, to pilot, and to put in front of a
minister. This document is about what to do *next*, not about a problem to fix.

The recommendation is to rebuild the front end as a component-based application (**Next.js +
TypeScript**) over roughly **8–10 weeks**, running alongside the current UI rather than replacing it
in one step. The reason is not that the current UI is bad — visually it is excellent, and its design
should be preserved almost exactly. The reason is that **a 5,839-line single file cannot be safely
worked on by more than one person, cannot be tested, and cannot be changed with confidence.** That
becomes the binding constraint the moment the ministry asks for its second change.

---

## 2. What exists today, stated plainly

| | |
|---|---|
| **Backend** | Production-grade. FastAPI, PostgreSQL + pgvector, Redis, six AI agents, 35 REST endpoints. **173 tests, `mypy --strict` clean.** Verified live: 23 of 24 acceptance criteria. |
| **Front end** | The v11 prototype, wired. One HTML file, 5,839 lines, ~562 KB, with all CSS and JavaScript inline. No build step, no tests, no components, no types. |

The asymmetry is the whole point of this document. The backend can be changed safely; the front end
cannot. Every future request — a new panel, a new role, an Arabic layout fix — lands on the side of
the system with no safety net.

### What the current front end does well, and must not be lost

Be clear about this, because a rebuild that loses it would be a step backwards:

- The **visual design is genuinely good** — the ministerial tone, the density, the restraint.
- The **information architecture works**: Command Centre → Circuit → Ministry Intelligence →
  Decisions → Memory is a coherent path through a complex domain.
- **Bilingual EN/AR with RTL** is already handled.
- The **classification and clearance vocabulary** is correctly modelled in the UI.

**The rebuild is a re-platforming, not a redesign.** Screens should come across close to
pixel-identical. If the minister notices a visual change, something has gone wrong.

---

## 3. Why rebuild — the four concrete constraints

Not "best practice". These are things that are true today and will cost the programme money.

### 3.1 It cannot be tested

There is no way to write a test against a 5,839-line HTML file with inline scripts. Today the
front end is verified by a human opening it and looking. That does not scale, and it means **any
change can silently break any screen** — including a classification badge, which in this system is a
security control the user relies on to know what they are looking at.

The backend has 173 tests. The front end has none, and structurally cannot have any.

### 3.2 Two people cannot work on it at once

One file means every change is a merge conflict. The moment a second developer joins — or the
ministry wants two features in parallel — the team is serialised. That is a hard ceiling on
delivery speed that no amount of effort removes.

### 3.3 Nothing is typed, so the API contract is enforced by hope

The backend publishes a complete OpenAPI schema. The front end ignores it and reads fields by
string. If a field is renamed, **the UI does not fail loudly — it renders `undefined`**, or worse,
renders nothing and looks fine. On a system whose entire value proposition is that it does not make
things up, a UI that can silently display nothing where a risk score should be is a real hazard.

With generated TypeScript types this becomes a compile error rather than a blank space on a
minister's screen.

### 3.4 The prototype's simulated logic is still inside it

The integration layer **stops** the prototype's fabrication at runtime: the fake circuit timer, the
invented audit rows, the hard-coded "48 sources checked". But the code that produces them is still
in the file, dormant. Today it is disabled. A future developer who does not know why could
re-enable any of it in a single line.

That is an acceptable risk for a pilot. It is not an acceptable risk for a production system
holding OFFICIAL-SENSITIVE data. **The fabrication should not merely be switched off — it should
not exist.**

---

## 4. Recommended stack

Chosen for a government context: boring, well-supported, long-lived. Nothing here is fashionable
for its own sake.

| Layer | Recommendation | Why this and not the alternative |
|---|---|---|
| **Framework** | **Next.js 15** (App Router) + **React 19** | Server components keep the OFFICIAL-SENSITIVE payload off the client where it is not needed. Mature, hireable, long support horizon. *Not* a pure SPA: server-side rendering matters when the data is classified. |
| **Language** | **TypeScript**, `strict: true` | The backend is `mypy --strict`. The front end should meet the same bar. |
| **API types** | **`openapi-typescript` + `openapi-fetch`**, generated from `/openapi.json` in CI | The contract stops being prose and becomes a compiler check. A renamed backend field breaks the build instead of the minister's screen. This is the single highest-value item in the table. |
| **Server state** | **TanStack Query** | Caching, polling, retries, stale-while-revalidate. The pipeline poll and the approvals queue are exactly what it is for. |
| **UI components** | **shadcn/ui** (Radix + Tailwind) | You own the code — no vendored black box inside a government system. Radix gives real accessibility primitives, which a government service will be audited on. |
| **Styling** | **Tailwind CSS v4** with the v11 palette as design tokens | Port the existing look directly; do not redesign. |
| **Charts** | **Recharts** or **visx** | Both are unopinionated enough to match the existing visual language. |
| **i18n / RTL** | **next-intl** + Tailwind logical properties | Arabic is a first-class requirement, not an afterthought. RTL must be structural, not a stylesheet override. |
| **Auth** | JWT in an **httpOnly, Secure, SameSite=Strict cookie** | ⚠️ **This is a security upgrade, not a preference.** See §6. |
| **Testing** | **Vitest** (unit) + **Playwright** (end-to-end) | Playwright is already installed and was used to verify the current wiring in a real browser. |
| **Quality gates** | ESLint + Prettier + `tsc --noEmit` in CI | Match the backend's discipline. |

### Deliberately *not* recommended

- **A heavy state library (Redux/MobX).** Nearly all state here is server state. TanStack Query
  handles it. Adding Redux would be ceremony without benefit.
- **A component library you cannot see inside** (MUI, Ant). Government procurement and security
  review both go better when you own the source.
- **A separate BFF/API gateway layer.** The FastAPI backend is already the correct boundary. Adding
  another hop adds latency, another attack surface, and another thing to secure — for nothing.

---

## 5. Migration plan — the strangler pattern

**Do not attempt a big-bang rewrite.** The current UI works; it should keep working every single day
of the rebuild. Replace it screen by screen, and keep the old one reachable until the new one is
demonstrably better.

```
Next.js app  ──►  /                 (new screens, added one at a time)
             ──►  /legacy/*         (the wired v11, still fully functional)
             ──►  /api/*            (the FastAPI backend — unchanged throughout)
```

The backend does not change at all during this work. That is the point of having built it as a
clean REST API: the front end is replaceable without touching the thing that holds the data.

| Phase | Duration | Deliverable | Why in this order |
|---|---|---|---|
| **0 — Foundation** | 1 wk | Next.js + TS + Tailwind; v11 palette as tokens; **generated API types from `/openapi.json`**; auth cookie flow; CI. | Nothing else is safe to build until the types are generated and auth is right. |
| **1 — Assistant** | 2 wks | Live Agent chat: streaming answers, citation cards, confidence, and the **abstention state as a first-class design** — not an error. | Highest-visibility screen and the one that proves the system is real. Ship it first. |
| **2 — Command Centre** | 2 wks | Dashboard, real counts, insights, risk cards, source health. | The daily-driver screen. |
| **3 — Pipeline & Approvals** | 2 wks | Live run view over the real orchestrator (per-agent status, tokens, cost, **failures shown as failures**), approvals queue, publish-on-approve. | The human-in-the-loop gate — the most safety-critical UI in the product. |
| **4 — Briefings, Memory, Audit** | 2 wks | Bilingual briefing reader (EN/AR side-by-side), Strategic Memory, audit log with **chain verification shown in the UI**. | Completes the intelligence cycle. |
| **5 — Cutover** | 1 wk | Accessibility audit (WCAG 2.2 AA), Arabic review with a native reviewer, load test, decommission `/legacy`. | Do not skip the Arabic review. See §7. |

**Total: 8–10 weeks** with one or two front-end engineers. The range is honest: Arabic RTL and the
accessibility audit are the two items most likely to run long, because they are the two most often
underestimated.

---

## 6. Security: three things to change during the rebuild

The backend already enforces authentication, roles, classification and audit **server-side** — that
work is done and is not affected by the front end. These three items are front-end-side and should
be fixed as part of the rebuild.

### 6.1 Move the JWT out of `sessionStorage` and into an httpOnly cookie ⚠️

**This is the most important item in this document.**

The current wiring stores the access token in `sessionStorage`. That is normal practice and is
acceptable for a pilot, but it means **any successful XSS anywhere in the app can read the token and
impersonate the user** — including a Cabinet Head.

An `httpOnly` cookie cannot be read by JavaScript at all. Combined with `Secure`, `SameSite=Strict`
and a CSRF token, the same XSS becomes far less useful to an attacker.

For a system rated OFFICIAL-SENSITIVE, this is the right default. It requires a small backend change
(set the cookie on login; read it in the auth dependency) and is straightforward — but it must be
done deliberately, so it is called out here rather than left to be discovered.

### 6.2 Add a Content-Security-Policy

The single-file prototype requires `unsafe-inline` for scripts and styles, which defeats most of
CSP's value. A component-based build removes that requirement entirely and allows a strict,
nonce-based policy. **This is a security benefit of the rebuild that is easy to overlook.**

### 6.3 Never let the client decide what may be shown

The backend already excludes over-cleared content at the SQL layer — a viewer's query does not
return OFFICIAL-SENSITIVE rows, so the browser never holds them. The new front end must preserve
that property: **the UI hides things because the server did not send them, never because the UI
chose not to draw them.**

A client-side `if (role === 'admin')` around data the server already sent is not a security control.
It is a decoration over a leak.

---

## 7. Two things that are not code

Both will be missed if they are not scheduled.

**A native Arabic reviewer.** The briefing agent produces genuine Arabic (verified: 1,005 characters
of real Arabic script, with a structural faithfulness check against the English). But **no one has
yet judged whether its *register* is right for a ministerial reader.** Fluent and correct is not the
same as appropriate. This needs a named person and a slot in the schedule.

**An accessibility audit.** A UK/EU/UAE government service will be held to WCAG 2.2 AA. Radix gives a
strong foundation, but the audit itself is work, and it is far cheaper to do during the rebuild than
after it.

---

## 8. Cost and risk of *not* rebuilding

Being fair to the do-nothing option, because it is a legitimate choice:

**If DANAH stays a pilot** — a demo, a proof of value, a tool for a small group — **the wired v11 is
adequate and rebuilding is a waste of money.** Do not rebuild for its own sake.

**If DANAH goes into daily ministerial service, the single-file front end becomes the bottleneck:**

- Every change carries regression risk on every screen, with no tests to catch it.
- Only one person can work on it at a time.
- A backend field rename shows the minister a blank space instead of failing the build.
- CSP must stay weak.
- The dormant simulation code stays in the file, one line away from being re-enabled.

**Recommended trigger:** commit to the rebuild when the ministry commits to DANAH as an operational
system rather than a pilot. Not before — and not after the second change request lands.

---

## 9. What to do this week

1. **Rotate the OpenAI API key.** It was shared over chat during integration and must be considered
   compromised. (`platform.openai.com` → revoke → issue a new one → update `.env`.)
2. **Change both seeded passwords** — `admin@ministry.gov` and `executive@ministry.gov` currently
   share the initial password from `.env`.
3. **Set `CORS_ORIGINS`** to the real domain and **remove the development `null` origin**.
4. **Create the real approvers.** The approval queue is only as real as the named officials in it.
5. **Demonstrate the wired v11** and get a decision on pilot vs. operational — that decision is what
   determines whether §5 happens at all.

---

## 10. One-paragraph summary for the client

> DANAH is now a working system: a real AI backend with 173 passing tests, connected to the command
> centre you have already approved. It answers from your documents with citations, runs six real AI
> agents over live data, and publishes nothing without a named human approving it. The front end is
> the prototype, wired — which is genuinely fit to pilot with. What it cannot do is grow: it is a
> single 5,839-line file that cannot be tested, cannot be worked on by two people at once, and
> cannot enforce a strict security policy. When the ministry commits to DANAH as an operational
> service rather than a pilot, the front end should be rebuilt in Next.js and TypeScript over about
> 8–10 weeks — keeping the current design almost exactly, screen by screen, with the existing system
> running throughout. Until that decision is taken, no rebuild is needed.
