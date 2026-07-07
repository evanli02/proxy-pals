/* Standin — minimal frontend. Vanilla JS, hash routing, no build step. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const topbar = $("#topbar");

/* ---------- state & api ---------- */
const store = {
  get token() { return localStorage.getItem("standin_token"); },
  set token(v) { v ? localStorage.setItem("standin_token", v) : localStorage.removeItem("standin_token"); },
  get userId() { return localStorage.getItem("standin_uid"); },
  set userId(v) { v ? localStorage.setItem("standin_uid", v) : localStorage.removeItem("standin_uid"); },
};

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (store.token) headers["Authorization"] = "Bearer " + store.token;
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: form || (body ? JSON.stringify(body) : undefined) });
  if (res.status === 401 && location.hash !== "#/auth") { store.token = null; nav("#/auth"); throw new Error("Signed out"); }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `Request failed (${res.status})`);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._timer); t._timer = setTimeout(() => (t.hidden = true), 3200);
}
/* ---------- avatar renderer (Mii-ish, parametric) ---------- */
const SHAPES = {
  blob:  '<ellipse cx="50" cy="58" rx="30" ry="32" fill="{B}"/>',
  round: '<circle cx="50" cy="56" r="31" fill="{B}"/>',
  square:'<rect x="21" y="28" width="58" height="58" rx="16" fill="{B}"/>',
  bean:  '<path d="M50 24 C72 24 80 44 76 62 C72 82 62 90 50 90 C38 90 28 82 24 62 C20 44 28 24 50 24 Z" fill="{B}"/>',
  egg:   '<ellipse cx="50" cy="58" rx="26" ry="34" fill="{B}"/>',
};
const PATTERNS = {
  none: "",
  spots: '<circle cx="34" cy="72" r="4.5" fill="#FFFFFF" opacity=".35"/><circle cx="58" cy="80" r="3.5" fill="#FFFFFF" opacity=".35"/><circle cx="68" cy="66" r="3" fill="#FFFFFF" opacity=".35"/>',
  stripes: '<path d="M24 72 h52 M28 80 h44" stroke="#FFFFFF" opacity=".3" stroke-width="5" stroke-linecap="round"/>',
  belly: '<ellipse cx="50" cy="74" rx="16" ry="13" fill="#FFFFFF" opacity=".35"/>',
};
const EYES = {
  dot: '<circle cx="38" cy="46" r="4" fill="#20241D"/><circle cx="62" cy="46" r="4" fill="#20241D"/>',
  happy: '<path d="M32 46 q6 -8 12 0 M56 46 q6 -8 12 0" stroke="#20241D" stroke-width="3" fill="none" stroke-linecap="round"/>',
  star: '<text x="32" y="52" font-size="14">✦</text><text x="56" y="52" font-size="14">✦</text>',
  sleepy: '<path d="M32 46 q6 5 12 0 M56 46 q6 5 12 0" stroke="#20241D" stroke-width="3" fill="none" stroke-linecap="round"/>',
  wink: '<circle cx="38" cy="46" r="4" fill="#20241D"/><path d="M56 46 q6 -6 12 0" stroke="#20241D" stroke-width="3" fill="none" stroke-linecap="round"/>',
  big: '<circle cx="38" cy="46" r="7" fill="#FFF"/><circle cx="62" cy="46" r="7" fill="#FFF"/><circle cx="39.5" cy="47" r="3.5" fill="#20241D"/><circle cx="63.5" cy="47" r="3.5" fill="#20241D"/>',
  side: '<circle cx="41" cy="46" r="4" fill="#20241D"/><circle cx="65" cy="46" r="4" fill="#20241D"/>',
  shades: '<rect x="28" y="40" width="18" height="11" rx="4" fill="#20241D"/><rect x="54" y="40" width="18" height="11" rx="4" fill="#20241D"/><path d="M46 44 h8" stroke="#20241D" stroke-width="3"/>',
};
const MOUTH = {
  smile: '<path d="M40 62 q10 10 20 0" stroke="#20241D" stroke-width="3" fill="none" stroke-linecap="round"/>',
  open: '<ellipse cx="50" cy="64" rx="7" ry="9" fill="#20241D"/>',
  flat: '<path d="M42 64 h16" stroke="#20241D" stroke-width="3" stroke-linecap="round"/>',
  cat: '<path d="M40 62 q5 7 10 0 q5 7 10 0" stroke="#20241D" stroke-width="3" fill="none" stroke-linecap="round"/>',
  grin: '<path d="M38 61 q12 13 24 0 z" fill="#FFF" stroke="#20241D" stroke-width="2.5"/>',
  tongue: '<path d="M40 61 q10 9 20 0" stroke="#20241D" stroke-width="3" fill="none" stroke-linecap="round"/><path d="M47 65 q3 8 8 4 q1 -4 -1 -6 z" fill="#D4707F"/>',
  smirk: '<path d="M42 64 q9 6 18 -2" stroke="#20241D" stroke-width="3" fill="none" stroke-linecap="round"/>',
  ooo: '<circle cx="50" cy="64" r="5" fill="none" stroke="#20241D" stroke-width="3"/>',
};
const ACC = {
  none: "",
  sprout: '<path d="M50 16 q0 -10 8 -12 M50 16 q0 -8 -8 -10" stroke="#2F5D50" stroke-width="3" fill="none" stroke-linecap="round"/>',
  halo: '<ellipse cx="50" cy="10" rx="16" ry="5" fill="none" stroke="#C77E3C" stroke-width="3"/>',
  antenna: '<path d="M50 18 v-10" stroke="#20241D" stroke-width="3" stroke-linecap="round"/><circle cx="50" cy="6" r="4" fill="#C77E3C"/>',
  bow: '<path d="M50 14 l-10 -6 v12 z M50 14 l10 -6 v12 z" fill="#A3524B"/>',
  crown: '<path d="M36 18 l4 -10 l6 7 l4 -10 l4 10 l6 -7 l4 10 z" fill="#C77E3C" stroke="#20241D" stroke-width="1.5"/>',
  flower: '<g transform="translate(66,14)"><circle r="3.5" fill="#C77E3C"/><circle cx="0" cy="-6" r="3.5" fill="#F0E4E4"/><circle cx="5.7" cy="1.8" r="3.5" fill="#F0E4E4"/><circle cx="-5.7" cy="1.8" r="3.5" fill="#F0E4E4"/><circle cx="3.5" cy="-4.9" r="3.5" fill="#F0E4E4"/><circle cx="-3.5" cy="-4.9" r="3.5" fill="#F0E4E4"/></g>',
  headphones: '<path d="M26 46 q0 -26 24 -26 q24 0 24 26" stroke="#20241D" stroke-width="4" fill="none"/><rect x="20" y="42" width="9" height="15" rx="4" fill="#20241D"/><rect x="71" y="42" width="9" height="15" rx="4" fill="#20241D"/>',
  horns: '<path d="M34 24 q-8 -8 -4 -16 q8 4 10 12 M66 24 q8 -8 4 -16 q-8 4 -10 12" fill="#A3524B"/>',
  beanie: '<path d="M27 34 q23 -26 46 0 l-2 6 q-21 -10 -42 0 z" fill="#6B5B9E"/><circle cx="50" cy="12" r="5" fill="#E4E0F0"/>',
};
const BLUSH = {
  off: "",
  on: '<ellipse cx="31" cy="56" rx="5" ry="3" fill="#D4707F" opacity=".45"/><ellipse cx="69" cy="56" rx="5" ry="3" fill="#D4707F" opacity=".45"/>',
};
function avatarSVG(av, size = 72) {
  const a = av || {};
  const body = (SHAPES[a.shape] || SHAPES.blob).replaceAll("{B}", esc(a.body || "#2F5D50"));
  return `<svg width="${size}" height="${size}" viewBox="0 0 100 100" role="img" aria-label="avatar">
    <rect width="100" height="100" rx="18" fill="${esc(a.bg || "#DFE9E4")}"/>
    ${body}${PATTERNS[a.pattern] || ""}${BLUSH[a.blush] || ""}
    ${EYES[a.eyes] || EYES.dot}${MOUTH[a.mouth] || MOUTH.smile}${ACC[a.acc] || ""}
  </svg>`;
}

