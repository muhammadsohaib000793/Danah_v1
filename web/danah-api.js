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
  /* The ministry's ranks, each a REAL seeded account, shown with the role and clearance the
     backend actually enforces. The prototype had eleven client-side roles that changed only the
     chrome; these seven are real logins whose access the server decides. The four backend roles
     (admin / executive / analyst / viewer) are the true access tiers the titles map onto. */
  const PERSONAS = [
    { email: 'admin@ministry.gov',     title: 'Cabinet Head',          role: 'admin',     clr: 'OFFICIAL-SENSITIVE', can: 'Full control · approvals · audit', c: '#c79a2e' },
    { email: 'executive@ministry.gov', title: 'Secretary General',     role: 'executive', clr: 'OFFICIAL-SENSITIVE', can: 'Approve & publish',                 c: '#5b8cff' },
    { email: 'minister@ministry.gov',  title: 'Minister',              role: 'executive', clr: 'OFFICIAL-SENSITIVE', can: 'Approve & publish',                 c: '#5b8cff' },
    { email: 'dg@ministry.gov',        title: 'Director General',      role: 'executive', clr: 'OFFICIAL-SENSITIVE', can: 'Approve & publish',                 c: '#5b8cff' },
    { email: 'analyst@ministry.gov',   title: 'Strategic Analyst',     role: 'analyst',   clr: 'OFFICIAL',           can: 'Upload docs · run pipeline',        c: '#e08a2b' },
    { email: 'advisor@ministry.gov',   title: 'Senior Policy Advisor', role: 'analyst',   clr: 'OFFICIAL',           can: 'Upload docs · run pipeline',        c: '#e08a2b' },
    { email: 'viewer@ministry.gov',    title: 'Entity Focal Point',    role: 'viewer',    clr: 'INTERNAL',           can: 'Read-only · classification-limited', c: '#2ecc71' },
    { email: 'guest@ministry.gov',     title: 'Guest Viewer',          role: 'viewer',    clr: 'INTERNAL',           can: 'Read-only · classification-limited', c: '#2ecc71' },
  ];
  window.danahPickPersona = function (email) {
    const e = document.getElementById('loginEmail'); if (e) e.value = email;
    document.querySelectorAll('.danah-persona').forEach((c) => { c.style.borderColor = '#243049'; });
    const card = document.querySelector('.danah-persona[data-email="' + email + '"]');
    if (card) card.style.borderColor = '#5b8cff';
    // Fill the email only — the password is always typed by hand. Clear anything the browser
    // may have auto-filled, then focus the field so it can be entered manually.
    const p = document.getElementById('loginPass'); if (p) { p.value = ''; p.focus(); }
  };

  window.renderLogin = function renderLogin() {
    const r = document.querySelector('#login-root');
    if (!r) return;
    const emblem = (typeof emblemSVG === 'function') ? emblemSVG() : '';
    const cards = PERSONAS.map((p) => `
      <button type="button" class="danah-persona" data-email="${p.email}" onclick="danahPickPersona('${p.email}')"
        style="text-align:left;border:1px solid ${p.email === 'admin@ministry.gov' ? '#5b8cff' : '#243049'};background:#0e1526;border-radius:11px;padding:11px 12px;cursor:pointer;transition:border-color .12s;display:flex;flex-direction:column;gap:5px">
        <div style="display:flex;align-items:center;gap:7px">
          <span style="width:8px;height:8px;border-radius:50%;background:${p.c};flex:none"></span>
          <span style="color:#e8eefc;font-size:13px;font-weight:700">${p.title}</span>
          <span style="margin-left:auto;font-size:9px;font-weight:700;letter-spacing:.05em;color:${p.c};background:${p.c}22;border:1px solid ${p.c}55;padding:2px 7px;border-radius:20px;text-transform:uppercase">${p.role}</span>
        </div>
        <div style="font-size:11px;color:#8fa0c4">Clearance <b style="color:#c7d4ef">${p.clr}</b></div>
        <div style="font-size:11px;color:#7f8db0">${p.can}</div>
      </button>`).join('');
    r.innerHTML = `
      <div class="login-scrim" role="dialog" aria-modal="true" aria-label="Official Sign-In">
        <div class="login-card" style="max-width:min(640px,94vw);max-height:92vh;overflow-y:auto">
          <div class="login-top">
            <div class="login-emblem">${emblem}</div>
            <div><h1>UNITED ARAB EMIRATES</h1><h2>MINISTRY OF CABINET AFFAIRS</h2></div>
          </div>
          <div class="login-sub">DANAH — Agentic AI Command Centre. Pick an account to fill its email, then type the password.</div>
          <div style="font-size:10.5px;font-weight:700;letter-spacing:.08em;color:#7f8db0;margin:4px 2px 9px;text-transform:uppercase">Official accounts · role &amp; clearance shown</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(188px,1fr));gap:9px;margin-bottom:16px">${cards}</div>
          <div class="login-field"><label>Email</label>
            <input id="loginEmail" type="email" autocomplete="username" value="admin@ministry.gov"
              style="width:100%;padding:11px 12px;border-radius:8px;border:1px solid #2a3550;background:#0e1526;color:#e8eefc;font-size:14px" /></div>
          <div class="login-field"><label>Password <span style="font-weight:400;color:#7f8db0;text-transform:none;letter-spacing:0">· type it manually</span></label>
            <input id="loginPass" type="password" autocomplete="new-password" autocorrect="off" autocapitalize="off" spellcheck="false"
              style="width:100%;padding:11px 12px;border-radius:8px;border:1px solid #2a3550;background:#0e1526;color:#e8eefc;font-size:14px" /></div>
          <div id="loginErr" style="display:none;color:#ff8095;font-size:12.5px;margin:8px 2px 0"></div>
          <button class="btn btn-primary" id="loginBtn" style="width:100%;margin-top:14px">Sign in</button>
          <div style="margin-top:12px;font-size:11.5px;color:#7f8db0;line-height:1.55">
            All accounts are real and share the demo password you set. Roles and clearance are
            decided by the backend (argon2 + JWT) and enforced on every request — never in this
            browser. The four backend roles are the real access tiers these titles map onto.
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
    // If they land (or reload) directly on a live-data page, populate it now — not only on nav.
    refreshForRoute((typeof S !== 'undefined' && S.route) || 'home');
    if (typeof window.danahMaybeTour === 'function') window.danahMaybeTour();  // guided tour on first login
    if (typeof toast === 'function') toast(`Signed in as ${me.role} — connected to the live backend`);
  }

  window.signOut = function signOut() {
    tok.clear();
    SESSION = null;
    state.user = null;
    if (typeof closeModal === 'function') closeModal();
    showLogin();
  };

  /* ---- role switching is decided by the server, not the browser --------
   * The prototype let you switch between eleven roles client-side ("Preview as
   * C-Suite", "Switch verified role"). That is now actively misleading: your
   * role comes from your login and is enforced on every request, so switching
   * it in the browser changes the chrome but not what the API will return — a
   * viewer who "previews as C-Suite" still gets no OFFICIAL-SENSITIVE data. So
   * when live, every entry point (header chip, visibility card, Access page,
   * Settings) is intercepted and explains the real behaviour: sign in as a
   * different user to see a different role. Offline, the prototype's simulated
   * switcher is left intact so a backend-less demo still works.            */
  const _protoSetRole = window.setRole;
  const _protoRoleSwitcher = window.openRoleSwitcher;
  const _protoPreviewLower = window.previewLowerRank;
  const roleMsg =
    'Your role is set by your login and enforced on the server — it cannot be switched in the ' +
    'browser. Sign out and sign in as admin, executive, analyst or viewer to see each role.';

  window.setRole = function (...args) {
    if (state.live) { if (typeof toast === 'function') toast(roleMsg); return; }
    return typeof _protoSetRole === 'function' ? _protoSetRole.apply(this, args) : undefined;
  };
  window.openRoleSwitcher = function (...args) {
    if (state.live) { if (typeof toast === 'function') toast(roleMsg); return; }
    return typeof _protoRoleSwitcher === 'function' ? _protoRoleSwitcher.apply(this, args) : undefined;
  };
  window.previewLowerRank = function (...args) {
    if (state.live) {
      if (typeof toast === 'function') {
        toast('Access is enforced server-side. To see the restricted view, sign in as ' +
          'viewer@ministry.gov rather than previewing here.');
      }
      return;
    }
    return typeof _protoPreviewLower === 'function' ? _protoPreviewLower.apply(this, args) : undefined;
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
     =====================================================================
     The prototype's decision card renders sections this pipeline does not produce:
     "Financial impact", "Red-Team dissent", and "Possible outcomes — 3–5 modelled
     scenarios" whose probabilities (20/45/20/10) are hard-coded in makeOutcomes().
     Feeding it a real approval crashed it outright — d.evidence was undefined and
     it called .map() on it.

     The fix is not to invent the missing fields. This is the screen where a human
     commits their name to publishing intelligence; a fabricated 45%-likely scenario
     or an invented financial figure sitting next to a real risk is precisely the
     harm the whole system is built to prevent. So the card shows what the analysis
     genuinely produced, and states plainly what it did not.                        */
  window.openDecision = function openDecision(id) {
    const d = (DECISIONS || []).find((x) => x.id === id);
    if (!d) return;
    const i = d.insight || {};

    const sev = i.severity ?? d.severity ?? 0;
    const conf = Math.round(((i.confidence ?? d.confidence) || 0) * 100);
    const like = i.likelihood == null ? null : Math.round(i.likelihood * 100);

    const recs = (i.recommendations || []);
    const cites = (i.citations || {});
    const citeIds = [...(cites.items || []), ...(cites.chunks || [])];

    const row = (k, v) =>
      `<div style="display:flex;gap:12px;padding:7px 0;border-bottom:1px solid #1e2942">
         <span style="flex:0 0 150px;color:#7f8db0;font-size:12px">${esc(k)}</span>
         <span style="color:#dce6fb;font-size:13px">${v}</span></div>`;

    const recsHtml = recs.length
      ? recs.map((r, n) => `
          <div style="padding:10px 12px;margin-bottom:7px;border-radius:8px;background:#0e1526;
               border:1px solid #222e4a">
            <div style="color:#dce6fb;font-size:13px;font-weight:600">
              ${String.fromCharCode(97 + n)}. ${esc(r.action || r)}</div>
            ${r.rationale ? `<div style="color:#8494b5;font-size:12px;margin-top:4px">${esc(r.rationale)}</div>` : ''}
            ${r.owner ? `<div style="color:#6f7d9c;font-size:11.5px;margin-top:4px">Owner: ${esc(r.owner)}${r.horizon ? ` · ${esc(r.horizon)}` : ''}</div>` : ''}
          </div>`).join('')
      : `<div style="color:#7f8db0;font-size:12.5px">The agent proposed no actions for this insight.</div>`;

    openModal(`<div class="modal wide" onclick="event.stopPropagation()" role="dialog" aria-modal="true">
      <div class="modal-head">
        <div class="mh-ic bg-orange tone-orange">${typeof ic === 'function' ? ic('spark', 21) : ''}</div>
        <div>
          <h3>${esc(d.title)}</h3>
          <p>${esc(d.subjectType || 'insight')} · drafted by the ${esc(i.created_by_agent || 'agent')} agent · awaiting your decision</p>
        </div>
        <button class="modal-x" onclick="closeModal()" aria-label="Close">✕</button>
      </div>

      <div class="modal-body">
        <div style="padding:9px 12px;border-radius:8px;background:#3a2a08;border:1px solid #8a6a12;
             color:#ffc46b;font-size:12.5px;margin-bottom:16px">
          <b>NOT PUBLISHED.</b> Nothing here is visible to anyone else until you approve it.
          DANAH recommends; you decide, and your decision is written to the audit chain.
        </div>

        ${row('Severity', `${sev} / 5`)}
        ${row('Confidence', `${conf}%`)}
        ${row('Likelihood', like == null ? '<i style="color:#7f8db0">not estimable from the evidence</i>' : `${like}%`)}
        ${row('Domains', (i.domains || []).map(esc).join(', ') || '<i style="color:#7f8db0">none</i>')}
        ${row('Classification', esc(i.classification || 'OFFICIAL'))}

        <h4 style="margin:18px 0 8px;color:#dce6fb;font-size:13px">The analysis</h4>
        <div style="color:#e6edfb;font-size:14px;line-height:1.7;white-space:pre-wrap">${esc(i.body || d.summary || '')}</div>

        <h4 style="margin:18px 0 8px;color:#dce6fb;font-size:13px">Recommended actions</h4>
        ${recsHtml}

        <h4 style="margin:18px 0 8px;color:#dce6fb;font-size:13px">Evidence</h4>
        <div style="color:#8494b5;font-size:12.5px">
          ${citeIds.length
            ? `Grounded in ${citeIds.length} cited source${citeIds.length === 1 ? '' : 's'} from the corpus.`
            : '<b style="color:#ff8095">No citations.</b> An insight with no citations should not be published.'}
        </div>

        <div style="margin-top:18px;padding:11px 12px;border-radius:8px;background:#0e1526;
             border:1px dashed #2a3550;color:#7f8db0;font-size:11.5px;line-height:1.6">
          <b style="color:#9fb0d0">What this analysis does not include.</b>
          No financial modelling, no red-team dissent and no probability-weighted scenarios — this
          pipeline does not produce them, and the fields the prototype reserved for them are left
          empty rather than filled with plausible-looking numbers. Weigh this on the evidence above.
        </div>
      </div>

      <div class="modal-foot">
        <button class="btn btn-ghost" onclick="closeModal()">Close</button>
        <button class="btn btn-ghost" onclick="deferDecision('${esc(id)}')">Request changes</button>
        <button class="btn btn-primary" onclick="approveDecision('${esc(id)}')">Approve &amp; publish</button>
      </div>
    </div>`);
  };

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
        // The approval queue is executive-only server-side; a viewer/analyst hydrate must not
        // fire a request the server will (correctly) 403, or the console fills with noise.
        (isExecutive() ? call('/approvals?status=pending').catch(() => null) : Promise.resolve(null)),
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
      // The audit log is admin-only server-side; only an admin fetches it, so a lower role's
      // hydrate never fires a request the server refuses.
      const audit = backendRole() === 'admin' ? await call('/audit?limit=60').catch(() => null) : null;
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

      // Attach the full insight behind each approval. The decision card needs the body,
      // the recommendations and the citations — an approval row alone carries only a title
      // and a score, and a human cannot responsibly approve what they cannot read.
      const byId = new Map(rows.map((i) => [i.id, i]));
      const missing = appr
        .filter((a) => a.subject_type === 'insight' && !byId.has(a.subject_id))
        .slice(0, 20);
      const fetched = await Promise.all(
        missing.map((a) => call(`/insights/${a.subject_id}`).catch(() => null))
      );
      fetched.filter(Boolean).forEach((i) => byId.set(i.id, i));

      DECISIONS.length = 0;
      appr.forEach((a) => {
        const insight = byId.get(a.subject_id) || null;
        DECISIONS.push({
          id: a.id,
          title: a.subject_title || insight?.title || `${a.subject_type} awaiting decision`,
          summary: a.subject_summary || insight?.body || '',
          ministry: (insight?.domains && insight.domains[0]) || 'all',
          urgency: (a.subject_severity || insight?.severity || 0) >= 4 ? 'critical' : 'high',
          confidence: a.subject_confidence ?? insight?.confidence ?? 0,
          severity: a.subject_severity ?? insight?.severity ?? 0,
          score: Math.round(((a.subject_confidence ?? insight?.confidence) || 0) * 100),
          owner: a.assigned_role,
          timeToImpact: '—',
          subjectType: a.subject_type,
          insight,
        });
      });

      if (dash?.counts) {
        window.DANAH_DASH = dash;
        bindCircuit(dash);
      }
    } catch (e) {
      console.warn('[DANAH] hydrate failed', e);
    }

    // Notifications (the Alerts page) and the knowledge base run on their own fetches so a
    // slow document list never holds up the dashboard. Both are fire-and-forget.
    refreshAlerts();
    refreshDocuments();
  }

  function relativeTime(iso) {
    if (!iso) return '';
    const then = Date.parse(iso);
    if (isNaN(then)) return '';
    // Date.now() is fine in the browser; only the workflow sandbox forbids it.
    const s = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (s < 60) return 'just now';
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }

  /* =====================================================================
     5. ALERTS — the real notification feed, not the prototype's five demo rows
     =====================================================================
     The prototype's Alerts page and the sidebar's unread badge both read a hard-coded
     ALERTS array. The backend already produces real notifications — approval-pending,
     briefing-published, cost-alert, source-failure — addressed to a role or a person,
     and returned only to the recipient. So Alerts now shows exactly what the logged-in
     user is actually notified about, and "mark read" writes through to the server.

     Note for the demo: approval notifications are addressed to role=executive, so the
     admin's Alerts page may be near-empty while the executive's is full. That is correct,
     not a bug — admin is not the approver. Sign in as executive@ministry.gov to see them. */
  const NOTIF_MAP = {
    approval_pending: { type: 'policy', priority: 'high' },
    briefing_published: { type: 'success', priority: 'medium' },
    cost_alert: { type: 'risk', priority: 'critical' },
    source_failure: { type: 'risk', priority: 'high' },
  };

  async function refreshAlerts() {
    if (!state.live || !tok.access || typeof ALERTS === 'undefined') return;
    let rows;
    try {
      rows = await call('/notifications?limit=50');
    } catch (_) {
      return; // leave whatever is there; never blank the page on a transient error
    }
    const list = Array.isArray(rows) ? rows : (rows?.items || []);
    ALERTS.length = 0;
    list.forEach((n) => {
      const m = NOTIF_MAP[n.kind] || { type: 'policy', priority: 'medium' };
      ALERTS.push({
        id: n.id,
        type: m.type,
        priority: m.priority,
        ministry: '',
        title: n.title,
        action: n.body,
        ago: relativeTime(n.created_at),
        read: !!n.read_at,
      });
    });
    if (typeof renderSidebar === 'function') renderSidebar(); // updates the unread badge
    if (S && S.route === 'alerts' && typeof render === 'function') render();
  }

  // Read-through to the server. Optimistic locally so the UI is instant; the server call is
  // the source of truth and cannot mark another user's notification (the ownership check is
  // part of the UPDATE, not a lookup before it).
  window.readAlert = async function readAlert(id) {
    const a = (typeof ALERTS !== 'undefined' ? ALERTS : []).find((x) => x.id === id);
    if (a) a.read = true;
    if (typeof renderSidebar === 'function') renderSidebar();
    if (typeof render === 'function') render();
    if (state.live) { try { await call('/notifications/read', { method: 'POST', body: { ids: [id] } }); } catch (_) {} }
  };
  window.markAllRead = async function markAllRead() {
    (typeof ALERTS !== 'undefined' ? ALERTS : []).forEach((a) => (a.read = true));
    if (typeof toast === 'function') toast('All notifications marked read');
    if (typeof renderSidebar === 'function') renderSidebar();
    if (typeof render === 'function') render();
    if (state.live) { try { await call('/notifications/read', { method: 'POST', body: { ids: [] } }); } catch (_) {} }
  };

  /* =====================================================================
     6. KNOWLEDGE BASE — a real document-upload control and a real indexed list
     =====================================================================
     The prototype's "Verified Knowledge Monitor" is entirely synthetic, and there was no
     way to add knowledge through the UI at all — documents could only arrive via the seed
     or the API. Yet the whole point of grounded chat is that answers come from the
     ministry's own documents. This replaces that page, when live, with a working knowledge
     base: upload a file, watch it index, and see it become citable.

     Upload is classification-gated on the server (a user cannot classify above their own
     clearance). The dropdown is capped to match, so the control never offers what the server
     will reject. */
  const CLASS_ORDER = ['PUBLIC', 'INTERNAL', 'OFFICIAL', 'OFFICIAL_SENSITIVE'];
  const docsState = { items: [], counts: {}, uploading: false, msg: '' };

  function allowedClassifications() {
    const ceiling = (state.user && state.user.clearance) || 'OFFICIAL';
    const cap = CLASS_ORDER.indexOf(ceiling);
    return CLASS_ORDER.slice(0, cap < 0 ? CLASS_ORDER.length : cap + 1);
  }

  async function refreshDocuments() {
    if (!state.live || !tok.access) return;
    try {
      const [docs, counts] = await Promise.all([
        call('/knowledge/documents?limit=100').catch(() => null),
        call('/knowledge/documents/count').catch(() => null),
      ]);
      docsState.items = Array.isArray(docs) ? docs : (docs?.items || []);
      docsState.counts = counts || {};
    } catch (_) { /* keep the last good list */ }
    if (S && S.route === 'knowledge' && typeof render === 'function') render();
  }

  window.danahRefreshDocs = refreshDocuments;

  window.danahUpload = async function danahUpload() {
    const fileEl = document.getElementById('kbFile');
    const titleEl = document.getElementById('kbTitle');
    const classEl = document.getElementById('kbClass');
    const btn = document.getElementById('kbUploadBtn');
    if (!fileEl || !fileEl.files || !fileEl.files.length) {
      docsState.msg = 'Choose a file first.';
      if (typeof render === 'function') render();
      return;
    }
    const file = fileEl.files[0];
    const fd = new FormData();
    fd.append('file', file);
    if (titleEl && titleEl.value.trim()) fd.append('title', titleEl.value.trim());
    fd.append('classification', (classEl && classEl.value) || 'OFFICIAL');

    docsState.uploading = true;
    docsState.msg = `Uploading “${file.name}”…`;
    if (btn) { btn.disabled = true; }
    if (typeof render === 'function') render();

    try {
      const resp = await fetch(`${API}/api/knowledge/documents`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${tok.access}` }, // no Content-Type — browser sets the multipart boundary
        body: fd,
      });
      if (!resp.ok) {
        // Surface the server's own reason. A 403 here can be the role gate (viewers cannot
        // upload at all) or the classification gate (analyst picking above their ceiling) —
        // the message from the server tells the truth about which; do not guess.
        let detail = `HTTP ${resp.status}`;
        let code = '';
        try { const e = await resp.json(); detail = e?.error?.message || e?.detail || detail; code = e?.error?.code || ''; } catch (_) {}
        docsState.msg = code === 'permission_denied'
          ? 'Your role does not permit uploading documents (analyst clearance or above is required).'
          : `Upload failed: ${detail}`;
        docsState.uploading = false;
        if (typeof render === 'function') render();
        return;
      }
      const doc = await resp.json();
      docsState.uploading = false;
      docsState.msg = `“${doc.title}” uploaded — indexing now (real embeddings)…`;
      if (fileEl) fileEl.value = '';
      if (titleEl) titleEl.value = '';
      await refreshDocuments();
      pollDocuments(doc.id, 12);
    } catch (e) {
      docsState.uploading = false;
      docsState.msg = `Upload failed: ${e.message}. Is the backend running?`;
      if (typeof render === 'function') render();
    }
  };

  // Poll until the just-uploaded document reaches a terminal state, so the operator sees
  // pending → indexed happen. Needs the worker running; if it never indexes, the row simply
  // stays "pending" and the note explains why.
  function pollDocuments(id, tries) {
    if (tries <= 0) return;
    setTimeout(async () => {
      await refreshDocuments();
      const d = docsState.items.find((x) => x.id === id);
      if (d && (d.status === 'indexed' || d.status === 'failed')) {
        docsState.msg = d.status === 'indexed'
          ? `“${d.title}” is indexed (${d.chunk_count} chunk${d.chunk_count === 1 ? '' : 's'}) — you can now ask the Live Agent about it.`
          : `“${d.title}” failed to index: ${d.error || 'unknown error'}.`;
        if (typeof render === 'function') render();
        return;
      }
      pollDocuments(id, tries - 1);
    }, 3000);
  }

  function docStatusPill(s, chunks) {
    const map = {
      pending: ['orange', 'Pending'],
      processing: ['blue', 'Indexing…'],
      indexed: ['green', `Indexed · ${chunks} chunk${chunks === 1 ? '' : 's'}`],
      failed: ['red', 'Failed'],
    };
    const x = map[s] || ['navy', s];
    return `<span class="pill bg-${x[0]} tone-${x[0]}"><span class="sdot" style="background:var(--${x[0]})"></span>${x[1]}</span>`;
  }

  function realKnowledgePage() {
    const opts = allowedClassifications()
      .map((c) => `<option value="${c}" ${c === 'OFFICIAL' || (c === 'INTERNAL' && !allowedClassifications().includes('OFFICIAL')) ? 'selected' : ''}>${c.replace('_', '-')}</option>`)
      .join('');
    const c = docsState.counts || {};
    const total = docsState.items.length;
    const indexed = c.indexed || 0;

    const rows = docsState.items.length
      ? docsState.items.map((d) => `
          <tr style="cursor:default">
            <td><div style="font-weight:600;font-size:13px">${esc(d.title)}</div>
              <div class="muted" style="font-size:11px">${esc(d.filename || '')}</div></td>
            <td><span class="cls-tag">${esc((d.classification || '').replace('_', '-'))}</span></td>
            <td>${docStatusPill(d.status, d.chunk_count)}</td>
            <td style="font-size:12px;color:var(--ink-2)">${esc(String(d.language || '').toUpperCase())}</td>
            <td style="font-size:12px;color:var(--ink-3)">${esc(relativeTime(d.created_at))}</td>
          </tr>`).join('')
      : `<tr><td colspan="5" class="muted" style="text-align:center;padding:26px">No documents yet. Upload one above — it becomes searchable and citable once indexed.</td></tr>`;

    const msg = docsState.msg
      ? `<div class="callout ${/fail|cannot|requires/i.test(docsState.msg) ? '' : 'amber'}" style="margin-top:12px">${docsState.uploading ? '<span class="spin" style="display:inline-block;width:13px;height:13px;margin-right:8px;border:2px solid #2b3a5c;border-top-color:#5b8cff;border-radius:50%;animation:danahspin .8s linear infinite;vertical-align:-2px"></span>' : ''}${esc(docsState.msg)}</div><style>@keyframes danahspin{to{transform:rotate(360deg)}}</style>`
      : '';

    // Only analyst-and-above may upload — the server enforces it (require_analyst on the route),
    // so a viewer offered an upload form would get a 403 they could not understand. Show the form
    // only to roles that can actually use it; a viewer gets the read-only library and a plain note.
    const role = (state.user && state.user.role) || '';
    const canUpload = role === 'admin' || role === 'executive' || role === 'analyst';
    const uploadSection = canUpload
      ? `<div class="card card-pad section">
      <h3 style="font-family:var(--display);font-size:15px;font-weight:600;margin-bottom:4px">Upload a document</h3>
      <p class="muted" style="font-size:12px;margin-bottom:16px">Text, Markdown or PDF. It is chunked and embedded with the real provider, then becomes citable in chat. You can only classify at or below your own clearance (${esc(((state.user && state.user.clearance) || 'OFFICIAL').replace('_', '-'))}).</p>
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end">
        <div style="flex:2;min-width:220px"><label style="display:block;font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--ink-3);margin-bottom:6px">File</label>
          <input id="kbFile" type="file" accept=".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf" style="width:100%;font-size:13px;padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:var(--surface-2);color:var(--ink)"></div>
        <div style="flex:2;min-width:180px"><label style="display:block;font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--ink-3);margin-bottom:6px">Title (optional)</label>
          <input id="kbTitle" class="inp" placeholder="Defaults to the filename"></div>
        <div style="flex:1;min-width:150px"><label style="display:block;font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:var(--ink-3);margin-bottom:6px">Classification</label>
          <div class="select" style="width:100%"><select id="kbClass">${opts}</select></div></div>
        <button id="kbUploadBtn" class="btn btn-primary" onclick="danahUpload()" ${docsState.uploading ? 'disabled' : ''}>Upload &amp; index</button>
      </div>
      ${msg}
    </div>`
      : `<div class="callout section" style="border-color:var(--line);background:var(--surface-2)">${typeof ic === 'function' ? ic('lock', 13) : ''} &nbsp;Uploading documents requires <b>analyst</b> clearance or above. Your role (<b>${esc(role || 'viewer')}</b>) has read-only access to the knowledge base — you can see the documents below that your clearance permits.</div>`;

    return `
    <div class="page-head">
      <div class="page-title"><h1>Knowledge Base</h1><p>Upload documents into the ministry's grounded knowledge base. Once indexed, the Live Agent can cite them.</p></div>
      <div class="page-controls">
        <span class="pill bg-green tone-green">${indexed}/${total} indexed</span>
        <button class="btn btn-ghost" onclick="danahRefreshDocs()">Refresh</button>
      </div>
    </div>

    ${uploadSection}

    <div class="card section">
      <div style="padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center">
        <h3 style="font-family:var(--display);font-size:15px;font-weight:600">Indexed documents</h3>
        <span class="muted" style="font-size:12px">${(c.pending || 0)} pending · ${(c.processing || 0)} indexing · ${indexed} indexed · ${(c.failed || 0)} failed</span>
      </div>
      <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Document</th><th>Classification</th><th>Status</th><th>Lang</th><th>Uploaded</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </div>

    <div class="callout" style="border-color:var(--green-line);background:var(--green-bg)">${typeof ic === 'function' ? ic('shield', 13, 'tone-green') : ''} &nbsp;Only documents at or below your clearance are listed — the classification filter is a database rule, not a UI one. What you upload here is what grounds the assistant's answers.</div>`;
  }

  // Route the Knowledge page through the real one when live; keep the prototype's synthetic
  // monitor for the offline demo. PAGES holds the function reference, so this must mutate the
  // map (reassigning window.pageKnowledge would not change what render() dispatches to).
  if (typeof PAGES !== 'undefined' && PAGES.knowledge) {
    const _protoKnowledgePage = PAGES.knowledge;
    PAGES.knowledge = function () {
      return state.live ? realKnowledgePage() : _protoKnowledgePage();
    };
  }

  /* =====================================================================
     EXTENDED BACKEND COVERAGE
     Real, already-built backend routes the prototype never had a screen for.
     Each is gated to match the server so a user is never shown a page the API
     would 403, and nothing is invented — an empty backend yields an empty page.
       · Approvals        GET /approvals · POST /approvals/{id}/decision   (executive+)
       · Briefings        GET /briefings · GET /briefings/{id} · POST /generate (EN+AR)
       · Strategic Memory GET /memory                                       (analyst+)
       · Intelligence Feed GET /items                                       (any role)
     ===================================================================== */
  function backendRole() { return (state.user && state.user.role) || ''; }
  function isExecutive() { const r = backendRole(); return r === 'admin' || r === 'executive'; }
  function isAnalyst() { const r = backendRole(); return r === 'admin' || r === 'executive' || r === 'analyst'; }

  /* ---- add the new destinations to the sidebar (deduped) ---- */
  if (typeof NAV !== 'undefined' && Array.isArray(NAV)) {
    const addBefore = (beforeId, entry) => {
      if (NAV.some((n) => n.id === entry.id)) return;
      const i = NAV.findIndex((n) => n.id === beforeId);
      if (i >= 0) NAV.splice(i, 0, entry); else NAV.push(entry);
    };
    addBefore('agents', { id: 'feed', label: 'Intelligence Feed', icon: 'activity' });
    addBefore('governance', { id: 'approvals', label: 'Approvals', icon: 'checks' });
    addBefore('governance', { id: 'memory', label: 'Strategic Memory', icon: 'memory' });
  }

  /* ---- hide routes a role/mode cannot use, so the menu never lies ---- */
  const _protoNavLocked = window.navLocked;
  window.navLocked = function (route) {
    if (route === 'approvals' || route === 'feed' || route === 'memory') {
      if (!state.live) return true;                 // no real data offline; prototype has no such page
      if (route === 'approvals') return !isExecutive();
      if (route === 'memory') return !isAnalyst();
      return false;                                 // feed: any authenticated role
    }
    return typeof _protoNavLocked === 'function' ? _protoNavLocked(route) : false;
  };

  /* ======================= APPROVALS ======================= */
  const apprState = { items: [], filter: 'pending', loading: false, msg: '' };

  async function refreshApprovals() {
    if (!state.live || !isExecutive()) return;
    apprState.loading = true;
    if (S && S.route === 'approvals' && typeof render === 'function') render();
    try {
      const rows = await call('/approvals?status=' + apprState.filter + '&limit=100').catch(() => null);
      apprState.items = Array.isArray(rows) ? rows : [];
    } catch (_) { /* keep last good */ }
    apprState.loading = false;
    if (S && S.route === 'approvals' && typeof render === 'function') render();
  }
  window.danahRefreshApprovals = refreshApprovals;
  window.danahApprFilter = function (f) { apprState.filter = f; apprState.msg = ''; refreshApprovals(); };

  window.danahDecide = async function (id, decision) {
    apprState.msg = decision === 'approved' ? 'Publishing…' : decision === 'rejected' ? 'Rejecting…' : 'Sending back…';
    if (typeof render === 'function') render();
    try {
      await call('/approvals/' + id + '/decision', { method: 'POST', body: { decision, comment: '' } });
      apprState.msg = decision === 'approved'
        ? 'Approved & published — written to the tamper-evident audit log.'
        : decision === 'rejected'
          ? 'Rejected — written to the audit log.'
          : 'Sent back for changes — written to the audit log.';
      await refreshApprovals();
      if (typeof hydrate === 'function') await hydrate();   // publication changes insights + dashboard
      if (typeof render === 'function') render();
    } catch (e) {
      apprState.msg = e.status === 409
        ? 'Already decided — a second decision is refused; the first is part of the record.'
        : e.status === 403 ? 'Only an executive or admin can decide approvals.'
          : 'Decision failed: ' + e.message;
      if (typeof render === 'function') render();
    }
  };

  function sevPill(sev) {
    if (sev == null) return '';
    const map = { 5: ['red', 'Critical'], 4: ['orange', 'High'], 3: ['blue', 'Medium'], 2: ['green', 'Low'], 1: ['green', 'Low'] };
    const x = map[sev] || ['navy', 'Sev ' + sev];
    return `<span class="pill bg-${x[0]} tone-${x[0]}">${x[1]}</span>`;
  }

  function realApprovalsPage() {
    if (!isExecutive()) {
      return `
        <div class="page-head"><div class="page-title"><h1>Approvals</h1><p>The human-in-the-loop publication gate.</p></div></div>
        <div class="callout section" style="border-color:var(--line);background:var(--surface-2)">${ic('lock', 13)} &nbsp;Only an <b>executive</b> or <b>admin</b> can act on the approval queue — nothing DANAH produces is published without a named human here. Your role (<b>${esc(backendRole() || 'viewer')}</b>) can read published intelligence but cannot approve. Sign in as <b>executive@ministry.gov</b> to use this queue.</div>`;
    }
    const tabs = ['pending', 'approved', 'rejected', 'changes_requested'].map((f) =>
      `<button class="btn ${apprState.filter === f ? 'btn-primary' : 'btn-ghost'} btn-sm" onclick="danahApprFilter('${f}')">${f.replace('_', ' ')}</button>`).join(' ');

    const rows = apprState.items.length
      ? apprState.items.map((a) => `
        <div class="card card-pad" style="margin-bottom:12px">
          <div style="display:flex;gap:14px;align-items:flex-start">
            <div class="lic bg-orange tone-orange" style="width:38px;height:38px;flex:none">${ic(a.subject_type === 'briefing' ? 'doc' : 'zap', 18)}</div>
            <div style="flex:1;min-width:0">
              <div style="font-size:14px;font-weight:600">${esc(a.subject_title || '(untitled ' + a.subject_type + ')')}</div>
              <div class="muted" style="font-size:12.5px;margin-top:4px;line-height:1.5">${esc(a.subject_summary || '')}</div>
              <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:9px">
                <span class="tag">${esc(a.subject_type)}</span>
                ${a.subject_confidence != null ? `<span class="tag">${Math.round(a.subject_confidence * 100)}% confidence</span>` : ''}
                ${sevPill(a.subject_severity)}
                <span class="tag">${ic('bot', 11)} ${esc(a.requested_by_agent)}</span>
                <span class="tag">${esc(relativeTime(a.created_at))}</span>
              </div>
            </div>
          </div>
          ${apprState.filter === 'pending'
        ? `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;padding-top:14px;border-top:1px solid var(--line-2)">
                 <button class="btn btn-primary btn-sm" onclick="danahDecide('${a.id}','approved')">${ic('check', 14)} Approve &amp; publish</button>
                 <button class="btn btn-ghost btn-sm" onclick="danahDecide('${a.id}','changes_requested')">Request changes</button>
                 <button class="btn btn-ghost btn-sm" onclick="danahDecide('${a.id}','rejected')">Reject</button>
               </div>`
        : `<div class="muted" style="font-size:12px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line-2)">${esc(a.status)}${a.decided_at ? ' · ' + esc(relativeTime(a.decided_at)) : ''}${a.comment ? ' · “' + esc(a.comment) + '”' : ''}</div>`}
        </div>`).join('')
      : `<div class="empty">${ic('check', 42)}<h4>Nothing ${apprState.filter === 'pending' ? 'awaiting approval' : 'in this view'}</h4><p>${apprState.filter === 'pending' ? 'The queue is clear. Run the pipeline to generate insights that need a decision.' : 'No items with this status.'}</p></div>`;

    const msg = apprState.msg
      ? `<div class="callout ${/fail|already|only/i.test(apprState.msg) ? '' : 'amber'}" style="margin-bottom:14px">${esc(apprState.msg)}</div>` : '';

    return `
      <div class="page-head">
        <div class="page-title"><h1>Approvals</h1><p>Nothing DANAH produces is published until a named human decides here. Every decision is written to the hash-chained audit log.</p></div>
        <div class="page-controls"><button class="btn btn-ghost" onclick="danahRefreshApprovals()">Refresh</button></div>
      </div>
      <div style="display:flex;gap:7px;flex-wrap:wrap;margin-bottom:16px">${tabs}</div>
      ${msg}
      ${apprState.loading ? '<div class="callout amber">Loading the queue…</div>' : rows}`;
  }

  /* ======================= BRIEFINGS (Reports) ======================= */
  const briefState = { items: [], loading: false, msg: '' };

  async function refreshBriefings() {
    if (!state.live) return;
    briefState.loading = true;
    if (S && S.route === 'reports' && typeof render === 'function') render();
    try {
      const rows = await call('/briefings?limit=30').catch(() => null);
      briefState.items = Array.isArray(rows) ? rows : [];
    } catch (_) { /* keep last good */ }
    briefState.loading = false;
    if (S && S.route === 'reports' && typeof render === 'function') render();
  }
  window.danahRefreshBriefings = refreshBriefings;

  window.danahOpenBriefing = async function (id) {
    if (typeof openModal !== 'function') return;
    openModal(`<div class="modal wide" onclick="event.stopPropagation()"><div class="modal-body" style="padding:44px;text-align:center;color:var(--ink-3)">Loading briefing…</div></div>`);
    try {
      const b = await call('/briefings/' + id);
      const secs = (b.sections || []).map((s) => `
        <div style="margin:16px 0;padding-top:16px;border-top:1px solid var(--line-2)">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:22px">
            <div><h4 class="dc-h">${esc(s.heading_en)}</h4><p class="story-b">${esc(s.body_en)}</p></div>
            <div dir="rtl"><h4 class="dc-h">${esc(s.heading_ar)}</h4><p class="story-b">${esc(s.body_ar)}</p></div>
          </div>
        </div>`).join('');
      const cites = (b.citations || []).length
        ? `<h4 class="dc-h">Sources</h4><div class="dc-tags">${b.citations.map((c) => `<span class="tag">${ic('shield', 11)} ${esc(c.title || ('source ' + c.n))}</span>`).join('')}</div>` : '';
      openModal(`<div class="modal wide" onclick="event.stopPropagation()">
        <div class="modal-head"><div class="mh-ic bg-blue tone-blue">${ic('doc', 21)}</div>
          <div><h3>${esc(b.title)}</h3><p>Bilingual briefing · ${esc((b.classification || '').replace('_', '-'))} · ${esc(b.status)}${b.approval_status ? ' · approval: ' + esc(b.approval_status) : ''}</p></div>
          <button class="modal-x" onclick="closeModal()">${ic('x', 18)}</button></div>
        <div class="modal-body">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:22px">
            <div><h4 class="dc-h">English</h4><p class="story-b">${esc(b.body_en)}</p></div>
            <div dir="rtl"><h4 class="dc-h">العربية</h4><p class="story-b">${esc(b.body_ar)}</p></div>
          </div>
          ${secs}
          ${cites}
          <div class="callout" style="margin-top:16px;border-color:var(--green-line);background:var(--green-bg)">${ic('shield', 12, 'tone-green')} &nbsp;English and Arabic are produced together by the Briefing Agent — a briefing whose Arabic pass fails is never published as English-only.</div>
        </div>
        <div class="modal-foot"><button class="btn btn-ghost" onclick="closeModal()">Close</button></div></div>`);
    } catch (e) {
      openModal(`<div class="modal" onclick="event.stopPropagation()"><div class="modal-head"><h3>Briefing</h3><button class="modal-x" onclick="closeModal()">${ic('x', 18)}</button></div><div class="modal-body" style="color:var(--red)">Could not load the briefing: ${esc(e.message)}</div><div class="modal-foot"><button class="btn btn-ghost" onclick="closeModal()">Close</button></div></div>`);
    }
  };

  window.danahGenerateBriefing = async function () {
    if (!isExecutive()) { if (typeof toast === 'function') toast('Only an executive can generate a briefing.'); return; }
    briefState.msg = 'Generating a briefing from the current insights — this calls the model and can take a minute…';
    if (typeof render === 'function') render();
    try {
      const b = await call('/briefings/generate', { method: 'POST', body: { force: false }, timeout: 180000 });
      briefState.msg = `Draft briefing “${b.title}” created — it is now in the Approvals queue for publishing.`;
      await refreshBriefings();
      if (typeof render === 'function') render();
    } catch (e) {
      briefState.msg = e.status === 403 ? 'Only an executive can generate a briefing.' : 'Generation failed: ' + e.message;
      if (typeof render === 'function') render();
    }
  };

  function realReportsPage() {
    const rows = briefState.items.length
      ? briefState.items.map((b) => `
        <div class="card card-pad hover" style="margin-bottom:12px;cursor:pointer" onclick="danahOpenBriefing('${b.id}')">
          <div style="display:flex;gap:14px;align-items:center">
            <div class="lic bg-blue tone-blue" style="width:44px;height:44px;flex:none">${ic('doc', 21)}</div>
            <div style="flex:1;min-width:0">
              <div style="font-size:14px;font-weight:600">${esc(b.title)}</div>
              <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">
                <span class="tag">${esc(b.date)}</span>
                <span class="cls-tag">${esc((b.classification || '').replace('_', '-'))}</span>
                <span class="pill bg-${b.status === 'published' ? 'green' : 'orange'} tone-${b.status === 'published' ? 'green' : 'orange'}">${esc(b.status)}</span>
                ${b.confidence != null ? `<span class="tag">${Math.round(b.confidence * 100)}% confidence</span>` : ''}
              </div>
            </div>
            <span class="link">Open EN / AR ${ic('arrow', 14)}</span>
          </div>
        </div>`).join('')
      : `<div class="empty">${ic('doc', 42)}<h4>No briefings yet</h4><p>${isExecutive() ? 'Generate one from the current insights, or run the pipeline first.' : 'Briefings appear here once an executive generates and publishes them.'}</p></div>`;

    const msg = briefState.msg ? `<div class="callout ${/fail|only/i.test(briefState.msg) ? '' : 'amber'}" style="margin-bottom:14px">${esc(briefState.msg)}</div>` : '';

    return `
      <div class="page-head">
        <div class="page-title"><h1>Reports &amp; Briefings</h1><p>Executive briefings composed by the Briefing Agent — always bilingual (English and Arabic). Open one to read both side by side.</p></div>
        <div class="page-controls">
          <button class="btn btn-ghost" onclick="danahRefreshBriefings()">Refresh</button>
          ${isExecutive() ? `<button class="btn btn-primary" onclick="danahGenerateBriefing()">${ic('spark', 16)} Generate briefing</button>` : ''}
        </div>
      </div>
      ${msg}
      ${briefState.loading ? '<div class="callout amber">Loading briefings…</div>' : `<div class="section">${rows}</div>`}`;
  }

  /* ======================= STRATEGIC MEMORY ======================= */
  const memState = { items: [], loading: false };

  async function refreshMemory() {
    if (!state.live || !isAnalyst()) return;
    memState.loading = true;
    if (S && S.route === 'memory' && typeof render === 'function') render();
    try { const rows = await call('/memory?limit=100').catch(() => null); memState.items = Array.isArray(rows) ? rows : []; } catch (_) { /* keep */ }
    memState.loading = false;
    if (S && S.route === 'memory' && typeof render === 'function') render();
  }
  window.danahRefreshMemory = refreshMemory;

  function realMemoryPage() {
    if (!isAnalyst()) {
      return `<div class="page-head"><div class="page-title"><h1>Strategic Memory</h1></div></div>
        <div class="callout section" style="border-color:var(--line);background:var(--surface-2)">${ic('lock', 13)} &nbsp;Institutional memory is available to <b>analyst</b> clearance and above. Your role (<b>${esc(backendRole() || 'viewer')}</b>) does not have access to it.</div>`;
    }
    const rows = memState.items.length
      ? memState.items.map((e) => `
        <div class="lrow" style="cursor:default">
          <div class="lic bg-blue tone-blue">${ic('memory', 18)}</div>
          <div class="lbody">
            <div class="ltitle">${esc(e.title)}</div>
            <div class="lmeta">
              <span class="tag">${esc(e.kind)}</span>
              <span class="cls-tag">${esc((e.classification || '').replace('_', '-'))}</span>
              ${(e.tags || []).slice(0, 4).map((t) => `<span class="tag">${esc(t)}</span>`).join('')}
              <span class="tag">${esc(relativeTime(e.created_at))}</span>
            </div>
            <div class="ldesc">${esc(e.content)}</div>
          </div>
        </div>`).join('')
      : `<div class="empty">${ic('memory', 42)}<h4>No memory entries yet</h4><p>The Memory Agent records decisions and lessons here after pipeline runs.</p></div>`;
    return `
      <div class="page-head"><div class="page-title"><h1>Strategic Memory</h1><p>Decisions, lessons and standing context the agents recall so the ministry never re-proposes what it already tried. Clearance-filtered in SQL.</p></div>
        <div class="page-controls"><button class="btn btn-ghost" onclick="danahRefreshMemory()">Refresh</button></div></div>
      <div class="card section">${memState.loading ? '<div class="callout amber" style="margin:14px">Loading…</div>' : rows}</div>`;
  }

  /* ======================= INTELLIGENCE FEED (items) ======================= */
  const feedState = { items: [], total: 0, loading: false };

  async function refreshFeed() {
    if (!state.live) return;
    feedState.loading = true;
    if (S && S.route === 'feed' && typeof render === 'function') render();
    try { const p = await call('/items?limit=60').catch(() => null); feedState.items = (p && p.items) || []; feedState.total = (p && p.total) || 0; } catch (_) { /* keep */ }
    feedState.loading = false;
    if (S && S.route === 'feed' && typeof render === 'function') render();
  }
  window.danahRefreshFeed = refreshFeed;

  function urgPill(u) {
    if (!u) return '';
    const map = { critical: 'red', high: 'orange', medium: 'blue', low: 'green' };
    return `<span class="pill bg-${map[u] || 'navy'} tone-${map[u] || 'navy'}">${esc(u)}</span>`;
  }

  function realFeedPage() {
    const rows = feedState.items.length
      ? feedState.items.map((it) => `
        <div class="lrow" style="cursor:${it.url ? 'pointer' : 'default'}" ${it.url ? `onclick="window.open('${esc(it.url)}','_blank','noopener')"` : ''}>
          <div class="lic" style="background:var(--navy);color:#fff">${ic('activity', 18)}</div>
          <div class="lbody">
            <div class="ltitle">${esc(it.title)}</div>
            <div class="lmeta">
              <span class="tag">${ic('shield', 11)} ${esc(it.source_name)}</span>
              ${it.category ? `<span class="tag">${esc(it.category)}</span>` : ''}
              ${urgPill(it.urgency)}
              ${it.relevance != null ? `<span class="tag">${Math.round(it.relevance * 100)}% relevant</span>` : ''}
              <span class="cls-tag">${esc((it.classification || '').replace('_', '-'))}</span>
              <span class="tag">${esc(relativeTime(it.published_at || it.created_at))}</span>
            </div>
            ${it.summary ? `<div class="ldesc">${esc(it.summary)}</div>` : ''}
          </div>
        </div>`).join('')
      : `<div class="empty">${ic('activity', 42)}<h4>No items yet</h4><p>Ingested source items appear here after a source sync or a pipeline run, each triaged by the Signal Agent.</p></div>`;
    return `
      <div class="page-head"><div class="page-title"><h1>Intelligence Feed</h1><p>Raw items ingested from the ministry's sources, triaged by the Signal Agent. Clearance-filtered in SQL — you only see what your role may read.</p></div>
        <div class="page-controls"><span class="pill" style="background:var(--navy);color:#fff">${feedState.total} items</span><button class="btn btn-ghost" onclick="danahRefreshFeed()">Refresh</button></div></div>
      <div class="card section">${feedState.loading ? '<div class="callout amber" style="margin:14px">Loading…</div>' : rows}</div>`;
  }

  /* ---- register the pages: real when live, prototype (or home) when offline ---- */
  if (typeof PAGES !== 'undefined') {
    PAGES.approvals = function () { return state.live ? realApprovalsPage() : (typeof pageHome === 'function' ? pageHome() : ''); };
    PAGES.feed = function () { return state.live ? realFeedPage() : (typeof pageHome === 'function' ? pageHome() : ''); };
    const _protoMemory = PAGES.memory;
    PAGES.memory = function () { return state.live ? realMemoryPage() : (typeof _protoMemory === 'function' ? _protoMemory() : ''); };
    const _protoReports = PAGES.reports;
    PAGES.reports = function () { return state.live ? realReportsPage() : (typeof _protoReports === 'function' ? _protoReports() : ''); };
  }

  /* ---- one dispatcher so login and navigation populate live pages the same way ---- */
  function refreshForRoute(route) {
    if (!state.live) return;
    if (route === 'alerts') refreshAlerts();
    else if (route === 'knowledge') refreshDocuments();
    else if (route === 'approvals') refreshApprovals();
    else if (route === 'reports') refreshBriefings();
    else if (route === 'memory') refreshMemory();
    else if (route === 'feed') refreshFeed();
  }

  // Refresh the live data when the user actually navigates to these pages, not only at login.
  const _protoGo = window.go;
  if (typeof _protoGo === 'function') {
    window.go = function (route) {
      const out = _protoGo.apply(this, arguments);
      refreshForRoute(route);
      return out;
    };
  }

  /* =====================================================================
     EXTENDED BACKEND COVERAGE — part 2
       · User Management  GET/POST/PATCH /api/admin/users   (admin)
       · Sources          GET /api/sources · POST /{id}/sync · PATCH /{id}  (view any; sync analyst+; toggle admin)
       · Conversations    GET /api/agent/chat/sessions · /{id}   (own history)
       · First-login guided tour of every page the role can see
     ===================================================================== */
  function isAdmin() { return backendRole() === 'admin'; }

  if (typeof NAV !== 'undefined' && Array.isArray(NAV)) {
    const addBefore2 = (beforeId, entry) => {
      if (NAV.some((n) => n.id === entry.id)) return;
      const i = NAV.findIndex((n) => n.id === beforeId);
      if (i >= 0) NAV.splice(i, 0, entry); else NAV.push(entry);
    };
    addBefore2('agents', { id: 'chats', label: 'Conversations', icon: 'spark' });
    addBefore2('reports', { id: 'sources', label: 'Sources', icon: 'link' });
    addBefore2('governance', { id: 'users', label: 'User Management', icon: 'user' });
  }

  const _navLockedPrev = window.navLocked;
  window.navLocked = function (route) {
    if (route === 'users') return !state.live || !isAdmin();
    if (route === 'sources' || route === 'chats') return !state.live;
    return typeof _navLockedPrev === 'function' ? _navLockedPrev(route) : false;
  };

  const _goPrev2 = window.go;
  if (typeof _goPrev2 === 'function') {
    window.go = function (route) {
      const out = _goPrev2.apply(this, arguments);
      if (state.live) {
        if (route === 'users') refreshUsers();
        else if (route === 'sources') refreshSources();
        else if (route === 'chats') refreshChats();
      }
      return out;
    };
  }

  /* ======================= USER MANAGEMENT (admin) ======================= */
  const usersState = { items: [], loaded: false, loading: false, msg: '' };
  async function refreshUsers() {
    if (!state.live || !isAdmin()) return;
    usersState.loading = true;
    if (S && S.route === 'users' && typeof render === 'function') render();
    try { const rows = await call('/admin/users?limit=200').catch(() => null); usersState.items = Array.isArray(rows) ? rows : []; } catch (_) { /* keep */ }
    usersState.loading = false; usersState.loaded = true;
    if (S && S.route === 'users' && typeof render === 'function') render();
  }
  window.danahRefreshUsers = refreshUsers;

  async function danahUserPatch(id, patch) {
    usersState.msg = 'Updating…'; if (typeof render === 'function') render();
    try { await call('/admin/users/' + id, { method: 'PATCH', body: patch }); usersState.msg = 'User updated — recorded in the audit log.'; await refreshUsers(); }
    catch (e) { usersState.msg = e.status === 403 ? (e.message || 'Not permitted.') : 'Update failed: ' + e.message; if (typeof render === 'function') render(); }
  }
  window.danahSetUserRole = function (id, role) { danahUserPatch(id, { role }); };
  window.danahToggleUser = function (id, active) { danahUserPatch(id, { is_active: active }); };

  window.danahNewUser = function () {
    if (typeof openModal !== 'function') return;
    openModal(`<div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head"><div class="mh-ic bg-blue tone-blue">${ic('user', 21)}</div><div><h3>Create user</h3><p>Clearance follows the role and is enforced server-side.</p></div><button class="modal-x" onclick="closeModal()">${ic('x', 18)}</button></div>
      <div class="modal-body">
        <div class="field"><label>Full name</label><input id="nuName" class="inp" placeholder="Jane Analyst"></div>
        <div class="field"><label>Email</label><input id="nuEmail" class="inp" type="email" placeholder="jane@ministry.gov"></div>
        <div class="field"><label>Temporary password</label><input id="nuPass" class="inp" type="password" placeholder="At least 12 characters"></div>
        <div class="field"><label>Role</label><div class="select" style="width:100%"><select id="nuRole">
          <option value="viewer">viewer · INTERNAL</option><option value="analyst">analyst · OFFICIAL</option><option value="executive">executive · OFFICIAL-SENSITIVE</option><option value="admin">admin · OFFICIAL-SENSITIVE</option></select></div></div>
        <div id="nuErr" style="display:none;color:var(--red);font-size:12.5px;margin-top:4px"></div>
      </div>
      <div class="modal-foot"><button class="btn btn-ghost" onclick="closeModal()">Cancel</button><button class="btn btn-primary" onclick="danahCreateUser()">Create user</button></div></div>`);
  };
  window.danahCreateUser = async function () {
    const name = (document.getElementById('nuName') || {}).value || '';
    const email = (document.getElementById('nuEmail') || {}).value || '';
    const password = (document.getElementById('nuPass') || {}).value || '';
    const role = (document.getElementById('nuRole') || {}).value || 'viewer';
    const errEl = document.getElementById('nuErr');
    try {
      await call('/admin/users', { method: 'POST', body: { full_name: name.trim(), email: email.trim(), password, role } });
      if (typeof closeModal === 'function') closeModal();
      usersState.msg = `Created ${email.trim()} (${role}) — recorded in the audit log.`;
      await refreshUsers();
    } catch (e) {
      if (errEl) { errEl.textContent = e.status === 409 ? 'An account with that email already exists.' : (e.message || 'Create failed.'); errEl.style.display = 'block'; }
    }
  };

  function realUsersPage() {
    if (!isAdmin()) {
      return `<div class="page-head"><div class="page-title"><h1>User Management</h1></div></div>
        <div class="callout section" style="border-color:var(--line);background:var(--surface-2)">${ic('lock', 13)} &nbsp;User administration is <b>admin</b> only. Your role (<b>${esc(backendRole() || '')}</b>) cannot manage accounts.</div>`;
    }
    if (!usersState.loaded && !usersState.loading) setTimeout(refreshUsers, 0);
    const meId = (state.user && state.user.id) || '';
    const rows = usersState.items.map((u) => `
      <tr>
        <td><div style="font-weight:600;font-size:13px">${esc(u.full_name || '')}</div><div class="muted" style="font-size:11.5px">${esc(u.email)}</div></td>
        <td><div class="select" style="min-width:118px"><select onchange="danahSetUserRole('${u.id}', this.value)" ${u.id === meId ? 'disabled title="You cannot change your own role"' : ''}>
          ${['viewer', 'analyst', 'executive', 'admin'].map((r) => `<option value="${r}" ${u.role === r ? 'selected' : ''}>${r}</option>`).join('')}</select></div></td>
        <td><span class="cls-tag">${esc((u.clearance || '').replace('_', '-'))}</span></td>
        <td>${u.is_active ? '<span class="pill bg-green tone-green">Active</span>' : '<span class="pill bg-red tone-red">Disabled</span>'}</td>
        <td style="font-size:12px;color:var(--ink-3)">${u.last_login_at ? esc(relativeTime(u.last_login_at)) : 'never'}</td>
        <td>${u.id === meId ? '<span class="muted" style="font-size:11px">you</span>' : (u.is_active
        ? `<button class="btn btn-ghost btn-sm" onclick="danahToggleUser('${u.id}',false)">Deactivate</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="danahToggleUser('${u.id}',true)">Activate</button>`)}</td>
      </tr>`).join('');
    const msg = usersState.msg ? `<div class="callout ${/fail|exist|not permitted/i.test(usersState.msg) ? '' : 'amber'}" style="margin-bottom:14px">${esc(usersState.msg)}</div>` : '';
    return `
      <div class="page-head"><div class="page-title"><h1>User Management</h1><p>Create accounts and set roles. Clearance follows the role; changing a password or disabling an account revokes its tokens at once. Every change is audited.</p></div>
        <div class="page-controls"><button class="btn btn-ghost" onclick="danahRefreshUsers()">Refresh</button><button class="btn btn-primary" onclick="danahNewUser()">${ic('user', 16)} Create user</button></div></div>
      ${msg}
      <div class="card section" style="overflow-x:auto"><table class="tbl"><thead><tr><th>User</th><th>Role</th><th>Clearance</th><th>Status</th><th>Last login</th><th></th></tr></thead>
        <tbody>${usersState.loading && !usersState.items.length ? '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">Loading…</td></tr>' : (rows || '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">No users.</td></tr>')}</tbody></table></div>`;
  }

  /* ======================= SOURCES ======================= */
  const srcState = { items: [], loaded: false, loading: false, msg: '' };
  async function refreshSources() {
    if (!state.live) return;
    srcState.loading = true;
    if (S && S.route === 'sources' && typeof render === 'function') render();
    try { const rows = await call('/sources').catch(() => null); srcState.items = Array.isArray(rows) ? rows : []; } catch (_) { /* keep */ }
    srcState.loading = false; srcState.loaded = true;
    if (S && S.route === 'sources' && typeof render === 'function') render();
  }
  window.danahRefreshSources = refreshSources;
  window.danahSyncSource = async function (id) {
    srcState.msg = 'Syncing — running the connector now…'; if (typeof render === 'function') render();
    try {
      const r = await call('/sources/' + id + '/sync', { method: 'POST', timeout: 120000 });
      srcState.msg = r.status === 'ok' ? `Synced — fetched ${r.fetched}, new ${r.created}, duplicates ${r.duplicates}.` : `Source reported “${r.status}”${r.error ? ' · ' + r.error : ''}.`;
      await refreshSources();
    } catch (e) { srcState.msg = e.status === 403 ? 'Syncing requires analyst clearance or above.' : 'Sync failed: ' + e.message; if (typeof render === 'function') render(); }
  };
  window.danahToggleSource = async function (id, enabled) {
    if (!isAdmin()) { if (typeof toast === 'function') toast('Only an admin can enable or disable a source.'); return; }
    try { await call('/sources/' + id, { method: 'PATCH', body: { enabled } }); srcState.msg = enabled ? 'Source enabled.' : 'Source disabled.'; await refreshSources(); }
    catch (e) { srcState.msg = 'Update failed: ' + e.message; if (typeof render === 'function') render(); }
  };
  function srcHealthPill(h) {
    const map = { healthy: ['green', 'Healthy'], stale: ['orange', 'Stale'], failing: ['red', 'Failing'], disabled: ['navy', 'Disabled'], unknown: ['blue', 'Unknown'] };
    const x = map[h] || ['blue', h || 'unknown'];
    return `<span class="pill bg-${x[0] === 'navy' ? 'blue' : x[0]} tone-${x[0] === 'navy' ? 'blue' : x[0]}"><span class="sdot" style="background:var(--${x[0] === 'navy' ? 'ink-3' : x[0]})"></span>${x[1]}</span>`;
  }
  function realSourcesPage() {
    if (!srcState.loaded && !srcState.loading) setTimeout(refreshSources, 0);
    const analyst = isAnalyst(), admin = isAdmin();
    const rows = srcState.items.map((s) => `
      <tr>
        <td><div style="font-weight:600;font-size:13px">${esc(s.name)}</div><div class="muted" style="font-size:11.5px">${esc(s.connector)} · ${esc(s.type)}</div></td>
        <td>${srcHealthPill(s.health)}</td>
        <td style="font-size:12px">${s.item_count} items</td>
        <td style="font-size:12px;color:var(--ink-3)">${s.last_synced_at ? esc(relativeTime(s.last_synced_at)) : 'never'}${s.last_status ? ' · ' + esc(s.last_status) : ''}</td>
        <td>${s.enabled ? '<span class="pill bg-green tone-green">Enabled</span>' : '<span class="pill" style="background:var(--surface-2);color:var(--ink-3);border:1px solid var(--line)">Disabled</span>'}</td>
        <td style="white-space:nowrap">
          ${analyst ? `<button class="btn btn-ghost btn-sm" onclick="danahSyncSource('${s.id}')">${ic('refresh', 13)} Sync</button>` : ''}
          ${admin ? `<button class="btn btn-ghost btn-sm" onclick="danahToggleSource('${s.id}',${s.enabled ? 'false' : 'true'})">${s.enabled ? 'Disable' : 'Enable'}</button>` : ''}
        </td>
      </tr>`).join('');
    const msg = srcState.msg ? `<div class="callout ${/fail|require/i.test(srcState.msg) ? '' : 'amber'}" style="margin-bottom:14px">${esc(srcState.msg)}</div>` : '';
    return `
      <div class="page-head"><div class="page-title"><h1>Sources</h1><p>The connectors DANAH ingests from, with live health computed from real poll history. ${analyst ? 'Sync one on demand — ' : ''}item counts are within your clearance.</p></div>
        <div class="page-controls"><button class="btn btn-ghost" onclick="danahRefreshSources()">Refresh</button></div></div>
      ${msg}
      <div class="card section" style="overflow-x:auto"><table class="tbl"><thead><tr><th>Source</th><th>Health</th><th>Items</th><th>Last sync</th><th>Scheduler</th><th></th></tr></thead>
        <tbody>${srcState.loading && !srcState.items.length ? '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">Loading…</td></tr>' : (rows || '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">No sources configured.</td></tr>')}</tbody></table></div>
      <div class="callout" style="border-color:var(--green-line);background:var(--green-bg)">${ic('shield', 13, 'tone-green')} &nbsp;Health is real: a source goes <b>stale</b> after three missed polls and <b>failing</b> after a sync error — the same function the scheduler uses.</div>`;
  }

  /* ======================= CONVERSATIONS (chat history) ======================= */
  const chatsState = { items: [], loaded: false, loading: false };
  async function refreshChats() {
    if (!state.live) return;
    chatsState.loading = true;
    if (S && S.route === 'chats' && typeof render === 'function') render();
    try { const rows = await call('/agent/chat/sessions').catch(() => null); chatsState.items = Array.isArray(rows) ? rows : []; } catch (_) { /* keep */ }
    chatsState.loading = false; chatsState.loaded = true;
    if (S && S.route === 'chats' && typeof render === 'function') render();
  }
  window.danahRefreshChats = refreshChats;
  window.danahOpenChat = async function (id) {
    if (typeof openModal !== 'function') return;
    openModal(`<div class="modal wide" onclick="event.stopPropagation()"><div class="modal-body" style="padding:44px;text-align:center;color:var(--ink-3)">Loading conversation…</div></div>`);
    try {
      const s = await call('/agent/chat/sessions/' + id);
      const msgs = (s.messages || []).map((m) => {
        const cites = (m.citations || []).length ? `<div class="muted" style="font-size:11px;margin-top:6px">${m.citations.map((c) => `[${c.n}] ${esc(c.title || '')}`).join(' · ')}</div>` : '';
        return `<div style="margin-bottom:14px">
          <div style="font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:${m.role === 'user' ? 'var(--navy)' : 'var(--orange)'};margin-bottom:4px">${m.role === 'user' ? 'You' : 'DANAH'}${(m.confidence != null && m.role !== 'user') ? ' · ' + Math.round(m.confidence * 100) + '% confidence' : ''}</div>
          <div class="story-b" style="white-space:pre-wrap">${esc(m.content)}</div>${cites}</div>`;
      }).join('');
      openModal(`<div class="modal wide" onclick="event.stopPropagation()">
        <div class="modal-head"><div class="mh-ic bg-orange tone-orange">${ic('spark', 21)}</div><div><h3>${esc(s.title || 'Conversation')}</h3><p>${s.message_count} messages · ${esc(relativeTime(s.created_at))}</p></div><button class="modal-x" onclick="closeModal()">${ic('x', 18)}</button></div>
        <div class="modal-body">${msgs || '<div class="muted">No messages.</div>'}</div>
        <div class="modal-foot"><button class="btn btn-ghost" onclick="closeModal()">Close</button></div></div>`);
    } catch (e) {
      openModal(`<div class="modal" onclick="event.stopPropagation()"><div class="modal-head"><h3>Conversation</h3><button class="modal-x" onclick="closeModal()">${ic('x', 18)}</button></div><div class="modal-body" style="color:var(--red)">Could not load: ${esc(e.message)}</div><div class="modal-foot"><button class="btn btn-ghost" onclick="closeModal()">Close</button></div></div>`);
    }
  };
  function realChatsPage() {
    if (!chatsState.loaded && !chatsState.loading) setTimeout(refreshChats, 0);
    const rows = chatsState.items.length
      ? chatsState.items.map((s) => `
        <div class="lrow" style="cursor:pointer" onclick="danahOpenChat('${s.id}')">
          <div class="lic bg-orange tone-orange">${ic('spark', 18)}</div>
          <div class="lbody"><div class="ltitle">${esc(s.title || 'Untitled conversation')}</div>
            <div class="lmeta"><span class="tag">${s.message_count} messages</span><span class="tag">${esc(relativeTime(s.created_at))}</span></div></div>
          <div style="align-self:center;color:var(--ink-4)">${ic('chevron', 18)}</div>
        </div>`).join('')
      : `<div class="empty">${ic('spark', 42)}<h4>No conversations yet</h4><p>Ask the Live Agent something — your conversations are saved here so you can re-read the answers and their citations.</p></div>`;
    return `
      <div class="page-head"><div class="page-title"><h1>Conversations</h1><p>Your saved chats with the Live Agent — grounded answers with their citations, kept for reference.</p></div>
        <div class="page-controls"><button class="btn btn-ghost" onclick="danahRefreshChats()">Refresh</button></div></div>
      <div class="card section">${chatsState.loading && !chatsState.items.length ? '<div class="callout amber" style="margin:14px">Loading…</div>' : rows}</div>`;
  }

  if (typeof PAGES !== 'undefined') {
    PAGES.users = function () { return state.live ? realUsersPage() : (typeof pageHome === 'function' ? pageHome() : ''); };
    PAGES.sources = function () { return state.live ? realSourcesPage() : (typeof pageHome === 'function' ? pageHome() : ''); };
    PAGES.chats = function () { return state.live ? realChatsPage() : (typeof pageHome === 'function' ? pageHome() : ''); };
  }

  /* ======================= FIRST-LOGIN GUIDED TOUR ======================= */
  const TOUR_GUIDE = {
    home: ['Command Centre', 'Your live overview — real figures pulled from the backend, the Ask bar, and the decisions awaiting you.'],
    risks: ['Risks & Blind Spots', 'Real insights the AI produced, filtered to risks. Policy Watch and UAE Success Stories are the same list, by type.'],
    feed: ['Intelligence Feed', 'Raw items ingested from the ministry’s sources, each triaged by the Signal Agent — clearance-filtered.'],
    chats: ['Conversations', 'Your chat history with the Live Agent. Open any one to re-read the grounded answer and its citations.'],
    agents: ['AI Agents', 'Run the real six-agent pipeline and watch it reason step by step, with real tokens and cost.'],
    knowledge: ['Verified Knowledge', 'Upload documents. Once indexed, the Live Agent can cite them in its answers.'],
    sources: ['Sources', 'The connectors DANAH ingests from, with live health. Sync one on demand.'],
    reports: ['Reports & Briefings', 'Executive briefings composed by the Briefing Agent — always bilingual, English and Arabic.'],
    tasks: ['Action Tracker', 'Decisions turned into owned, tracked actions — move each from To-do to Done; every change is audited.'],
    approvals: ['Approvals', 'Nothing DANAH produces is published until you decide here. Every decision is written to the audit log.'],
    memory: ['Strategic Memory', 'What the agents remember, so the ministry never re-proposes what it already tried.'],
    users: ['User Management', 'Create accounts and set roles — clearance follows the role and is enforced server-side.'],
    governance: ['Governance & Audit', 'The tamper-evident, hash-chained log of every action in the system.'],
    alerts: ['Alerts', 'Real notifications, addressed to your role.'],
    settings: ['Settings', 'Appearance, language, and your session.'],
  };
  const TOUR = { steps: [], i: 0 };
  function danahTourEnd() {
    try { localStorage.setItem('danah.tour.seen', '1'); } catch (_) {}
    const o = document.getElementById('danah-tour'); if (o) o.remove();
    document.querySelectorAll('.nav-item').forEach((n) => { n.style.boxShadow = ''; });
  }
  window.danahTourEnd = danahTourEnd;
  function danahTourShow() {
    const step = TOUR.steps[TOUR.i];
    if (!step) { danahTourEnd(); return; }
    if (typeof go === 'function') go(step);
    const g = TOUR_GUIDE[step] || [step, ''];
    let o = document.getElementById('danah-tour');
    if (!o) { o = document.createElement('div'); o.id = 'danah-tour'; o.className = 'no-print'; document.body.appendChild(o); }
    o.style.cssText = 'position:fixed;z-index:100000;left:50%;bottom:26px;transform:translateX(-50%);width:min(520px,92vw);background:#0f1d30;color:#e8eefc;border:1px solid #24406b;border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.5);padding:16px 18px';
    o.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="font-size:10px;font-weight:700;letter-spacing:.08em;color:#5ee9a8;text-transform:uppercase">Guided tour · ${TOUR.i + 1}/${TOUR.steps.length}</span>
        <button onclick="danahTourEnd()" style="margin-left:auto;background:none;border:none;color:#8fa0c4;cursor:pointer;font-size:12px">Skip tour</button>
      </div>
      <div style="font-family:var(--display,inherit);font-size:16px;font-weight:700;margin-bottom:5px">${esc(g[0])}</div>
      <div style="font-size:13px;line-height:1.55;color:#b9c6e0">${esc(g[1])}</div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        ${TOUR.i > 0 ? `<button class="btn btn-ghost btn-sm" onclick="danahTourNav(-1)">Back</button>` : ''}
        <button class="btn btn-primary btn-sm" onclick="danahTourNav(1)">${TOUR.i === TOUR.steps.length - 1 ? 'Done' : 'Next'}</button>
      </div>`;
    document.querySelectorAll('.nav-item').forEach((n) => { n.style.boxShadow = ''; });
    setTimeout(() => { const a = document.querySelector('.nav-item.active'); if (a) a.style.boxShadow = '0 0 0 2px #5b8cff, 0 0 0 6px rgba(91,140,255,.25)'; }, 60);
  }
  window.danahTourNav = function (d) {
    TOUR.i += d;
    if (TOUR.i >= TOUR.steps.length) { danahTourEnd(); return; }
    if (TOUR.i < 0) TOUR.i = 0;
    danahTourShow();
  };
  function danahMaybeTour() {
    if (!state.live) return;
    let seen = false; try { seen = localStorage.getItem('danah.tour.seen') === '1'; } catch (_) {}
    if (seen) return;
    const order = ['home', 'risks', 'feed', 'chats', 'agents', 'knowledge', 'sources', 'reports', 'tasks', 'approvals', 'memory', 'users', 'governance', 'alerts', 'settings'];
    TOUR.steps = order.filter((r) => TOUR_GUIDE[r] && (typeof window.navLocked !== 'function' || !window.navLocked(r)));
    TOUR.i = 0;
    setTimeout(danahTourShow, 500);
  }
  window.danahMaybeTour = danahMaybeTour;
  window.danahReplayTour = function () { try { localStorage.removeItem('danah.tour.seen'); } catch (_) {} danahMaybeTour(); };
  const _protoReplayTour = window.replayTour;
  window.replayTour = function () { if (state.live) return window.danahReplayTour(); return typeof _protoReplayTour === 'function' ? _protoReplayTour() : undefined; };

  /* ======================= ACTION TRACKER (tasks) — real backend, real table ======================= */
  if (typeof NAV !== 'undefined' && Array.isArray(NAV) && !NAV.some((n) => n.id === 'tasks')) {
    const gi = NAV.findIndex((n) => n.id === 'approvals');
    const entry = { id: 'tasks', label: 'Action Tracker', icon: 'target' };
    if (gi >= 0) NAV.splice(gi, 0, entry); else NAV.push(entry);
  }
  const _navLockedT = window.navLocked;
  window.navLocked = function (route) {
    if (route === 'tasks') return !state.live;
    return typeof _navLockedT === 'function' ? _navLockedT(route) : false;
  };
  const _goT = window.go;
  if (typeof _goT === 'function') {
    window.go = function (route) { const out = _goT.apply(this, arguments); if (state.live && route === 'tasks') refreshTasks(); return out; };
  }

  const tasksState = { items: [], loaded: false, loading: false, msg: '' };
  async function refreshTasks() {
    if (!state.live) return;
    tasksState.loading = true;
    if (S && S.route === 'tasks' && typeof render === 'function') render();
    try { const rows = await call('/tasks?limit=200').catch(() => null); tasksState.items = Array.isArray(rows) ? rows : []; } catch (_) { /* keep */ }
    tasksState.loading = false; tasksState.loaded = true;
    if (S && S.route === 'tasks' && typeof render === 'function') render();
  }
  window.danahRefreshTasks = refreshTasks;

  window.danahTaskAdvance = async function (id, statusVal) {
    tasksState.msg = 'Updating…'; if (typeof render === 'function') render();
    try { await call('/tasks/' + id, { method: 'PATCH', body: { status: statusVal } }); tasksState.msg = 'Action updated — recorded in the audit log.'; await refreshTasks(); }
    catch (e) { tasksState.msg = e.status === 403 ? 'Updating actions requires analyst clearance or above.' : 'Update failed: ' + e.message; if (typeof render === 'function') render(); }
  };
  window.danahNewTask = function () {
    if (typeof openModal !== 'function') return;
    openModal(`<div class="modal" onclick="event.stopPropagation()">
      <div class="modal-head"><div class="mh-ic" style="background:var(--navy);color:#fff">${ic('target', 21)}</div><div><h3>New action</h3><p>Track a decision through to done. It is created at OFFICIAL classification.</p></div><button class="modal-x" onclick="closeModal()">${ic('x', 18)}</button></div>
      <div class="modal-body">
        <div class="field"><label>Title</label><input id="ntTitle" class="inp" placeholder="e.g. Stand up the national talent academy"></div>
        <div class="field"><label>Owner</label><input id="ntOwner" class="inp" placeholder="Ministry / person responsible"></div>
        <div class="field"><label>Urgency</label><div class="select" style="width:100%"><select id="ntUrg"><option value="low">low</option><option value="medium" selected>medium</option><option value="high">high</option><option value="critical">critical</option></select></div></div>
        <div class="field"><label>Notes (optional)</label><textarea id="ntDesc" class="inp" rows="3"></textarea></div>
        <div id="ntErr" style="display:none;color:var(--red);font-size:12.5px;margin-top:4px"></div>
      </div>
      <div class="modal-foot"><button class="btn btn-ghost" onclick="closeModal()">Cancel</button><button class="btn btn-primary" onclick="danahCreateTask()">Create action</button></div></div>`);
  };
  window.danahCreateTask = async function () {
    const title = (document.getElementById('ntTitle') || {}).value || '';
    const owner = (document.getElementById('ntOwner') || {}).value || '';
    const urgency = (document.getElementById('ntUrg') || {}).value || 'medium';
    const description = (document.getElementById('ntDesc') || {}).value || '';
    const errEl = document.getElementById('ntErr');
    if (!title.trim()) { if (errEl) { errEl.textContent = 'A title is required.'; errEl.style.display = 'block'; } return; }
    try {
      await call('/tasks', { method: 'POST', body: { title: title.trim(), owner: owner.trim(), urgency, description: description.trim() } });
      if (typeof closeModal === 'function') closeModal();
      tasksState.msg = 'Action created — recorded in the audit log.';
      await refreshTasks();
    } catch (e) { if (errEl) { errEl.textContent = e.status === 403 ? 'Creating actions requires analyst clearance or above.' : (e.message || 'Create failed.'); errEl.style.display = 'block'; } }
  };

  function taskUrgPill(u) { const m = { critical: 'red', high: 'orange', medium: 'blue', low: 'green' }; return `<span class="pill bg-${m[u] || 'blue'} tone-${m[u] || 'blue'}">${esc(u)}</span>`; }
  function taskCard(t, analyst) {
    const tone = t.status === 'done' ? 'green' : t.status === 'blocked' ? 'red' : t.status === 'in_progress' ? 'blue' : 'orange';
    const actions = analyst ? (
      t.status === 'pending' ? `<button class="btn btn-ghost btn-sm" onclick="danahTaskAdvance('${t.id}','in_progress')">${ic('play', 13)} Start</button>`
        : t.status === 'in_progress' ? `<button class="btn btn-ghost btn-sm" onclick="danahTaskAdvance('${t.id}','done')">${ic('check', 13)} Complete</button><button class="btn btn-ghost btn-sm" onclick="danahTaskAdvance('${t.id}','blocked')">Block</button>`
          : t.status === 'blocked' ? `<button class="btn btn-ghost btn-sm" onclick="danahTaskAdvance('${t.id}','in_progress')">${ic('play', 13)} Resume</button>`
            : `<button class="btn btn-ghost btn-sm" onclick="danahTaskAdvance('${t.id}','pending')">${ic('refresh', 13)} Reopen</button>`
    ) : '';
    return `<div class="card card-pad" style="margin-bottom:10px">
      <div style="display:flex;gap:9px;align-items:flex-start">
        <div class="lic bg-${tone} tone-${tone}" style="width:30px;height:30px;flex:none">${ic('checks', 15)}</div>
        <div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:600;line-height:1.35">${esc(t.title)}</div>
          ${t.description ? `<div class="muted" style="font-size:11.5px;margin-top:3px">${esc(t.description)}</div>` : ''}</div>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px">
        ${t.owner ? `<span class="tag">${ic('user', 11)} ${esc(t.owner)}</span>` : ''}
        ${taskUrgPill(t.urgency)}
        ${t.status === 'blocked' ? '<span class="pill bg-red tone-red">Blocked</span>' : ''}
        <span class="cls-tag">${esc((t.classification || '').replace('_', '-'))}</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;margin:11px 0 4px"><div class="meter"><i style="width:${t.progress || 0}%;background:var(--${tone})"></i></div><span class="muted" style="font-size:11px;font-weight:600">${t.progress || 0}%</span></div>
      ${actions ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${actions}</div>` : ''}
    </div>`;
  }
  function realTasksPage() {
    if (!tasksState.loaded && !tasksState.loading) setTimeout(refreshTasks, 0);
    const analyst = isAnalyst();
    const cols = [
      ['To do', (t) => t.status === 'pending'],
      ['In progress', (t) => t.status === 'in_progress' || t.status === 'blocked'],
      ['Done', (t) => t.status === 'done'],
    ];
    const board = cols.map((col) => {
      const items = tasksState.items.filter(col[1]);
      return `<div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><h3 style="font-family:var(--display,inherit);font-size:12.5px;font-weight:700;letter-spacing:.5px">${col[0].toUpperCase()}</h3><span class="muted" style="font-size:12px;font-weight:600">${items.length}</span></div>
        ${items.map((t) => taskCard(t, analyst)).join('') || `<div class="card card-pad muted" style="font-size:12px;text-align:center">None</div>`}
      </div>`;
    }).join('');
    const msg = tasksState.msg ? `<div class="callout ${/fail|require/i.test(tasksState.msg) ? '' : 'amber'}" style="margin-bottom:14px">${esc(tasksState.msg)}</div>` : '';
    return `
      <div class="page-head"><div class="page-title"><h1>Action Tracker</h1><p>Decisions turned into owned, tracked actions — clearance-filtered, with every change written to the audit log.${analyst ? '' : ' Read-only for your role.'}</p></div>
        <div class="page-controls"><button class="btn btn-ghost" onclick="danahRefreshTasks()">Refresh</button>${analyst ? `<button class="btn btn-primary" onclick="danahNewTask()">${ic('target', 16)} New action</button>` : ''}</div></div>
      ${msg}
      ${tasksState.loading && !tasksState.items.length ? '<div class="callout amber">Loading…</div>' : `<div class="grid g-3" style="align-items:start">${board}</div>`}`;
  }
  if (typeof PAGES !== 'undefined') {
    PAGES.tasks = function () { return state.live ? realTasksPage() : (typeof pageHome === 'function' ? pageHome() : ''); };
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
    'Agent Roster',
    'Cabinet Affairs agents',
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

  /* =====================================================================
     UI POLISH
       · Sidebar grouped into collapsible sections — fewer things on screen at
         once (click a header to expand/collapse), and the scroll position is
         preserved across navigation so the clicked item no longer jumps to top.
       · Guided tour re-runs on every login, anchored beside the highlighted
         menu item (not a floating card), covering every page the role can see.
       · Responsive + hover CSS: clickable rows clamp long text and reveal it on
         hover; the login card and grids reflow on small screens.
     ===================================================================== */
  (function injectPolishCSS() {
    if (document.getElementById('danah-polish-css')) return;
    const s = document.createElement('style');
    s.id = 'danah-polish-css';
    s.textContent =
      '#sidebar .nav-group-head{display:flex;align-items:center;gap:8px;padding:8px 12px;margin:8px 2px 2px;font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-4);cursor:pointer;user-select:none;border-radius:8px;transition:background .12s,color .12s}' +
      '#sidebar .nav-group-head:hover{background:var(--surface-2);color:var(--ink-2)}' +
      '#sidebar .nav-group-head .chev{margin-left:auto;color:var(--ink-4);transition:transform .18s}' +
      '#sidebar .nav-group.collapsed .chev{transform:rotate(-90deg)}' +
      '#sidebar .nav-group.collapsed .nav-group-body{display:none}' +
      '.lrow[onclick]{cursor:pointer}' +
      '.lrow .ldesc{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}' +
      '.lrow:hover .ldesc{-webkit-line-clamp:unset}' +
      '#danah-tour{transition:top .16s ease,left .16s ease}' +
      '@media(max-width:600px){.login-card{padding:22px 18px!important}.page-controls{flex-wrap:wrap}}';
    document.head.appendChild(s);
  })();

  /* ---- collapsible, scroll-preserving sidebar (live only) ---- */
  let navCollapsed = {};
  try { navCollapsed = JSON.parse(localStorage.getItem('danah.nav.collapsed') || '{}') || {}; } catch (_) { navCollapsed = {}; }
  const NAV_GROUPS = [
    { label: null, ids: ['home'] },
    { label: 'Intelligence', ids: ['risks', 'policy', 'success', 'feed', 'chats', 'agents', 'knowledge', 'sources', 'reports'] },
    { label: 'Decisions', ids: ['tasks', 'approvals', 'memory'] },
    { label: 'Administration', ids: ['users', 'governance'] },
    { label: 'Account', ids: ['alerts', 'settings'] },
  ];
  function danahRenderSidebar() {
    const sb = document.querySelector('#sidebar'); if (!sb) return;
    const prev = sb.querySelector('.nav'); const st = prev ? prev.scrollTop : 0;
    const unread = (typeof ALERTS !== 'undefined') ? ALERTS.filter((a) => !a.read).length : 0;
    const defs = {}; NAV.forEach((n) => { if (!n.divider) defs[n.id] = n; });
    const itemHtml = (id) => {
      const it = defs[id]; if (!it) return '';
      if (typeof navLocked === 'function' && navLocked(id)) return '';
      const label = (typeof tt === 'function') ? tt('nav.' + id, it.label) : it.label;
      const active = (typeof S !== 'undefined' && S.route === id);
      return `<div class="nav-item ${active ? 'active' : ''}" onclick="go('${id}')" role="link" tabindex="0"
          onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();go('${id}')}" ${active ? 'aria-current="page"' : ''}>
          ${ic(it.icon, 18)}<span>${label}</span>${id === 'alerts' && unread ? `<span class="badge">${unread}</span>` : ''}</div>`;
    };
    const groups = NAV_GROUPS.map((g) => {
      const items = g.ids.map(itemHtml).filter(Boolean);
      if (!items.length) return '';
      if (!g.label) return items.join('');
      const collapsed = navCollapsed[g.label] ? 'collapsed' : '';
      return `<div class="nav-group ${collapsed}"><div class="nav-group-head" onclick="danahToggleNavGroup('${g.label}')">${g.label}<span class="chev">${ic('chevron', 14)}</span></div><div class="nav-group-body">${items.join('')}</div></div>`;
    }).join('');
    sb.innerHTML = `<nav class="nav" role="navigation" aria-label="Primary">${groups}</nav>
      <div class="nav-foot"><div class="nav-item ${S.route === 'help' ? 'active' : ''}" onclick="go('help')" role="link" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();go('help')}">${ic('help', 18)}<span>${(typeof tt === 'function') ? tt('nav.help', 'Help & Support') : 'Help & Support'}</span></div></div>`;
    const now = sb.querySelector('.nav'); if (now) now.scrollTop = st;
  }
  window.danahRenderSidebar = danahRenderSidebar;
  window.danahToggleNavGroup = function (label) { navCollapsed[label] = !navCollapsed[label]; try { localStorage.setItem('danah.nav.collapsed', JSON.stringify(navCollapsed)); } catch (_) {} danahRenderSidebar(); };
  window.danahExpandAllNav = function () { navCollapsed = {}; danahRenderSidebar(); };
  const _renderSidebarOrig = window.renderSidebar;
  window.renderSidebar = function () { return state.live ? danahRenderSidebar() : (typeof _renderSidebarOrig === 'function' ? _renderSidebarOrig.apply(this, arguments) : undefined); };

  /* ---- guided tour: every login, anchored beside the highlighted item ---- */
  const TState = { steps: [], i: 0 };
  function tourEl() { let o = document.getElementById('danah-tour'); if (!o) { o = document.createElement('div'); o.id = 'danah-tour'; o.className = 'no-print'; document.body.appendChild(o); } return o; }
  function tourPosition(o) {
    const active = document.querySelector('#sidebar .nav-item.active');
    const narrow = window.innerWidth < 920;
    if (active && !narrow) {
      try { active.scrollIntoView({ block: 'nearest' }); } catch (_) {}
      const r = active.getBoundingClientRect();
      o.style.left = (r.right + 14) + 'px';
      o.style.top = Math.max(12, Math.min(window.innerHeight - 250, r.top - 6)) + 'px';
      o.style.bottom = 'auto'; o.style.transform = 'none';
    } else {
      o.style.left = '50%'; o.style.bottom = '20px'; o.style.top = 'auto'; o.style.transform = 'translateX(-50%)';
    }
  }
  function tourShow() {
    const step = TState.steps[TState.i]; if (!step) { tourEnd(); return; }
    if (typeof go === 'function') go(step);
    const g = (typeof TOUR_GUIDE !== 'undefined' && TOUR_GUIDE[step]) || [step, ''];
    const o = tourEl();
    o.style.cssText = 'position:fixed;z-index:100000;background:#0f1d30;color:#e8eefc;border:1px solid #24406b;border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.5);padding:15px 17px;width:min(360px,92vw)';
    o.innerHTML =
      `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="font-size:9.5px;font-weight:700;letter-spacing:.08em;color:#5ee9a8;text-transform:uppercase">Tour · ${TState.i + 1}/${TState.steps.length}</span>
        <button onclick="danahTourEnd()" style="margin-left:auto;background:none;border:none;color:#8fa0c4;cursor:pointer;font-size:12px">Skip</button>
      </div>
      <div style="font-family:var(--display,inherit);font-size:15px;font-weight:700;margin-bottom:4px">${esc(g[0])}</div>
      <div style="font-size:12.5px;line-height:1.55;color:#b9c6e0">${esc(g[1])}</div>
      <div style="display:flex;gap:7px;justify-content:flex-end;margin-top:13px">
        ${TState.i > 0 ? `<button class="btn btn-ghost btn-sm" onclick="danahTourNav(-1)">Back</button>` : ''}
        <button class="btn btn-primary btn-sm" onclick="danahTourNav(1)">${TState.i === TState.steps.length - 1 ? 'Done' : 'Next'}</button>
      </div>`;
    document.querySelectorAll('.nav-item').forEach((n) => { n.style.boxShadow = ''; });
    setTimeout(() => { const a = document.querySelector('#sidebar .nav-item.active'); if (a) a.style.boxShadow = '0 0 0 2px #5b8cff,0 0 0 6px rgba(91,140,255,.22)'; tourPosition(o); }, 90);
  }
  function tourEnd() { const o = document.getElementById('danah-tour'); if (o) o.remove(); document.querySelectorAll('.nav-item').forEach((n) => { n.style.boxShadow = ''; }); }
  window.danahTourEnd = tourEnd;
  window.danahTourNav = function (d) { TState.i += d; if (TState.i >= TState.steps.length) { tourEnd(); return; } if (TState.i < 0) TState.i = 0; tourShow(); };
  window.danahMaybeTour = function () {
    if (!state.live) return;
    const order = ['home', 'risks', 'feed', 'chats', 'agents', 'knowledge', 'sources', 'reports', 'tasks', 'approvals', 'memory', 'users', 'governance', 'alerts', 'settings'];
    TState.steps = order.filter((r) => (typeof TOUR_GUIDE !== 'undefined' && TOUR_GUIDE[r]) && (typeof window.navLocked !== 'function' || !window.navLocked(r)));
    TState.i = 0;
    if (typeof window.danahExpandAllNav === 'function') window.danahExpandAllNav();  // so every item is visible to point at
    setTimeout(tourShow, 450);
  };
  window.danahReplayTour = function () { window.danahMaybeTour(); };
  window.addEventListener('resize', () => { const o = document.getElementById('danah-tour'); if (o) tourPosition(o); });

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
