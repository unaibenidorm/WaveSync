"""Windowed extraction of analysis signals with ffmpeg (never the whole film).

* :func:`extract_frames` – grey 32×32 thumbnails at native frame rate plus
  exact timestamps (read from the ``showinfo`` filter), cropped of borders.
* :func:`sample_border_frames` – a few small frames spread across the file
  for black-bar detection.
* :func:`extract_audio` – mono (or side channel) float32 PCM at 16 kHz.

All times are seconds relative to the file start.
"""

from __future__ import annotations

import re

import numpy as np

from wavesync.core.errors import CancelToken, FFmpegError
from wavesync.core.ffmpeg import binaries, fmt_time, run
from wavesync.core.models import AudioStream, MediaInfo
from wavesync.core.video_align import THUMB, CropBox, detect_borders

_PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+(?:e-?\d+)?)")
BORDER_SAMPLE_W = 160


def _base(cancel_safe: bool = True) -> list[str]:
    return [str(binaries().ffmpeg), "-hide_banner", "-nostdin"]


def _run_decode(args: list[str], cancel: CancelToken | None) -> tuple[bytes, str]:
    """Decode with FFmpeg's best available hardware backend, then fall back to CPU.

    ``-hwaccel auto`` lets FFmpeg select NVDEC, VAAPI, QSV or another backend
    supported by the installed build. The filters still produce the same
    software-visible 32x32 grayscale frames; hardware acceleration only
    changes how the compressed source is decoded.
    """
    try:
        insert_at = 1
        accelerated = [*args[:insert_at], "-hwaccel", "auto", *args[insert_at:]]
        return run(accelerated, cancel)
    except FFmpegError:
        # Hardware support is optional. Retry the exact same operation on CPU
        # rather than returning a silent or partial analysis.
        return run(args, cancel)


def extract_frames(
    info: MediaInfo,
    start: float,
    duration: float,
    crop: CropBox | None = None,
    cancel: CancelToken | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(frames uint8 (N,32,32), times float64 (N,))`` for ``[start, start+duration)``."""
    if info.video is None:
        raise ValueError("no video stream")
    start_c = max(0.0, start)
    duration = duration - (start_c - start)
    if duration <= 0 or start_c >= info.duration:
        return np.zeros((0, THUMB, THUMB), np.uint8), np.zeros(0)
    vf = []
    if crop is not None:
        vf.append(crop.filter())
    vf += [f"scale={THUMB}:{THUMB}:flags=area", "format=gray", "showinfo"]
    args = _base() + [
        "-loglevel", "info",
        "-ss", fmt_time(start_c), "-t", fmt_time(duration), "-copyts",
        "-i", str(info.path),
        "-map", f"0:{info.video.index}", "-an", "-sn", "-dn",
        "-vf", ",".join(vf), "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    out, err = _run_decode(args, cancel)
    fsize = THUMB * THUMB
    n = len(out) // fsize
    frames = np.frombuffer(out[: n * fsize], dtype=np.uint8).reshape(n, THUMB, THUMB)
    times = np.array([float(m.group(1)) for m in _PTS_RE.finditer(err)], dtype=np.float64) - info.start_time
    m = min(n, times.size)
    frames, times = frames[:m], times[:m]
    order = np.argsort(times, kind="stable")
    return frames[order], times[order]


def sample_border_frames(info: MediaInfo, n: int = 9, cancel: CancelToken | None = None) -> np.ndarray:
    """Grab ``n`` small grey frames spread over the file (for border detection)."""
    if info.video is None or info.video.width == 0:
        return np.zeros((0, 1, 1), np.uint8)
    w = BORDER_SAMPLE_W
    h = max(2, int(round(w * info.video.height / info.video.width / 2)) * 2)
    frames = []
    for i in range(n):
        t = info.duration * (0.08 + 0.84 * i / max(1, n - 1))
        args = _base() + [
            "-loglevel", "error", "-ss", fmt_time(t), "-i", str(info.path),
            "-map", f"0:{info.video.index}", "-frames:v", "1",
            "-vf", f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "pipe:1",
        ]
        try:
            out, _ = _run_decode(args, cancel)
        except FFmpegError:
            out, _ = run(args, cancel, check=False)
        if len(out) >= w * h:
            frames.append(np.frombuffer(out[: w * h], dtype=np.uint8).reshape(h, w))
    return np.stack(frames) if frames else np.zeros((0, h, w), np.uint8)


def detect_crop(info: MediaInfo, cancel: CancelToken | None = None) -> CropBox | None:
    """Detect black bars of ``info``'s video (``None`` if none)."""
    if info.video is None:
        return None
    return detect_borders(sample_border_frames(info, cancel=cancel), info.video.width, info.video.height)


def extract_audio(
    info: MediaInfo,
    stream: AudioStream,
    start: float,
    duration: float,
    sr: int = 16000,
    mode: str = "full",
    cancel: CancelToken | None = None,
) -> np.ndarray:
    """PCM float32 for ``[start, start+duration)``; zero-padded where outside the file.

    ``mode="sides"`` returns L−R of a stereo downmix (removes centre-panned
    dialogue); falls back to ``"full"`` for mono streams.
    """
    total = max(0, int(round(duration * sr)))
    out_arr = np.zeros(total, dtype=np.float32)
    start_c = max(0.0, start)
    lead = int(round((start_c - start) * sr))
    dur_c = duration - (start_c - start)
    if dur_c <= 0 or start_c >= info.duration or total == 0:
        return out_arr
    sides = mode == "sides" and stream.channels >= 2
    args = _base() + [
        "-loglevel", "error", "-ss", fmt_time(start_c), "-t", fmt_time(dur_c),
        "-i", str(info.path), "-map", f"0:{stream.index}", "-vn", "-sn", "-dn",
        "-af", "aresample=async=1",
        "-ac", "2" if sides else "1", "-ar", str(sr), "-f", "f32le", "pipe:1",
    ]
    out, _ = _run_decode(args, cancel)
    pcm = np.frombuffer(out[: len(out) // 4 * 4], dtype=np.float32)
    if sides:
        pcm = pcm[: pcm.size // 2 * 2].reshape(-1, 2)
        diff = (pcm[:, 0] - pcm[:, 1]) * 0.5
        mid = (pcm[:, 0] + pcm[:, 1]) * 0.5
        diff_rms = float(np.sqrt(np.mean(diff * diff))) if diff.size else 0.0
        mid_rms = float(np.sqrt(np.mean(mid * mid))) if mid.size else 0.0
        if diff_rms < 0.15 * mid_rms:
            pcm = mid
        else:
            pcm = diff + 0.25 * mid
    k = min(pcm.size, total - lead)
    if k > 0:
        out_arr[lead: lead + k] = pcm[:k]
    return out_arr
