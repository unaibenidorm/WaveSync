"""Probing media files with ``ffprobe -print_format json``."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from wavesync.core.errors import CancelToken, FFmpegError, MediaError
from wavesync.core.ffmpeg import binaries, run
from wavesync.core.i18n import _
from wavesync.core.models import AudioStream, MediaInfo, VideoStream

_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def _rate(value: str | None) -> float:
    if not value or value in ("0/0", "N/A"):
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


def _tag_duration(tags: dict[str, str]) -> float | None:
    """Parse Matroska ``DURATION`` tags like ``01:23:45.678000000``."""
    for key, val in tags.items():
        if key.upper().startswith("DURATION"):
            try:
                h, m, s = val.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                return None
    return None


def parse_probe(path: Path, data: dict[str, Any]) -> MediaInfo:
    """Build a :class:`MediaInfo` from ffprobe's JSON output."""
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    audio: list[AudioStream] = []
    video: VideoStream | None = None
    subs = 0
    for s in streams:
        ctype = s.get("codec_type")
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        if ctype == "audio":
            audio.append(
                AudioStream(
                    index=int(s["index"]),
                    position=len(audio),
                    codec=s.get("codec_name", "?"),
                    channels=int(s.get("channels") or 0),
                    channel_layout=s.get("channel_layout", "") or "",
                    sample_rate=int(s.get("sample_rate") or 0),
                    language=tags.get("language", ""),
                    title=tags.get("title", ""),
                    duration=_float(s.get("duration")) or _tag_duration(s.get("tags") or {}),
                    bit_rate=int(s["bit_rate"]) if str(s.get("bit_rate", "")).isdigit() else None,
                    default=bool(disp.get("default")),
                )
            )
        elif ctype == "video" and video is None and not disp.get("attached_pic"):
            side = s.get("side_data_list") or []
            hdr = (s.get("color_transfer") in _HDR_TRANSFERS) or any(
                "Mastering display" in (d.get("side_data_type") or "") or "DOVI" in (d.get("side_data_type") or "")
                for d in side
            )
            video = VideoStream(
                index=int(s["index"]),
                codec=s.get("codec_name", "?"),
                width=int(s.get("width") or 0),
                height=int(s.get("height") or 0),
                fps_real=_rate(s.get("r_frame_rate")),
                fps_avg=_rate(s.get("avg_frame_rate")),
                hdr=bool(hdr),
                pix_fmt=s.get("pix_fmt", "") or "",
                color_transfer=s.get("color_transfer", "") or "",
            )
        elif ctype == "subtitle":
            subs += 1
    duration = _float(fmt.get("duration"))
    if duration is None:
        cands = [_float(s.get("duration")) or _tag_duration(s.get("tags") or {}) for s in streams]
        cands = [c for c in cands if c]
        duration = max(cands) if cands else 0.0
    return MediaInfo(
        path=path,
        duration=float(duration),
        format_name=fmt.get("format_name", ""),
        start_time=_float(fmt.get("start_time")) or 0.0,
        audio_streams=tuple(audio),
        video=video,
        subtitle_count=subs,
        chapter_count=len(data.get("chapters") or []),
        size_bytes=int(fmt.get("size") or 0),
    )


def probe(path: str | Path, cancel: CancelToken | None = None) -> MediaInfo:
    """Probe ``path``. Raises :class:`MediaError` for missing/corrupt files."""
    p = Path(path)
    if not p.is_file():
        raise MediaError(_("El archivo «{name}» no existe.").format(name=p.name))
    args = [
        str(binaries().ffprobe), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(p),
    ]
    try:
        out, _err = run(args, cancel)
    except FFmpegError as exc:
        raise MediaError(
            _("No se pudo leer «{name}»: el archivo está dañado o no es un formato multimedia.").format(name=p.name),
            exc.detail,
        ) from exc
    try:
        data = json.loads(out.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(_("ffprobe devolvió datos ilegibles para «{name}».").format(name=p.name)) from exc
    if not data.get("streams"):
        raise MediaError(_("«{name}» no contiene pistas multimedia.").format(name=p.name))
    return parse_probe(p, data)


def require_audio(info: MediaInfo) -> None:
    """Raise :class:`MediaError` if ``info`` has no audio stream."""
    if not info.has_audio:
        raise MediaError(_("«{name}» no tiene pistas de audio.").format(name=info.path.name))

