"""
Auth primitives for the lo-fi prototype. Stdlib only, no external services.

Scope honesty: this is deliberately minimal -- salted PBKDF2 password hashes
and opaque random bearer tokens stored hashed. It is fine for a prototype with
test users. Before anything public, swap the token layer for a managed
provider (Clerk/Auth0/Supabase): only `get_current_user` in webapp/app.py and
the signup/login routes touch this module, so the swap is contained.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations_s, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_s),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def mint_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Tokens are stored hashed so a DB leak doesn't leak live credentials.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
