"""The media-limits contract, as far as QGIS can honour it.

Port of geodit-ui ``runtime/mediaLimits.ts`` (the pinned table — same values as
geodit-ui ``docs/media-limits-frontend.md`` §1 and api-v2
``MEDIA_LIMITS_CONTRACT.md``), ``convert/sniff.ts`` and the format half of
``convert/keepRules.ts``. Every upload is ONE format per kind: JPEG photos
(long side ≤ 2048 px, quality 70), PDF documents, AAC-in-MP4 audio, H.264-in-MP4
video, PNG signatures. QGIS converts photos and draws signatures, but it can't
transcode audio or video, so those are accepted only when the file already is
the kind's format (the contract's §4.3 exception) and fits the limit.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Dict, List, Optional, Tuple

from .model import QType

MEDIA_LIMITS: Dict = {
    "version": 1,
    "kinds": {
        "image": {
            "q_type": 51,
            "content_type": "image/jpeg",
            "extension": "jpg",
            "max_bytes": 15728640,
            "profile": {"long_side_px": 2048, "jpeg_quality": 70},
        },
        "document": {"q_type": 52, "content_type": "application/pdf", "extension": "pdf", "max_bytes": 26214400},
        "audio": {
            "q_type": 53,
            "content_type": "audio/mp4",
            "extension": "m4a",
            "max_bytes": 52428800,
            "profile": {
                "codec": "aac",
                "channels": 1,
                "he_aac_bitrate_bps": 24000,
                "lc_bitrate_bps": 32000,
                "keep_original_max_bps": 40000,
            },
        },
        "video": {
            "q_type": 54,
            "content_type": "video/mp4",
            "extension": "mp4",
            "max_bytes": 209715200,
            "profile": {
                "video_codec": "h264",
                "audio_codec": "aac",
                "short_side_px": 480,
                "max_frame_rate": 30,
                "bitrate_bps": 1000000,
                "audio_bitrate_bps": 96000,
                "keep_original_max_bps": 1400000,
            },
        },
        "signature": {
            "q_type": 55,
            "content_type": "image/png",
            "extension": "png",
            "max_bytes": 2097152,
            "profile": {"long_side_px": 600, "crop_to_ink": True},
        },
    },
    "recording_minutes": {"max": 10, "default": 10},
}

# Numbers the contract states in prose (Android `MediaEncoding.kt`).
SIGNATURE_INK_PAD_PX = 24
SIGNATURE_MIN_SAVED_STROKE_PX = 2.5
SIGNATURE_LIVE_STROKE_PX = 4

KIND_BY_QTYPE = {
    QType.IMAGE: "image",
    QType.DOCUMENT: "document",
    QType.AUDIO: "audio",
    QType.VIDEO: "video",
    QType.SIGNATURE: "signature",
}


def media_spec(kind: str) -> Dict:
    return MEDIA_LIMITS["kinds"][kind]


def upload_name(name: str, kind: str) -> str:
    """``name`` with its extension swapped for the kind's one."""
    dot = name.rfind(".")
    base = (name[:dot] if dot > 0 else name).strip() or kind
    return f"{base}.{media_spec(kind)['extension']}"


def format_limit(max_bytes: int) -> str:
    """ "15 MB" — ``max_bytes`` is MiB-based."""
    return f"{int(max_bytes / (1024 * 1024) + 0.5)} MB"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


# ------------------------------------------------------------------ geometry
# Port of geodit-ui `convert/geometry.ts`.
def _round_half_up(value: float) -> int:
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def photo_target(width: int, height: int) -> Optional[Tuple[int, int]]:
    """A photo's upload size: the long side exactly 2048 px when it's longer,
    the short side rounded half up; None when it already fits (never enlarged)."""
    target = media_spec("image")["profile"]["long_side_px"]
    long_side = max(width, height)
    if long_side <= target:
        return None
    short = max(1, _round_half_up(min(width, height) * target / long_side))
    return (target, short) if width >= height else (short, target)


