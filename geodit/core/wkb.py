"""WKB helpers.

The server sends geometry as PostGIS hex-EWKB with the SRID flag set
(``ST_AsEWKB``). Neither GDAL nor QGIS reads that flag
(``Unsupported WKB type 536870918``), so every server geometry is normalised
here to plain 2D little-endian ISO WKB before it reaches OGR. Z/M ordinates are
dropped: the Android client already flattens to 2D (it edits through GeoJSON),
so the server's live data is 2D in practice.

GeoPackage geometry blobs are standard WKB behind a small header;
``gpkg_blob_to_wkb`` strips it so a stored blob can be compared with a server
geometry.
"""

from __future__ import annotations

import struct
from typing import List, Optional

POINT = 1
LINESTRING = 2
POLYGON = 3
MULTIPOINT = 4
MULTILINESTRING = 5
MULTIPOLYGON = 6
GEOMETRYCOLLECTION = 7

_EWKB_Z = 0x80000000
_EWKB_M = 0x40000000
_EWKB_SRID = 0x20000000
_EWKB_FLAGS = _EWKB_Z | _EWKB_M | _EWKB_SRID

# Server layer kinds (Shapefile.g_type) → the Multi* type the local layer uses.
GTYPE_TO_MULTI = {1: MULTIPOINT, 2: MULTILINESTRING, 3: MULTIPOLYGON}
_SINGLE_TO_MULTI = {POINT: MULTIPOINT, LINESTRING: MULTILINESTRING, POLYGON: MULTIPOLYGON}


class WkbError(ValueError):
    """Malformed or unsupported (E)WKB."""


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise WkbError("truncated WKB")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def uint32(self, endian: str) -> int:
        return struct.unpack(endian + "I", self.take(4))[0]


def _read_header(r: _Reader):
    order = r.byte()
    if order not in (0, 1):
        raise WkbError(f"bad byte-order marker {order}")
    endian = "<" if order == 1 else ">"
    raw = r.uint32(endian)
    has_z = bool(raw & _EWKB_Z)
    has_m = bool(raw & _EWKB_M)
    if raw & _EWKB_SRID:
        r.take(4)  # the SRID itself — always 4326 on this API
    code = raw & ~_EWKB_FLAGS & 0xFFFFFFFF
    if code >= 1000:  # ISO flavour: 1000 = Z, 2000 = M, 3000 = ZM
        dims, code = divmod(code, 1000)
        has_z = has_z or dims in (1, 3)
        has_m = has_m or dims in (2, 3)
    if code < POINT or code > GEOMETRYCOLLECTION:
        raise WkbError(f"unsupported geometry type {code}")
    return endian, code, 2 + int(has_z) + int(has_m)


def _copy_points(r: _Reader, out: bytearray, endian: str, ncoord: int, count: int) -> None:
    size = 8 * ncoord
    for _ in range(count):
        chunk = r.take(size)
        x, y = struct.unpack(endian + "dd", chunk[:16])
        out += struct.pack("<dd", x, y)


def _convert(r: _Reader, out: bytearray) -> int:
    endian, code, ncoord = _read_header(r)
    out += b"\x01" + struct.pack("<I", code)
    if code == POINT:
        _copy_points(r, out, endian, ncoord, 1)
    elif code == LINESTRING:
        n = r.uint32(endian)
        out += struct.pack("<I", n)
        _copy_points(r, out, endian, ncoord, n)
    elif code == POLYGON:
        rings = r.uint32(endian)
        out += struct.pack("<I", rings)
        for _ in range(rings):
            n = r.uint32(endian)
            out += struct.pack("<I", n)
            _copy_points(r, out, endian, ncoord, n)
    else:  # multi* / collection: nested geometries carry their own headers
        n = r.uint32(endian)
        out += struct.pack("<I", n)
        for _ in range(n):
            _convert(r, out)
    return code


def ewkb_to_wkb2d(data: bytes) -> bytes:
    """(E)WKB in any byte order / dimension → 2D little-endian ISO WKB."""
    r = _Reader(bytes(data))
    out = bytearray()
    _convert(r, out)
    if r.pos != len(r.data):
        raise WkbError("trailing bytes after geometry")
    return bytes(out)


def hex_to_wkb2d(hex_geom: str) -> bytes:
    try:
        raw = bytes.fromhex(hex_geom)
    except (TypeError, ValueError) as exc:
        raise WkbError("geometry is not valid hex") from exc
    return ewkb_to_wkb2d(raw)


def geometry_type(wkb: bytes) -> int:
    """Base geometry type code (1..7) of a WKB/EWKB buffer."""
    _, code, _ = _read_header(_Reader(bytes(wkb)))
    return code


def to_multi(wkb: bytes) -> bytes:
    """Wrap a single Point/LineString/Polygon (2D little-endian WKB) in its Multi*
    type; multi types and collections pass through unchanged."""
    code = geometry_type(wkb)
    multi = _SINGLE_TO_MULTI.get(code)
    if multi is None:
        return bytes(wkb)
    return b"\x01" + struct.pack("<II", multi, 1) + bytes(wkb)


def gpkg_blob_to_wkb(blob: Optional[bytes]) -> Optional[bytes]:
    """Standard WKB inside a GeoPackage geometry blob (``None`` for NULL)."""
    if blob is None:
        return None
    blob = bytes(blob)
    if len(blob) < 8 or blob[0:2] != b"GP":
        raise WkbError("not a GeoPackage geometry blob")
    flags = blob[3]
    envelope = (flags >> 1) & 0x07
    sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    if envelope not in sizes:
        raise WkbError(f"bad GeoPackage envelope code {envelope}")
    return blob[8 + sizes[envelope] :]


def wkb_to_hex(wkb: bytes) -> str:
    return bytes(wkb).hex().upper()


def iter_points(wkb: bytes) -> List[tuple]:
    """All (x, y) vertices of a 2D little-endian WKB (bounding boxes, tests)."""
    r = _Reader(bytes(wkb))
    pts: List[tuple] = []

    def walk() -> None:
        endian, code, ncoord = _read_header(r)
        size = 8 * ncoord

        def read_pts(count: int) -> None:
            for _ in range(count):
                chunk = r.take(size)
                pts.append(struct.unpack(endian + "dd", chunk[:16]))

        if code == POINT:
            read_pts(1)
        elif code == LINESTRING:
            read_pts(r.uint32(endian))
        elif code == POLYGON:
            for _ in range(r.uint32(endian)):
                read_pts(r.uint32(endian))
        else:
            for _ in range(r.uint32(endian)):
                walk()

    walk()
    return pts
