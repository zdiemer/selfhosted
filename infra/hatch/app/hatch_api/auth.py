"""Bearer-key auth with scopes.

Ported from infra/sms-relay/app/sms_relay/auth.py (a submodule, so this is a
copy rather than an import). The load-bearing detail is unchanged: compare
against EVERY configured key with hmac.compare_digest, because a dict lookup
would leak key material through timing.

Never accept a key as a query parameter. Traefik's JSON access log records
RequestPath including the query string, and those logs ship to Grafana Cloud.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, Header, HTTPException, status

from hatch_api.config import ApiKey, settings

logger = logging.getLogger(__name__)


async def resolve_caller(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ApiKey:
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_api_key and settings.accept_x_api_key:
        presented = x_api_key.strip()

    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    matched: ApiKey | None = None
    for candidate in settings.api_keys:
        if hmac.compare_digest(presented, candidate.key):
            matched = candidate
    if matched is None:
        logger.warning("rejected request with unknown API key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched


def require_scope(scope: str):
    """Dependency factory: 403 unless the caller's key carries `scope`."""

    async def _dep(caller: ApiKey = Depends(resolve_caller)) -> ApiKey:
        if scope not in caller.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this key does not carry the '{scope}' scope",
                headers={"X-Hatch-Code": "scope_required"},
            )
        return caller

    return _dep


require_read = require_scope("read")
require_act = require_scope("act")