function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------- router ---------- */
function nav(hash) { location.hash = hash; }
window.addEventListener("hashchange", render);
$("#signout").addEventListener("click", () => { store.token = null; store.userId = null; nav("#/auth"); });

let dmPoll = null;
async function render() {
  clearInterval(dmPoll); dmPoll = null;
  const hash = location.hash || "#/browse";
  if (!store.token && hash !== "#/auth") return nav("#/auth");
  topbar.hidden = !store.token;
  document.querySelectorAll("[data-nav]").forEach(a =>
    a.classList.toggle("active", hash.startsWith("#/" + a.dataset.nav)));
  view.innerHTML = "";
  try {
    if (hash === "#/auth") return renderAuth();
    if (hash === "#/interview") return renderInterview();
    if (hash === "#/me") return renderMe();
    if (hash === "#/connections") return renderConnections();
    if (hash.startsWith("#/dm/")) return renderDm(hash.split("/")[2]);
    if (hash.startsWith("#/profile/")) return renderProfile(hash.split("/")[2]);
    return renderBrowse();
  } catch (e) { view.appendChild(el(`<p class="err">${esc(e.message)}</p>`)); }
}

/* ================= AUTH ================= */
function renderAuth() {
  topbar.hidden = true;
  let mode = "signup";
  const root = el(`<div>
    <h1 class="hero">Stand<em>in</em><br>Your stand-in does the small talk.</h1>
    <p class="screen-sub">Train a proxy that chats as you. Meet people by talking to theirs first.</p>
    <div class="tabs">
      <button data-m="signup" class="active">Create account</button>
      <button data-m="login">Sign in</button>
    </div>
    <div class="card" style="border-top-left-radius:0">
      <div data-only="signup">
        <label class="fld">Name</label><input type="text" id="a-name" autocomplete="name">
        <label class="fld">Age</label><input type="number" id="a-age" min="18" max="120">
      </div>
      <label class="fld">Email</label><input type="email" id="a-email" autocomplete="email">
      <label class="fld">Password <span class="hint">(8+ characters)</span></label>
      <input type="password" id="a-pass" autocomplete="current-password">
      <p class="err" id="a-err"></p>
      <button class="btn btn-primary" id="a-go">Create account</button>
    </div>
  </div>`);
  root.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
    mode = b.dataset.m;
    root.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
    root.querySelector("[data-only=signup]").style.display = mode === "signup" ? "" : "none";
    $("#a-go", root).textContent = mode === "signup" ? "Create account" : "Sign in";
  }));
  $("#a-go", root).addEventListener("click", async () => {
    $("#a-err", root).textContent = "";
    try {
      const email = $("#a-email", root).value.trim(), password = $("#a-pass", root).value;
      const out = mode === "signup"
        ? await api("/api/auth/signup", { method: "POST", body: { email, password, name: $("#a-name", root).value.trim(), age: Number($("#a-age", root).value) } })
        : await api("/api/auth/login", { method: "POST", body: { email, password } });
      store.token = out.token; store.userId = out.user_id;
      const me = await api("/api/users/me");
      nav(me.profile_live ? "#/browse" : "#/interview");
    } catch (e) { $("#a-err", root).textContent = e.message; }
  });
  view.appendChild(root);
}

