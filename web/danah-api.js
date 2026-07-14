/* =====================================================================
   DANAH — v11 ⇄ backend integration layer
   =====================================================================

   This file turns the v11 prototype into the real product. It loads AFTER
   the prototype's own script and redefines the four functions that were
   simulations, so the screens, layout and design are untouched — only the
   source of truth changes.

   Replaced:
     signIn / renderLogin  dummy SSO role-picker  → POST /api/auth/login (argon2 + JWT)
     askDanah              keyword matching       → POST /api/agent/chat (real model, real citations)
     runPipeline           14 strings on a timer  → POST /api/pipeline/run + poll the real run
     approveDecision       local array splice     → POST /api/approvals/{id}/decision

   A note on honesty, which is the whole point of this system:

   The prototype could afford to invent things — it was labelled as a
   simulation. This cannot. So the rules here are strict:

     * Nothing is invented. If the backend returns no insights, the screen
       shows no insights. An empty state is a truthful state.
     * An abstention is rendered verbatim. When the model says the corpus
       does not cover a question, that answer reaches the user intact — it
       is not "helpfully" padded with something plausible.
     * Confidence and citations come from the response, never from the UI.
     * If the backend is unreachable the header says DEMO, loudly. The
       prototype's synthetic data still renders (so a demo never dies on
       stage) but it is never passed off as live.
   ===================================================================== */
