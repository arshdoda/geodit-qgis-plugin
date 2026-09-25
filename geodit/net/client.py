"""Geodit API client — the endpoints the desktop plugin uses.

Contract notes (api-v2, verified against code):

* Every request sends ``X-Client-Type: desktop``. Never ``mobile``: a mobile
  login revokes the user's Android session (single-mobile-session rule).
* ``Authorization`` is the literal ``Bearer <access>`` — ``token_type`` in the
  login response is ``"JWT"`` and must not be used to build the header.
* Snowflake ids travel as strings; list params (``survey_area_geom_id``,
  ``feat_geom_id``) are JSON lists of INTEGERS (strings get a 422).
* ``last_fetched`` is epoch milliseconds (seconds silently match nothing).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

from ..core.clock import ServerClock
from ..core.policy import parse_retry_after
from ..core.schema import cursor_from_next
from .errors import ApiError, NetworkError, ServerTooOld, SessionExpired, Unauthorized, error_for_status
from .tokens import TokenStore
from .transport import HttpResponse, Transport

CLIENT_TYPE = "desktop"


@dataclass
class LoginResult:
    requires_2fa: bool
    access: Optional[str] = None
    refresh: Optional[str] = None
    mfa_token: Optional[str] = None


@dataclass
class Page:
    results: List[dict]
    cursor: Optional[str]  # cursor of the NEXT page, None when drained


def _ids_param(ids: Iterable[int]) -> str:
    return json.dumps([int(i) for i in ids], separators=(",", ":"))


class ApiClient:
    def __init__(
        self,
        base_url: str,
        transport: Transport,
        tokens: Optional[TokenStore] = None,
        clock: Optional[ServerClock] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.transport = transport
        self.tokens = tokens or TokenStore()
        self.clock = clock or ServerClock()
        # A QgsFeedback the owning task cancels: aborts the in-flight request.
        self.feedback = None
        # HTTP requests made through this client, and the time they took.
        self.requests = 0
        self.request_seconds = 0.0

    # ------------------------------------------------------------------ core
    def url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        url = self.base_url + path.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urlencode(clean, doseq=True)
        return url

    def call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        auth: bool = True,
        feedback=None,
    ) -> Any:
        url = self.url(path, params)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        token = self.tokens.access_token(self._refresh_access) if auth else None
        resp = self._send(method, url, payload, token, feedback)
        if resp.status == 401 and auth:
            # Access token rejected (revoked session keeps its access token
            # valid until expiry, so this is usually expiry skew): refresh once.
            self.tokens.force_refresh(self._refresh_access, token)
            token = self.tokens.access_token(self._refresh_access)
            resp = self._send(method, url, payload, token, feedback)
            if resp.status == 401:
                raise SessionExpired()
        return self._decode(resp)

    def _send(self, method, url, payload, token, feedback) -> HttpResponse:
        headers = {"Accept": "application/json", "X-Client-Type": CLIENT_TYPE}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        started = time.monotonic()
        try:
            resp = self.transport.request(method, url, headers, payload, feedback or self.feedback)
        finally:
            self.requests += 1
            self.request_seconds += time.monotonic() - started
        self.clock.observe_date_header(resp.headers.get("date"))
        return resp

    def _decode(self, resp: HttpResponse) -> Any:
        if resp.status == 0:
            raise NetworkError(resp.error or "")
        data: Any = None
        if resp.body:
            try:
                data = json.loads(resp.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = None
        if 200 <= resp.status < 300:
            return data
        message, errors = "", None
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("detail") or "")
            if isinstance(data.get("errors"), dict):
                errors = data["errors"]
        cls = error_for_status(resp.status)
        raise cls(
            message,
            status=resp.status,
            errors=errors,
            retry_after=parse_retry_after(resp.headers.get("retry-after")),
            request_id=resp.headers.get("x-request-id"),
        )

    def _refresh_access(self, refresh_token: str) -> Tuple[str, str]:
        # In the JSON body, never the URL: a query string lands in request and
        # access logs, and a refresh token is a 7-30 day credential.
        body = json.dumps({"refresh": refresh_token}).encode("utf-8")
        resp = self._send("POST", self.url("user/refresh"), body, None, None)
        if resp.status in (400, 401, 403):
            raise SessionExpired()
        if resp.status == 422:
            # Only a server that predates the body form answers 422 here (its
            # refresh still requires `?refresh=`); a current one 422s only a
            # request with no token at all, which is never sent.
            raise ServerTooOld(status=422, request_id=resp.headers.get("x-request-id"))
        data = self._decode(resp)
        return data["access"], data.get("refresh") or refresh_token

    # ------------------------------------------------------------------ auth
    def login(
        self,
        *,
        password: str,
        username: Optional[str] = None,
        phone_number: Optional[str] = None,
        country_code: str = "+91",
        remember_me: bool = False,
    ) -> LoginResult:
        body: Dict[str, Any] = {"password": password, "remember_me": bool(remember_me)}
        if username:
            body["username"] = username
        else:
            body["phone_number"] = phone_number
            body["country_code"] = country_code
        data = self.call("POST", "user/login", body=body, auth=False)
        return self._login_result(data)

    def login_verify(self, *, mfa_token: str, code: str) -> LoginResult:
        data = self.call("POST", "user/2fa/login-verify", body={"mfa_token": mfa_token, "code": code}, auth=False)
        return self._login_result(data)

    def _login_result(self, data: Mapping) -> LoginResult:
        result = LoginResult(
            requires_2fa=bool(data.get("requires_2fa")),
            access=data.get("access"),
            refresh=data.get("refresh"),
            mfa_token=data.get("mfa_token"),
        )
        if not result.requires_2fa:
            if not (result.access and result.refresh):
                raise ApiError("Login response had no tokens.")
            self.tokens.set_tokens(result.access, result.refresh)
        return result

    def logout(self) -> None:
        refresh = self.tokens.refresh_token
        try:
            if refresh and not self.tokens.is_dead:
                self.call("POST", "user/logout", body={"refresh": refresh})
        except (Unauthorized, NetworkError):
            pass  # already dead / offline: the session simply expires
        finally:
            self.tokens.clear()

    def profile(self) -> dict:
        return self.call("GET", "user/profile") or {}

    def register_device(self, device_uuid: str) -> int:
        # Only device_uuid: the optional app/android fields overwrite the
        # user's single UserDevice row, which belongs to the Android app.
        data = self.call("POST", "settings/device", body={"device_uuid": device_uuid})
        return int(data["worker_id"])

    # -------------------------------------------------------------- projects
    @staticmethod
    def _as_list(data) -> List[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data["results"]
        return []

    def projects_desktop(self) -> List[dict]:
        """Map projects where the user is Owner/Admin/Editor, with a fresh
        ``is_expired`` and the Map row of Page access. 403 when the user holds
        none of those roles on any project (QGIS is not for them)."""
        return self._as_list(self.call("GET", "projects/desktop/list"))

    # ------------------------------------------------------------------- map
    def shp_list(self, proj_id: int) -> dict:
        return self.call("GET", f"map/{proj_id}/mobile/shp-list") or {}

    def sa_feat_page(self, proj_id: int, survey_area_id: int, cursor: Optional[str] = None, feedback=None) -> Page:
        data = self.call(
            "GET",
            f"map/{proj_id}/mobile/sa-feat-list",
            params={"survey_area_id": survey_area_id, "last_fetched": 0, "cursor": cursor},
            feedback=feedback,
        )
        return Page(self._as_list(data), cursor_from_next((data or {}).get("next")))

    def sa_feat_attrs(self, proj_id: int, survey_area_id: int, geom_ids: Sequence[int], feedback=None) -> List[dict]:
        if not geom_ids:
            return []
        data = self.call(
            "GET",
            f"map/{proj_id}/mobile/sa-feat-attr-list",
            params={"survey_area_id": survey_area_id, "survey_area_geom_id": _ids_param(geom_ids)},
            feedback=feedback,
        )
        return self._as_list(data)

    def feat_page(
        self,
        proj_id: int,
        *,
        survey_area_id: int,
        geom_ids: Sequence[int],
        shp_id: int,
        g_type: int,
        last_fetched: int,
        cursor: Optional[str] = None,
        feedback=None,
    ) -> Page:
        data = self.call(
            "GET",
            f"map/{proj_id}/mobile/feat-list",
            params={
                "survey_area_id": survey_area_id,
                "survey_area_geom_id": _ids_param(geom_ids),
                "shp_id": shp_id,
                "geom_type": g_type,
                "last_fetched": int(last_fetched),
                "cursor": cursor,
            },
            feedback=feedback,
        )
        return Page(self._as_list(data), cursor_from_next((data or {}).get("next")))

    def feat_attrs(
        self, proj_id: int, *, shp_id: int, g_type: int, feat_ids: Sequence[int], feedback=None
    ) -> List[dict]:
        if not feat_ids:
            return []
        data = self.call(
            "GET",
            f"map/{proj_id}/mobile/feat-attr-list",
            params={"shp_id": shp_id, "feat_geom_id": _ids_param(feat_ids), "geom_type": g_type},
            feedback=feedback,
        )
        return self._as_list(data)

    def feat_batch(self, proj_id: int, ops: List[dict], feedback=None) -> List[dict]:
        data = self.call("POST", f"map/{proj_id}/mobile/feat-batch", body={"ops": ops}, feedback=feedback)
        return list((data or {}).get("results") or [])