/* ================= INTERVIEW ================= */
async function renderInterview() {
  const status = await api("/api/interview/status");
  const root = el(`<div>
    <h1 class="screen-title">Train your standin</h1>
    <p class="screen-sub">Answer in your natural texting style — your standin learns your voice from how you write. More detail = a better standin. Your profile goes live when you finish.</p>
    <div class="progress"><div class="track"><div class="fill" id="iv-fill"></div></div>
      <span class="mono" id="iv-count"></span></div>
    <div class="chat" id="iv-chat"></div>
    <div id="iv-card"></div>
    <form class="composer" id="iv-form">
      <input type="text" id="iv-input" placeholder="Type your answer" autocomplete="off">
      <button class="btn btn-primary" id="iv-send">Send</button>
      <button class="btn btn-quiet" id="iv-skip" type="button" title="Skip this question">Skip</button>
    </form>
  </div>`);
  view.appendChild(root);
  const chat = $("#iv-chat", root), cardHost = $("#iv-card", root),
        form = $("#iv-form", root), input = $("#iv-input", root);

  const setProgress = r => {
    $("#iv-fill", root).style.width = (100 * r.asked_count / r.total_main_questions) + "%";
    $("#iv-count", root).textContent = `${r.asked_count}/${r.total_main_questions}`;
  };
  const add = (role, text) => {
    chat.appendChild(el(`<div class="msg ${role}">${esc(text)}</div>`));
    chat.lastElementChild.scrollIntoView({ block: "end" });
  };

  setProgress(status);
  (status.transcript || []).forEach(m => add(m.role === "user" ? "you" : "bot", m.content));
  if (!status.transcript || !status.transcript.length) {
    add("bot", "Hey!! I'm your standin-in-training — excited to get to know you. Two tips before we start: answer in your natural texting style (your standin learns to sound like YOU, typos and all), and give as much detail as you feel like — the more you share, the better it can speak for you. Say hi whenever you're ready!");
  }

  async function handle(result) {
    setProgress(result);
    cardHost.innerHTML = "";
    if (result.profile_ready) {
      add("bot", "That's everything. Your standin is built and your profile is live.");
      chat.appendChild(el(`<div class="notice">Try it out: <a href="#/profile/${store.userId}">talk to your own standin</a> to hear how it represents you, or <a href="#/browse">browse people</a>.</div>`));
      form.style.display = "none";
      return;
    }
    if (result.question) { form.style.display = "none"; renderSurveyCard(result.question, cardHost, handle); return; }
    form.style.display = "";
    if (result.reply) add("bot", result.reply);
  }

  const skipBtn = $("#iv-skip", root), sendBtn = $("#iv-send", root);
  function lock(on) {  // no double-texting: composer is disabled while waiting
    input.disabled = on; sendBtn.disabled = on; skipBtn.disabled = on;
    if (!on) input.focus();
  }
  async function turn(fn, youText) {
    if (input.disabled) return;
    if (youText) add("you", youText);
    lock(true);
    const t = el(`<div class="typing"></div>`); chat.appendChild(t);
    t.scrollIntoView({ block: "end" });
    try { const result = await fn(); t.remove(); await handle(result); }
    catch (e) { t.remove(); toast(e.message); }
    lock(false);
  }
  form.addEventListener("submit", ev => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    // the final turn compiles the proxy and can take ~30s
    turn(() => api("/api/interview/message", { method: "POST", body: { text } }), text);
  });
  skipBtn.addEventListener("click", () =>
    turn(() => api("/api/interview/skip", { method: "POST" }), "(skipped)"));

  if (status.question) { form.style.display = "none"; renderSurveyCard(status.question, cardHost, handle); }
  else if (status.profile_ready) handle(status);
}