(function () {
  'use strict';

  const API = (window.DANAH_API_BASE || '').replace(/\/$/, ''); // same origin by default
  const TOKEN_KEY = 'danah.access';
  const RTOKEN_KEY = 'danah.refresh';

  const state = {
    live: false,
    user: null,
    pollTimer: null,
  };

  /* ---------- token store -------------------------------------------- */
  const tok = {
    get access() { return sessionStorage.getItem(TOKEN_KEY) || ''; },
    set access(v) { v ? sessionStorage.setItem(TOKEN_KEY, v) : sessionStorage.removeItem(TOKEN_KEY); },
    get refresh() { return sessionStorage.getItem(RTOKEN_KEY) || ''; },
    set refresh(v) { v ? sessionStorage.setItem(RTOKEN_KEY, v) : sessionStorage.removeItem(RTOKEN_KEY); },
    clear() { this.access = ''; this.refresh = ''; },
  };

  /* ---------- fetch wrapper ------------------------------------------ */
  async function call(path, { method = 'GET', body, auth = true, timeout = 60000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    const headers = { 'Content-Type': 'application/json' };
    if (auth && tok.access) headers['Authorization'] = `Bearer ${tok.access}`;

    let resp;
    try {
      resp = await fetch(`${API}/api${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }

    // One transparent refresh. A 15-minute access token will expire in the
    // middle of a long pipeline poll otherwise, and the user would be thrown
    // back to the login screen mid-run for no reason they could see.
    if (resp.status === 401 && auth && tok.refresh) {
      const ok = await refreshToken();
      if (ok) return call(path, { method, body, auth, timeout });
    }

    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const err = await resp.json();
        detail = err?.error?.message || err?.detail || detail;
      } catch (_) { /* body was not JSON; the status is all we have */ }
      const e = new Error(detail);
      e.status = resp.status;
      throw e;
    }
    return resp.status === 204 ? null : resp.json();
  }

  async function refreshToken() {
    try {
      const r = await call('/auth/refresh', {
        method: 'POST',
        body: { refresh_token: tok.refresh },
        auth: false,
      });
      tok.access = r.access_token;
      if (r.refresh_token) tok.refresh = r.refresh_token;
      return true;
    } catch (_) {
      tok.clear();
      return false;
    }
  }

  /* ---------- role mapping ------------------------------------------- *
   * The backend has four roles and is the only thing that enforces them.
   * v11 has ten, and used them to decide what to draw. So the backend role
   * chooses which v11 persona to render — but it decides nothing about
   * access. If the UI and the server ever disagree about what this user may
   * see, the server wins, because the server is the one holding the data.  */
  const ROLE_MAP = {
    admin: 'cabinethead',
    executive: 'sg',
    analyst: 'analyst',
    viewer: 'focal',
  };

  /* ---------- LIVE / DEMO badge --------------------------------------- */
  function setBadge(live, note) {
    state.live = live;
    let b = document.getElementById('danah-live-badge');
    if (!b) {
      b = document.createElement('div');
      b.id = 'danah-live-badge';
      b.style.cssText =
        'position:fixed;z-index:99999;bottom:14px;right:14px;padding:7px 12px;border-radius:999px;' +
        'font:600 11px/1 system-ui,sans-serif;letter-spacing:.06em;display:flex;gap:7px;align-items:center;' +
        'box-shadow:0 4px 18px rgba(0,0,0,.28);cursor:default;user-select:none';
      document.body.appendChild(b);
    }
    b.style.background = live ? '#0b3d2c' : '#4a2c00';
    b.style.color = live ? '#5ee9a8' : '#ffc46b';
    b.style.border = `1px solid ${live ? '#12805b' : '#8a5a10'}`;
    b.innerHTML =
      `<span style="width:7px;height:7px;border-radius:50%;background:currentColor;` +
      `box-shadow:0 0 8px currentColor"></span>` +
      (live ? 'LIVE — REAL AI BACKEND' : 'DEMO — BACKEND OFFLINE');
    b.title = note || (live
      ? 'Answers, insights and briefings come from the real model and the real database.'
      : 'The backend is unreachable. Screens show the prototype\'s synthetic data — nothing here is real.');
  }

  async function probe() {
    try {
      const h = await call('/healthz', { auth: false, timeout: 6000 });
      const ok = h.database === 'up' && h.llm_configured;
      setBadge(ok, ok ? '' : 'Backend reachable but not fully configured.');
      return ok;
    } catch (_) {
      setBadge(false);
      return false;
    }
  }

  /* =====================================================================
     1. LOGIN — real credentials, not a role dropdown
     ===================================================================== */
  window.renderLogin = function renderLogin() {
    const r = document.querySelector('#login-root');
    if (!r) return;
    const emblem = (typeof emblemSVG === 'function') ? emblemSVG() : '';
    r.innerHTML = `
      <div class="login-scrim" role="dialog" aria-modal="true" aria-label="Official Sign-In">
        <div class="login-card">
          <div class="login-top">
            <div class="login-emblem">${emblem}</div>
            <div><h1>UNITED ARAB EMIRATES</h1><h2>MINISTRY OF CABINET AFFAIRS</h2></div>
          </div>
          <div class="login-sub">
            DANAH — Agentic AI Command Centre. Sign in to continue.
          </div>
          <div class="login-field">
            <label>Email</label>
            <input id="loginEmail" type="email" autocomplete="username"
                   value="admin@ministry.gov"
                   style="width:100%;padding:11px 12px;border-radius:8px;border:1px solid #2a3550;
                          background:#0e1526;color:#e8eefc;font-size:14px" />
          </div>
          <div class="login-field">
            <label>Password</label>
            <input id="loginPass" type="password" autocomplete="current-password"
                   style="width:100%;padding:11px 12px;border-radius:8px;border:1px solid #2a3550;
                          background:#0e1526;color:#e8eefc;font-size:14px" />
          </div>
          <div id="loginErr" style="display:none;color:#ff8095;font-size:12.5px;margin:8px 2px 0"></div>
          <button class="btn btn-primary" id="loginBtn" style="width:100%;margin-top:14px">
            Sign in
          </button>
          <div style="margin-top:12px;font-size:11.5px;color:#7f8db0;line-height:1.55">
            Authenticated server-side (argon2 + JWT). Your role and clearance are decided by the
            backend and enforced on every request — never in this browser.
          </div>
        </div>
      </div>`;

    const go = () => doLogin();
    document.getElementById('loginBtn').addEventListener('click', go);
    document.getElementById('loginPass').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') go();
    });
    document.getElementById('loginEmail').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') document.getElementById('loginPass').focus();
    });
  };

  async function doLogin() {
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPass').value;
    const errEl = document.getElementById('loginErr');
    const btn = document.getElementById('loginBtn');
    errEl.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Signing in…';

    try {
      const t = await call('/auth/login', {
        method: 'POST',
        body: { email, password },
        auth: false,
        timeout: 20000,
      });
      tok.access = t.access_token;
      if (t.refresh_token) tok.refresh = t.refresh_token;

      const me = await call('/auth/me');
      state.user = me;
      await establishSession(me);
    } catch (e) {
      // Say what actually happened. "Something went wrong" teaches the user nothing
      // and hides a backend that is simply not running.
      errEl.textContent =
        e.status === 401 ? 'Incorrect email or password.'
        : e.status === 429 ? 'Too many attempts. Wait a minute and try again.'
        : `Cannot reach the backend — ${e.message}. Is the stack running (docker compose up)?`;
      errEl.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  }

  async function establishSession(me) {
    const vRole = ROLE_MAP[me.role] || 'analyst';
    const ro = (typeof ROLES !== 'undefined' && ROLES.find((x) => x.id === vRole)) || {};
    const name = me.full_name || me.email;
    const initials = name.split(/[\s@.]+/).filter(Boolean).slice(0, 2)
      .map((w) => w[0].toUpperCase()).join('');

    SESSION = {
      name,
      initials,
      rank: vRole,
      rankName: ro.name || me.role,
      // The clearance shown is the backend's, not v11's table — the server is the
      // only thing that decides what this user may read, so it is the only thing
      // entitled to say what their clearance is.
      clearance: me.clearance || ro.clearance || 'Official',
      dept: ro.dept || '—',
      ts: (typeof nowStamp === 'function') ? nowStamp() : new Date().toISOString(),
      method: 'DANAH backend · argon2 + JWT',
      backendRole: me.role,
      email: me.email,
    };
    S.role = vRole;

    hideLogin();
    if (typeof applyEnv === 'function') applyEnv();
    if (typeof renderSidebar === 'function') renderSidebar();
    if (typeof renderHeader === 'function') renderHeader();

    await hydrate();
    if (typeof render === 'function') render();
    if (typeof toast === 'function') toast(`Signed in as ${me.role} — connected to the live backend`);
  }

  window.signOut = function signOut() {
    tok.clear();
    SESSION = null;
    state.user = null;
    if (typeof closeModal === 'function') closeModal();
    showLogin();
  };

  /* =====================================================================
     2. LIVE AGENT CHAT — a real model, real citations, real abstention
     ===================================================================== */
  window.askDanah = async function askDanah(q) {
    if (!state.live) return legacyAsk(q);

    openAnswerModal(q, null, true);
    try {
      const r = await call('/agent/chat', {
        method: 'POST',
        body: { message: q, language: (S && S.lang === 'ar') ? 'ar' : 'en' },
        timeout: 120000,
      });
      openAnswerModal(q, r, false);
    } catch (e) {
      openAnswerModal(q, { __error: e.message }, false);
    }
  };

  function legacyAsk(q) {
    // Backend down: fall back to the prototype's scripted answer, clearly marked.
    // Better a demo that still runs than a dead screen — but it must never be
    // mistaken for the real thing.
    const res = (typeof ansGeneric === 'function') ? ansGeneric(q) : null;
    if (res && typeof openCopilot === 'function') openCopilot(q, res);
    if (typeof toast === 'function') toast('Backend offline — showing the prototype\'s simulated answer');
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function openAnswerModal(q, r, loading) {
    let inner;

    if (loading) {
      inner = `<div style="padding:26px 4px;color:#9fb0d0;font-size:14px">
        <div class="spin" style="display:inline-block;width:14px;height:14px;margin-right:9px;
             border:2px solid #2b3a5c;border-top-color:#5b8cff;border-radius:50%;
             animation:danahspin .8s linear infinite;vertical-align:-2px"></div>
        Retrieving from the knowledge base and reasoning over what it found…
      </div>
      <style>@keyframes danahspin{to{transform:rotate(360deg)}}</style>`;
    } else if (r.__error) {
      inner = `<div style="color:#ff8095;font-size:14px;padding:16px 2px">
        The request failed: ${esc(r.__error)}
      </div>`;
    } else {
      const cites = r.citations || [];
      const pctv = Math.round((r.confidence || 0) * 100);

      // Grounded=false is not a failure and must not be dressed up as one. It is the
      // system refusing to answer beyond its evidence — the single most important
      // behaviour it has. Show it plainly.
      const banner = r.grounded
        ? `<div style="display:flex;gap:9px;align-items:center;padding:9px 12px;border-radius:8px;
             background:#0b2a1e;border:1px solid #145c3f;color:#5ee9a8;font-size:12.5px;margin-bottom:14px">
             <b>GROUNDED</b> · ${cites.length} citation${cites.length === 1 ? '' : 's'} ·
             confidence ${pctv}%
           </div>`
        : `<div style="display:flex;gap:9px;align-items:center;padding:9px 12px;border-radius:8px;
             background:#3a2a08;border:1px solid #8a6a12;color:#ffc46b;font-size:12.5px;margin-bottom:14px">
             <b>ABSTAINED</b> · the corpus does not support an answer — DANAH declined rather than guess
           </div>`;

      const citeHtml = cites.length
        ? `<div style="margin-top:18px">
             <div style="font-size:11px;letter-spacing:.08em;color:#7f8db0;margin-bottom:8px">SOURCES</div>
             ${cites.map((c) => `
               <div style="display:flex;gap:10px;padding:10px 12px;margin-bottom:7px;border-radius:8px;
                    background:#0e1526;border:1px solid #222e4a">
                 <div style="flex:0 0 22px;height:22px;border-radius:6px;background:#1b2942;color:#8fb0ff;
                      font:600 11px/22px system-ui;text-align:center">${esc(c.n)}</div>
                 <div style="flex:1;min-width:0">
                   <div style="color:#dce6fb;font-size:13px;font-weight:600">${esc(c.title)}</div>
                   <div style="color:#8494b5;font-size:12px;margin-top:3px;line-height:1.5">${esc(c.snippet)}</div>
                   ${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener"
                        style="color:#5b8cff;font-size:11.5px">${esc(c.url)}</a>` : ''}
                 </div>
               </div>`).join('')}
           </div>`
        : '';

      inner = `${banner}
        <div style="color:#e6edfb;font-size:14.5px;line-height:1.72;white-space:pre-wrap">${esc(r.answer)}</div>
        ${citeHtml}
        <div style="margin-top:16px;padding-top:12px;border-top:1px solid #1e2942;color:#6f7d9c;font-size:11px">
          ${r.tokens_in + r.tokens_out} tokens · ${r.latency_ms} ms · DANAH advises, you decide.
        </div>`;
    }

    const icon = (typeof ic === 'function') ? ic('spark', 21) : '';
    openModal(`<div class="modal wide cop" onclick="event.stopPropagation()" role="dialog"
                    aria-modal="true" aria-label="Ask DANAH">
      <div class="modal-head">
        <div class="mh-ic bg-orange tone-orange">${icon}</div>
        <div><h3>Ask DANAH</h3><p>${esc(q)}</p></div>
        <button class="modal-x" onclick="closeModal()" aria-label="Close">✕</button>
      </div>
      <div class="modal-body">${inner}</div>
      <div class="modal-foot">
        <button class="btn btn-ghost" onclick="closeModal()">Close</button>
      </div>
    </div>`);
  }

  /* =====================================================================
     2b. THE LIVE AGENT PANEL — the *other* chat, and the one users reach for
     =====================================================================
     v11 has two chats that look identical to a user and share nothing in code:
     the "Ask DANAH" bar (askDanah -> openCopilot) and this persistent side
     panel (sendLiveAgentMessage -> generateLiveAgentResponse). Wiring only the
     first left the second still keyword-matching: asked "how many moons does
     Jupiter have?", it replied with a canned paragraph about navigating the
     sidebar. Worse than a wrong answer — it looked like a working AI giving a
     confident non-answer, on the panel a minister is most likely to open.     */
  window.sendLiveAgentMessage = async function sendLiveAgentMessage() {
    const input = document.querySelector('#laInput');
    if (!input) return;
    const q = (input.value || '').trim();
    if (!q) return;

    if (!state.live) return legacySend(q, input);

    if (LA.typingTimer) { clearTimeout(LA.typingTimer); LA.typingTimer = null; }
    addAgentMessage('user', q);
    input.value = '';
    input.focus();

    LA.abortRequested = false;
    LA.busy = true;
    LA._typing = true;
    if (typeof paintBusy === 'function') paintBusy();
    if (typeof paintMessages === 'function') paintMessages();

    try {
      const r = await call('/agent/chat', {
        method: 'POST',
        body: { message: q, language: (S && S.lang === 'ar') ? 'ar' : 'en' },
        timeout: 120000,
      });
      LA._typing = false;
      LA.busy = false;
      addAgentMessage('agent', formatChat(r), chatActions(r));
    } catch (e) {
      LA._typing = false;
      LA.busy = false;
      addAgentMessage('agent', `The request failed: ${e.message}`);
    }

    LA.lastResponseAt = Date.now();
    if (typeof paintBusy === 'function') paintBusy();
    if (typeof paintMessages === 'function') paintMessages();
    const again = document.querySelector('#laInput');
    if (again) again.focus();
  };

  function legacySend(q, input) {
    // Backend down. Keep the prototype's behaviour so a demo survives, but say so.
    addAgentMessage('user', q);
    input.value = '';
    addAgentMessage('agent',
      'The DANAH backend is offline, so I cannot answer from the knowledge base. ' +
      'Start the stack (docker compose up -d) and ask again.');
    if (typeof paintMessages === 'function') paintMessages();
  }

  /* The panel renders text, not HTML, so the grounding verdict has to be carried in the
     words themselves. An abstention must read as a deliberate refusal — never as an
     apology or an error, and never quietly padded into something that sounds like an
     answer. That refusal is the product. */
  function formatChat(r) {
    if (!r.grounded) {
      return (
        'ABSTAINED — the corpus does not support an answer.\n\n' +
        r.answer +
        '\n\nI will not answer beyond the evidence I hold. Add a document on this subject ' +
        'and ask again.'
      );
    }
    const cites = (r.citations || [])
      .map((c) => `[${c.n}] ${c.title}${c.snippet ? ` — ${c.snippet.slice(0, 140)}` : ''}`)
      .join('\n');
    const pctv = Math.round((r.confidence || 0) * 100);
    return (
      `GROUNDED · ${r.citations.length} citation${r.citations.length === 1 ? '' : 's'} · confidence ${pctv}%\n\n` +
      `${r.answer}\n\n` +
      `SOURCES\n${cites}`
    );
  }

  function chatActions(r) {
    const acts = [];
    if (r.grounded && (r.citations || []).some((c) => c.document_id)) {
      acts.push({ id: 'go:knowledge', label: 'Open Verified Knowledge' });
    }
    if (!r.grounded) acts.push({ id: 'go:knowledge', label: 'Add a document' });
    return acts;
  }

  /* =====================================================================
     3. PIPELINE — a real orchestrator run, polled, not a scripted timer
     ===================================================================== */
  window.runPipeline = async function runPipeline() {
    if (!state.live) {
      if (typeof toast === 'function') toast('Backend offline — cannot run the real pipeline');
      return;
    }
    if (S.route !== 'agents') {
      go('agents');
      setTimeout(runPipeline, 420);
      return;
    }
    const con = document.querySelector('#console');
    if (!con) return;
    con.innerHTML = '';

    const steps = document.querySelectorAll('.step');
    steps.forEach((s) => s.classList.remove('done', 'active'));
    if (typeof AGENTS !== 'undefined') { AGENTS.forEach((a) => (a.status = 'idle')); renderAgentsLive(); }

    log(con, 'info', 'SYSTEM', 'Requesting a real pipeline run from the orchestrator…');

    let runId;
    try {
      const r = await call('/pipeline/run', { method: 'POST', body: { max_items: 12 } });
      runId = r.run_id;
      log(con, 'ok', 'QUEUED', `Run ${runId} accepted. Six agents will now analyse real ingested items.`);
      log(con, 'info', 'NOTE', 'This is a live model call over real data — expect 60–120 seconds, not 3.');
    } catch (e) {
      log(con, 'warn', 'ERROR', `Could not start a run: ${e.message}`);
      return;
    }

    const seen = new Set();
    const started = Date.now();

    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      let d;
      try {
        d = await call(`/pipeline/runs/${runId}`);
      } catch (e) {
        log(con, 'warn', 'ERROR', `Polling failed: ${e.message}`);
        clearInterval(state.pollTimer);
        return;
      }

      // Report each agent as the backend actually finishes it — including failures.
      // The simulation could not fail; this can, and when it does the operator must
      // see which agent failed and why, not a green tick.
      (d.steps || []).forEach((s) => {
        if (seen.has(s.id)) return;
        seen.add(s.id);
        const ok = s.status === 'completed';
        const ag = (typeof AGENTS !== 'undefined') && AGENTS.find((a) => a.id === s.agent);
        if (ag) { ag.status = ok ? 'done' : 'idle'; renderAgentsLive(); }
        log(
          con,
          ok ? 'ok' : 'warn',
          String(s.agent).toUpperCase(),
          ok
            ? `completed — ${s.tokens_in + s.tokens_out} tokens, $${Number(s.cost_usd).toFixed(4)}, ${s.latency_ms} ms`
            : `FAILED — ${s.error || s.status}`
        );
        const idx = Math.min(seen.size - 1, steps.length - 1);
        steps.forEach((el2, n) => { el2.classList.remove('active'); if (n < idx) el2.classList.add('done'); });
        if (steps[idx]) steps[idx].classList.add('active');
      });

      if (['completed', 'partial', 'failed'].includes(d.status)) {
        clearInterval(state.pollTimer);
        steps.forEach((el2) => { el2.classList.remove('active'); el2.classList.add('done'); });

        const st = d.stats || {};
        const secs = ((Date.now() - started) / 1000).toFixed(1);
        log(con, d.status === 'failed' ? 'warn' : 'ok', 'SYSTEM',
          `Run ${d.status} in ${secs}s — ${st.risks || 0} risks, ${st.opportunities || 0} opportunities, ` +
          `${st.policies || 0} policy signals, ${st.briefings || 0} briefings, ${st.memories || 0} memories. ` +
          `${d.total_tokens} tokens · $${Number(d.total_cost_usd || 0).toFixed(4)}.`);

        if (st.failed_steps && st.failed_steps.length) {
          log(con, 'warn', 'PARTIAL',
            `These agents failed and produced nothing: ${st.failed_steps.join(', ')}. ` +
            `The rest of the run was kept.`);
        }
        if (!st.memories) {
          log(con, 'info', 'MEMORY',
            'The Memory agent judged nothing in this run durable enough to remember. ' +
            'That is a decision, not a failure — it refuses to restate an insight as a memory.');
        }

        await hydrate();
        if (typeof render === 'function') render();
        if (typeof toast === 'function') toast(`Pipeline ${d.status} — insights are awaiting human approval`);
      }
    }, 3000);
  };

  /* =====================================================================
     4. APPROVALS — the human gate, for real
     ===================================================================== */
  window.approveDecision = async function approveDecision(id) {
    if (!state.live) { if (typeof toast === 'function') toast('Backend offline'); return; }
    await decide(id, 'approved', 'Approved via DANAH command centre');
  };
  window.deferDecision = async function deferDecision(id) {
    if (!state.live) { if (typeof toast === 'function') toast('Backend offline'); return; }
    await decide(id, 'changes_requested', 'Deferred — more analysis requested');
  };
  window.requestAltDecision = async function requestAltDecision(id) {
    if (!state.live) { if (typeof toast === 'function') toast('Backend offline'); return; }
    await decide(id, 'changes_requested', 'Alternative options requested');
  };

  async function decide(id, decision, comment) {
    try {
      await call(`/approvals/${id}/decision`, { method: 'POST', body: { decision, comment } });
      if (typeof closeModal === 'function') closeModal();
      await hydrate();
      if (typeof render === 'function') render();
      if (typeof toast === 'function') {
        toast(decision === 'approved'
          ? 'Approved and PUBLISHED — recorded in the tamper-evident audit log'
          : 'Sent back — recorded in the tamper-evident audit log');
      }
    } catch (e) {
      if (typeof toast === 'function') toast(`Decision failed: ${e.message}`);
    }
  }

  /* =====================================================================
     5. HYDRATE — replace the synthetic arrays with what the database holds
     ===================================================================== */
  async function hydrate() {
    if (!state.live) return;
    try {
      const [insights, approvals, dash] = await Promise.all([
        call('/insights?limit=25').catch(() => null),
        call('/approvals?status=pending').catch(() => null),
        call('/dashboard/summary').catch(() => null),
      ]);

      const rows = Array.isArray(insights) ? insights : (insights?.items || []);
      if (rows.length) {
        INSIGHTS.length = 0;
        rows.forEach((i) => INSIGHTS.push({
          id: i.id,
          type: i.kind,
          title: i.title,
          body: i.body,
          ministry: (i.domains && i.domains[0]) || 'all',
          urgency: i.severity >= 4 ? 'critical' : i.severity >= 3 ? 'high' : 'medium',
          confidence: i.confidence,
          impact: i.impact,
          status: i.status,
          agent: i.created_by_agent,
        }));
      }

      // The audit log is the one screen that must never show a fabricated row. It is the
      // system's evidence that a human decided, and its whole value is that it can be
      // verified against a hash chain. The prototype ships invented entries ("Fatma Almulla
      // approved…"). Left in place beside real ones, they would be indistinguishable —
      // and an audit trail you cannot trust is not an audit trail. Replace, never merge.
      const audit = await call('/audit?limit=60').catch(() => null);
      const arows = Array.isArray(audit) ? audit : (audit?.items || []);
      if (typeof AUDIT !== 'undefined') {
        AUDIT.length = 0;
        arows.forEach((a) => AUDIT.push({
          ts: (a.ts || '').replace('T', ' ').slice(0, 16) + ' GST',
          actor: a.actor_type === 'system' ? 'DANAH · System'
            : a.actor_type === 'agent' ? `DANAH · ${a.action.split('.')[0]}`
            : (a.detail?.email || a.actor_id || 'user'),
          action: a.action,
          detail: typeof a.detail === 'object'
            ? Object.entries(a.detail).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(' · ')
            : String(a.detail ?? ''),
          cls: 'OFFICIAL',
          hash: (a.entry_hash || '').slice(0, 12),
        }));
      }

      const appr = Array.isArray(approvals) ? approvals : (approvals?.items || []);
      DECISIONS.length = 0;
      appr.forEach((a) => DECISIONS.push({
        id: a.id,
        title: a.subject_title || `${a.subject_type} awaiting decision`,
        summary: a.subject_summary || '',
        ministry: 'all',
        urgency: (a.subject_severity || 0) >= 4 ? 'critical' : 'high',
        confidence: a.subject_confidence || 0,
        score: Math.round((a.subject_confidence || 0) * 100),
        owner: a.assigned_role,
        timeToImpact: '—',
        subjectType: a.subject_type,
      }));

      if (dash?.counts) {
        window.DANAH_DASH = dash;
        bindCircuit(dash);
      }
    } catch (e) {
      console.warn('[DANAH] hydrate failed', e);
    }
  }

  /* The hero panel — the first thing anyone sees — shipped with invented numbers:
     "48 sources checked · 23 items updated · 91% confidence". They were honest in a
     prototype that said "synthetic demo logic" underneath. They are not honest now,
     sitting directly above real insights. Bind them to what the database actually
     holds. Where the backend has no equivalent figure, show nothing rather than
     leave the prototype's.                                                        */
  function bindCircuit(dash) {
    if (typeof CIRCUIT === 'undefined') return;
    const c = dash.counts || {};
    const k = dash.kpi || {};
    const run = dash.latest_run;

    CIRCUIT.sourcesChecked = (dash.source_health || []).length;
    CIRCUIT.itemsUpdated = c.items_total ?? 0;
    CIRCUIT.decisionsPrepared = c.approvals_pending ?? 0;
    CIRCUIT.risksEscalated = c.risks_open ?? 0;
    CIRCUIT.briefingsReady = c.briefings_total ?? c.insights_published ?? 0;
    CIRCUIT.confidence = k.avg_insight_confidence ?? 0;
    CIRCUIT.lastRefresh = run?.finished_at
      ? new Date(run.finished_at).toLocaleString('en-GB', { hour12: true })
      : 'no run yet';
    CIRCUIT.next = run ? `last run: ${run.status}` : 'no run yet — press Run Pipeline';
    CIRCUIT.running = !!run;

    relabel();
    markSimulated();
  }

  /* The prototype labelled its own simulations honestly — "synthetic demo logic",
     "simulated in this prototype", "production hooks ready". Those labels are now false
     in the opposite direction: they describe fabrication above numbers that are real, and
     a viewer would discount true figures as fake. Honesty is not a one-way ratchet; a
     label that understates what the system does is still a label that misleads.

     Walk the text nodes rather than the elements — the strings sit inside elements that
     have children, so an element-level scan silently misses every one of them.          */
  /* Relabel ONLY what is genuinely wired. The first version of this list was too eager: it
     rewrote "simulated in this prototype" to "running server-side, for real" on the Live
     Intelligence Engine panel — which is NOT wired. The screen then claimed the panel was real
     directly above the panel's own honest warning that it was synthetic. Overclaiming is the
     same failure as underclaiming, and on this product it is the worse one. Panels that are
     still simulated keep their warnings and get an explicit badge (see markSimulated).       */
  const RELABEL = [
    ['synthetic demo logic', 'live data · real agents'],   // hero panel — wired
    ['production hooks ready', 'connected to the DANAH backend'],
    ['Simulate next cycle', 'Run pipeline now'],           // button now triggers the real run
    // The Live Agent panel's own footer. Its chat is wired now, so "synthetic data" is false
    // there — and it sits directly under a real, cited answer.
    ['human approval required · synthetic data', 'human approval required · live data'],
  ];

  /* Panels v11 ships that are NOT wired to the backend. They still show invented numbers, so
     they are badged, not silently left to be mistaken for real. Naming them is more useful to
     the client than hiding them: it says exactly what remains to be built.                   */
  const SIMULATED_PANELS = [
    'DANAH Live Intelligence Engine',
    'What Changed Since Yesterday',
    'National Strategic Health',
    'Agent Roster',
    'Cabinet Affairs agents',
    'ACTION TRACKER',
    'NATIONAL SNAPSHOT',
  ];

  function markSimulated() {
    const heads = document.querySelectorAll('h1,h2,h3,h4,.card-h,.sec-h,.panel-h,[class*="head"]');
    heads.forEach((h) => {
      const label = (h.textContent || '').trim();
      if (!SIMULATED_PANELS.some((p) => label.toLowerCase().startsWith(p.toLowerCase()))) return;
      if (h.querySelector('.danah-sim')) return;
      const b = document.createElement('span');
      b.className = 'danah-sim';
      b.textContent = 'SIMULATED — not wired';
      b.title =
        'This panel still shows the prototype\'s invented numbers. It is not connected to the ' +
        'backend. The Command Centre header, Decisions, AI Agents, Chat and Audit are real.';
      b.style.cssText =
        'margin-left:9px;padding:2px 7px;border-radius:999px;background:#4a2c00;color:#ffc46b;' +
        'border:1px solid #8a5a10;font:600 9.5px/1.6 system-ui,sans-serif;letter-spacing:.05em;' +
        'vertical-align:middle;white-space:nowrap';
      h.appendChild(b);
    });
  }

  function relabel(root) {
    const walker = document.createTreeWalker(root || document.body, NodeFilter.SHOW_TEXT, null);
    const hits = [];
    let n;
    while ((n = walker.nextNode())) {
      const t = n.nodeValue;
      if (!t || t.length < 6) continue;
      if (RELABEL.some(([from]) => t.includes(from))) hits.push(n);
    }
    hits.forEach((node) => {
      let t = node.nodeValue;
      RELABEL.forEach(([from, to]) => { t = t.split(from).join(to); });
      node.nodeValue = t;
    });
  }

  // v11 re-renders whole panels on navigation, which restores the original strings. Re-run
  // the relabel after each render rather than once at boot.
  const _render = window.render;
  if (typeof _render === 'function') {
    window.render = function () {
      const out = _render.apply(this, arguments);
      if (state.live) setTimeout(() => { relabel(); markSimulated(); }, 0);
      return out;
    };
  }

  /* ---------- silence the simulations ---------------------------------- *
   * The prototype runs two background loops that fabricate activity: a 9-second
   * "circuit tick" that invents refreshed sources and prepared decisions, and an
   * always-on "intelligence engine" that invents cycles. Harmless in a prototype
   * that was labelled as one. Not harmless here: they would scatter invented
   * numbers across the same screens now showing real ones, and no viewer could
   * tell which was which. A number nobody can trust is worse than no number.
   *
   * The prototype badge goes too — it now says PROTOTYPE about a system that is
   * really running, which is its own kind of lie.                             */
  function silenceSimulations() {
    try {
      if (typeof CIRCUIT_TIMER !== 'undefined' && CIRCUIT_TIMER) {
        clearInterval(CIRCUIT_TIMER);
        CIRCUIT_TIMER = null;
      }
      if (typeof stopIntelligenceEngine === 'function') stopIntelligenceEngine();
      if (typeof ENGINE_TIMER !== 'undefined' && ENGINE_TIMER) {
        clearInterval(ENGINE_TIMER);
        ENGINE_TIMER = null;
      }
      if (typeof CIRCUIT !== 'undefined' && CIRCUIT.log) {
        CIRCUIT.log.length = 0;
        CIRCUIT.log.unshift({
          t: new Date().toLocaleTimeString('en-GB', { hour12: false }),
          c: 'live',
          m: 'Connected to the DANAH backend. Simulated cycles stopped — activity below is real.',
        });
      }
      const pb = document.querySelector('.proto-badge, #protoBadge');
      if (pb) pb.remove();
    } catch (e) {
      console.warn('[DANAH] could not fully silence the simulations', e);
    }
  }

  /* ---------- boot ----------------------------------------------------- */
  async function boot() {
    const live = await probe();
    if (live) silenceSimulations();

    if (live && tok.access) {
      // Survive a page refresh — the prototype lost everything on reload, which was
      // one of the client's explicit complaints.
      try {
        const me = await call('/auth/me');
        state.user = me;
        await establishSession(me);
        return;
      } catch (_) { tok.clear(); }
    }
    if (typeof requireAuth === 'function') requireAuth();
    else if (typeof showLogin === 'function') showLogin();
  }

  window.DANAH = { call, hydrate, probe, state, tok };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
