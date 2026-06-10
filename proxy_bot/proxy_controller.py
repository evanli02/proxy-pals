import os
import threading
import logging
from http import HTTPStatus

from flask import Flask, request, jsonify

from commons.clients import make_slack_client, get_signing_secret
from commons.db import get_conversations_collection
from commons.slack import verify_slack_signature
from proxy_bot.proxy_bot_service import (
    handle_event,
    update_partner_map_from_csv,
    process_clear_mode_if_requested,
)
from commons.history import archive_and_clear_history
from proxy_bot.rag.qa_extract import extract_qa_pairs_from_conversation
from proxy_bot.rag.store import upsert_qa_items
from transform_csv.export_utils import export_conversations_to_excel_and_upload

app = Flask(__name__)
log = logging.getLogger("proxy_controller")

# ---- Clients --------------------------------------------------------------
web, BOT_USER_ID = make_slack_client("PROXY_SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = get_signing_secret("PROXY_SLACK_SIGNING_SECRET")


@app.route("/proxy/slack/partners", methods=["POST"])
def set_partners():
    """Slash command endpoint to set partner mappings from CSV-like input.

    Expected text payload lines: user_name,partner_name,mode
    - user_name and partner_name refer to Slack profile real_name or display_name
    - As a fallback, literal Slack user IDs (e.g., U0ABC...) are also accepted
    - mode in {strict, mimicking} and is optional (defaults to mimicking)
    """
    # Verify Slack signature
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    body = request.get_data(as_text=True)
    if not verify_slack_signature(body, timestamp, signature, SLACK_SIGNING_SECRET):
        return jsonify({"error": "Invalid signature"}), 403

    text = (request.form.get("text") if request.form is not None else None) or (
        (request.json or {}).get("text") if request.is_json else ""
    )
    text = text or ""
    
    try:
        text = process_clear_mode_if_requested(text)
    except Exception as e:
        log.error(f"Failed to process clear mode: {e}")
        return jsonify({"response_type": "ephemeral", "text": "Failed to update the data. Please try again."})

    updated, errors = update_partner_map_from_csv(text)
    err_count = len(errors)
    err_snippet = "\n".join(errors[:5]) if errors else ""
    summary = (
        f"Updated {updated} partner mapping(s)."
        + (f"\nErrors ({err_count}):\n{err_snippet}" if err_count else "")
    )
    # Respond ephemerally to the command invoker
    return jsonify({"response_type": "ephemeral", "text": summary})


@app.route("/proxy/slack/export", methods=["POST"])
def export_data():
    """Slash command endpoint to export conversation data to CSV."""
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    body = request.get_data(as_text=True)
    if not verify_slack_signature(body, timestamp, signature, SLACK_SIGNING_SECRET):
        return jsonify({"error": "Invalid signature"}), 403

    text = (request.form.get("text") if request.form is not None else None) or (
        (request.json or {}).get("text") if request.is_json else ""
    )
    text = text or ""
    
    if text.strip() == "123456":
        channel_id = request.form.get("channel_id")
        if not channel_id and request.is_json:
            channel_id = request.json.get("channel_id")
            
        if channel_id:
            threading.Thread(
                target=export_conversations_to_excel_and_upload,
                args=(web, channel_id)
            ).start()
            return jsonify({"response_type": "ephemeral", "text": "Starting Excel export. The file will be uploaded shortly."})
        else:
            return jsonify({"response_type": "ephemeral", "text": "Could not find channel ID."})
    else:
        return jsonify({"response_type": "ephemeral", "text": "Incorrect password or command."})


@app.route("/proxy/slack/events", methods=["POST"])
def proxy_slack_events():
    data = request.json or {}

    # Signature verification
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    body = request.get_data(as_text=True)
    if not verify_slack_signature(body, timestamp, signature, SLACK_SIGNING_SECRET):
        return jsonify({"error": "Invalid signature"}), 403

    # URL verification
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        if event.get("type") == "message":
            t = threading.Thread(target=handle_event, args=(event,))
            t.start()
        return jsonify({"status": "ok"})

    return jsonify({"status": "ignored"})


@app.route("/proxy/health", methods=["GET"])
def proxy_health():
    return jsonify({
        "status": "healthy",
        "bot_user_id": BOT_USER_ID
    })


@app.route("/proxy", methods=["GET"])
def proxy_home():
    return jsonify({"status": "running", "bot": "Proxy Bot", "bot_user_id": BOT_USER_ID})

_RAG_REINDEX_LOCK = threading.Lock()
_RAG_REINDEX_RUNNING = False

@app.route("/proxy/rag/reindex", methods=["GET"])
def proxy_rag_reindex_async_all():
    global _RAG_REINDEX_RUNNING

    def _job():
        global _RAG_REINDEX_RUNNING
        try:
            conv_col = get_conversations_collection()
            query = {}  # 전체 스캔
            proj = {
                "messages": 1,
                "updated_at": 1,
                "user_id": 1,
                "channel_id": 1,
            }

            stats = {
                "conversations_scanned": 0,
                "conversations_with_qa": 0,
                "qa_extracted": 0,
                "qa_upserted": 0,
            }

            cursor = conv_col.find(query, projection=proj).batch_size(200)
            for conv in cursor:
                stats["conversations_scanned"] += 1
                qa_items = extract_qa_pairs_from_conversation(conv)
                if not qa_items:
                    continue
                stats["conversations_with_qa"] += 1
                stats["qa_extracted"] += len(qa_items)
                upsert_qa_items(qa_items)
                stats["qa_upserted"] += len(qa_items)

            log.info(f"[RAG REINDEX] Finished: {stats}")
        except Exception as e:
            log.exception(f"[RAG REINDEX] Error: {e}")
        finally:
            with _RAG_REINDEX_LOCK:
                _RAG_REINDEX_RUNNING = False

    with _RAG_REINDEX_LOCK:
        if _RAG_REINDEX_RUNNING:
            return jsonify({"status": "already_running"}), HTTPStatus.ACCEPTED
        _RAG_REINDEX_RUNNING = True

    t = threading.Thread(target=_job, daemon=True)
    t.start()
    return jsonify({"status": "started"}), HTTPStatus.ACCEPTED

if __name__ == "__main__":
    log.info("Starting Proxy Bot controller")
    port = int(os.environ.get("PORT", 5002))
    app.run(host="0.0.0.0", port=port, debug=False)