/* ----- structured survey cards ----- */
function renderSurveyCard(q, host, onResult) {
  const submit = async answer => {
    const btn = $(".js-submit", host); if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
    try {
      const result = await api("/api/interview/answer", { method: "POST", body: { question_id: q.question_id, answer } });
      if (result.profile_ready) toast("Building your standin — this can take up to 30 seconds.");
      onResult(result);
    } catch (e) { toast(e.message); if (btn) { btn.disabled = false; btn.textContent = "Save answers"; } }
  };

  if (q.type === "likert_battery") {
    const card = el(`<div class="card survey">
      <p class="prompt">${esc(q.prompt)}</p>
      <div class="scale-ends mono"><span>1 · ${esc(q.scale_labels[0])}</span><span>7 · ${esc(q.scale_labels[6])}</span></div>
      <div class="items"></div>
      <div class="row spread" style="margin-top:14px">
        <span class="mono" data-count>0/${q.items.length} rated</span>
        <button class="btn btn-primary js-submit" disabled>Save answers</button>
      </div></div>`);
    const answers = {};
    const items = $(".items", card);
    q.items.forEach(it => {
      const rowEl = el(`<div class="battery-item"><p>${esc(it.text)}</p><div class="seg" role="radiogroup" aria-label="${esc(it.text)}"></div></div>`);
      const seg = $(".seg", rowEl);
      for (let v = 1; v <= 7; v++) {
        const b = el(`<button type="button" aria-label="${v}: ${esc(q.scale_labels[v - 1])}" title="${esc(q.scale_labels[v - 1])}">${v}</button>`);
        b.addEventListener("click", () => {
          answers[it.id] = v;
          seg.querySelectorAll("button").forEach((x, i) => x.classList.toggle("sel", i + 1 === v));
          const n = Object.keys(answers).length;
          $("[data-count]", card).textContent = `${n}/${q.items.length} rated`;
          $(".js-submit", card).disabled = n < q.items.length;
        });
        seg.appendChild(b);
      }
      items.appendChild(rowEl);
    });
    $(".js-submit", card).addEventListener("click", () => submit(answers));
    host.appendChild(card);

  } else if (q.type === "list") {
    const card = el(`<div class="card survey">
      <p class="prompt">${esc(q.prompt)}</p>
      <div class="list-rows"></div>
      <div class="row spread">
        <button class="btn btn-quiet js-add" type="button">+ Add another</button>
        <button class="btn btn-primary js-submit">Save answers</button>
      </div><p class="err js-err"></p></div>`);
    const rows = $(".list-rows", card);
    const addRow = () => rows.appendChild(el(`<input type="text" placeholder="…">`));
    for (let i = 0; i < q.min_items; i++) addRow();
    $(".js-add", card).addEventListener("click", addRow);
    $(".js-submit", card).addEventListener("click", () => {
      const vals = [...rows.querySelectorAll("input")].map(i => i.value.trim()).filter(Boolean);
      if (vals.length < q.min_items) { $(".js-err", card).textContent = `List at least ${q.min_items}.`; return; }
      submit(vals);
    });
    host.appendChild(card);

  } else if (q.type === "long_text") {
    const card = el(`<div class="card survey">
      <p class="prompt">${esc(q.prompt)}</p>
      <textarea rows="7"></textarea>
      <p class="hint charcount mono js-count">0 / ${q.recommended_chars} recommended</p>
      <button class="btn btn-primary js-submit">Save answer</button></div>`);
    const ta = $("textarea", card);
    ta.addEventListener("input", () =>
      $(".js-count", card).textContent = `${ta.value.length} / ${q.recommended_chars} recommended`);
    $(".js-submit", card).addEventListener("click", () => {
      if (!ta.value.trim()) { toast("Write a little something first."); return; }
      submit(ta.value.trim());
    });
    host.appendChild(card);

  } else if (q.type === "choice") {
    const card = el(`<div class="card survey">
      <p class="prompt">${esc(q.prompt)}</p>
      <div class="choice-grid"></div>
      <div class="row spread">
        ${q.optional ? `<button class="btn btn-quiet js-skip" type="button">Skip</button>` : "<span></span>"}
        <button class="btn btn-primary js-submit" disabled>Save answer</button>
      </div></div>`);
    let chosen = null;
    const grid = $(".choice-grid", card);
    q.options.forEach(opt => {
      const b = el(`<button type="button">${esc(opt)}</button>`);
      b.addEventListener("click", () => {
        chosen = opt;
        grid.querySelectorAll("button").forEach(x => x.classList.toggle("sel", x === b));
        $(".js-submit", card).disabled = false;
      });
      grid.appendChild(b);
    });
    $(".js-submit", card).addEventListener("click", () => submit(chosen));
    if (q.optional) $(".js-skip", card).addEventListener("click", () => submit(null));
    host.appendChild(card);
  }
}

/* ================= MY PROFILE ================= */
const MODES = [
  { key: "strict", label: "Grounded", hint: "Sticks strictly to what you've taught it; politely deflects everything else. Safest." },
  { key: "mimic", label: "Natural", hint: "Sounds like you and makes reasonable small inferences from what it knows. Recommended." },
  { key: "free", label: "Improv", hint: "Free-flowing — may riff beyond what you've shared. Most fun, least controlled." },
];

