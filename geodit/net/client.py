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
from ..forms.unique import UNIQUE_VIOLATION, parse_violated_groups
from .errors import (
    ApiError,
    NetworkError,
    ServerTooOld,
    SessionExpired,
    ValidationFailed,
    error_for_status,
)
from .multipart import build_multipart
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


def _snowflakes_param(ids: Iterable[int]) -> str:
    """Answer ids are snowflakes: sent as strings (the route takes either)."""
    return json.dumps([str(int(i)) for i in ids], separators=(",", ":"))


@dataclass
class UniqueVerdict:
    """``ans-unique-constraint``: free, or a violation naming the colliding
    groups (``None`` when the server didn't say which)."""

    ok: bool
    groups: Optional[List[int]] = None


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

    @staticmethod
    def _json(resp: HttpResponse) -> Any:
        """The decoded JSON body, ``None`` when there is none or it isn't JSON."""
        if not resp.body:
            return None
        try:
            return json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    def _decode(self, resp: HttpResponse) -> Any:
        if resp.status == 0:
            raise NetworkError(resp.error or "")
        data = self._json(resp)
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
            body=data,
        )

    def _refresh_access(self, refresh_token: str) -> Tuple[str, str]:
        # In the JSON body, never the URL: a query string lands in request and
        # access logs, and a refresh token is a 7-30 day credential.
        body = json.dumps({"refresh": refresh_token}).encode("utf-8")
        resp = self._send("POST", self.url("user/refresh"), body, None, None)
        if resp.status in (400, 401, 403):
            raise SessionExpired()
        if resp.status == 404 and isinstance(self._json(resp), dict):
            # The API's own 404: a server before api-v2 R1 answers it for a
            # user who no longer exists (now a 401). The account is gone, so
            # the session is — never a sign of an older server. A proxy's HTML
            # 404 stays a NotFound.
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
        """End the session on the server, best effort: with no access token
        (a stored session) it refreshes first, as ``/user/logout`` needs a
        Bearer. Never raises an API error — it must not replace the error a
        caller logs out for."""
        refresh = self.tokens.refresh_token
        try:
            if refresh and not self.tokens.is_dead:
                self.call("POST", "user/logout", body={"refresh": refresh})
        except ApiError:
            pass  # already dead, offline, throttled: the session simply expires
        finally:
            self.tokens.clear()

    def revoke_session(self, sid: int) -> None:
        """End one of the signed-in user's own sessions by its id — another
        session's refresh token isn't needed (``/user/sessions/{sid}``)."""
        self.call("DELETE", f"user/sessions/{int(sid)}")

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

    def project_detail(self, proj_id: int) -> dict:
        """The project as the web reads it: ``survey_settings.web_access`` (the
        Web access settings, ``null`` when none are set) is what the plugin needs."""
        data = self.call("GET", f"projects/detail/{int(proj_id)}")
        return data if isinstance(data, dict) else {}

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

    # ----------------------------------------------------------- answers
    # The feature form: the WEB answer routes, never `mobile/ans-*` (their
    # creates need a client-minted id).
    def form_data(self, proj_id: int, form_id: int) -> dict:
        return self.call("GET", f"forms/{proj_id}/form-data/{int(form_id)}") or {}

    def forms_list_basic(self, proj_id: int) -> List[dict]:
        """The project's forms (``{id, name, ...}``) — names for the layers a form is attached to."""
        return self._as_list(self.call("GET", f"forms/{proj_id}/list-basic"))

    def ans_data_list(self, proj_id: int, form_id: int, ans_ids: Sequence[int]) -> List[dict]:
        """Stored values of up to 100 responses (``{ques_id, ans_id, page_key, value}``)."""
        if not ans_ids:
            return []
        data = self.call(
            "GET",
            f"data/{proj_id}/mobile/ans-data-list",
            params={"form_id": int(form_id), "ans_ids": _snowflakes_param(ans_ids)},
        )
        return self._as_list(data)

    def ans_rows(self, proj_id: int, form_id: int, ans_ids: Sequence[int]) -> List[dict]:
        """The response rows (status, surveyor, dates) of up to 100 ids — a
        missing id is deleted, or out of the caller's assigned-data scope."""
        if not ans_ids:
            return []
        data = self.call(
            "GET",
            f"data/{proj_id}/mobile/ans-list",
            params={"form_id": int(form_id), "ans_ids": _snowflakes_param(ans_ids)},
        )
        return self._as_list(data)

    def team_list_basic(self, proj_id: int) -> List[dict]:
        """Every member (owner included) — resolves surveyor / verifier ids."""
        return self._as_list(self.call("GET", f"team/{proj_id}/list-basic"))

    def latest_id(self, proj_id: int, form_id: int, ques_id: int) -> int:
        """The highest counter submitted for a unique-id question (0 when none)."""
        data = self.call("GET", f"data/{proj_id}/latest-id", params={"form_id": int(form_id), "ques_id": int(ques_id)})
        try:
            return int((data or {}).get("latest_value") or 0)
        except (TypeError, ValueError):
            return 0

    def ans_create(
        self, proj_id: int, *, form_id: int, feature_id: int, feature_shp_id: int, answers: List[dict]
    ) -> dict:
        """Create a response linked to a feature. ``if_unlinked``: a feature that
        already links a live response answers 409 (``Conflict.linked_ans_id``)
        instead of being re-pointed. There is no idempotency key, so a caller
        must never retry this blindly."""
        body = {
            "form_id": int(form_id),
            "feature_id": str(int(feature_id)),
            "feature_shp_id": int(feature_shp_id),
            "answers": answers,
            "if_unlinked": True,
        }
        return self.call("POST", f"data/{proj_id}/ans-create", body=body) or {}

    def ans_update(self, proj_id: int, ans_id: int, *, form_id: int, answers: List[dict], clear: List[dict]) -> dict:
        body: Dict[str, Any] = {"form_id": int(form_id), "answers": answers}
        if clear:
            body["clear"] = clear
        return self.call("PATCH", f"data/{proj_id}/ans-update/{int(ans_id)}", body=body) or {}

    def ans_status(self, proj_id: int, ans_id: int, *, form_id: int, status: int) -> dict:
        return (
            self.call(
                "PATCH",
                f"data/{proj_id}/ans-status/{int(ans_id)}",
                params={"form_id": int(form_id)},
                body={"status": int(status)},
            )
            or {}
        )

    def ans_unique_constraint(
        self, proj_id: int, *, form_id: int, items: List[dict], exclude_ans_id: Optional[int] = None
    ) -> UniqueVerdict:
        """Probe ONE request's combinations. A 422 whose message says "Unique
        constraint violated" is a verdict; every other failure raises (the
        caller fails closed)."""
        try:
            self.call(
                "POST",
                f"data/{proj_id}/ans-unique-constraint",
                params={
                    "form_id": int(form_id),
                    "exclude_ans_id": str(int(exclude_ans_id)) if exclude_ans_id is not None else None,
                },
                body=items,
            )
            return UniqueVerdict(ok=True)
        except ValidationFailed as exc:
            if UNIQUE_VIOLATION.search(exc.message or ""):
                return UniqueVerdict(ok=False, groups=parse_violated_groups(exc.body))
            raise

    # ------------------------------------------------------------- media
    def media_presign(self, proj_id: int, *, kind: str, file_name: str, content_type: str, size: int) -> dict:
        """One presigned S3 POST (``{url, fields, key}``) for a file of exactly
        ``size`` bytes (the web route, as geodit-ui uses it)."""
        data = self.call(
            "POST",
            f"data/{proj_id}/media-presign",
            body={
                "uploads": [
                    {
                        "client_ref": "0",
                        "kind": kind,
                        "file_name": file_name,
                        "content_type": content_type,
                        "max_bytes": int(size),
                    }
                ]
            },
        )
        uploads = (data or {}).get("uploads") or []
        if not uploads or not isinstance(uploads[0], Mapping):
            raise ApiError("The server didn't return an upload slot.")
        return dict(uploads[0])

    def media_url_by_key(self, proj_id: int, key: str) -> str:
        """A presigned GET (about 5 minutes) for a stored media key."""
        data = self.call("GET", f"data/{proj_id}/media-url-by-key", params={"key": key})
        return str((data or {}).get("url") or "")

    def _raw(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes], feedback) -> HttpResponse:
        """A request to an absolute URL (S3) — no Authorization, no client header."""
        started = time.monotonic()
        try:
            return self.transport.request(method, url, headers, body, feedback or self.feedback)
        finally:
            self.requests += 1
            self.request_seconds += time.monotonic() - started

    def s3_post(
        self, url: str, fields: Mapping[str, str], *, file_name: str, content_type: str, data: bytes, feedback=None
    ) -> None:
        body, header = build_multipart(fields, file_name, content_type, data)
        resp = self._raw("POST", url, {"Content-Type": header}, body, feedback)
        if resp.status == 0:
            raise NetworkError(resp.error or "")
        if resp.status not in (200, 201, 204):
            raise ApiError(f"Upload to storage failed (HTTP {resp.status}). Please retry.", status=resp.status)

    def fetch_bytes(self, url: str, feedback=None) -> bytes:
        resp = self._raw("GET", url, {}, None, feedback)
        if resp.status == 0:
            raise NetworkError(resp.error or "")
        if not 200 <= resp.status < 300:
            raise ApiError(f"Download failed (HTTP {resp.status}).", status=resp.status)
        return resp.body
