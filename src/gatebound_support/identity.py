"""Identity token verification (SPEC §2).

JWT, HS256, key SUPPORT_IDENTITY_SECRET, issuer "gatebound-web", audience
"gatebound-support". An invalid or missing token is never an error here — it just means
"not logged in"; callers decide what to do about it.
"""

from __future__ import annotations

from typing import TypedDict

import jwt

from .settings import Settings, enabled


class Identity(TypedDict):
    account_id: str
    name: str


def verify_identity_token(token: str | None, settings: Settings) -> Identity | None:
    if not token or not enabled(settings.SUPPORT_IDENTITY_SECRET):
        return None
    try:
        payload = jwt.decode(
            token,
            settings.SUPPORT_IDENTITY_SECRET,
            algorithms=["HS256"],
            issuer="gatebound-web",
            audience="gatebound-support",
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    return {"account_id": str(sub), "name": str(payload.get("name") or "")}
