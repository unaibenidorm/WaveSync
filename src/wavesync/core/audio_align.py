"""Language-independent audio delay estimation.

All functions work on in-memory NumPy arrays (mono ``float32``) so they are
easy to test with synthetic signals. Lag convention: the returned lag ``d``
satisfies ``A(t) ≈ B(t - d)`` where both signals are expressed on the master
time axis.

Two estimators:

* :func:`gcc_phat` – generalised cross-correlation with phase transform. The
  spectrum is whitened so that every frequency band contributes equally;
  bands where dialogue usually lives (300–3400 Hz) can be down-weighted so
  that music/effects that survive dubbing dominate. Sub-sample precision via
  parabolic interpolation.
* :func:`envelope_xcorr` – normalised cross-correlation of spectral-flux
  envelopes (robust to different mixes, coarser).

Both return a :class:`LagEstimate` with a confidence in ``[0, 1]`` derived
from the peak-to-sidelobe ratio.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from scipy import signal

VOICE_BAND = (300.0, 3400.0)


@dataclass(frozen=True, slots=True)
class LagEstimate:
    """Result of a lag estimation."""

    lag: float  #: seconds, ``A(t) ≈ B(t - lag)``
    confidence: float  #: 0..1
    psr: float  #: peak-to-sidelobe ratio (σ units)
    peak_ratio: float  #: main peak / highest secondary peak


def psr_to_confidence(psr: float, peak_ratio: float) -> float:
    """Map peak statistics to a 0..1 confidence.

    For unrelated signals the maximum of the correlation over thousands of
    lags behaves like the maximum of Gaussian noise (≈ 4–5.5 σ) with a
    secondary peak of similar height, hence the offsets below.
    """
    c_psr = np.clip((psr - 7.0) / 10.0, 0.0, 1.0)
    c_ratio = np.clip((peak_ratio - 1.15) / 0.6, 0.0, 1.0)
    return float(min(1.0, 0.5 * c_psr + 0.5 * c_ratio) if c_psr > 0 else 0.0)


def bandpass(x: np.ndarray, sr: int, low: float = 80.0, high: float = 6000.0) -> np.ndarray:
    """Zero-phase band-pass filter."""
    high = min(high, 0.45 * sr)
    sos = signal.butter(4, [low, high], btype="bandpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, x).astype(np.float32)


def normalize(x: np.ndarray) -> np.ndarray:
    """Remove DC and scale to unit RMS (silence stays silence)."""
    x = np.asarray(x, dtype=np.float32)
    x = x - x.mean() if x.size else x
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    return x / rms if rms > 1e-9 else x


def preprocess(x: np.ndarray, sr: int, band: tuple[float, float] = (80.0, 6000.0)) -> np.ndarray:
    """Band-pass + normalisation used before every estimator."""
    if x.size < 64:
        return np.asarray(x, dtype=np.float32)
    return normalize(bandpass(x, sr, *band))


def tempo_fraction(tempo: float, max_den: int = 2000) -> Fraction:
    """Rational approximation of ``tempo`` (exact for the NTSC/PAL presets)."""
    return Fraction(tempo).limit_denominator(max_den)


def map_b_to_master(b: np.ndarray, tempo: float) -> np.ndarray:
    """Resample a B signal so that it plays at master speed.

    ``out[n]`` ≈ ``b(n * tempo)``: the result has ``len(b) / tempo`` samples.
    """
    if abs(tempo - 1.0) < 1e-12:
        return np.asarray(b, dtype=np.float32)
    frac = tempo_fraction(tempo)
    # Output rate / input rate = 1 / tempo = den / num.
    return signal.resample_poly(b, frac.denominator, frac.numerator).astype(np.float32)


def _parabolic(y_m1: float, y0: float, y_p1: float) -> float:
    denom = y_m1 - 2.0 * y0 + y_p1
    if abs(denom) < 1e-20:
        return 0.0
    return float(np.clip(0.5 * (y_m1 - y_p1) / denom, -0.5, 0.5))


def _peak_stats(r: np.ndarray, k0: int, exclude: int, secondary_exclude: int) -> tuple[float, float]:
    """PSR and main/secondary peak ratio of correlation ``r`` around index ``k0``."""
    peak = float(r[k0])
    mask = np.ones(r.size, dtype=bool)
    mask[max(0, k0 - exclude): k0 + exclude + 1] = False
    side = r[mask]
    if side.size < 8:
        return 0.0, 1.0
    psr = (peak - float(side.mean())) / (float(side.std()) + 1e-12)
    mask2 = np.ones(r.size, dtype=bool)
    mask2[max(0, k0 - secondary_exclude): k0 + secondary_exclude + 1] = False
    sec = float(np.max(r[mask2])) if mask2.any() else 0.0
    ratio = peak / sec if sec > 1e-12 else 10.0
    return psr, ratio


def _lag_window(a_start: float, b_start: float, sr: float, center: float, search: float, n_a: int, n_b: int
                ) -> tuple[int, int]:
    """Integer correlation-index range covering ``center ± search``."""
    base = a_start - b_start  # lag at k = 0
    k_lo = int(np.floor((center - search - base) * sr))
    k_hi = int(np.ceil((center + search - base) * sr))
    return max(k_lo, -(n_a - 1)), min(k_hi, n_b - 1)


def gcc_phat(
    a: np.ndarray,
    b: np.ndarray,
    sr: int,
    *,
    a_start: float = 0.0,
    b_start: float = 0.0,
    center: float = 0.0,
    search: float = 1.0,
    band: tuple[float, float] = (80.0, 6000.0),
    voice_weight: float = 1.0,
) -> LagEstimate:
    """GCC-PHAT lag estimate restricted to ``center ± search``.

    ``a`` starts at master time ``a_start``; ``b`` (already on the master time
    axis, see :func:`map_b_to_master`) starts at ``b_start``.
    ``voice_weight`` (0..1) scales the 300–3400 Hz band to reduce dialogue.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    n = a.size + b.size
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    cross = fa * np.conj(fb)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    weight = ((freqs >= band[0]) & (freqs <= band[1])).astype(np.float64)
    if voice_weight != 1.0:
        weight[(freqs >= VOICE_BAND[0]) & (freqs <= VOICE_BAND[1])] *= voice_weight
    mag = np.abs(cross)
    cross = cross / (mag + 1e-12 * (mag.max() + 1e-30)) * weight
    r_full = np.fft.irfft(cross, nfft)
    # r_full[k] peaks where a[n] ≈ b[n - k]; negative k wrap to the end.
    k_lo, k_hi = _lag_window(a_start, b_start, sr, center, search, a.size, b.size)
    if k_hi <= k_lo:
        return LagEstimate(center, 0.0, 0.0, 1.0)
    ks = np.arange(k_lo, k_hi + 1)
    r = r_full[ks % nfft]
    i0 = int(np.argmax(r))
    frac = _parabolic(r[i0 - 1], r[i0], r[i0 + 1]) if 0 < i0 < r.size - 1 else 0.0
    lag = (a_start - b_start) + (ks[i0] + frac) / sr
    psr, ratio = _peak_stats(r, i0, exclude=max(2, int(0.002 * sr)), secondary_exclude=max(3, int(0.005 * sr)))
    return LagEstimate(float(lag), psr_to_confidence(psr, ratio), float(psr), float(ratio))


