"""Background tasks (``QgsTask``).

Rules: ``run()`` executes in a worker thread and touches only plain data plus
objects it creates itself (the HTTP transport, GDAL datasets). Results reach
layers or widgets on the main thread only: in ``finished()``, and through the
queued ``layerReady`` signal a sync emits as each part is done. The owner keeps
a strong reference to each task until ``finished()`` fires.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable, Optional

from qgis.core import QgsFeedback, QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from ..core.clock import ServerClock
from ..net.client import ApiClient
from ..net.tokens import TokenStore
from ..net.transport import QgsTransport
from .context import SyncContext, SyncReport
from .engine import SyncEngine


class SyncTask(QgsTask):
    # A ``LayerEvent``: the survey area, or one layer, finished its part of the
    # tick. Emitted from the worker thread; receivers in the main thread get it
    # queued.
    layerReady = pyqtSignal(object)

    def __init__(
        self,
        description: str,
        ctx: SyncContext,
        tokens: TokenStore,
        clock: ServerClock,
        on_done: Callable[[SyncTask], None],
        *,
        quiet: bool = False,
    ) -> None:
        flags = QgsTask.Flag.CanCancel
        hidden = getattr(QgsTask.Flag, "Hidden", None)  # QGIS 3.26+
        if quiet and hidden is not None:
            # A background tick (timer, after a save) stays out of QGIS's task bar.
            flags = flags | hidden
        super().__init__(description, flags)
        self.quiet = quiet
        self.ctx = ctx
        self.tokens = tokens
        self.clock = clock
        self.on_done = on_done
        self.feedback = QgsFeedback()
        self.report: Optional[SyncReport] = None
        self.error: Optional[str] = None
        # What the engine is doing now ("Downloading Parcels"). Written by the
        # worker thread, read on the main thread when ``progressChanged`` (queued)
        # arrives; a str attribute swap is atomic.
        self.step_text = ""

    def _on_progress(self, pct: float, text: str) -> None:
        self.step_text = text
        self.setProgress(pct)

    def cancel(self) -> None:
        self.feedback.cancel()  # aborts the in-flight HTTP request
        super().cancel()

    def run(self) -> bool:
        try:
            client = ApiClient(self.ctx.base_url, QgsTransport(), self.tokens, self.clock)
            client.feedback = self.feedback
            engine = SyncEngine(
                self.ctx,
                client,
                is_canceled=self.isCanceled,
                progress=self._on_progress,
                on_event=self.layerReady.emit,
            )
            self.report = engine.run()
            return True
        except Exception:  # noqa: BLE001 - never raise out of a task
            self.error = traceback.format_exc()
            return False

    def finished(self, result: bool) -> None:
        self.on_done(self)


class CallTask(QgsTask):
    """Run ``fn(client)`` off the main thread with a fresh transport; hand
    ``(value, error)`` back on the main thread."""

    def __init__(
        self,
        description: str,
        base_url: str,
        tokens: TokenStore,
        clock: ServerClock,
        fn: Callable[[ApiClient], Any],
        on_done: Callable[[Any, Optional[BaseException]], None],
    ) -> None:
        super().__init__(description, QgsTask.Flag.Silent)
        self.base_url = base_url
        self.tokens = tokens
        self.clock = clock
        self.fn = fn
        self.on_done = on_done
        self.value: Any = None
        self.exc: Optional[BaseException] = None

    def run(self) -> bool:
        try:
            self.value = self.fn(ApiClient(self.base_url, QgsTransport(), self.tokens, self.clock))
            return True
        except BaseException as exc:  # noqa: BLE001 - delivered to on_done
            self.exc = exc
            return False

    def finished(self, result: bool) -> None:
        self.on_done(self.value, self.exc)
