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
function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------- router ---------- */
function nav(hash) { location.hash = hash; }
window.addEventListener("hashchange", render);
$("#signout").addEventListener("click", () => { store.token = null; store.userId = null; nav("#/auth"); });

async function render() {
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
      <button class="btn btn-primary">Send</button>
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

  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    add("you", text); input.value = "";
    const t = el(`<div class="typing"></div>`); chat.appendChild(t);
    try {
      // the final turn compiles the proxy and can take ~30s
      const result = await api("/api/interview/message", { method: "POST", body: { text } });
      t.remove(); await handle(result);
    } catch (e) { t.remove(); toast(e.message); }
  });

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
async function renderMe() {
  const me = await api("/api/users/me");
  const root = el(`<div>
    <h1 class="screen-title">${esc(me.name)}<span class="hint">, ${me.age}</span></h1>
    <p class="screen-sub">${me.profile_live
      ? `Your profile is live. <a href="#/profile/${store.userId}">Talk to your own standin</a> to audit how it represents you.`
      : `Your profile isn't live yet — <a href="#/interview">finish training your standin</a> to go live.`}</p>

    <div class="card">
      <b>Photos</b> <span class="hint">(up to 6)</span>
      <div class="photo-strip" id="me-photos"></div>
      <input type="file" id="me-file" accept="image/jpeg,image/png,image/webp" hidden>
    </div>

    <div class="card">
      <label class="fld">Bio</label>
      <textarea id="me-bio" rows="4" placeholder="A couple of lines about you">${esc(me.bio)}</textarea>
      <div class="toggle">
        <input type="checkbox" id="me-vis" ${me.transcript_visibility ? "checked" : ""}>
        <label for="me-vis"><b>Review standin conversations</b><br>
          <span class="hint">You'll be able to read chats people have with your standin from now on. People are told before they chat that you review.</span></label>
      </div>
      <p class="err" id="me-err"></p>
      <button class="btn btn-primary" id="me-save" style="margin-top:12px">Save changes</button>
    </div>
  </div>`);
  view.appendChild(root);

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

  $("#me-save", root).addEventListener("click", async () => {
    $("#me-err", root).textContent = "";
    try {
      await api("/api/users/me", { method: "PATCH", body: {
        bio: $("#me-bio", root).value, transcript_visibility: $("#me-vis", root).checked } });
      toast("Saved");
    } catch (e) { $("#me-err", root).textContent = e.message; }
  });
}

/* ================= BROWSE ================= */
async function renderBrowse() {
  const { profiles } = await api("/api/users");
  const root = el(`<div>
    <h1 class="screen-title">People</h1>
    <p class="screen-sub">Open a profile to chat with their standin first.</p>
    <div class="grid" id="b-grid"></div>
  </div>`);
  view.appendChild(root);
  const grid = $("#b-grid", root);
  if (!profiles.length) {
    grid.replaceWith(el(`<div class="card">No live profiles yet. Once others finish training, they'll show up here — share the link around.</div>`));
    return;
  }
  profiles.forEach(p => {
    const c = el(`<div class="card profile-card" role="button" tabindex="0">
      <div class="ph">${p.photos.length ? `<img src="/api/photos/${p.photos[0]}" alt="">` : `<span class="initial">${esc(p.name[0] || "?")}</span>`}</div>
      <div class="meta"><b>${esc(p.name)}</b>, ${p.age}<br><span class="hint">${esc((p.bio || "").slice(0, 64))}</span></div>
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
  const root = el(`<div>
    <h1 class="screen-title">${esc(p.name)}<span class="hint">, ${p.age}</span></h1>
    ${p.bio ? `<p class="screen-sub">${esc(p.bio)}</p>` : ""}
    <div class="gallery">${p.photos.map(pid => `<img src="/api/photos/${pid}" alt="">`).join("")}</div>
    ${isSelf ? `<div class="notice">This is your own standin — what you hear is what others hear.</div>` : ""}
    ${!isSelf && p.transcript_visibility ? `<div class="row" style="margin-bottom:8px"><span class="badge">reviews standin chats</span><span class="hint">${esc(p.name)} can read this conversation.</span></div>` : ""}
    <div class="chat" id="p-chat"></div>
    <form class="composer" id="p-form">
      <input type="text" id="p-input" placeholder="Say something to ${esc(p.name)}'s standin" autocomplete="off">
      <button class="btn btn-primary">Send</button>
    </form>
  </div>`);
  view.appendChild(root);
  const chat = $("#p-chat", root), form = $("#p-form", root), input = $("#p-input", root);
  chat.appendChild(el(`<div class="speaker">STANDIN · SPEAKS AS ${esc(p.name.toUpperCase())}</div>`));

  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const text = input.value.trim(); if (!text) return;
    chat.appendChild(el(`<div class="msg you">${esc(text)}</div>`)); input.value = "";
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
  });
}

/* ---------- boot ---------- */
render();