async function renderMe() {
  const me = await api("/api/users/me");
  const root = el(`<div>
    <h1 class="screen-title">${esc(me.name)}<span class="hint">, ${me.age}${me.city ? " · " + esc(me.city) : ""}</span></h1>
    <p class="screen-sub">${me.profile_live
      ? `Your profile is live. <a href="#/profile/${store.userId}">Talk to your own standin</a> to audit how it represents you.`
      : `Your profile isn't live yet — <a href="#/interview">finish training your standin</a> to go live.`}</p>

    <div class="card">
      <b>Photos</b> <span class="hint">(up to 6)</span>
      <div class="photo-strip" id="me-photos"></div>
      <input type="file" id="me-file" accept="image/jpeg,image/png,image/webp" hidden>
    </div>

    <div class="card">
      <b>Your standin's identity</b>
      <p class="hint">This is all strangers see until you mutually connect.</p>
      <div class="row" style="align-items:center;gap:14px">
        <div id="me-avatar">${avatarSVG(me.avatar, 84)}</div>
        <button class="btn btn-quiet" id="me-av-random" type="button">🎲 Randomize</button>
      </div>
      <div id="me-av-opts"></div>
      <label class="fld">Pseudonym</label>
      <input type="text" id="me-pseud" value="${esc(me.pseudonym)}" maxlength="40">
      <label class="fld">Gender <span class="hint">(your standin may share this; leave blank to keep private)</span></label>
      <input type="text" id="me-gender" value="${esc(me.gender || "")}" placeholder="e.g. male, female, nonbinary">
    </div>

    <div class="card">
      <label class="fld">Name <span class="hint">(shown only to mutual connections)</span></label>
      <input type="text" id="me-name" value="${esc(me.name)}">
      <label class="fld">City</label>
      <input type="text" id="me-city" value="${esc(me.city)}" placeholder="e.g. Ithaca, NY">
      <label class="fld">Bio <span class="hint">(one sentence — funny remark or quick intro)</span></label>
      <textarea id="me-bio" rows="2" placeholder="A line about you">${esc(me.bio)}</textarea>
      <div class="row" style="margin-top:8px">
        <button class="btn btn-quiet" id="me-suggest" type="button">Suggest bios from my training</button>
      </div>
      <div class="row" id="me-bio-chips" style="margin-top:8px"></div>
      <div class="toggle">
        <input type="checkbox" id="me-vis" ${me.transcript_visibility ? "checked" : ""}>
        <label for="me-vis"><b>Review standin conversations</b><br>
          <span class="hint">You'll be able to read chats people have with your standin from now on. People are told before they chat that you review.</span></label>
      </div>
      <p class="err" id="me-err"></p>
      <button class="btn btn-primary" id="me-save" style="margin-top:12px">Save changes</button>
    </div>

    <div class="card">
      <b>How your standin speaks</b>
      <div id="me-modes"></div>
    </div>

    <details class="card collapsible" id="me-knowledge-card">
      <summary><b>Questions your standin can answer</b>
        <span class="hint">everything it has learned about you</span></summary>
      <p class="hint">Edit an answer if it's wrong or outdated; delete anything you don't want it knowing.</p>
      <div id="me-knowledge"></div>
    </details>

    <details class="card collapsible" id="me-review-card">
      <summary><b>Questions your standin couldn't answer</b>
        <span class="hint" id="me-review-count"></span></summary>
      <p class="hint">People asked these; answer any of them and your standin learns the answer.</p>
      <div id="me-review"></div>
    </details>

    <div class="card">
      <b>Retrain</b>
      <p class="hint">Start the interview over from the beginning. Your current standin stays live until the new training is finished.</p>
      <button class="btn btn-danger" id="me-retrain" type="button">Retrain from scratch</button>
    </div>
  </div>`);
  view.appendChild(root);

  /* photos */
  const strip = $("#me-photos", root), file = $("#me-file", root);
  function paintPhotos(photos) {
    strip.innerHTML = "";
    photos.forEach(pid => {
      const s = el(`<div class="slot"><img src="/api/photos/${pid}" alt="Your photo">
        <button class="del" aria-label="Remove photo">×</button></div>`);
      $(".del", s).addEventListener("click", async () => {
        await api(`/api/users/me/photos/${pid}`, { method: "DELETE" });
        const fresh = await api("/api/users/me"); paintPhotos(fresh.photos);
      });
      strip.appendChild(s);
    });
    if (photos.length < 6) {
      const addBtn = el(`<button class="slot linklike" type="button" aria-label="Add photo" style="cursor:pointer;font-size:2rem;color:var(--you)">+</button>`);
      addBtn.addEventListener("click", () => file.click());
      strip.appendChild(addBtn);
    }
  }
  paintPhotos(me.photos);
  file.addEventListener("change", async () => {
    if (!file.files.length) return;
    const fd = new FormData(); fd.append("file", file.files[0]);
    try {
      await api("/api/users/me/photos", { method: "POST", form: fd });
      const fresh = await api("/api/users/me"); paintPhotos(fresh.photos);
    } catch (e) { toast(e.message); }
    file.value = "";
  });

  /* avatar builder */
  const AV_OPTS = {
    shape: ["blob", "round", "square", "bean", "egg"],
    eyes: ["dot", "happy", "star", "sleepy", "wink", "big", "side", "shades"],
    mouth: ["smile", "open", "flat", "cat", "grin", "tongue", "smirk", "ooo"],
    acc: ["none", "sprout", "halo", "antenna", "bow", "crown", "flower", "headphones", "horns", "beanie"],
    pattern: ["none", "spots", "stripes", "belly"],
    blush: ["off", "on"],
    body: ["#2F5D50", "#C77E3C", "#6B5B9E", "#A3524B", "#3F7D8C", "#8C7B3F",
           "#B85C79", "#4A6FA5", "#5E8C61", "#8A6552"],
    bg: ["#DFE9E4", "#F4E6D5", "#E4E0F0", "#F0E4E4", "#E0EDF0", "#EFEBD8",
         "#EAE0EC", "#DCEBE0", "#F1E3D3", "#E3E7F0"],
  };
  const AV_DEFAULTS = { bg: "#DFE9E4", body: "#2F5D50", shape: "blob",
    eyes: "dot", mouth: "smile", acc: "none", pattern: "none", blush: "off" };
  let av = Object.assign({}, AV_DEFAULTS, me.avatar || {});
  function paintAvatar() {
    $("#me-avatar", root).innerHTML = avatarSVG(av, 84);
    const host = $("#me-av-opts", root); host.innerHTML = "";
    [["shape", "Shape"], ["eyes", "Eyes"], ["mouth", "Mouth"], ["acc", "Extra"],
     ["pattern", "Pattern"], ["blush", "Blush"], ["body", "Color"], ["bg", "Backdrop"]]
      .forEach(([k, label]) => {
        const row = el(`<div class="av-row"><span class="mono av-label">${label}</span><span class="av-opts"></span></div>`);
        const opts = $(".av-opts", row);
        AV_OPTS[k].forEach(v => {
          const isColor = v.startsWith("#");
          let b;
          if (isColor) {
            b = el(`<button type="button" class="swatch ${av[k] === v ? "sel" : ""}" style="background:${v}" aria-label="${v}"></button>`);
          } else {
            // mini live preview: this option applied to the current avatar
            const preview = avatarSVG(Object.assign({}, av, { [k]: v }), 40);
            b = el(`<button type="button" class="av-pick ${av[k] === v ? "sel" : ""}" aria-label="${v}" title="${v}">${preview}</button>`);
          }
          b.addEventListener("click", async () => {
            av[k] = v; paintAvatar();
            try { await api("/api/users/me", { method: "PATCH", body: { avatar: av } }); } catch (e) { toast(e.message); }
          });
          opts.appendChild(b);
        });
        host.appendChild(row);
      });
  }
  paintAvatar();
  $("#me-av-random", root).addEventListener("click", async () => {
    Object.keys(AV_OPTS).forEach(k => av[k] = AV_OPTS[k][Math.floor(Math.random() * AV_OPTS[k].length)]);
    paintAvatar();
    try { await api("/api/users/me", { method: "PATCH", body: { avatar: av } }); } catch (e) { toast(e.message); }
  });

  /* bio suggestions */
  $("#me-suggest", root).addEventListener("click", async () => {
    const btn = $("#me-suggest", root);
    btn.disabled = true; btn.textContent = "Thinking…";
    try {
      const { suggestions } = await api("/api/users/me/bio-suggestions", { method: "POST" });
      const chips = $("#me-bio-chips", root); chips.innerHTML = "";
      if (!suggestions.length) toast("No suggestions yet — finish training first.");
      suggestions.forEach(s => {
        const c = el(`<button class="chip" type="button">${esc(s)}</button>`);
        c.addEventListener("click", () => { $("#me-bio", root).value = s; });
        chips.appendChild(c);
      });
    } catch (e) { toast(e.message); }
    btn.disabled = false; btn.textContent = "Suggest bios from my training";
  });

  /* mode picker */
  let mode = me.proxy_mode || "mimic";
  const modesHost = $("#me-modes", root);
  function paintModes() {
    modesHost.innerHTML = "";
    MODES.forEach(m => {
      const r = el(`<label class="mode-row ${m.key === mode ? "sel" : ""}">
        <input type="radio" name="pmode" ${m.key === mode ? "checked" : ""}>
        <span><b>${m.label}</b><br><span class="hint">${m.hint}</span></span></label>`);
      $("input", r).addEventListener("change", async () => {
        mode = m.key; paintModes();
        try { await api("/api/users/me", { method: "PATCH", body: { proxy_mode: mode } }); toast(`Standin set to ${m.label}`); }
        catch (e) { toast(e.message); }
      });
      modesHost.appendChild(r);
    });
  }
  paintModes();

  /* knowledge: what the standin CAN answer (lazy-loaded on expand) */
  const knowledgeCard = $("#me-knowledge-card", root);
  const knowledgeHost = $("#me-knowledge", root);
  let knowledgeLoaded = false;
  async function paintKnowledge() {
    const { items } = await api("/api/knowledge");
    knowledgeHost.innerHTML = "";
    if (!items.length) {
      knowledgeHost.appendChild(el(`<p class="hint">Nothing yet — finish training and your standin's knowledge will show up here.</p>`));
      return;
    }
    items.forEach(it => {
      const rowEl = el(`<div class="review-item">
        <p><b>${esc(it.question)}</b></p>
        <p class="js-answer">${esc(it.answer)}</p>
        <div class="row js-actions">
          <button class="btn btn-quiet js-edit" type="button">Edit</button>
          <button class="btn btn-danger js-del" type="button">Delete</button>
        </div>
        <div class="row js-editor" hidden>
          <input type="text" value="${esc(it.answer)}">
          <button class="btn btn-primary js-save" type="button">Save</button>
          <button class="btn btn-quiet js-cancel" type="button">Cancel</button>
        </div>
      </div>`);
      const editor = $(".js-editor", rowEl), actions = $(".js-actions", rowEl);
      $(".js-edit", rowEl).addEventListener("click", () => { editor.hidden = false; actions.hidden = true; $("input", editor).focus(); });
      $(".js-cancel", rowEl).addEventListener("click", () => { editor.hidden = true; actions.hidden = false; });
      $(".js-save", rowEl).addEventListener("click", async () => {
        const val = $("input", editor).value.trim();
        if (!val) return;
        try {
          await api(`/api/knowledge/${it.id}`, { method: "PATCH", body: { answer: val } });
          $(".js-answer", rowEl).textContent = val;
          editor.hidden = true; actions.hidden = false;
          toast("Updated — your standin will answer with this now.");
        } catch (e) { toast(e.message); }
      });
      $(".js-del", rowEl).addEventListener("click", async () => {
        if (!confirm("Delete this? Your standin will no longer know it.")) return;
        try { await api(`/api/knowledge/${it.id}`, { method: "DELETE" }); rowEl.remove(); }
        catch (e) { toast(e.message); }
      });
      knowledgeHost.appendChild(rowEl);
    });
  }
  knowledgeCard.addEventListener("toggle", () => {
    if (knowledgeCard.open && !knowledgeLoaded) { knowledgeLoaded = true; paintKnowledge(); }
  });

  /* review loop */
  const reviewHost = $("#me-review", root);
  async function paintReview() {
    const { questions } = await api("/api/review");
    $("#me-review-count", root).textContent = questions.length ? `${questions.length} pending` : "";
    reviewHost.innerHTML = "";
    if (!questions.length) {
      reviewHost.appendChild(el(`<p class="hint">Nothing pending — your standin has been able to answer everything so far.</p>`));
      return;
    }
    questions.forEach(q => {
      const rowEl = el(`<div class="review-item">
        <p><span class="badge">${esc(q.category)}</span> ${esc(q.question)}</p>
        <div class="row"><input type="text" placeholder="Your answer"><button class="btn btn-quiet" type="button">Teach</button></div>
      </div>`);
      $("button", rowEl).addEventListener("click", async () => {
        const val = $("input", rowEl).value.trim();
        if (!val) return;
        try {
          await api(`/api/review/${q.id}/answer`, { method: "POST", body: { answer: val } });
          toast("Learned — your standin can answer that now.");
          paintReview();
        } catch (e) { toast(e.message); }
      });
      reviewHost.appendChild(rowEl);
    });
  }
  paintReview();

  /* retrain */
  $("#me-retrain", root).addEventListener("click", async () => {
    if (!confirm("Start the interview over? Your answers so far will be discarded (your current standin keeps working until you finish).")) return;
    await api("/api/interview/restart", { method: "POST" });
    nav("#/interview");
  });

  /* save */
  $("#me-save", root).addEventListener("click", async () => {
    $("#me-err", root).textContent = "";
    try {
      await api("/api/users/me", { method: "PATCH", body: {
        name: $("#me-name", root).value.trim() || null,
        pseudonym: $("#me-pseud", root).value.trim() || null,
        gender: $("#me-gender", root).value.trim(),
        city: $("#me-city", root).value.trim(),
        bio: $("#me-bio", root).value,
        transcript_visibility: $("#me-vis", root).checked } });
      toast("Saved");
    } catch (e) { $("#me-err", root).textContent = e.message; }
  });
}

