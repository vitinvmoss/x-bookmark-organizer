"""Single-account auth: env-based credentials + signed Flask sessions + CSRF.

Env:
  APP_USERNAME, APP_PASSWORD_HASH (werkzeug pbkdf2/scrypt hash, never plaintext),
  SESSION_SECRET (required in production).

No public registration. No X passwords/cookies collected.
"""
from __future__ import annotations
import hmac
import os
import secrets

from werkzeug.security import check_password_hash


def app_username():
    return os.environ.get("APP_USERNAME", "")


def password_configured():
    return bool(app_username() and os.environ.get("APP_PASSWORD_HASH"))


def verify_credentials(username, password):
    """Constant-shape verification; False when not configured."""
    expected_user = app_username()
    expected_hash = os.environ.get("APP_PASSWORD_HASH", "")
    if not expected_user or not expected_hash:
        return False
    user_ok = hmac.compare_digest(str(username or ""), expected_user)
    try:
        pass_ok = check_password_hash(expected_hash, str(password or ""))
    except Exception:
        pass_ok = False
    return bool(user_ok and pass_ok)


def session_secret():
    return os.environ.get("SESSION_SECRET", "")


def is_production():
    env = (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "").lower()
    if env in ("production", "prod"):
        return True
    return bool(os.environ.get("RENDER"))  # Render sets RENDER=true


def ensure_csrf_token(session):
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_hex(32)
        session["csrf_token"] = tok
    return tok


def valid_csrf(session, provided):
    expected = session.get("csrf_token") or ""
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))
