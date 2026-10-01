"""Capture time and GPS survive a photo's re-encode.

Port of geodit-ui ``convert/exif.ts``: the 11 tags Android's upload normalizer
copies (``PhotoNormalizer.kt``) are read from the source JPEG and written back
into the re-encoded one. No Orientation tag (the pixels are stored upright) and
no IFD1, so no thumbnail. The web reads the source tags with exifr (any image
format); QGIS reads them from a JPEG's own EXIF segment — the camera format.
"""

from __future__ import annotations

import re
import struct
from typing import Any, Dict, List, Optional, Tuple

CARRIED_TAGS = (
    "DateTimeOriginal",
    "OffsetTimeOriginal",
    "SubSecTimeOriginal",
    "GPSLatitudeRef",
    "GPSLatitude",
    "GPSLongitudeRef",
    "GPSLongitude",
    "GPSAltitudeRef",
    "GPSAltitude",
    "GPSDateStamp",
    "GPSTimeStamp",
)

_DATE_TIME = re.compile(r"[0-9]{4}:[0-9]{2}:[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_OFFSET = re.compile(r"[+-][0-9]{2}:[0-9]{2}")
_DATE = re.compile(r"[0-9]{4}:[0-9]{2}:[0-9]{2}")
_SUBSEC = re.compile(r"[0-9]{1,9}")

_EXIF_TAGS = {0x9003: "DateTimeOriginal", 0x9011: "OffsetTimeOriginal", 0x9291: "SubSecTimeOriginal"}
_GPS_TAGS = {
    0x0001: "GPSLatitudeRef",
    0x0002: "GPSLatitude",
    0x0003: "GPSLongitudeRef",
    0x0004: "GPSLongitude",
    0x0005: "GPSAltitudeRef",
    0x0006: "GPSAltitude",
    0x0007: "GPSTimeStamp",
    0x001D: "GPSDateStamp",
}
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


# ------------------------------------------------------------------ reading
def _read_ifd(tiff: bytes, offset: int, little: bool, names: Dict[int, str]) -> Tuple[Dict[str, Any], Dict[int, int]]:
    """Wanted ``names`` of one IFD, plus the pointer tags (value offsets)."""
    endian = "<" if little else ">"
    values: Dict[str, Any] = {}
    pointers: Dict[int, int] = {}
    if offset + 2 > len(tiff):
        return values, pointers
    (count,) = struct.unpack_from(endian + "H", tiff, offset)
    for n in range(count):
        at = offset + 2 + 12 * n
        if at + 12 > len(tiff):
            break
        tag, kind, number = struct.unpack_from(endian + "HHI", tiff, at)
        if tag in (0x8769, 0x8825) and kind == 4:
            (pointers[tag],) = struct.unpack_from(endian + "I", tiff, at + 8)
            continue
        name = names.get(tag)
        size = _TYPE_SIZE.get(kind)
        if name is None or size is None:
            continue
        length = size * number
        if length <= 4:
            data = tiff[at + 8 : at + 8 + length]
        else:
            (value_at,) = struct.unpack_from(endian + "I", tiff, at + 8)
            data = tiff[value_at : value_at + length]
            if len(data) < length:
                continue
        values[name] = _decode(kind, number, data, endian)
    return values, pointers


def _decode(kind: int, number: int, data: bytes, endian: str) -> Any:
    if kind == 2:
        return data.split(b"\0", 1)[0].decode("latin-1")
    if kind in (1, 7):
        return list(data[:number])
    if kind == 3:
        return list(struct.unpack(endian + "H" * number, data[: 2 * number]))
    if kind == 4:
        return list(struct.unpack(endian + "I" * number, data[: 4 * number]))
    if kind in (5, 10):
        fmt = "I" if kind == 5 else "i"
        out = []
        for i in range(number):
            num, den = struct.unpack(endian + fmt + fmt, data[8 * i : 8 * i + 8])
            out.append(num / den if den else 0.0)
        return out
    return None


def _app1_tiff(jpeg: bytes) -> Optional[bytes]:
    """The TIFF body of a JPEG's EXIF APP1 segment."""
    if jpeg[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(jpeg):
        if jpeg[i] != 0xFF:
            return None
        marker = jpeg[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        if marker in (0xDA, 0xD9):
            return None
        (length,) = struct.unpack(">H", jpeg[i + 2 : i + 4])
        if marker == 0xE1 and jpeg[i + 4 : i + 10] == b"Exif\0\0":
            return jpeg[i + 10 : i + 2 + length]
        i += 2 + length
    return None


def read_exif(jpeg: bytes) -> Dict[str, Any]:
    """The carried tags of a JPEG as raw values (``exifr``'s unrevived shapes)."""
    tiff = _app1_tiff(jpeg)
    if not tiff or len(tiff) < 8:
        return {}
    little = tiff[:2] == b"II"
    if not little and tiff[:2] != b"MM":
        return {}
    endian = "<" if little else ">"
    (ifd0,) = struct.unpack_from(endian + "I", tiff, 4)
    _, pointers = _read_ifd(tiff, ifd0, little, {})
    raw: Dict[str, Any] = {}
    if 0x8769 in pointers:
        values, _ = _read_ifd(tiff, pointers[0x8769], little, _EXIF_TAGS)
        raw.update(values)
    if 0x8825 in pointers:
        values, _ = _read_ifd(tiff, pointers[0x8825], little, _GPS_TAGS)
        raw.update(values)
    return raw


def _triple(value: Any) -> Optional[List[float]]:
    if isinstance(value, list) and len(value) == 3 and all(isinstance(n, (int, float)) and n >= 0 for n in value):
        return [value[0], value[1], value[2]]
    return None


def _first_number(value: Any) -> Optional[float]:
    number = value[0] if isinstance(value, list) and value else value
    return number if isinstance(number, (int, float)) and not isinstance(number, bool) else None


def exif_from_raw(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only well-formed values (``exifFromExifr``)."""
    if not raw:
        return {}
    out: Dict[str, Any] = {}

    def text(key: str) -> Optional[str]:
        value = raw.get(key)
        return value.strip() if isinstance(value, str) else None

    date_time = text("DateTimeOriginal")
    if date_time and _DATE_TIME.fullmatch(date_time):
        out["DateTimeOriginal"] = date_time
    offset = text("OffsetTimeOriginal")
    if offset and _OFFSET.fullmatch(offset):
        out["OffsetTimeOriginal"] = offset
    sub_sec = raw.get("SubSecTimeOriginal")
    if isinstance(sub_sec, (str, int)) and _SUBSEC.fullmatch(str(sub_sec).strip()):
        out["SubSecTimeOriginal"] = str(sub_sec).strip()
    lat, lon = _triple(raw.get("GPSLatitude")), _triple(raw.get("GPSLongitude"))
    lat_ref, lon_ref = text("GPSLatitudeRef"), text("GPSLongitudeRef")
    if lat and lon and lat_ref in ("N", "S") and lon_ref in ("E", "W"):
        out.update(GPSLatitude=lat, GPSLatitudeRef=lat_ref, GPSLongitude=lon, GPSLongitudeRef=lon_ref)
        altitude = _first_number(raw.get("GPSAltitude"))
        if altitude is not None and altitude >= 0:
            out["GPSAltitude"] = altitude
            out["GPSAltitudeRef"] = 1 if _first_number(raw.get("GPSAltitudeRef")) == 1 else 0
    gps_date = text("GPSDateStamp")
    if gps_date and _DATE.fullmatch(gps_date):
        out["GPSDateStamp"] = gps_date
    gps_time = _triple(raw.get("GPSTimeStamp"))
    if gps_time:
        out["GPSTimeStamp"] = gps_time
    return out


# ------------------------------------------------------------------ writing
def _ascii(text: str) -> bytes:
    return bytes((ord(c) & 0x7F) for c in text) + b"\0"


def _rationals(values: List[float]) -> bytes:
    out = b""
    for value in values:
        den = 1_000_000
        while value * den > 0xFFFFFFFF and den > 1:
            den //= 10
        out += struct.pack("<II", int(round(value * den)), den)
    return out


def _entry(tag: int, kind: int, count: int, data: bytes) -> list:
    return [tag, kind, count, data]


def _ascii_entry(tag: int, value: str) -> list:
    data = _ascii(value)
    return _entry(tag, 2, len(data), data)


def _ifd_size(entries: List[list]) -> int:
    return 2 + 12 * len(entries) + 4 + sum(len(e[3]) + (len(e[3]) & 1) for e in entries if len(e[3]) > 4)


def _write_ifd(tiff: bytearray, offset: int, entries: List[list]) -> None:
    struct.pack_into("<H", tiff, offset, len(entries))
    data_at = offset + 2 + 12 * len(entries) + 4
    for n, (tag, kind, count, data) in enumerate(entries):
        at = offset + 2 + 12 * n
        struct.pack_into("<HHI", tiff, at, tag, kind, count)
        if len(data) <= 4:
            tiff[at + 8 : at + 8 + len(data)] = data
        else:
            struct.pack_into("<I", tiff, at + 8, data_at)
            tiff[data_at : data_at + len(data)] = data
            data_at += len(data) + (len(data) & 1)
    struct.pack_into("<I", tiff, offset + 2 + 12 * len(entries), 0)


def build_exif_app1(tags: Dict[str, Any]) -> Optional[bytes]:
    """An APP1 segment (marker included) with the carried tags, or None."""
    exif: List[list] = []
    if tags.get("DateTimeOriginal"):
        exif.append(_entry(0x9000, 7, 4, b"0232"))
        exif.append(_ascii_entry(0x9003, tags["DateTimeOriginal"]))
        if tags.get("OffsetTimeOriginal"):
            exif.append(_ascii_entry(0x9011, tags["OffsetTimeOriginal"]))
        if tags.get("SubSecTimeOriginal"):
            exif.append(_ascii_entry(0x9291, tags["SubSecTimeOriginal"]))
    gps: List[list] = []
    if (
        tags.get("GPSLatitude")
        and tags.get("GPSLatitudeRef")
        and tags.get("GPSLongitude")
        and tags.get("GPSLongitudeRef")
    ):
        gps.append(_entry(0x0000, 1, 4, bytes([2, 3, 0, 0])))
        gps.append(_ascii_entry(0x0001, tags["GPSLatitudeRef"]))
        gps.append(_entry(0x0002, 5, 3, _rationals(tags["GPSLatitude"])))
        gps.append(_ascii_entry(0x0003, tags["GPSLongitudeRef"]))
        gps.append(_entry(0x0004, 5, 3, _rationals(tags["GPSLongitude"])))
        if tags.get("GPSAltitude") is not None:
            gps.append(_entry(0x0005, 1, 1, bytes([tags.get("GPSAltitudeRef") or 0])))
            gps.append(_entry(0x0006, 5, 1, _rationals([tags["GPSAltitude"]])))
        if tags.get("GPSTimeStamp"):
            gps.append(_entry(0x0007, 5, 3, _rationals(tags["GPSTimeStamp"])))
        if tags.get("GPSDateStamp"):
            gps.append(_ascii_entry(0x001D, tags["GPSDateStamp"]))
    if not exif and not gps:
        return None
    # IFD0 holds only the two pointers, patched once the offsets are known.
    ifd0: List[list] = []
    if exif:
        ifd0.append(_entry(0x8769, 4, 1, struct.pack("<I", 0)))
    if gps:
        ifd0.append(_entry(0x8825, 4, 1, struct.pack("<I", 0)))
    exif_offset = 8 + _ifd_size(ifd0)
    gps_offset = exif_offset + (_ifd_size(exif) if exif else 0)
    for entry in ifd0:
        entry[3] = struct.pack("<I", exif_offset if entry[0] == 0x8769 else gps_offset)
    tiff = bytearray(gps_offset + (_ifd_size(gps) if gps else 0))
    tiff[0:8] = b"II*\0\x08\0\0\0"
    _write_ifd(tiff, 8, ifd0)
    if exif:
        _write_ifd(tiff, exif_offset, exif)
    if gps:
        _write_ifd(tiff, gps_offset, gps)
    length = 2 + 6 + len(tiff)
    if length > 0xFFFF:
        return None
    return b"\xff\xe1" + struct.pack(">H", length) + b"Exif\0\0" + bytes(tiff)


def insert_app1(jpeg: bytes, app1: bytes) -> bytes:
    """``jpeg`` with ``app1`` after SOI (and after a leading JFIF APP0)."""
    at = 2
    if len(jpeg) > 5 and jpeg[2] == 0xFF and jpeg[3] == 0xE0:
        at = 4 + ((jpeg[4] << 8) | jpeg[5])
    return jpeg[:at] + app1 + jpeg[at:]


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def read_jpeg_info(data: bytes) -> Optional[Tuple[int, int, int]]:
    """``(width, height, orientation)`` from a JPEG's header, without decoding it."""
    if data[:2] != b"\xff\xd8":
        return None
    orientation = 1
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _SOF:
            if i + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height, orientation
        if marker == 0xE1 and length >= 8 and data[i + 4 : i + 10] == b"Exif\0\0":
            found = _orientation(data[i + 10 : i + 2 + length])
            orientation = found if found is not None else orientation
        if marker in (0xDA, 0xD9):
            return None
        i += 2 + length
    return None


def _orientation(tiff: bytes) -> Optional[int]:
    if len(tiff) < 8:
        return None
    little = tiff[:2] == b"II"
    endian = "<" if little else ">"
    (ifd,) = struct.unpack_from(endian + "I", tiff, 4)
    if ifd + 2 > len(tiff):
        return None
    (count,) = struct.unpack_from(endian + "H", tiff, ifd)
    for n in range(count):
        at = ifd + 2 + 12 * n
        if at + 12 > len(tiff):
            return None
        (tag,) = struct.unpack_from(endian + "H", tiff, at)
        if tag == 0x0112:
            (value,) = struct.unpack_from(endian + "H", tiff, at + 8)
            return value if 1 <= value <= 8 else None
    return None