/* ================= BROWSE (anonymous) ================= */
async function renderBrowse() {
  const { profiles } = await api("/api/explore");
  const root = el(`<div>
    <h1 class="screen-title">Standins</h1>
    <p class="screen-sub">Everyone here is anonymous — you meet the standin first. If the conversation clicks, send a like.</p>
    <div class="grid" id="b-grid"></div>
  </div>`);
  view.appendChild(root);
  const grid = $("#b-grid", root);
  if (!profiles.length) {
    grid.replaceWith(el(`<div class="card">No live standins yet. Once others finish training, they'll show up here.</div>`));
    return;
  }
  profiles.forEach(p => {
    const c = el(`<div class="card profile-card" role="button" tabindex="0">
      <div class="ph anon">${avatarSVG(p.avatar, 110)}</div>
      <div class="meta"><b>${esc(p.pseudonym)}</b><br>
        ${(p.chips && p.chips.length)
          ? p.chips.map(c => `<span class="why-chip">${esc(c)}</span>`).join("")
          : `<span class="hint mono">standin</span>`}</div>
    </div>`);
    const open = () => nav(`#/profile/${p.user_id}`);
    c.addEventListener("click", open);
    c.addEventListener("keydown", e => { if (e.key === "Enter") open(); });
    grid.appendChild(c);
  });
}

