"""Client-side feature ids, bit-compatible with the server's ``helpers/ids.py``.

    id = ((ms - EPOCH_MS) << 22) | (worker_id << 10) | sequence

The server stores a ``mobile/feat-batch`` create under the id the client sent,
and ids are unique only per layer — a create whose id already exists in that
layer is taken for an idempotent retry and OVERWRITES the row. So a minter
must never hand out the same id twice for one layer, even across restarts:
the caller persists ``state`` per layer file and restores it on the next run.
"""

from __future__ import annotations

from typing import Tuple

EPOCH_MS = 1704067200000  # 2024-01-01 UTC — helpers.ids.EPOCH
WORKER_BITS = 12
SEQUENCE_BITS = 10
MAX_WORKER = (1 << WORKER_BITS) - 1  # 4095
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1  # 1023
DEVICE_WORKER_MIN = 101  # helpers.ids.DEVICE_WORKER_ID_MIN


class SnowflakeMinter:
    """Monotonic minter for one worker id.

    ``last_ms`` / ``last_seq`` are the last id's timestamp and sequence; the
    next id is always strictly greater, so a clock that jumps backwards (NTP
    step, a server-offset correction) can never re-issue an id.
    """

    def __init__(self, worker_id: int, last_ms: int = 0, last_seq: int = -1) -> None:
        if not 0 <= int(worker_id) <= MAX_WORKER:
            raise ValueError(f"worker_id {worker_id} out of range 0..{MAX_WORKER}")
        self.worker_id = int(worker_id)
        self.last_ms = int(last_ms)
        self.last_seq = int(last_seq)

    def mint(self, now_ms: int) -> int:
        ts = max(int(now_ms), self.last_ms, EPOCH_MS)
        if ts == self.last_ms:
            seq = self.last_seq + 1
            if seq > MAX_SEQUENCE:
                # Sequence exhausted in this millisecond: borrow the next one
                # rather than sleeping — the id only has to be unique and
                # ordered, not exactly timestamped.
                ts, seq = self.last_ms + 1, 0
        else:
            seq = 0
        self.last_ms, self.last_seq = ts, seq
        return ((ts - EPOCH_MS) << (WORKER_BITS + SEQUENCE_BITS)) | (self.worker_id << SEQUENCE_BITS) | seq

    @property
    def state(self) -> Tuple[int, int]:
        return self.last_ms, self.last_seq


def decode(snowflake: int) -> Tuple[int, int, int]:
    """``(timestamp_ms, worker_id, sequence)`` of an id."""
    snowflake = int(snowflake)
    seq = snowflake & MAX_SEQUENCE
    worker = (snowflake >> SEQUENCE_BITS) & MAX_WORKER
    ts = (snowflake >> (WORKER_BITS + SEQUENCE_BITS)) + EPOCH_MS
    return ts, worker, seq