def spectral_flux(x: np.ndarray, sr: int, hop: int = 16, nfft: int = 512, voice_weight: float = 1.0
                  ) -> np.ndarray:
    """Onset envelope (half-wave rectified log-spectral flux) sampled at ``sr / hop``."""
    if x.size < nfft:
        return np.zeros(max(1, x.size // hop), dtype=np.float32)
    f, _t, z = signal.stft(x, fs=sr, nperseg=nfft, noverlap=nfft - hop, boundary=None, padded=False)
    mag = np.log1p(np.abs(z) * 10.0)
    w = np.ones_like(f)
    if voice_weight != 1.0:
        w[(f >= VOICE_BAND[0]) & (f <= VOICE_BAND[1])] = voice_weight
    flux = np.maximum(np.diff(mag, axis=1), 0.0)
    env = (w[:, None] * flux).sum(axis=0)
    env = np.concatenate([[0.0], env])
    # Frame j covers samples [j*hop, j*hop+nfft); its centre is the time reference.
    return env.astype(np.float32)


def envelope_xcorr(
    a: np.ndarray,
    b: np.ndarray,
    sr: int,
    *,
    a_start: float = 0.0,
    b_start: float = 0.0,
    center: float = 0.0,
    search: float = 1.0,
    hop: int = 16,
    voice_weight: float = 1.0,
) -> LagEstimate:
    """Lag estimate from normalised cross-correlation of spectral-flux envelopes."""
    ea = spectral_flux(np.asarray(a, np.float64), sr, hop, voice_weight=voice_weight)
    eb = spectral_flux(np.asarray(b, np.float64), sr, hop, voice_weight=voice_weight)
    ea = (ea - ea.mean()) / (ea.std() + 1e-12)
    eb = (eb - eb.mean()) / (eb.std() + 1e-12)
    env_sr = sr / hop
    n = ea.size + eb.size
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    r_full = np.fft.irfft(np.fft.rfft(ea, nfft) * np.conj(np.fft.rfft(eb, nfft)), nfft) / max(1, ea.size)
    k_lo, k_hi = _lag_window(a_start, b_start, env_sr, center, search, ea.size, eb.size)
    if k_hi <= k_lo:
        return LagEstimate(center, 0.0, 0.0, 1.0)
    ks = np.arange(k_lo, k_hi + 1)
    r = r_full[ks % nfft]
    i0 = int(np.argmax(r))
    frac = _parabolic(r[i0 - 1], r[i0], r[i0 + 1]) if 0 < i0 < r.size - 1 else 0.0
    lag = (a_start - b_start) + (ks[i0] + frac) / env_sr
    psr, ratio = _peak_stats(r, i0, exclude=max(2, int(0.01 * env_sr)), secondary_exclude=max(3, int(0.02 * env_sr)))
    return LagEstimate(float(lag), psr_to_confidence(psr, ratio), float(psr), float(ratio))


def block_similarity(a: np.ndarray, b: np.ndarray, sr: int, block: float = 0.25) -> np.ndarray:
    """Normalised correlation per block between two aligned, equal-length signals."""
    n = int(block * sr)
    m = min(a.size, b.size) // n
    if m == 0:
        return np.zeros(0, dtype=np.float32)
    aa = a[: m * n].reshape(m, n).astype(np.float64)
    bb = b[: m * n].reshape(m, n).astype(np.float64)
    aa -= aa.mean(axis=1, keepdims=True)
    bb -= bb.mean(axis=1, keepdims=True)
    num = (aa * bb).sum(axis=1)
    den = np.sqrt((aa * aa).sum(axis=1) * (bb * bb).sum(axis=1)) + 1e-12
    return (num / den).astype(np.float32)

