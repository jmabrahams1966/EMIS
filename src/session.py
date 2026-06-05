"""HMAC-signed session cookies + PKCE helpers for Web UI SSO.

The Web UI Lambda needs a way to:
  1. Remember "this browser is signed in as user_id X" across requests.
  2. Carry an OAuth PKCE code_verifier between /login and /auth/callback
     without storing per-request state server-side.

Both are solved with HMAC-signed cookies using the existing ``WEB_UI_TOKEN``
env var as the secret. Sessions last 7 days by default; the login-state
cookie lasts 10 minutes (enough to complete an OAuth round-trip).

Format::

    cookie value = base64url(payload).base64url(hmac-sha256(payload, secret))

where ``payload`` is JSON like ``{"u":"<user_id>","exp":<unix>}`` for sessions
and ``{"v":"<pkce_verifier>","n":"<nonce>","exp":<unix>}`` for login state.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

SESSION_COOKIE = "emis_session"
LOGIN_STATE_COOKIE = "emis_login_state"

SESSION_TTL_SEC = 7 * 24 * 3600       # 7 days
LOGIN_STATE_TTL_SEC = 10 * 60          # 10 minutes


def _secret() -> bytes:
    s = os.getenv("WEB_UI_TOKEN", "")
    if not s:
        raise RuntimeError("WEB_UI_TOKEN env var is empty; session signing impossible")
    return s.encode("utf-8")


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _sign(payload: dict[str, Any]) -> str:
    """Return ``payload_b64.sig_b64`` (signed token string)."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    p = _b64u_encode(raw)
    sig = hmac.new(_secret(), p.encode("ascii"), hashlib.sha256).digest()
    return p + "." + _b64u_encode(sig)


def _verify(token: str) -> dict[str, Any] | None:
    """Return the payload if signature + expiry are valid, else None."""
    if not token or token.count(".") != 1:
        return None
    p, sig = token.split(".", 1)
    expected = hmac.new(_secret(), p.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u_encode(expected), sig):
        return None
    try:
        payload = json.loads(_b64u_decode(p))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp", 0)
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return payload


# ── Cookie parsing ───────────────────────────────────────────────────────

def parse_cookies(event: dict[str, Any]) -> dict[str, str]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = headers.get("cookie", "") or ""
    out: dict[str, str] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ── Session (user signed in) ─────────────────────────────────────────────

def make_session_cookie(user_id: str, ttl_sec: int = SESSION_TTL_SEC) -> str:
    """Return a ``Set-Cookie`` header value for a new signed session."""
    token = _sign({"u": user_id, "exp": int(time.time()) + ttl_sec})
    return (
        f"{SESSION_COOKIE}={token}; "
        f"Path=/; HttpOnly; Secure; SameSite=Lax; "
        f"Max-Age={ttl_sec}"
    )


def clear_session_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"


def session_user_id(event: dict[str, Any]) -> str | None:
    """Return the user_id from a valid session cookie, or None."""
    cookies = parse_cookies(event)
    payload = _verify(cookies.get(SESSION_COOKIE, ""))
    return payload.get("u") if payload else None


# ── PKCE login state (carries code_verifier across OAuth round-trip) ─────

def make_login_state() -> tuple[str, str, str]:
    """Mint a fresh PKCE pair + signed state cookie.

    Returns ``(code_challenge, state_param, set_cookie_value)``:
      - ``code_challenge`` goes into the Azure authorize URL
      - ``state_param`` is a short nonce echoed back by Azure on callback
      - ``set_cookie_value`` is a ``Set-Cookie`` header carrying the verifier
    """
    verifier = secrets.token_urlsafe(48)
    challenge = _b64u_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    nonce = secrets.token_urlsafe(16)
    token = _sign({"v": verifier, "n": nonce, "exp": int(time.time()) + LOGIN_STATE_TTL_SEC})
    cookie = (
        f"{LOGIN_STATE_COOKIE}={token}; "
        f"Path=/; HttpOnly; Secure; SameSite=Lax; "
        f"Max-Age={LOGIN_STATE_TTL_SEC}"
    )
    return challenge, nonce, cookie


def consume_login_state(event: dict[str, Any], expected_nonce: str) -> str | None:
    """Verify the PKCE state cookie matches ``expected_nonce`` and return the
    code_verifier. Returns None if missing/expired/tampered/nonce-mismatch."""
    cookies = parse_cookies(event)
    payload = _verify(cookies.get(LOGIN_STATE_COOKIE, ""))
    if not payload:
        return None
    if payload.get("n") != expected_nonce:
        return None
    return payload.get("v")


def clear_login_state_cookie() -> str:
    return (
        f"{LOGIN_STATE_COOKIE}=; Path=/; HttpOnly; Secure; "
        f"SameSite=Lax; Max-Age=0"
    )
