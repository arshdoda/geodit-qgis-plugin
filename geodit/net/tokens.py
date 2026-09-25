"""Access/refresh token state, shared by every thread of one signed-in session.

* The access token (15 min in prod) is refreshed shortly before it expires and
  once on a 401. Refreshes are single-flight: one thread refreshes, the others
  wait on the lock and reuse the new token.
* ``/user/refresh`` returns a new access token and the SAME refresh token (no
  rotation); the session hard-expires 7 d (30 d with ``remember_me``) after
  login. A dead refresh token raises ``SessionExpired`` — the password is never
  replayed (7 failures lock the account on every device).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Tuple

from ..core import jwt as jwt_claims
from .errors import SessionExpired

REFRESH_BEFORE_EXPIRY_S = 60

Refresher = Callable[[str], Tuple[str, str]]


class TokenStore:
    def __init__(
        self,
        access: Optional[str] = None,
        refresh: Optional[str] = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._lock = threading.RLock()
        self._now = now
        self._access = access
        self._refresh = refresh
        self._dead = False

    # -- state -------------------------------------------------------------
    def set_tokens(self, access: str, refresh: str) -> None:
        with self._lock:
            self._access, self._refresh, self._dead = access, refresh, False

    def clear(self) -> None:
        with self._lock:
            self._access = self._refresh = None
            self._dead = False

    @property
    def refresh_token(self) -> Optional[str]:
        with self._lock:
            return self._refresh

    @property
    def has_session(self) -> bool:
        with self._lock:
            return bool(self._refresh) and not self._dead

    @property
    def is_dead(self) -> bool:
        with self._lock:
            return self._dead

    @property
    def user_id(self) -> Optional[int]:
        with self._lock:
            return jwt_claims.user_id(self._access) or jwt_claims.user_id(self._refresh)

    # -- access ------------------------------------------------------------
    def access_token(self, refresher: Refresher) -> str:
        with self._lock:
            if self._access is None or self._expires_soon(self._access):
                self._refresh_locked(refresher)
            if self._access is None:
                raise RuntimeError("no access token after refreshing")
            return self._access

    def force_refresh(self, refresher: Refresher, stale_access: Optional[str]) -> None:
        """Refresh after a 401 — unless another thread already replaced the
        token that got the 401."""
        with self._lock:
            if self._access is not None and self._access != stale_access:
                return
            self._refresh_locked(refresher)

    def _expires_soon(self, token: str) -> bool:
        exp = jwt_claims.expires_at(token)
        return exp is not None and exp - self._now() < REFRESH_BEFORE_EXPIRY_S

    def _refresh_locked(self, refresher: Refresher) -> None:
        if self._dead or not self._refresh:
            raise SessionExpired()
        try:
            access, refresh = refresher(self._refresh)
        except SessionExpired:
            self._dead = True
            self._access = None
            raise
        self._access = access
        self._refresh = refresh or self._refresh
