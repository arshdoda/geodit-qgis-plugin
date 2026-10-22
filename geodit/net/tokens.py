"""Access/refresh token state, shared by every thread of one signed-in session.

* The access token (15 min in prod) is refreshed shortly before it expires and
  once on a 401. Refreshes are single-flight: one thread refreshes, the others
  wait on the lock and reuse the new token. Reading the tokens never waits for
  a refresh in flight: the state is a snapshot, replaced whole.
* ``/user/refresh`` returns a new access token and the same refresh token —
  or, where api-v2 rotates desktop sessions (``REFRESH_TOKEN_ROTATION``), the
  session's next generation (``gen``; the same ``sid`` and ``exp``). The token
  it replaced is still answered for 5 minutes; any other replay ends the
  session. So only the newest token may be used, and a kept session ("Stay
  signed in") shares it with the password store through its ``Keeper``: each
  refresh first takes a later generation another QGIS window stored, and
  reports a token it got back. The session hard-expires 7 d (30 d with
  ``remember_me``) after login either way.
* A dead refresh token raises ``SessionExpired`` — the password is never
  replayed (7 failures lock the account on every device).
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable, NamedTuple, Optional, Protocol, Tuple

from ..core import jwt as jwt_claims
from .errors import SessionExpired

REFRESH_BEFORE_EXPIRY_S = 60

Refresher = Callable[[str], Tuple[str, str]]

# The password store's copy of a kept session's refresh token, next to ours (``stored_copy``).
STORED_OLDER = "older"  # an earlier generation of our session: ours replaces it
STORED_CURRENT = "current"  # ours, or a generation at least as new (another window rotated)
STORED_OTHER = "other"  # another session's: another window kept another sign-in
STORED_NONE = "none"  # nothing stored (signed out in another window), or unreadable


def stored_copy(ours: str, stored: Optional[str]) -> str:
    """What the stored token is next to ``ours`` (``STORED_*``). A token the
    server answered again within its grace carries the same generation as
    different bytes: current, not older."""
    if not stored:
        return STORED_NONE
    if stored == ours:
        return STORED_CURRENT
    sid = jwt_claims.session_id(ours)
    if sid is None or jwt_claims.session_id(stored) != sid:
        return STORED_OTHER
    return STORED_OLDER if jwt_claims.later_generation(ours, stored) else STORED_CURRENT


class Keeper(Protocol):
    """A kept session's link to the password store (the plugin's). Both calls
    come from the refreshing thread, under the store's lock."""

    def latest(self) -> Optional[str]:
        """The stored refresh token, never prompting; None when unreadable."""
        ...

    def rotated(self) -> None:
        """A refresh returned a new refresh token; must return at once."""
        ...


class _State(NamedTuple):
    access: Optional[str]
    refresh: Optional[str]
    dead: bool = False


class TokenStore:
    def __init__(
        self,
        access: Optional[str] = None,
        refresh: Optional[str] = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._lock = threading.RLock()  # held for a whole refresh: single-flight
        self._now = now
        self._state = _State(access, refresh)  # replaced whole, under the lock; read without it
        self._keeper: Optional[Keeper] = None

    # -- state -------------------------------------------------------------
    def set_tokens(self, access: Optional[str], refresh: str) -> None:
        with self._lock:
            self._state = _State(access, refresh)

    def clear(self) -> None:
        with self._lock:
            self._state = _State(None, None)
            self._keeper = None

    def keep(self, keeper: Optional[Keeper]) -> None:
        """Share the refresh token with the password store while the session is
        kept ("Stay signed in"); None when it no longer is. Never waits."""
        self._keeper = keeper

    @property
    def keeper(self) -> Optional[Keeper]:
        return self._keeper

    @property
    def kept(self) -> bool:
        return self._keeper is not None

    @property
    def refresh_token(self) -> Optional[str]:
        return self._state.refresh

    @property
    def has_session(self) -> bool:
        state = self._state
        return bool(state.refresh) and not state.dead

    @property
    def is_dead(self) -> bool:
        return self._state.dead

    @property
    def user_id(self) -> Optional[int]:
        state = self._state
        return jwt_claims.user_id(state.access) or jwt_claims.user_id(state.refresh)

    @property
    def session_id(self) -> Optional[int]:
        state = self._state
        return jwt_claims.session_id(state.refresh) or jwt_claims.session_id(state.access)

    def settled_refresh(self, timeout: float) -> Optional[str]:
        """The refresh token once a refresh in flight has landed — waiting at
        most ``timeout`` seconds, the token as it is after that. For storing it
        as QGIS quits."""
        if self._lock.acquire(timeout=timeout):
            self._lock.release()
        return self._state.refresh

    # -- access ------------------------------------------------------------
    def access_token(self, refresher: Refresher) -> str:
        with self._lock:
            access = self._state.access
            if access is None or self._expires_soon(access):
                self._refresh_locked(refresher)
                access = self._state.access
            if access is None:
                raise RuntimeError("no access token after refreshing")
            return access

    def force_refresh(self, refresher: Refresher, stale_access: Optional[str]) -> None:
        """Refresh after a 401 — unless another thread already replaced the
        token that got the 401."""
        with self._lock:
            access = self._state.access
            if access is not None and access != stale_access:
                return
            self._refresh_locked(refresher)

    def _expires_soon(self, token: str) -> bool:
        exp = jwt_claims.expires_at(token)
        return exp is not None and exp - self._now() < REFRESH_BEFORE_EXPIRY_S

    def _refresh_locked(self, refresher: Refresher) -> None:
        state = self._state
        if state.dead or not state.refresh:
            raise SessionExpired()
        keeper = self._keeper
        sent = state.refresh
        if keeper is not None:
            stored = None
            with contextlib.suppress(Exception):  # an unreadable store leaves ours in use
                stored = keeper.latest()
            if jwt_claims.later_generation(stored, sent):
                sent = stored  # another QGIS window rotated this session since: its token is the live one
        try:
            access, refresh = refresher(sent)
        except SessionExpired:
            self._state = _State(None, sent, dead=True)
            raise
        refresh = refresh or sent
        self._state = _State(access, refresh)
        if keeper is not None and refresh != state.refresh:
            with contextlib.suppress(Exception):
                keeper.rotated()
