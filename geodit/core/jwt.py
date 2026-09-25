"""Read (never verify) JWT claims — expiry and user id of our own tokens."""

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
