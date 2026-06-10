import hmac, hashlib, logging

log = logging.getLogger("slack.py")


def verify_slack_signature(request_body: str, timestamp: str, signature: str, SLACK_SIGNING_SECRET: str) -> bool:
    """Verify Slack request signature."""
    if not SLACK_SIGNING_SECRET:
        log.warning("SLACK_SIGNING_SECRET not set, skipping signature verification")
        return True

    base = f"v0:{timestamp}:{request_body}".encode()
    my_sig = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(my_sig, signature)
