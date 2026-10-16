"""Measurers: objects that estimate the local lag between A and B in a window.

A measurer answers two questions, always on the master time axis with B
resampled by a tempo hypothesis ``h`` (``B_mapped(m) = B(m·h)``):

* :meth:`Measurer.measure` – lag ``d`` such that ``A(t) ≈ B_mapped(t - d)``
  inside ``[t_a, t_a + win)``, searched in ``center ± search``.
* :meth:`Measurer.profile` – fine-grained similarity over time for a set of
  fixed lags (used to locate edit points between segments).

:class:`VideoMeasurer` works on files (ffmpeg thumbnails);
:class:`AudioMeasurer` works on any pair of ``read(start, duration)``
callables, so tests can feed synthetic arrays.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

import numpy as np

from wavesync.core import audio_align as aa
from wavesync.core import video_align as va
from wavesync.core.errors import CancelToken
from wavesync.core.extract import detect_crop, extract_audio, extract_frames
from wavesync.core.models import AudioStream, MediaInfo


@dataclass(frozen=True, slots=True)
class Measure:
    """One local lag measurement."""

    lag: float
    confidence: float


class Measurer(Protocol):
    """Protocol implemented by video and audio measurers."""

    kind: str  #: "video" or "audio"

    def measure(self, t_a: float, win: float, center: float, search: float, h: float) -> Measure | None:
        """Estimate the lag in a window."""
        ...

    def profile(self, t0: float, t1: float, lags: list[float], h: float) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(times, sims[len(lags), N])`` of local similarity for fixed lags."""
        ...


