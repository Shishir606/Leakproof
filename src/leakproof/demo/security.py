from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken


class InvalidSessionToken(ValueError):
    pass


class SessionTokenExpired(InvalidSessionToken):
    pass


class InvalidRecoveryToken(ValueError):
    pass


class RecoveryTokenExpired(InvalidRecoveryToken):
    pass


@dataclass(frozen=True)
class SessionTokenClaims:
    session_id: str
    merchant_id: str
    expires_at: datetime


@dataclass(frozen=True)
class RecoveryTokenClaims:
    session_id: str
    merchant_id: str
    order_id: str
    amount_paise: int
    currency: str
    expires_at: datetime


def _key_material(secret: str) -> bytes:
    return (secret or "leakproof-simulation-only-signing-secret").encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if _b64encode(decoded) != value:
        raise binascii.Error("non-canonical base64url encoding")
    return decoded


def issue_session_token(
    session_id: str, merchant_id: str, expires_at: datetime, secret: str
) -> str:
    payload = json.dumps(
        {
            "purpose": "demo_session",
            "sid": session_id,
            "mid": merchant_id,
            "exp": int(expires_at.timestamp()),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _b64encode(payload)
    signature = hmac.new(
        _key_material(secret), f"session-token-v1.{encoded}".encode(), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_session_token(token: str, secret: str, *, now: datetime) -> SessionTokenClaims:
    try:
        encoded, encoded_signature = token.split(".", 1)
        signature = _b64decode(encoded_signature)
        expected = hmac.new(
            _key_material(secret), f"session-token-v1.{encoded}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise InvalidSessionToken("invalid session token")
        payload = json.loads(_b64decode(encoded))
        if payload.get("purpose") != "demo_session":
            raise InvalidSessionToken("invalid session token purpose")
        expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
        if now.astimezone(UTC) >= expires_at:
            raise SessionTokenExpired("session token expired")
        session_id = str(payload["sid"])
        merchant_id = str(payload["mid"])
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        OSError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        if isinstance(exc, InvalidSessionToken):
            raise
        raise InvalidSessionToken("invalid session token") from exc
    if not session_id or not merchant_id:
        raise InvalidSessionToken("invalid session token")
    return SessionTokenClaims(session_id, merchant_id, expires_at)


def issue_recovery_token(
    session_id: str,
    merchant_id: str,
    order_id: str,
    amount_paise: int,
    currency: str,
    expires_at: datetime,
    secret: str,
) -> str:
    if not session_id or not merchant_id or not order_id or amount_paise <= 0:
        raise ValueError("recovery token claims are incomplete")
    if len(currency) != 3 or not currency.isupper():
        raise ValueError("recovery token currency must be an uppercase ISO code")
    payload = json.dumps(
        {
            "purpose": "checkout_recovery",
            "sid": session_id,
            "mid": merchant_id,
            "oid": order_id,
            "amount": amount_paise,
            "currency": currency,
            "exp": int(expires_at.timestamp()),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _b64encode(payload)
    signature = hmac.new(
        _key_material(secret), f"recovery-token-v1.{encoded}".encode(), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_recovery_token(token: str, secret: str, *, now: datetime) -> RecoveryTokenClaims:
    try:
        encoded, encoded_signature = token.split(".", 1)
        signature = _b64decode(encoded_signature)
        expected = hmac.new(
            _key_material(secret), f"recovery-token-v1.{encoded}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise InvalidRecoveryToken("invalid recovery token")
        payload = json.loads(_b64decode(encoded))
        if payload.get("purpose") != "checkout_recovery":
            raise InvalidRecoveryToken("invalid recovery token purpose")
        expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
        if now.astimezone(UTC) >= expires_at:
            raise RecoveryTokenExpired("recovery token expired")
        session_id = str(payload["sid"])
        merchant_id = str(payload["mid"])
        order_id = str(payload["oid"])
        amount_paise = int(payload["amount"])
        currency = str(payload["currency"])
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        OSError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        if isinstance(exc, InvalidRecoveryToken):
            raise
        raise InvalidRecoveryToken("invalid recovery token") from exc
    if (
        not session_id
        or not merchant_id
        or not order_id
        or amount_paise <= 0
        or len(currency) != 3
        or not currency.isupper()
    ):
        raise InvalidRecoveryToken("invalid recovery token")
    return RecoveryTokenClaims(
        session_id,
        merchant_id,
        order_id,
        amount_paise,
        currency,
        expires_at,
    )


def recipient_hash(recipient: str, secret: str) -> str:
    return hmac.new(
        _key_material(secret), f"demo-recipient-v1:{recipient}".encode(), hashlib.sha256
    ).hexdigest()


def encrypt_recipient(recipient: str, secret: str) -> str:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(b"demo-recipient-encryption-v1:" + _key_material(secret)).digest()
    )
    return Fernet(key).encrypt(recipient.encode()).decode()


def decrypt_recipient(ciphertext: str, secret: str) -> str:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(b"demo-recipient-encryption-v1:" + _key_material(secret)).digest()
    )
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("recipient ciphertext is invalid") from exc
