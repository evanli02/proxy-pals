# Standin API Reference

For client developers (KMP/mobile/web). Live interactive docs: `/docs` · machine spec for codegen: `/openapi.json` · verify deployed version: `GET /api/health` → `{"ok": true, "version": "0.5.1"}`.

**Base URL:** `https://<your-app>.herokuapp.com`

## Conventions

- **Auth:** every endpoint except `signup`, `login`, `GET /api/photos/{id}`, and `health` requires `Authorization: Bearer <token>`. Tokens come from signup/login and don't expire (prototype).
- **Errors:** always `{"detail": "human-readable reason"}` with: `401` bad/missing token · `403` action not allowed (e.g. DM without connection) · `404` not found / not visible to you · `409` conflict (duplicate email, photo cap, not trained yet) · `413`/`415` photo too big / wrong type · `422` validation (body shape, bad survey answer — detail explains which field).
- **IDs:** users `u_<hex>`, conversations `px_<hex>`, photos `ph_<hex>`, knowledge `qa_<hex>`.
- **Timestamps:** ISO-8601 strings, UTC.
- **The anonymity rule (read this first):** the only thing that stays hidden between strangers is the **real name** (profiles show pseudonym + avatar until a **mutual like** exists). A standin freely shares its owner's age, gender, location, and occupation — and `GET /api/proxy/{id}/card` surfaces those in the chat header. Several endpoints return different shapes depending on connection status; each is documented below.

---

## Auth

### POST /api/auth/signup → 201
```json
// request
{ "email": "a@x.com", "password": "min 8 chars", "name": "Evan", "age": 23 }
// response
{ "token": "V3ZK...", "user_id": "u_3fa92c..." }
```
`409` if the email is taken. A random pseudonym + avatar are generated automatically.