/* ================= PROFILE + PROXY CHAT ================= */
const convs = {}; // target_id -> conversation_id (session-scoped)
async function renderProfile(targetId) {
  const p = await api(`/api/users/${targetId}`);
  const isSelf = targetId === store.userId;
  const anon = p.anonymous && !isSelf;

  const header = anon
    ? `<div class="row" style="align-items:center;gap:14px;margin-top:18px">
         ${avatarSVG(p.avatar, 84)}
         <div><h1 class="screen-title" style="margin:0">${esc(p.pseudonym)}</h1>
         <span class="hint mono">anonymous standin</span></div>
       </div>
       <p class="screen-sub" style="margin-top:10px">You'll see their real profile if you both like each other.</p>`
    : `<div class="row" style="align-items:center;gap:14px;margin-top:18px">
         ${avatarSVG(p.avatar, 64)}
         <div><h1 class="screen-title" style="margin:0">${esc(p.name)}<span class="hint">, ${p.age}${p.city ? " · " + esc(p.city) : ""}</span></h1>
         <span class="hint">standin: ${esc(p.pseudonym)}</span></div>
       </div>
       ${p.bio ? `<p class="screen-sub" style="margin-top:10px">${esc(p.bio)}</p>` : ""}
       ${(!isSelf && p.photos) ? `<div class="gallery">${p.photos.map(pid => `<img src="/api/photos/${pid}" alt="">`).join("")}</div>` : ""}`;

  const likeBar = isSelf ? "" : p.connected
    ? `<div class="row" style="margin:6px 0 12px"><span class="badge" style="background:var(--you-soft);color:var(--you);border-color:var(--you)">connected</span>
       <a class="btn btn-primary" href="#/dm/${targetId}">Message ${esc(p.name || p.pseudonym)}</a></div>`
    : `<div class="row" style="margin:6px 0 12px">
       ${p.likes_you ? `<span class="badge">liked your standin</span>` : ""}
       <button class="btn ${p.you_liked ? "btn-quiet" : "btn-primary"}" id="p-like" ${p.you_liked ? "disabled" : ""}>
         ${p.you_liked ? "Liked ✓ — waiting for them" : (p.likes_you ? "Like back to connect" : "Send like")}
       </button></div>`;

  const root = el(`<div>
    ${header}
    ${likeBar}
    ${isSelf ? `<div class="notice">This is your own standin — what you hear is what others hear.</div>` : ""}
    ${!isSelf && p.transcript_visibility ? `<div class="row" style="margin-bottom:8px"><span class="badge">reviews standin chats</span><span class="hint">They can read this conversation.</span></div>` : ""}
    <div class="chat" id="p-chat"></div>
    <form class="composer" id="p-form">
      <input type="text" id="p-input" placeholder="Say something to ${esc(p.pseudonym || p.name)}" autocomplete="off">
      <button class="btn btn-primary">Send</button>
    </form>
  </div>`);
  view.appendChild(root);
  const chat = $("#p-chat", root), form = $("#p-form", root), input = $("#p-input", root);
  chat.appendChild(el(`<div class="speaker">STANDIN · ${esc((p.pseudonym || "").toUpperCase())}</div>`));

  const likeBtn = $("#p-like", root);
  if (likeBtn) likeBtn.addEventListener("click", async () => {
    try {
      const out = await api(`/api/likes/${targetId}`, { method: "POST" });
      if (out.mutual) { toast("It's mutual! Profiles unlocked."); render(); }
      else { likeBtn.disabled = true; likeBtn.textContent = "Liked ✓ — waiting for them"; likeBtn.classList.replace("btn-primary", "btn-quiet"); }
    } catch (e) { toast(e.message); }
  });

  const sendBtn = $("button", form);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const text = input.value.trim(); if (!text || input.disabled) return;
    chat.appendChild(el(`<div class="msg you">${esc(text)}</div>`)); input.value = "";
    input.disabled = true; sendBtn.disabled = true;  // no double-texting
    const t = el(`<div class="typing"></div>`); chat.appendChild(t);
    t.scrollIntoView({ block: "end" });
    try {
      const out = await api(`/api/proxy/${targetId}/message`, { method: "POST",
        body: { text, conversation_id: convs[targetId] || null } });
      convs[targetId] = out.conversation_id;
      t.remove();
      chat.appendChild(el(`<div class="msg bot proxy">${esc(out.reply)}</div>`));
      chat.lastElementChild.scrollIntoView({ block: "end" });
    } catch (e) { t.remove(); toast(e.message); }
    input.disabled = false; sendBtn.disabled = false; input.focus();
  });
}

