"""``multipart/form-data`` bodies for S3 presigned POSTs.

S3 requires every policy field before the file, and the file part last and
named ``file`` (geodit-ui ``useS3Upload``: fields in order, then the file).
"""

from __future__ import annotations

import os
from typing import Mapping, Tuple


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def build_multipart(fields: Mapping[str, str], file_name: str, content_type: str, data: bytes) -> Tuple[bytes, str]:
    """``(body, content-type header)`` with ``fields`` in order and the file last."""
    boundary = "----GeoditQgis" + os.urandom(12).hex()
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(str(name))}"\r\n\r\n'.encode()
            + str(value).encode("utf-8")
            + b"\r\n"
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{_quote(file_name)}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