def signature_ink_box(strokes: List[List[Tuple[float, float]]], pad_size: Tuple[float, float]):
    """The ink's bounding box plus 24 px, clamped to the pad, as
    ``(left, top, width, height)``; one-point strokes (taps) carry no ink."""
    xs = [x for stroke in strokes if len(stroke) >= 2 for x, _ in stroke]
    ys = [y for stroke in strokes if len(stroke) >= 2 for _, y in stroke]
    if not xs:
        return None
    left = max(0.0, min(xs) - SIGNATURE_INK_PAD_PX)
    top = max(0.0, min(ys) - SIGNATURE_INK_PAD_PX)
    right = min(pad_size[0], max(xs) + SIGNATURE_INK_PAD_PX)
    bottom = min(pad_size[1], max(ys) + SIGNATURE_INK_PAD_PX)
    return (left, top, max(1.0, right - left), max(1.0, bottom - top))


def signature_output(width: float, height: float) -> Tuple[float, int, int]:
    """``(scale, width, height)`` putting the box's long side at exactly 600 px."""
    w = max(1, _round_half_up(width))
    h = max(1, _round_half_up(height))
    scale = media_spec("signature")["profile"]["long_side_px"] / max(w, h)
    return scale, max(1, _round_half_up(w * scale)), max(1, _round_half_up(h * scale))


def signature_saved_stroke(scale: float) -> float:
    """The saved stroke width: the live stroke scaled down with the signature,
    never below 2.5 px, never thicker when a small signature is enlarged."""
    return max(SIGNATURE_MIN_SAVED_STROKE_PX, SIGNATURE_LIVE_STROKE_PX * min(scale, 1))


# ------------------------------------------------------------------ sniffing
_HEIF_BRANDS = {"heic", "heix", "heim", "heis", "hevc", "hevx", "mif1", "msf1"}


def _ascii(b: bytes, start: int, length: int) -> str:
    return b[start : start + length].decode("latin-1")


def sniff_bytes(b: bytes) -> str:
    """The real format of a file from its first bytes — a name can't be trusted."""
    if len(b) < 4:
        return "unknown"
    if b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF:
        return "jpeg"
    if b[0] == 0x89 and _ascii(b, 1, 3) == "PNG":
        return "png"
    if _ascii(b, 0, 4) == "GIF8":
        return "gif"
    if _ascii(b, 0, 4) == "RIFF" and len(b) >= 12:
        form = _ascii(b, 8, 4)
        if form == "WEBP":
            return "webp"
        if form == "WAVE":
            return "wav"
    if _ascii(b, 0, 2) == "BM":
        return "bmp"
    if _ascii(b, 0, 4) in ("II*\0", "MM\0*"):
        return "tiff"
    if len(b) >= 12 and _ascii(b, 4, 4) == "ftyp":
        major = _ascii(b, 8, 4)
        size = min(struct.unpack(">I", b[0:4])[0], len(b))
        brands = [major]
        i = 16
        while i + 4 <= size:
            brands.append(_ascii(b, i, 4))
            i += 4
        if "avif" in brands or "avis" in brands:
            return "avif"
        if major in _HEIF_BRANDS:
            return "heic"
        if major == "qt  ":
            return "mov"
        if major.startswith("3g"):
            return "3gp"
        return "mp4"
    if b[0] == 0x1A and b[1] == 0x45 and b[2] == 0xDF and b[3] == 0xA3:
        return "matroska"
    if _ascii(b, 0, 4) == "OggS":
        return "ogg"
    if _ascii(b, 0, 4) == "fLaC":
        return "flac"
    if _ascii(b, 0, 3) == "ID3":
        return "mp3"
    if b[0] == 0xFF and (b[1] & 0xF6) == 0xF0:
        return "adts"
    if b[0] == 0xFF and (b[1] & 0xE0) == 0xE0:
        return "mp3"
    if _ascii(b, 0, 5) == "%PDF-":
        return "pdf"
    return "unknown"


def sniff_head(head: bytes) -> str:
    """``sniffBlob``: a PDF may carry junk before its ``%PDF-`` header."""
    fmt = sniff_bytes(head)
    if fmt != "unknown":
        return fmt
    return "pdf" if b"%PDF-" in head[:1024] else "unknown"