/* ================= CONNECTIONS ================= */
async function renderConnections() {
  const { connections, incoming } = await api("/api/connections");
  const root = el(`<div>
    <h1 class="screen-title">Connections</h1>
    <p class="screen-sub">Mutual likes unlock full profiles and direct messages.</p>
    <div class="card"><b>Liked your standin</b>
      <div id="c-incoming"></div></div>
    <div class="card"><b>Your connections</b>
      <div id="c-conns"></div></div>
  </div>`);
  view.appendChild(root);

  const inc = $("#c-incoming", root);
  if (!incoming.length) inc.appendChild(el(`<p class="hint">No incoming likes yet.</p>`));
  incoming.forEach(a => {
    const rowEl = el(`<div class="conn-row">
      ${avatarSVG(a.avatar, 48)}
      <div style="flex:1"><b>${esc(a.pseudonym)}</b><br><span class="hint mono">anonymous until mutual</span></div>
      <a class="btn btn-quiet" href="#/profile/${a.user_id}">Chat with their standin</a>
      <button class="btn btn-primary js-likeback">${a.you_liked ? "Liked ✓" : "Like back"}</button>
    </div>`);
    const b = $(".js-likeback", rowEl);
    if (a.you_liked) b.disabled = true;
    b.addEventListener("click", async () => {
      try {
        const out = await api(`/api/likes/${a.user_id}`, { method: "POST" });
        if (out.mutual) { toast("It's mutual! Profiles unlocked."); render(); }
      } catch (e) { toast(e.message); }
    });
    inc.appendChild(rowEl);
  });

  const cc = $("#c-conns", root);
  if (!connections.length) cc.appendChild(el(`<p class="hint">No connections yet — chat with standins in Browse and send likes.</p>`));
  connections.forEach(c => {
    const rowEl = el(`<div class="conn-row">
      ${c.photos && c.photos.length ? `<img class="conn-ph" src="/api/photos/${c.photos[0]}" alt="">` : avatarSVG(c.avatar, 48)}
      <div style="flex:1"><b>${esc(c.name)}</b>, ${c.age}${c.city ? " · " + esc(c.city) : ""}<br>
        <span class="hint">${esc((c.bio || "").slice(0, 60))}</span></div>
      <a class="btn btn-quiet" href="#/profile/${c.user_id}">Profile</a>
      <a class="btn btn-primary" href="#/dm/${c.user_id}">Message</a>
    </div>`);
    cc.appendChild(rowEl);
  });
}

/* ================= DIRECT MESSAGES ================= */
async function renderDm(peerId) {
  const p = await api(`/api/users/${peerId}`);
  const root = el(`<div>
    <h1 class="screen-title">${esc(p.name || p.pseudonym)}</h1>
    <p class="screen-sub">Direct messages — this is the real ${esc(p.name || "them")}, not their standin.</p>
    <div class="chat" id="d-chat"></div>
    <form class="composer" id="d-form">
      <input type="text" id="d-input" placeholder="Message ${esc(p.name || p.pseudonym)}" autocomplete="off">
      <button class="btn btn-primary">Send</button>
    </form>
  </div>`);
  view.appendChild(root);
  const chat = $("#d-chat", root), form = $("#d-form", root), input = $("#d-input", root);

  let count = 0;
  async function refresh() {
    try {
      const { messages } = await api(`/api/messages/${peerId}`);
      if (messages.length === count) return;
      count = messages.length;
      chat.innerHTML = "";
      messages.forEach(m => chat.appendChild(
        el(`<div class="msg ${m.from === store.userId ? "you" : "bot"}">${esc(m.text)}</div>`)));
      if (chat.lastElementChild) chat.lastElementChild.scrollIntoView({ block: "end" });
    } catch (e) { /* connection may have been revoked; stay quiet */ }
  }
  await refresh();
  dmPoll = setInterval(refresh, 4000);

  const sendBtn = $("button", form);
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const text = input.value.trim(); if (!text || input.disabled) return;
    input.value = ""; input.disabled = true; sendBtn.disabled = true;
    try { await api(`/api/messages/${peerId}`, { method: "POST", body: { text } }); await refresh(); }
    catch (e) { toast(e.message); }
    input.disabled = false; sendBtn.disabled = false; input.focus();
  });
}

/* ---------- boot ---------- */
render();
