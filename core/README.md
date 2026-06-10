# `core/` — Slack-free proxy core (Phase 0)

This package extracts the proxy "brains" out of `proxy_bot_service.py` so they
run from a plain function call — no Slack, no module-global state, safe under
web concurrency.

## The model: a proxy is data, not a process

Two things, deliberately separated:

- **`ProxyDefinition`** — a user's *read-only* proxy data (style, personality,
  SPC/life context, sample messages). Built once after training, cached, and
  shared by any number of simultaneous viewers. `ProxyDefinitionCache` holds
  these; call `.invalidate(user_id)` when a user re-trains.
- **`ProxySession`** — one conversation's *mutable* state `(viewer, target,
  conversation)`, with its own lock. Different conversations never share state,
  so a target's proxy can be talked to by many viewers at once with no
  cross-talk. This replaces the old `channel_state` / `seen_events` /
  `_latest_user_ts` globals (which only worked because Slack gave one DM channel
  per pair).

Unlimited concurrency on a single target comes from definitions being immutable
and sessions being isolated — *not* from running one bot instance per user.

## Usage (what a web route calls)

```python
from core import ProxyEngine

engine = ProxyEngine()  # builds Mongo-backed cache + OpenAI LLM lazily

result = engine.respond(
    viewer_id="user_42",
    target_id="user_7",
    conversation_id="conv_42_7",
    text="hey, what are you into?",
    target_visibility_on=True,   # T's transcript-visibility toggle, snapshotted
)

result.reply_text            # -> send to the viewer
result.envelope              # -> ProxyResponse (category/action/confidence/...)
result.unanswered_question   # -> persist to T's gap queue if not None
```

`generate_reply(...)` is the underlying pure function if you want to drive it
yourself (tests, or **proxy-to-proxy** later: two definitions, no human).

## What's injectable (and why)

- **LLM** — `ProxyEngine(llm=...)`. Tests pass a fake; nothing hits the network.
- **Definition source** — `ProxyDefinitionCache(fetch_record=..., resolve_name=...)`.
  Default reads the same Mongo fields as the old `fetch_partner_context`. Point
  `fetch_record` at the coalesced question-bank/SPC store when that lands —
  the engine doesn't change.
- **Session store** — `InMemorySessionStore` for the prototype; a Mongo-backed
  store implements the same 2 methods (`get_or_create`, `save`) and swaps in.

## What's intentionally gone

The partner-map system (`/set-partners`, `users.json`, the CSV parser, the
strict/mimic/free CSV modes) is dropped: in the web app the target is whatever
profile the viewer opened, resolved at call time, mode defaults to `mimic`.

## Interview (learning bot) side

The onboarding interview is extracted the same way.

```python
from core import InterviewEngine

engine = InterviewEngine()  # loads questions.json + lazy OpenAI by default

r = engine.respond(user_id="user_42", text="hey")
r.reply_text       # the interviewer's next message
r.profile_ready    # True once every main question has been asked
r.complete         # True when there's nothing left to ask
```

- `run_interview_turn(...)` is the pure turn function (a faithful port of the
  old `_handle_message_internal` state machine: follow-up stays on the same
  main; otherwise advance and clear follow-ups; complete when no main remains).
- `QuestionBank` is injectable. `default_question_bank()` loads the existing
  `learning_bot/questions.json`. **This is where your coalesced bank plugs in:**
  build a `QuestionBank` from your merged set (interview questions + SPC items)
  and pass it to `InterviewEngine(bank=...)` — nothing else changes.
- Questions may optionally carry `type` ("free_text" | "scale" | "choice") and
  `feeds_spc` (bool). The engine ignores them; the onboarding UI uses `type` to
  render scale/choice items as tappable inputs (so validated SPC items stay
  scorable instead of being answered as free text).
- `profile_ready` is the go-live gate you described.
- The onboarding greeting is dropped from the engine — the UI shows it when the
  interview screen opens.

## Run the smoke tests (offline)

```bash
PYTHONPATH=. python3 tests_core/test_proxy_core_smoke.py
PYTHONPATH=. python3 tests_core/test_interview_core_smoke.py
```

## Next within Phase 0

1. **Mongo-backed stores** (`SessionStore` + interview store) so conversations
   and interview progress persist across requests.
2. Wire both engines behind FastAPI: `POST /proxy/{target_id}/message` and
   `POST /interview/message`.
3. Feed captured interview answers into the SPC step that produces
   `style_rules` / `personality` / `spc_raw.context` — the fields
   `ProxyDefinition` already reads.
