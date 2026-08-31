from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException, status

from leakproof.config import get_settings


@dataclass(frozen=True)
class OperatorPrincipal:
    """Server-resolved operator identity and its permitted merchant boundary."""

    merchant_ids: frozenset[str]
    all_merchants: bool = False

    def permits(self, merchant_id: str) -> bool:
        return self.all_merchants or merchant_id in self.merchant_ids


def get_operator_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> OperatorPrincipal:
    settings = get_settings()
    configured_token = settings.operator_api_token
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator authentication is not configured",
        )

    scheme, separator, supplied_token = (authorization or "").partition(" ")
    valid = (
        separator == " "
        and scheme.casefold() == "bearer"
        and bool(supplied_token)
        and secrets.compare_digest(
            supplied_token.encode("utf-8"), configured_token.encode("utf-8")
        )
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid operator credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    merchant_ids = settings.operator_merchant_scope
    return OperatorPrincipal(
        merchant_ids=frozenset(item for item in merchant_ids if item != "*"),
        all_merchants="*" in merchant_ids,
    )
