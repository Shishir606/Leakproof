from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, model_validator

from leakproof.models.domain import LeakType
from leakproof.models.resources import EntityRef, RecoveryPurpose, ResourceContract


class InvalidSessionToken(ValueError):
    pass


class SessionTokenExpired(InvalidSessionToken):
    pass


class InvalidRecoveryToken(ValueError):
    pass


class RecoveryTokenExpired(InvalidRecoveryToken):
    pass


class InvalidCheckoutPaymentSignature(ValueError):
    pass


@dataclass(frozen=True)
class SessionTokenClaims:
    session_id: str
    merchant_id: str
    expires_at: datetime


class RecoveryTokenClaims(ResourceContract):
    version: int = Field(ge=1, le=2)
    session_id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    scenario_type: LeakType
    entity: EntityRef
    purpose: RecoveryPurpose
    amount_paise: int | None = Field(default=None, gt=0, strict=True)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    expires_at: datetime

    @property
    def order_id(self) -> str | None:
        return self.entity.entity_id if self.entity.entity_type == "order" else None

    @model_validator(mode="after")
    def valid_binding(self):
        allowed = {
            RecoveryPurpose.ORDER_CHECKOUT: (
                "order",
                {LeakType.PAYMENT_FAILURE, LeakType.CHECKOUT_ABANDON},
            ),
            RecoveryPurpose.INVOICE_HOSTED_PAYMENT: ("invoice", {LeakType.INVOICE_OVERDUE}),
            RecoveryPurpose.SUBSCRIPTION_METHOD_UPDATE: (
                "subscription",
                {LeakType.SUBSCRIPTION_HALT, LeakType.MANDATE_BROKEN},
            ),
        }
        entity_type, scenarios = allowed[self.purpose]
        if self.entity.entity_type != entity_type or self.scenario_type not in scenarios:
            raise ValueError("recovery purpose does not match scenario and entity")
        if entity_type != "subscription" and (self.amount_paise is None or self.currency is None):
            raise ValueError("payment recovery requires amount and currency")
        if (self.amount_paise is None) != (self.currency is None):
            raise ValueError("amount and currency must be bound together")
        if self.expires_at.tzinfo is None:
            raise ValueError("expiry requires timezone")
        return self


def _key_material(secret: str) -> bytes:
    return (secret or "leakproof-simulation-only-signing-secret").encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
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


def issue_resource_recovery_token(claims: RecoveryTokenClaims, secret: str) -> str:
    if claims.version != 2:
        raise ValueError("new recovery tokens must use version 2")
    encoded = _b64encode(claims.model_dump_json().encode())
    signature = hmac.new(
        _key_material(secret), f"recovery-token-v2.{encoded}".encode(), hashlib.sha256
    ).digest()
    return f"v2.{encoded}.{_b64encode(signature)}"


def issue_recovery_token(
    session_id: str,
    merchant_id: str,
    order_id: str,
    amount_paise: int,
    currency: str,
    expires_at: datetime,
    secret: str,
    *,
    scenario_type: LeakType = LeakType.PAYMENT_FAILURE,
) -> str:
    """Compatibility helper for original-order callers; all new tokens are v2."""
    return issue_resource_recovery_token(
        RecoveryTokenClaims(
            version=2,
            session_id=session_id,
            merchant_id=merchant_id,
            scenario_type=scenario_type,
            entity=EntityRef(entity_type="order", entity_id=order_id),
            purpose=RecoveryPurpose.ORDER_CHECKOUT,
            amount_paise=amount_paise,
            currency=currency,
            expires_at=expires_at,
        ),
        secret,
    )


def verify_recovery_token(
    token: str, secret: str, *, now: datetime, expected_purpose: RecoveryPurpose | None = None
) -> RecoveryTokenClaims:
    try:
        parts = token.split(".")
        if len(parts) == 3 and parts[0] == "v2":
            version, encoded, encoded_signature = 2, parts[1], parts[2]
        elif len(parts) == 2:
            version, (encoded, encoded_signature) = 1, parts
        else:
            raise InvalidRecoveryToken("unsupported recovery token version")
        expected = hmac.new(
            _key_material(secret), f"recovery-token-v{version}.{encoded}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64decode(encoded_signature), expected):
            raise InvalidRecoveryToken("invalid recovery token")
        payload = json.loads(_b64decode(encoded))
        if version == 1:
            if set(payload) != {"purpose", "sid", "mid", "oid", "amount", "currency", "exp"}:
                raise InvalidRecoveryToken("invalid legacy token claims")
            if payload["purpose"] != "checkout_recovery":
                raise InvalidRecoveryToken("invalid legacy token purpose")
            claims = RecoveryTokenClaims(
                version=1,
                session_id=payload["sid"],
                merchant_id=payload["mid"],
                scenario_type=LeakType.PAYMENT_FAILURE,
                entity=EntityRef(entity_type="order", entity_id=payload["oid"]),
                purpose=RecoveryPurpose.ORDER_CHECKOUT,
                amount_paise=payload["amount"],
                currency=payload["currency"],
                expires_at=datetime.fromtimestamp(payload["exp"], UTC),
            )
        else:
            claims = RecoveryTokenClaims.model_validate(payload)
            if claims.version != 2:
                raise InvalidRecoveryToken("invalid recovery token version")
        if expected_purpose is not None and claims.purpose != expected_purpose:
            raise InvalidRecoveryToken("invalid recovery token purpose")
        if now.astimezone(UTC) >= claims.expires_at:
            raise RecoveryTokenExpired("recovery token expired")
        return claims
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        OSError,
        UnicodeDecodeError,
        binascii.Error,
    ) as exc:
        if isinstance(exc, InvalidRecoveryToken):
            raise
        raise InvalidRecoveryToken("invalid recovery token") from exc


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


def verify_checkout_payment_signature(
    order_id: str,
    payment_id: str,
    signature: str,
    key_secret: str,
) -> None:
    """Verify Razorpay Checkout proof using the server-owned order identifier."""
    if not order_id or not payment_id or not key_secret:
        raise InvalidCheckoutPaymentSignature("checkout payment signature is invalid")
    expected = hmac.new(
        key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature.casefold(), expected):
        raise InvalidCheckoutPaymentSignature("checkout payment signature is invalid")
