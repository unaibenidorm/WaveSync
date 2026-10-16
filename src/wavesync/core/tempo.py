"""Tempo presets and the time-mapping convention.

**Convention.** ``tempo`` is the speed multiplier applied to audio B. A sound
at ``t_B`` in B is heard at ``t_A = t_B / tempo + offset`` in the master.

A preset named ``"X→Y"`` conforms material running at *X* fps (the B
source) to *Y* fps (the master): ``tempo = Y / X``. Example: a PAL DVD (B,
25 fps, sped up 4 %) against a 23.976 fps Blu-ray (A) → ``25→23.976`` →
``tempo = (24000/1001) / 25 ≈ 0.959041`` (B is slowed down).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

NTSC_FILM = Fraction(24000, 1001)


@dataclass(frozen=True, slots=True)
class TempoPreset:
    """A known frame-rate conversion."""

    key: str
    source_fps: Fraction  #: fps of B
    target_fps: Fraction  #: fps of A

    @property
    def tempo(self) -> float:
        """Speed multiplier for audio B."""
        return float(self.target_fps / self.source_fps)

    @property
    def fraction(self) -> Fraction:
        """Exact tempo as a fraction."""
        return self.target_fps / self.source_fps


PRESETS: tuple[TempoPreset, ...] = (
    TempoPreset("25→23.976", Fraction(25), NTSC_FILM),
    TempoPreset("23.976→25", NTSC_FILM, Fraction(25)),
    TempoPreset("24→23.976", Fraction(24), NTSC_FILM),
    TempoPreset("23.976→24", NTSC_FILM, Fraction(24)),
    TempoPreset("24→25", Fraction(24), Fraction(25)),
    TempoPreset("25→24", Fraction(25), Fraction(24)),
)


def preset_by_key(key: str) -> TempoPreset:
    """Look up a preset by key; raises ``KeyError``."""
    for p in PRESETS:
        if p.key == key:
            return p
    raise KeyError(key)


def snap_to_preset(
    tempo: float, tolerance: float = 8.0e-4, *, tol: float | None = None
) -> TempoPreset | None:
    """Return the preset closest to ``tempo`` if within ``tolerance``."""
    effective_tolerance = tolerance if tol is None else tol
    best = min(PRESETS, key=lambda p: abs(p.tempo - tempo))
    return best if abs(best.tempo - tempo) <= effective_tolerance else None


def candidate_tempos(duration_ratio: float | None = None, fps_ratio: float | None = None) -> list[float]:
    """Tempo hypotheses to try, most plausible first.

    ``duration_ratio`` = duration(B) / duration(A) ≈ tempo;
    ``fps_ratio`` = fps(A) / fps(B) when frame rates differ.
    """
    cands = [1.0] + [p.tempo for p in PRESETS]
    hints = [h for h in (fps_ratio, duration_ratio) if h and 0.5 < h < 2.0]

    def score(c: float) -> float:
        if not hints:
            return 0.0 if c == 1.0 else 1.0
        return min(abs(c - h) for h in hints) - (1e-3 if c == 1.0 else 0.0)

    return sorted(cands, key=score)


def a_to_b(t_a: float, offset: float, tempo: float) -> float:
    """Map a master time to the corresponding B time."""
    return (t_a - offset) * tempo


def b_to_a(t_b: float, offset: float, tempo: float) -> float:
    """Map a B time to the corresponding master time."""
    return t_b / tempo + offset

