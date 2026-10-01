"""HTTP transport abstraction.

The plugin talks HTTP through ``QgsBlockingNetworkRequest`` (``QgsTransport``),
which honours QGIS's proxy, SSL-exception and timeout settings and is safe to
use from a ``QgsTask`` worker thread; PATCH, which it doesn't offer, goes
through the thread's ``QgsNetworkAccessManager`` instead. Tests plug in an
in-process fake.
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
        if method not in ("GET", "POST"):
            return self._custom(method, req, body, feedback)
        blocking = QgsBlockingNetworkRequest()
        if method == "GET":
            blocking.get(req, True, feedback)
        else:
            blocking.post(req, QByteArray(body or b""), True, feedback)
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

    @staticmethod
    def _custom(method, req, body, feedback) -> HttpResponse:
        """A verb ``QgsBlockingNetworkRequest`` doesn't offer (PATCH — it has
        get / head / post / put / deleteResource only). Sent through the
        thread's ``QgsNetworkAccessManager`` (so QGIS's proxy, SSL and auth
        settings apply) and waited for in a local event loop, with the QGIS
        network timeout and the task's ``QgsFeedback`` able to abort it."""
        from qgis.core import QgsNetworkAccessManager
        from qgis.PyQt.QtCore import QByteArray, QEventLoop, QTimer
        from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

        manager = QgsNetworkAccessManager.instance()
        reply = manager.sendCustomRequest(req, method.encode("ascii"), QByteArray(body or b""))
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timed_out = []

        def on_timeout():
            timed_out.append(True)
            reply.abort()

        timer.timeout.connect(on_timeout)
        reply.finished.connect(loop.quit)
        if feedback is not None:
            if feedback.isCanceled():
                reply.abort()
            feedback.canceled.connect(reply.abort)
        try:
            if not reply.isFinished():
                timer.start(max(1000, int(QgsNetworkAccessManager.timeout() or 60_000)))
                loop.exec()
        finally:
            timer.stop()
            if feedback is not None:
                try:
                    feedback.canceled.disconnect(reply.abort)
                except (TypeError, RuntimeError):
                    pass
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        try:
            if status is None:
                canceled = reply.error() == QNetworkReply.NetworkError.OperationCanceledError
                if timed_out or (canceled and not (feedback is not None and feedback.isCanceled())):
                    # QgsNetworkAccessManager aborts on its own timer (as long as
                    # ours, started first): an abort the task didn't ask for.
                    return HttpResponse(status=0, error="The request timed out.")
                if canceled:
                    return HttpResponse(status=0, error="The request was canceled.")
                return HttpResponse(status=0, error=reply.errorString() or "network error")
            headers = {
                bytes(name).decode("latin-1").lower(): bytes(reply.rawHeader(name)).decode("latin-1")
                for name in reply.rawHeaderList()
            }
            return HttpResponse(status=int(status), headers=headers, body=bytes(reply.readAll()))
        finally:
            reply.deleteLater()