class VideoMeasurer:
    """Thumbnail-based measurer over two video files."""

    kind = "video"

    def __init__(self, a: MediaInfo, b: MediaInfo, cancel: CancelToken | None = None,
                 crop_a: va.CropBox | None | str = "auto", crop_b: va.CropBox | None | str = "auto") -> None:
        self.a, self.b, self.cancel = a, b, cancel
        self.crop_a = detect_crop(a, cancel) if crop_a == "auto" else crop_a
        self.crop_b = detect_crop(b, cancel) if crop_b == "auto" else crop_b
        self._cache: OrderedDict[tuple[float, float], tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self._b_cache: OrderedDict[tuple[float, float, float], tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self._cache_lock = RLock()

    def _a(self, t0: float, dur: float) -> tuple[np.ndarray, np.ndarray]:
        key = (round(t0, 4), round(dur, 4))
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        frames, times = extract_frames(self.a, t0, dur, self.crop_a, self.cancel)
        val = (va.normalize_frames(frames), times)
        with self._cache_lock:
            self._cache[key] = val
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
        return val

    def _b(self, m0: float, m1: float, h: float) -> tuple[np.ndarray, np.ndarray]:
        """B frames covering master-axis interval ``[m0, m1]`` for tempo ``h``."""
        key = (round(m0, 4), round(m1, 4), round(h, 8))
        with self._cache_lock:
            if key in self._b_cache:
                self._b_cache.move_to_end(key)
                return self._b_cache[key]
        frames, times = extract_frames(self.b, m0 * h, (m1 - m0) * h, self.crop_b, self.cancel)
        val = (va.normalize_frames(frames), times / h)
        with self._cache_lock:
            self._b_cache[key] = val
            while len(self._b_cache) > 48:
                self._b_cache.popitem(last=False)
        return val

    def measure(self, t_a: float, win: float, center: float, search: float, h: float) -> Measure | None:
        av, at = self._a(t_a, win)
        if at.size == 0:
            return None
        bv, bt = self._b(t_a - center - search, t_a + win - center + search, h)
        res = va.measure_lag(av, at, bv, bt, center=center, search=search)
        return None if res is None else Measure(res.lag, res.confidence)

    def profile(self, t0: float, t1: float, lags: list[float], h: float) -> tuple[np.ndarray, np.ndarray]:
        av, at = self._a(t0, t1 - t0)
        bv, bt = self._b(t0 - max(lags) - 0.5, t1 - min(lags) + 0.5, h)
        sims = np.stack([va.frame_similarity(av, at, bv, bt, d) for d in lags]) if at.size else np.zeros((len(lags), 0))
        return at, sims


Reader = Callable[[float, float], np.ndarray]


class AudioMeasurer:
    """GCC-PHAT / envelope measurer over two PCM readers.

    ``read_a(start, dur)`` returns master PCM; ``read_b(start, dur)`` returns
    B PCM in B's own time axis. Both at ``sr`` Hz, zero-padded outside the file.
    """

    kind = "audio"

    def __init__(self, read_a: Reader, read_b: Reader, sr: int = 16000, method: str = "gcc",
                 voice_weight: float = 0.3, band: tuple[float, float] = (80.0, 6000.0),
                 cancel: CancelToken | None = None) -> None:
        self.read_a, self.read_b, self.sr = read_a, read_b, sr
        self.method, self.voice_weight, self.band, self.cancel = method, voice_weight, band, cancel

    def _b_master(self, m0: float, m1: float, h: float) -> np.ndarray:
        raw = self.read_b(m0 * h, (m1 - m0) * h)
        return aa.map_b_to_master(raw, h)

    def measure(self, t_a: float, win: float, center: float, search: float, h: float) -> Measure | None:
        if self.cancel:
            self.cancel.raise_if_cancelled()
        a = self.read_a(t_a, win)
        if not np.any(a):
            return None
        m0 = t_a - center - search
        b = self._b_master(m0, t_a + win - center + search, h)
        if not np.any(b):
            return None
        a = aa.preprocess(a, self.sr, self.band)
        b = aa.preprocess(b, self.sr, self.band)
        if self.method == "envelope":
            est = aa.envelope_xcorr(a, b, self.sr, a_start=t_a, b_start=m0, center=center, search=search,
                                    voice_weight=self.voice_weight)
        else:
            est = aa.gcc_phat(a, b, self.sr, a_start=t_a, b_start=m0, center=center, search=search,
                              band=self.band, voice_weight=self.voice_weight)
        return Measure(est.lag, est.confidence)

    def profile(self, t0: float, t1: float, lags: list[float], h: float, block: float = 0.25
                ) -> tuple[np.ndarray, np.ndarray]:
        a = aa.preprocess(self.read_a(t0, t1 - t0), self.sr, self.band)
        rows = []
        for d in lags:
            b = aa.preprocess(self._b_master(t0 - d, t1 - d, h), self.sr, self.band)
            n = min(a.size, b.size)
            rows.append(aa.block_similarity(a[:n], b[:n], self.sr, block))
        m = min(r.size for r in rows) if rows else 0
        sims = np.stack([r[:m] for r in rows]) if rows else np.zeros((0, 0))
        times = t0 + (np.arange(m) + 0.5) * block
        # Correlation of raw waveforms is noisy; rescale so matching blocks ≈ video scale.
        return times, np.clip(sims * 2.0, -1.0, 1.0)


def file_audio_measurer(a: MediaInfo, a_stream: AudioStream, b: MediaInfo, b_stream: AudioStream,
                        *, sr: int = 16000, mode: str = "full", method: str = "gcc", voice_weight: float = 0.3,
                        cancel: CancelToken | None = None) -> AudioMeasurer:
    """:class:`AudioMeasurer` reading both files through ffmpeg."""

    def ra(start: float, dur: float) -> np.ndarray:
        return extract_audio(a, a_stream, start, dur, sr, mode, cancel)

    def rb(start: float, dur: float) -> np.ndarray:
        return extract_audio(b, b_stream, start, dur, sr, mode, cancel)

    return AudioMeasurer(ra, rb, sr, method, voice_weight, cancel=cancel)


def array_audio_measurer(a: np.ndarray, b: np.ndarray, sr: int, **kw: object) -> AudioMeasurer:
    """:class:`AudioMeasurer` over in-memory arrays (tests, offline use)."""

    def reader(x: np.ndarray) -> Reader:
        def read(start: float, dur: float) -> np.ndarray:
            n = int(round(dur * sr))
            i0 = int(round(start * sr))
            out = np.zeros(n, dtype=np.float32)
            s0, s1 = max(0, i0), min(x.size, i0 + n)
            if s1 > s0:
                out[s0 - i0: s1 - i0] = x[s0:s1]
            return out
        return read

    return AudioMeasurer(reader(a), reader(b), sr, **kw)  # type: ignore[arg-type]
