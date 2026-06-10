# Deployment Guide — Proxy Social Prototype (new Heroku app + new MongoDB)

End-to-end setup for a fresh deployment. Nothing from the old Slack deployment
is required. Time estimate: ~30 minutes.

---

## Part 1 — MongoDB Atlas (new database)

### 1.1 Create the cluster
1. Sign in at https://cloud.mongodb.com (create an account/org/project if needed).
2. **Create a cluster** → the **M0 Free** tier is fine for the prototype.
   Pick a region close to your Heroku region (e.g. AWS us-east-1).
3. Wait for the cluster to provision (~2 min).

### 1.2 Database user + network access
1. **Database Access → Add New Database User**: username + a strong password,
   role **Read and write to any database**. Save the password.
2. **Network Access → Add IP Address → Allow access from anywhere**
   (`0.0.0.0/0`). Heroku dynos have no fixed IPs, so this is required.
   (The DB is still protected by the username/password.)

### 1.3 Connection string — DATABASE NAME IS REQUIRED
1. Cluster → **Connect → Drivers** → copy the `mongodb+srv://...` string.
2. Insert your password AND **add a database name to the path** before the `?`:

   ```
   mongodb+srv://<user>:<password>@<cluster>.mongodb.net/proxyapp?retryWrites=true&w=majority
   ```

   ⚠️ The app resolves its database via the URI path (`get_database()` with no
   argument). Without `/proxyapp` (any name you like) in the path, every DB
   call fails. This is the most common misconfiguration — double-check it.

3. Collections (`users`, `auth_tokens`, `photos`, `conversations`,
   `interviews`, `proxy_sessions`, `qa_pairs`, `unanswered_questions`) are
   created automatically on first write. No schema setup needed.

### 1.4 Vector search index (required for proxy RAG)
The proxy retrieves training facts via Atlas Vector Search.

**1.4.0 — Create the database + collection first.** On a brand-new cluster the
search-index option doesn't appear until at least one collection exists, so
Atlas only offers "Add my own data" / "Load sample dataset". Do this:

1. Cluster → **Browse Collections** → **Add My Own Data** (do NOT load the
   sample dataset — it's hundreds of MB of unrelated data).
2. Database name: the SAME name you put in the URI path (e.g. `proxyapp`).
   Collection name: `qa_pairs`. → **Create**.
   (An empty collection is fine — the index can exist before any data.)

   *Alternative:* skip this and deploy the app first (Part 2); collections are
   created automatically once you sign up and finish an interview. The app runs
   fine in the meantime — proxy replies just lack RAG grounding (you'll see
   `[RAG] retrieval failed` warnings) until the index exists. Then come back
   and do 1.4.1.

**1.4.1 — Create the index.**

1. Cluster → **Atlas Search** (or "Search & Vector Search") → **Create Search
   Index** → **Atlas Vector Search** → **JSON Editor**.
2. Database: your DB name (e.g. `proxyapp`). Collection: `qa_pairs`.
   Index name: **`qa_emb_index`** (must match exactly).
3. Definition:

   ```json
   {
     "fields": [
       { "type": "vector", "path": "embedding", "numDimensions": 1536, "similarity": "cosine" },
       { "type": "filter", "path": "user_id" }
     ]
   }
   ```

4. Create. Status should become *Active* in ~1 minute.

If this index is missing the app still runs — proxy answers just lose RAG
grounding (you'll see `[RAG] retrieval failed` warnings in logs).

---

## Part 2 — Heroku (new app)

### 2.1 Prerequisites
- Heroku CLI installed (`brew install heroku/brew/heroku` on macOS) and `heroku login`.
- An OpenAI API key.

### 2.2 Repo must contain
```
core/                  # engine package (incl. questions_v2.json)
webapp/                # FastAPI app (app.py, auth.py, users.py, __init__.py)
commons/               # existing (db.py, spc_pipeline.py, ...)
proxy_bot/             # existing (prompts, rag/, question_categories.py)
learning_bot/          # existing (learning_bot_prompt.py used by core)
tests_core/            # test suites
Procfile               # web: uvicorn webapp.app:app --host 0.0.0.0 --port $PORT --workers 1
requirements.txt       # includes fastapi, uvicorn, python-multipart, email-validator
runtime.txt            # python-3.11.9
```
The old Slack files (`combined_app.py`, controllers, `commons/slack.py`) can
stay in the repo — they're simply no longer started by the Procfile.

### 2.3 Create the app and set config vars
```bash
cd <your-repo>
heroku create <your-app-name>

heroku config:set MONGODB_URI='mongodb+srv://<user>:<password>@<cluster>.mongodb.net/proxyapp?retryWrites=true&w=majority'
heroku config:set OPENAI_API_KEY='sk-...'

# optional model overrides (defaults shown)
# heroku config:set PROXY_MODEL=gpt-5.4 LEARNING_MODEL=gpt-5-mini SPC_MODEL=gpt-5.4

# once anyone besides you can reach the URL:
heroku config:set ALLOW_DEV_USER_HEADER=false
```

| Var | Required | Purpose |
|---|---|---|
| `MONGODB_URI` | yes | Atlas string **with DB name in path** |
| `OPENAI_API_KEY` | yes | proxy replies, interview turns, SPC profile, embeddings |
| `PROXY_MODEL` | no | proxy reply model (default `gpt-5.4`) |
| `LEARNING_MODEL` | no | interview + anything-else decomposition (default `gpt-5-mini`) |
| `SPC_MODEL` | no | personality profile generation (default `gpt-5.4`) |
| `ALLOW_DEV_USER_HEADER` | no | default `true`; **set `false` on shared deployments** — the X-User-Id fallback lets anyone impersonate anyone |

### 2.4 Deploy
```bash
git add -A && git commit -m "web prototype"
git push heroku main        # or: git push heroku <branch>:main
heroku ps:scale web=1       # MUST stay 1: in-process write-through caches
heroku logs --tail          # watch for "Uvicorn running"
```

### 2.5 Verify
```bash
curl https://<your-app-name>.herokuapp.com/api/health   # -> {"ok": true}
```
Open `https://<your-app-name>.herokuapp.com/docs` for the interactive API.

---

## Part 3 — First profile, end to end (via /docs)

1. **POST /api/auth/signup** — body:
   `{"email": "you@x.com", "password": "********", "name": "Your Name", "age": 23}`
   → copy the `token` and `user_id` from the response.
2. For every authenticated call below, fill the `authorization` parameter with:
   `Bearer <token>`
3. *(optional)* **PATCH /api/users/me** — set `bio`, and
   `"transcript_visibility": true` if you want chat partners to see the
   review-indicator.
4. **POST /api/users/me/photos** — upload up to 6 (jpeg/png/webp, ≤5MB each).
5. **POST /api/interview/message** — `{"text": "hi"}` starts the interview.
   Keep chatting through the free-text questions.
6. When a response contains a `question` object, answer via
   **POST /api/interview/answer** instead:
   - TIPI: `{"question_id": "SPC_TIPI", "answer": {"tipi_1": 6, "tipi_2": 3, ... "tipi_10": 4}}` (all 10, values 1–7)
   - PVQ: same shape, `pvq_1`..`pvq_21`
   - MBTI: `{"question_id": "SPC_MBTI", "answer": "INTP"}` or `"answer": null` to skip
   - loves/hates: `{"question_id": "SPC_LOVES", "answer": ["a","b","c","d","e"]}` (≥5)
   - routines: `{"question_id": "SPC_WEEKDAY", "answer": "I wake up at..."}`
   Each structured answer returns the next card directly.
7. The final answer flips `profile_ready: true`. **That request takes ~10–30s**:
   it scores the batteries, generates your personality profile, decomposes your
   "anything else" answer into QA pairs, embeds everything, and sets your
   profile live. Don't resubmit; let it finish.
8. **POST /api/proxy/{your_user_id}/message** — `{"text": "hey! what are you into?"}`
   → your proxy answers as you. Reuse the returned `conversation_id` in
   subsequent messages to continue the same conversation.
9. Second account: sign up again (new email), find the first profile via
   **GET /api/users**, and chat with its proxy.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 500s + `MONGODB_URI not set` / DB errors in logs | config var missing, or **no DB name in the URI path** |
| Proxy replies feel generic, `[RAG] retrieval failed` in logs | `qa_emb_index` missing/misnamed, or wrong collection/DB |
| `Invalid or expired token` | token wasn't prefixed with `Bearer ` in the header |
| Final interview request times out (H12, 30s) | compile step exceeded Heroku's 30s router limit — retry once (work continues server-side); if persistent, we move compilation to a background thread (small change, ask me) |
| Photo upload 415/413 | wrong content type or >5MB |
| State lost after dyno restart | expected for *in-flight* turns only; sessions/interviews persist via Mongo. Ensure `web=1`. |

## Limits to remember (prototype-by-design)
- **Single worker only** (`--workers 1`): write-through caches assume one process.
- Free/eco dynos sleep after 30 min idle; first request after sleep is slow.
- Photos live in Mongo (fine at this scale; swap to S3/Cloudinary later).
- Bearer-token auth is the prototype stub — swap for Clerk/Auth0 before public.
