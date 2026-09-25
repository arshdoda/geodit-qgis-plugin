"""HTTP transport abstraction.

The plugin talks HTTP through ``QgsBlockingNetworkRequest`` (``QgsTransport``),
which honours QGIS's proxy, SSL-exception and timeout settings and is safe to
use from a ``QgsTask`` worker thread. Tests plug in an in-process fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class HttpResponse:
    status: int  # 0 = no HTTP response (network-level failure)
    headers: Dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""
    error: str = ""


class Transport:
    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
        feedback=None,
    ) -> HttpResponse:  # pragma: no cover - interface
        raise NotImplementedError


class QgsTransport(Transport):
    """``QgsBlockingNetworkRequest``-backed transport.

    Build one per worker thread (inside ``QgsTask.run``). QGIS rewrites the
    ``User-Agent`` on every request (``<ua setting> QGIS/<version>/<os>``); the
    server reads the client kind from ``X-Client-Type`` instead.
    """

    def request(self, method, url, headers, body=None, feedback=None):
        from qgis.core import QgsBlockingNetworkRequest
        from qgis.PyQt.QtCore import QByteArray, QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest

        req = QNetworkRequest(QUrl(url))
        for name, value in headers.items():
            req.setRawHeader(name.encode("latin-1"), str(value).encode("latin-1"))
        # Tokens and project data must never land in QGIS's disk cache.
        req.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
        req.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.AlwaysNetwork,
        )
        blocking = QgsBlockingNetworkRequest()
        if method == "GET":
            blocking.get(req, True, feedback)
        elif method == "POST":
            blocking.post(req, QByteArray(body or b""), True, feedback)
        else:
            raise ValueError(f"unsupported method {method}")
        reply = blocking.reply()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status is None:
            return HttpResponse(status=0, error=blocking.errorMessage() or "network error")
        # QgsNetworkReplyContent exposes rawHeaderList()/rawHeader(), not the
        # QNetworkReply-style rawHeaderPairs().
        resp_headers = {
            bytes(name).decode("latin-1").lower(): bytes(reply.rawHeader(name)).decode("latin-1")
            for name in reply.rawHeaderList()
        }
        return HttpResponse(status=int(status), headers=resp_headers, body=bytes(reply.content()))
