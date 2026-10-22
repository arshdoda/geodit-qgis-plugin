"""Read (never verify) JWT claims — expiry, user and session id of our own tokens."""

from __future__ import annotations

import base64
import json
from typing import Optional


def decode_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (IndexError, ValueError, TypeError) as exc:
        raise ValueError("malformed token") from exc


def expires_at(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    try:
        exp = decode_claims(token).get("exp")
    except ValueError:
        return None
    return int(exp) if isinstance(exp, (int, float)) else None


def user_id(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    try:
        uid = decode_claims(token).get("user_id")
    except ValueError:
        return None
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def session_id(token: Optional[str]) -> Optional[int]:
    """The server's session id (``sid``): what ``DELETE /user/sessions/{sid}``
    ends. Not a secret — only the session's own user can end it."""
    if not token:
        return None
    try:
        sid = decode_claims(token).get("sid")
    except ValueError:
        return None
    try:
        return int(sid) if sid is not None else None
    except (TypeError, ValueError):
        return None


def generation(token: Optional[str]) -> Optional[int]:
    """A refresh token's generation within its session (``gen``, which api-v2's
    rotation counts up; a token from sign-in, or from a server that doesn't
    rotate, has none: 0). None when the token can't be read."""
    if not token:
        return None
    try:
        gen = decode_claims(token).get("gen", 0)
    except ValueError:
        return None
    return gen if isinstance(gen, int) and not isinstance(gen, bool) else None


def later_generation(token: Optional[str], than: Optional[str]) -> bool:
    """``token`` is a later generation of ``than``'s session: the same session
    id and a higher ``gen``."""
    sid = session_id(token)
    if sid is None or sid != session_id(than):
        return False
    gen, other = generation(token), generation(than)
    return gen is not None and other is not None and gen > other
