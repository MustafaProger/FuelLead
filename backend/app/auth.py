from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from hashlib import sha256
from hmac import compare_digest, new as new_hmac
import json

from app.config import Settings


AUTH_COOKIE_NAME = "fuellead_session"
AUTH_TOKEN_VERSION = 1


def credentials_match(email: str, password: str, settings: Settings) -> bool:
    if not settings.auth_configured:
        return False
    expected_email = settings.fuellead_auth_email.strip().casefold()
    supplied_email = email.strip().casefold()
    return compare_digest(supplied_email, expected_email) and compare_digest(
        password,
        settings.fuellead_auth_password,
    )


def _session_signing_key(settings: Settings) -> bytes:
    material = (
        f"{settings.fuellead_auth_session_secret}\0{settings.fuellead_auth_password}"
    ).encode("utf-8")
    return sha256(material).digest()


def create_session_token(email: str, settings: Settings) -> str:
    payload = json.dumps(
        {"email": email, "version": AUTH_TOKEN_VERSION},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded_payload = urlsafe_b64encode(payload).rstrip(b"=")
    signature = new_hmac(_session_signing_key(settings), encoded_payload, sha256).hexdigest()
    return f"{encoded_payload.decode('ascii')}.{signature}"


def session_email(token: str | None, settings: Settings) -> str | None:
    if not token or not settings.auth_configured:
        return None
    try:
        encoded_payload, supplied_signature = token.split(".", 1)
        expected_signature = new_hmac(
            _session_signing_key(settings),
            encoded_payload.encode("ascii"),
            sha256,
        ).hexdigest()
        if not compare_digest(supplied_signature, expected_signature):
            return None
        padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(urlsafe_b64decode(encoded_payload + padding).decode("utf-8"))
    except (BinasciiError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != AUTH_TOKEN_VERSION:
        return None
    if not compare_digest(
        str(payload.get("email", "")).strip().casefold(),
        settings.fuellead_auth_email.strip().casefold(),
    ):
        return None
    return settings.fuellead_auth_email.strip()
