"""Video-based alignment on tiny grey thumbnails (language independent).

Pipeline per analysis window:

1. Frames are extracted by ffmpeg (see :mod:`wavesync.core.extract`) after
   cropping black borders and scaling to 32×32 grey.
2. :func:`normalize_frames` makes each frame invariant to brightness,
   contrast, gamma and HDR/SDR tone curves by replacing pixel values with
   their **rank** inside the frame (any monotonic transfer function leaves
   ranks unchanged), then z-scoring. Corners are masked (logos/watermarks).
3. :func:`measure_lag` scores every candidate lag by the mean correlation
   between each A frame and the (time-interpolated) B frame shown at the
   same master time, then refines the best lag on a fine grid with parabolic
   interpolation (sub-frame precision).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import rankdata

THUMB = 32
CORNER = 6


def _mask(size: int = THUMB, corner: int = CORNER) -> np.ndarray:
    m = np.ones((size, size), dtype=bool)
    for ys in (slice(0, corner), slice(size - corner, size)):
        for xs in (slice(0, corner), slice(size - corner, size)):
            m[ys, xs] = False
    return m.ravel()


_MASK = _mask()


@dataclass(frozen=True, slots=True)
class CropBox:
    """Crop rectangle in source pixels (ffmpeg ``crop=w:h:x:y``)."""

    w: int
    h: int
    x: int
    y: int

    def filter(self) -> str:
        """ffmpeg filter string."""
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


def detect_borders(frames: np.ndarray, src_w: int, src_h: int, threshold: float = 28.0,
                   min_fraction: float = 0.02) -> CropBox | None:
    """Detect black bars from sampled grey frames ``(N, h, w)`` (uint8).

    A row/column belongs to a bar if its mean luminance stays below
    ``threshold`` in *every* sampled frame. Returns the crop in source
    pixel coordinates, or ``None`` when no significant bar is found.
    """
    if frames.ndim != 3 or frames.shape[0] == 0:
        return None
    f = frames.astype(np.float32)
    row_max = f.mean(axis=2).max(axis=0)  # (h,)
    col_max = f.mean(axis=1).max(axis=0)  # (w,)

    def span(profile: np.ndarray) -> tuple[int, int]:
        bright = np.nonzero(profile > threshold)[0]
        if bright.size == 0:
            return 0, profile.size
        return int(bright[0]), int(bright[-1]) + 1

    y0, y1 = span(row_max)
    x0, x1 = span(col_max)
    h, w = f.shape[1], f.shape[2]
    if (y1 - y0) < 0.2 * h or (x1 - x0) < 0.2 * w:
        return None  # mostly black samples: do not trust
    if (h - (y1 - y0)) < min_fraction * h:
        y0, y1 = 0, h
    if (w - (x1 - x0)) < min_fraction * w:
        x0, x1 = 0, w
    if (y0, y1, x0, x1) == (0, h, 0, w):
        return None
    sx, sy = src_w / w, src_h / h
    # Shrink by one sample pixel on each cropped side to avoid soft edges.
    if y0 > 0:
        y0 += 1
    if y1 < h:
        y1 -= 1
    if x0 > 0:
        x0 += 1
    if x1 < w:
        x1 -= 1
    cx, cy = int(round(x0 * sx)) & ~1, int(round(y0 * sy)) & ~1
    cw = (int(round(x1 * sx)) - cx) & ~1
    ch = (int(round(y1 * sy)) - cy) & ~1
    return CropBox(cw, ch, cx, cy)


def normalize_frames(frames: np.ndarray) -> np.ndarray:
    """Rank-normalise ``(N, 32, 32)`` frames into ``(N, D)`` unit-variance vectors.

    Constant frames (pure black, fades) become zero vectors so that they do
    not contribute to similarity.
    """
    n = frames.shape[0]
    if n == 0:
        return np.zeros((0, int(_MASK.sum())), dtype=np.float32)
    flat = frames.reshape(n, -1)[:, _MASK].astype(np.float32)
    ranks = rankdata(flat, axis=1, method="average").astype(np.float32)
    ranks -= ranks.mean(axis=1, keepdims=True)
    std = ranks.std(axis=1, keepdims=True)
    # Nearly flat frames (fades, black): rank of noise → drop.
    raw_std = flat.std(axis=1, keepdims=True)
    good = (std > 1e-6) & (raw_std > 1.5)
    out = np.where(good, ranks / np.where(std > 1e-6, std, 1.0), 0.0)
    return out.astype(np.float32)


@dataclass(frozen=True, slots=True)
class VideoLag:
    """Lag estimate from thumbnails."""

    lag: float
    confidence: float
    peak: float  #: mean frame correlation at the best lag
    second: float  #: best correlation away (> ``exclusion`` s) from the peak


def _scores(sim: np.ndarray, a_times: np.ndarray, b_times: np.ndarray, lags: np.ndarray,
            min_coverage: float) -> np.ndarray:
    """Mean interpolated similarity for every candidate lag."""
    na, nb = sim.shape
    q = a_times[:, None] - lags[None, :]  # (na, L) master times looked up in B
    pos = np.interp(q, b_times, np.arange(nb, dtype=np.float64), left=np.nan, right=np.nan)
    valid = ~np.isnan(pos)
    pos = np.where(valid, pos, 0.0)
    j0 = np.minimum(np.floor(pos).astype(np.int64), nb - 2) if nb >= 2 else np.zeros_like(pos, dtype=np.int64)
    frac = pos - j0
    rows = np.broadcast_to(np.arange(na)[:, None], j0.shape)
    if nb >= 2:
        s = (1.0 - frac) * sim[rows, j0] + frac * sim[rows, j0 + 1]
    else:
        s = sim[rows, j0]
    s = np.where(valid, s, 0.0)
    cnt = valid.sum(axis=0)
    # Ignore frames that are flat in A (zero vectors) for the coverage denominator.
    out = s.sum(axis=0) / np.maximum(cnt, 1)
    out[cnt < min_coverage * na] = np.nan
    return out


def frame_similarity(a_vec: np.ndarray, a_times: np.ndarray, b_vec: np.ndarray, b_times: np.ndarray,
                     lag: float) -> np.ndarray:
    """Per-A-frame correlation with B at a fixed ``lag`` (NaN where B is missing or A is flat)."""
    if a_vec.shape[0] == 0 or b_vec.shape[0] == 0:
        return np.full(a_vec.shape[0], np.nan, dtype=np.float32)
    sim = (a_vec @ b_vec.T) / a_vec.shape[1]
    nb = b_vec.shape[0]
    pos = np.interp(a_times - lag, b_times, np.arange(nb, dtype=np.float64), left=np.nan, right=np.nan)
    out = np.full(a_vec.shape[0], np.nan, dtype=np.float32)
    ok = ~np.isnan(pos)
    if nb < 2:
        out[ok] = sim[ok, 0]
    else:
        j0 = np.minimum(np.floor(pos[ok]).astype(np.int64), nb - 2)
        f = pos[ok] - j0
        rows = np.nonzero(ok)[0]
        out[ok] = (1 - f) * sim[rows, j0] + f * sim[rows, j0 + 1]
    flat = ~np.any(a_vec != 0, axis=1)
    out[flat] = np.nan
    return out


def measure_lag(
    a_vec: np.ndarray,
    a_times: np.ndarray,
    b_vec: np.ndarray,
    b_times: np.ndarray,
    *,
    center: float = 0.0,
    search: float = 30.0,
    coarse_step: float = 0.02,
    exclusion: float = 1.0,
    min_coverage: float = 0.5,
) -> VideoLag | None:
    """Estimate ``d`` such that A(t) ≈ B(t - d) from normalised frame vectors.

    ``a_times``/``b_times`` are frame timestamps on the master axis (B times
    already divided by the tempo hypothesis). Returns ``None`` when there is
    not enough usable (non-flat) material.
    """
    usable = np.any(a_vec != 0, axis=1)
    if usable.sum() < 8 or b_vec.shape[0] < 8:
        return None
    a_vec, a_times = a_vec[usable], a_times[usable]
    sim = (a_vec @ b_vec.T) / a_vec.shape[1]
    lags = np.arange(center - search, center + search + coarse_step / 2, coarse_step)
    sc = _scores(sim, a_times, b_times, lags, min_coverage)
    if np.all(np.isnan(sc)):
        return None
    i0 = int(np.nanargmax(sc))
    # Fine search ±2 coarse steps at 1 ms.
    fine = np.arange(lags[i0] - 2 * coarse_step, lags[i0] + 2 * coarse_step + 5e-4, 0.001)
    fs = _scores(sim, a_times, b_times, fine, min_coverage)
    if np.all(np.isnan(fs)):
        return None
    j0 = int(np.nanargmax(fs))
    # Sub-millisecond: centroid of the plateau-tolerant top region + parabola.
    best = fine[j0]
    if 0 < j0 < fs.size - 1 and not np.isnan(fs[j0 - 1]) and not np.isnan(fs[j0 + 1]):
        y_m, y0, y_p = fs[j0 - 1], fs[j0], fs[j0 + 1]
        den = y_m - 2 * y0 + y_p
        if abs(den) > 1e-12:
            best += 0.001 * float(np.clip(0.5 * (y_m - y_p) / den, -0.5, 0.5))
    peak = float(fs[j0])
    far = np.abs(lags - best) > exclusion
    second = float(np.nanmax(sc[far])) if np.any(far & ~np.isnan(sc)) else 0.0
    distinct = np.clip((peak - second) / 0.15, 0.0, 1.0)
    conf = float(np.clip(peak / 0.7, 0.0, 1.0) * distinct)
    return VideoLag(float(best), conf, peak, second)