### POST /api/auth/login → 200
Same response shape. `401` on bad credentials (same error whether the email exists or not — don't distinguish in UI).

---

## My profile

### GET /api/users/me → 200 (own full profile)
```json
{
  "user_id": "u_3fa92c...", "email": "a@x.com",
  "name": "Evan", "age": 23, "city": "Ithaca, NY", "gender": "male",
  "bio": "Ramen-powered CS student.",
  "pseudonym": "Cobalt Fox",
  "avatar": { "bg": "#DFE9E4", "body": "#2F5D50", "shape": "bean",
              "eyes": "wink", "mouth": "grin", "acc": "crown",
              "pattern": "spots", "blush": "on" },
  "photos": ["ph_ab12...", "ph_cd34..."],
  "transcript_visibility": false,
  "proxy_mode": "free",
  "profile_live": true,
  "anonymous": false
}
```

### PATCH /api/users/me → 200
Partial update; send only fields you're changing. Editable: `name`, `age` (18–120), `bio` (≤2000), `city`, `gender`, `pseudonym` (≤40), `avatar` (full dict, see Avatar params), `transcript_visibility` (bool), `proxy_mode` (`"strict" | "mimic" | "free"` — UI labels: Grounded / Natural / Improv; default `"free"`). Returns the updated own-profile. `profile_live` is **never** patchable — only completing the interview flips it.

### POST /api/users/me/bio-suggestions → 200
```json
{ "suggestions": ["Ramen-powered CS student.", "Will debate you about jazz."] }
```
3–5 one-sentence bios grounded in training data. `409` if training isn't finished. Takes 1–3s (LLM call).

### Avatar params
All string values. Omitted keys fall back to defaults.

| key | values |
|---|---|
| `shape` | `blob` `round` `square` `bean` `egg` `heart` `tall` `cloud` |
| `eyes` | `dot` `happy` `star` `sleepy` `wink` `big` `side` `shades` `heart` `angry` `closed` |
| `mouth` | `smile` `open` `flat` `cat` `grin` `tongue` `smirk` `ooo` `frown` `wavy` |
| `acc` | `none` `sprout` `halo` `antenna` `bow` `crown` `flower` `headphones` `horns` `beanie` `tophat` `cap` `glasses` `mustache` `scarf` |
| `pattern` | `none` `spots` `stripes` `belly` `freckles` `patch` |
| `blush` | `off` `on` |
| `body`, `bg` | any hex color (pickers offer 14 each; see `webapp/static/app.js` for SVG part definitions to render natively) |

---

## Photos

### POST /api/users/me/photos (multipart, field `file`) → 201
```json
{ "photo_id": "ph_ab12...", "url": "/api/photos/ph_ab12..." }
```
jpeg/png/webp, ≤5MB, max 6 per user (`415`/`413`/`409`).

### DELETE /api/users/me/photos/{photo_id} → 204
### GET /api/photos/{photo_id} → image bytes (no auth; use directly as image URLs)

---

## Training (the interview)

The interview runs in **three phases**, all through the same chat endpoints:

1. **Identity intake** — a short static set of questions (name, hometown, current location, occupation, languages), asked word-for-word with no follow-ups. Everything except the name is later shared by the standin.
2. **Topic conversations ×3** — a `topic_choice` card offers preset topics (plus a free-text "write your own"); the user picks one via `POST /api/interview/topic`, then chats about it. The interviewer asks 3–5 natural follow-ups per topic, then the next topic card appears.
3. **SPC survey** — the structured cards (TIPI, PVQ, MBTI, loves/hates, routines), unchanged, plus a final "anything else" free-text question.

One response model everywhere:

```json
// InterviewOut
{
  "reply": "nice!! so what got you into that?",  // or null
  "complete": false,
  "profile_ready": false,       // true == training done, profile went live
  "asked_count": 4,
  "total_main_questions": 16,
  "question": null,             // or a survey/topic card object (below)
  "transcript": null            // only populated by GET /status
}
```

**Client loop:**
1. `POST /api/interview/message` `{"text": "..."}`.
2. Always show `reply` when non-null (it can accompany a card, e.g. a topic wrap-up remark). If `question` is `null` → keep chatting.
3. If `question` is an object → hide the composer, render the card by `question.type`. Submit survey cards via `POST /api/interview/answer` `{"question_id": "...", "answer": <shape below>}`; submit **`topic_choice`** cards via `POST /api/interview/topic` `{"question_id": "...", "topic": "..."}`. The response may chain directly into the next card.
4. `POST /api/interview/skip` (no body) skips the current **free-text** question (mid-topic it ends the topic early).
5. When `profile_ready` flips true, training is compiled and the profile is live. **The request that flips it takes 10–30s** — show a "building your standin" state, don't time out early, never resubmit.
6. On app launch, `GET /api/interview/status` → `transcript` (list of `{"role": "user"|"assistant", "content": "..."}`) and any pending `question` to restore state.

### Card types & answer shapes

`question` always has `question_id`, `type`, `prompt`, `optional` (bool), plus type-specific fields:

**`topic_choice`** — pick a conversation topic. Extra fields: `options` (preset topic strings) and `allow_custom: true` (render a free-text input too). Submit via `POST /api/interview/topic` `{"question_id": "...", "topic": "<preset or custom, ≤200 chars>"}` → the reply opens the topic conversation; continue via `/message`. Wrong/out-of-order card or blank topic → `422`.

**`likert_battery`** — Qualtrics-style matrix. Extra fields: `scale_labels` (7 strings, 1→7) and `items` (`[{"id": "tipi_1", "text": "Extraverted, enthusiastic."}, ...]`).
Answer: **every** item rated `{"tipi_1": 6, "tipi_2": 3, ..., "tipi_10": 4}`, ints 1–7. Missing/out-of-range → `422`.

**`list`** — extra field `min_items`. Answer: `["thing1", ...]` with at least `min_items` non-empty strings.

**`long_text`** — extra field `recommended_chars` (advisory; show a counter, don't block). Answer: non-empty string.

**`choice`** — extra field `options` (strings). Answer: one of the options, or `null` to skip **iff** `optional` is true (MBTI is).

### POST /api/interview/restart → 204
Wipes interview progress; the existing standin stays live on old training until the new interview completes.

---

## Discovery

### GET /api/explore → 200 ← use this for the browse feed
```json
{ "profiles": [
  { "user_id": "u_9d...", "pseudonym": "Mossy Otter",
    "avatar": { "...": "..." }, "profile_live": true,
    "transcript_visibility": true, "anonymous": true,
    "chips": ["You both love live music", "Similar weekly rhythm"] }
] }
```
Ranked by compatibility; `chips` (0–3 strings) explain why — render them on the card. Excludes: self, existing connections, people you already liked. People who liked *you* appear boosted with chip `"Liked your standin"`. **Never** contains name/age/city/photos. Some entries may lack `chips` (unranked fallback) — render without.

### GET /api/users/{target_id} → 200 — **two shapes**

**Anonymous** (stranger — the default):
```json
{ "user_id": "u_9d...", "pseudonym": "Mossy Otter", "avatar": { "..." : "..." },
  "profile_live": true, "transcript_visibility": true, "anonymous": true,
  "you_liked": false, "likes_you": true, "connected": false }
```

**Full** (yourself, or a mutual connection): the own-profile shape minus `email`, plus the same `you_liked`/`likes_you`/`connected` flags (absent on self-view), with `"anonymous": false`.

**Branch on `anonymous`, not on presence of fields.** `404` if the user doesn't exist or isn't live yet (except self-view, which always works).

### GET /api/users → plain unranked list, same card shape as explore minus `chips`. Legacy; prefer `/api/explore`.

---

## Standin chat

### GET /api/proxy/{target_id}/card → 200
```json
{ "pseudonym": "Mossy Otter", "age": 23,
  "location": "Ithaca, NY", "hometown": "Buffalo, NY", "occupation": "CS student",
  "interests": ["basketball", "ramen", "hiking"],
  "topics": ["What's your hottest take?", "dream vacations"] }
```
The chat-header summary: render it near the standin's name **above the chat box** so the viewer knows who they're talking to and where to start. `age`/`location`/`hometown`/`occupation` come from the identity intake (never the real name); `interests` are things they love from training; `topics` are the topics they chose to talk about. Any field may be `null`/empty for legacy or untrained profiles. `404` if the target isn't live (self-view always works).

### POST /api/proxy/{target_id}/message → 200
```json
// request  (omit conversation_id on the first message)
{ "text": "hey! what are you into?", "conversation_id": "px_7c31..." }
// response
{ "conversation_id": "px_7c31...", "reply": "hey!! i'm mossy otter...",
  "target_visibility_on": true }
```
**Echo `conversation_id` back on every subsequent message** or you'll start a fresh conversation. Replies take 2–6s — show a typing indicator and disable the composer meanwhile. `target_visibility_on: true` means the profile owner can read this conversation — you **must** surface that to the user (consent feature). Self-chat (target = yourself) is always allowed, even pre-live. `404` if the target isn't live. There is currently **no endpoint to fetch past proxy-chat transcripts** — keep them client-side per session.

---

## Social

### POST /api/likes/{target_id} → 200
```json
{ "mutual": false }   // true == connection just formed: unlock profiles + DMs
```
`400` liking yourself · `404` target not live. Idempotent.

### GET /api/connections → 200
```json
{
  "connections": [ /* FULL profile objects (see own-profile shape, no email) */ ],
  "incoming":   [ /* ANONYMOUS profile objects + "you_liked": false */ ]
}
```
`incoming` = people who liked you, still anonymous — show pseudonym/avatar with "chat with their standin" and "like back" actions.

### POST /api/messages/{peer_id} → 200 · GET /api/messages/{peer_id} → 200
```json
// POST request: { "text": "hey!" }   // POST response: the created message
// GET response:
{ "messages": [ { "from": "u_3f...", "to": "u_9d...",
                  "text": "hey!", "at": "2026-07-07T13:48:12.703136" } ] }
```
Both `403` unless mutually connected. **No push — poll GET every ~4s while the thread is open.** Messages are oldest-first, capped at the latest 200.

---

## Standin knowledge

### GET /api/knowledge → 200
```json
{ "items": [ { "id": "qa_1a2b3c4d", "question": "Have you lived abroad?",
               "answer": "I lived in Japan for two years.",
               "created_at": "1751897292.71" } ] }
```
Everything the user's standin can answer, newest first.

### PATCH /api/knowledge/{qa_id} `{"answer": "new text"}` → 204 (re-embedded; effective on the standin's next reply)
### DELETE /api/knowledge/{qa_id} → 204 (standin no longer knows it)

### GET /api/review → 200
```json
{ "questions": [ { "id": "665f0c...", "question": "what's your favorite color?",
                   "category": "preference", "created_at": "2026-07-07 13:20:11" } ] }
```
Questions people asked the standin that it couldn't answer.

### POST /api/review/{item_id}/answer `{"answer": "forest green"}` → 204
The answer becomes standin knowledge immediately; the item leaves the pending list. `404` unknown/already answered.

---

## The like arc (client state machine)

```
browse (/api/explore)             everyone anonymous
  └─ open profile (anonymous shape) ── chat with standin ── POST /api/likes/{id}
        mutual:false → they see you in GET /api/connections "incoming" (anonymous)
        they like back → mutual:true
              ├─ GET /api/users/{id} now returns the FULL shape (both directions)
              ├─ both appear in each other's "connections"
              └─ POST/GET /api/messages/{peer} unlocked (poll GET)
```

## Client checklist

- Branch profile rendering on `anonymous`, never on field presence.
- Echo `conversation_id`; disable composers while awaiting replies.
- Handle a `question` card in **every** interview response, including from `/answer` (cards chain) and `/status` (restore).
- Budget 30s for the final interview request; 2–6s for standin replies.
- Show the `target_visibility_on` indicator before/while chatting.
- Poll DMs at ~4s; stop when the thread closes.
- All errors: read `detail` and surface it — `422` details name the offending field.