def sniff_file(path: str) -> str:
    with open(path, "rb") as fh:
        return sniff_head(fh.read(1024))


# ------------------------------------------------------------------ MP4 probe
@dataclass
class TrackInfo:
    kind: str  # "video" | "audio"
    codec: Optional[str]  # "avc", "hevc", "aac", "mp3", "opus", … (Mediabunny's names)
    width: int = 0
    height: int = 0


@dataclass
class MediaProbe:
    container: str
    size: int
    duration: float  # seconds; 0 when unknown
    video: Optional[TrackInfo]
    audio: Optional[TrackInfo]


_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}
_VIDEO_CODECS = {
    b"avc1": "avc",
    b"avc3": "avc",
    b"hvc1": "hevc",
    b"hev1": "hevc",
    b"vp09": "vp9",
    b"av01": "av1",
    b"mp4v": "mpeg4",
}
_AUDIO_CODECS = {b"Opus": "opus", b"fLaC": "flac", b"ac-3": "ac3", b"ec-3": "eac3", b".mp3": "mp3"}


def _boxes(fh: BinaryIO, start: int, end: int):
    """(type, payload start, box end) of each box in ``[start, end)``."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        payload = pos + 8
        if size == 1:
            large = fh.read(8)
            if len(large) < 8:
                return
            size = struct.unpack(">Q", large)[0]
            payload = pos + 16
        elif size == 0:
            size = end - pos
        if size < 8 or pos + size > end:
            return
        yield kind, payload, pos + size
        pos += size


def _descriptor_length(data: bytes, i: int):
    """An MPEG-4 descriptor's 1–4 byte length at ``i``: (length, next index)."""
    length = 0
    for _ in range(4):
        if i >= len(data):
            return None, i
        byte = data[i]
        i += 1
        length = (length << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    return length, i


def _esds_codec(fh: BinaryIO, start: int, end: int) -> Optional[str]:
    """AAC or not: the objectTypeIndication of the esds DecoderConfigDescriptor."""
    fh.seek(start)
    data = fh.read(min(end - start, 256))
    i = 4  # version + flags
    if i >= len(data) or data[i] != 0x03:  # ES_Descriptor
        return None
    _, i = _descriptor_length(data, i + 1)
    if i + 3 > len(data):
        return None
    flags = data[i + 2]
    i += 3  # ES_ID + flags
    if flags & 0x80:  # streamDependenceFlag
        i += 2
    if flags & 0x40 and i < len(data):  # URL_Flag
        i += 1 + data[i]
    if flags & 0x20:  # OCRstreamFlag
        i += 2
    if i >= len(data) or data[i] != 0x04:  # DecoderConfigDescriptor
        return None
    _, i = _descriptor_length(data, i + 1)
    if i >= len(data):
        return None
    oti = data[i]
    if oti in (0x40, 0x66, 0x67, 0x68):
        return "aac"
    if oti in (0x69, 0x6B):
        return "mp3"
    return None


def _sample_entry(fh: BinaryIO, stsd_start: int, stsd_end: int, handler: bytes) -> Optional[TrackInfo]:
    # stsd: version/flags (4) + entry count (4), then sample entries (boxes)
    for kind, payload, box_end in _boxes(fh, stsd_start + 8, stsd_end):
        if handler == b"vide":
            codec = _VIDEO_CODECS.get(kind, kind.decode("latin-1").strip())
            fh.seek(payload + 24)  # 6 reserved + 2 index + 16 pre-defined/reserved
            dims = fh.read(4)
            width, height = struct.unpack(">HH", dims) if len(dims) == 4 else (0, 0)
            return TrackInfo("video", codec, width, height)
        if handler == b"soun":
            if kind == b"mp4a":
                # audio sample entry: 28 bytes of fields, then child boxes (esds)
                for child, child_payload, child_end in _boxes(fh, payload + 28, box_end):
                    if child == b"esds":
                        return TrackInfo("audio", _esds_codec(fh, child_payload, child_end))
                return TrackInfo("audio", None)
            return TrackInfo("audio", _AUDIO_CODECS.get(kind, kind.decode("latin-1").strip()))
        return None
    return None


def _track(fh: BinaryIO, start: int, end: int) -> Optional[TrackInfo]:
    handler = None
    stsd = None
    stack = [(start, end)]
    while stack:
        s, e = stack.pop()
        for kind, payload, box_end in _boxes(fh, s, e):
            if kind == b"hdlr" and handler is None:  # mdia's, not a QuickTime minf data handler
                fh.seek(payload + 8)  # version/flags + pre_defined / component type
                handler = fh.read(4)
            elif kind == b"stsd":
                stsd = (payload, box_end)
            elif kind in _CONTAINERS:
                stack.append((payload, box_end))
    if handler not in (b"vide", b"soun") or stsd is None:
        return None
    return _sample_entry(fh, stsd[0], stsd[1], handler)


def probe_mp4(path: str) -> MediaProbe:
    """What the tracks of an MP4-family file are: enough to tell whether it
    already is the kind's format. Anything unreadable probes as no tracks."""
    size = os.path.getsize(path)
    container = sniff_file(path)
    video: Optional[TrackInfo] = None
    audio: Optional[TrackInfo] = None
    duration = 0.0
    if container in ("mp4", "mov", "3gp"):
        with open(path, "rb") as fh:
            for kind, payload, box_end in _boxes(fh, 0, size):
                if kind != b"moov":
                    continue
                for child, child_payload, child_end in _boxes(fh, payload, box_end):
                    if child == b"mvhd":
                        fh.seek(child_payload)
                        version = fh.read(1)
                        if version == b"\x01":
                            fh.seek(child_payload + 20)
                            raw = fh.read(12)
                            if len(raw) == 12:
                                timescale, length = struct.unpack(">IQ", raw)
                                duration = length / timescale if timescale else 0.0
                        else:
                            fh.seek(child_payload + 12)
                            raw = fh.read(8)
                            if len(raw) == 8:
                                timescale, length = struct.unpack(">II", raw)
                                duration = length / timescale if timescale else 0.0
                    elif child == b"trak":
                        track = _track(fh, child_payload, child_end)
                        if track is None:
                            continue
                        if track.kind == "video" and video is None:
                            video = track
                        elif track.kind == "audio" and audio is None:
                            audio = track
    return MediaProbe(container, size, duration, video, audio)


def is_kind_format(kind: str, probe: MediaProbe) -> bool:
    """§4.3 — already the kind's format, whatever its size, resolution or
    bitrate: AAC in MP4 (no picture), or H.264 (+ AAC, or silent) in MP4."""
    if probe.container != "mp4":
        return False
    if kind == "audio":
        return probe.video is None and probe.audio is not None and probe.audio.codec == "aac"
    return (
        probe.video is not None and probe.video.codec == "avc" and (probe.audio is None or probe.audio.codec == "aac")
    )


def accept_audio_video(kind: str, path: str) -> Optional[str]:
    """None when the file may be uploaded unchanged, else why not."""
    spec = media_spec(kind)
    size = os.path.getsize(path)
    if size <= 0:
        return "This file is empty."
    if size > spec["max_bytes"]:
        return f"This file is {format_size(size)} — the limit is {format_limit(spec['max_bytes'])}."
    try:
        probe = probe_mp4(path)
    except OSError as exc:
        return f"Couldn't read the file: {exc}"
    if is_kind_format(kind, probe):
        return None
    if kind == "audio":
        return (
            "QGIS can't convert audio. Pick an M4A file with AAC audio, or record it in the Geodit app or on the web."
        )
    return (
        "QGIS can't convert video. Pick an MP4 file with H.264 video (and AAC sound), or record it in "
        "the Geodit app or on the web."
    )


def image_formats_readable() -> List[str]:
    """File-dialog patterns for photos QGIS can read (Qt's image plugins)."""
    return ["*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp", "*.gif", "*.tif", "*.tiff", "*.heic", "*.heif"]
