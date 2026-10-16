"""Synchronisation engine: video first, audio as refinement, segment detection.

Entry points:

* :func:`analyze` – files in, :class:`~wavesync.core.models.SyncResult` out.
* :func:`analyze_with` – same algorithm on arbitrary measurers (tests).

Callbacks: ``on_progress(fraction: float, message: str)`` is invoked from the
worker thread; the UI must marshal it to the main loop itself.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from wavesync.core.errors import AnalysisError, CancelToken
from wavesync.core.ffmpeg import hwaccel_summary
from wavesync.core.i18n import _
from wavesync.core.measure import Measure, Measurer, VideoMeasurer, file_audio_measurer
from wavesync.core.models import AudioStream, MediaInfo, SyncResult, SyncSegment, SyncWindow, Verdict
from wavesync.core.probe import require_audio
from wavesync.core.robust import ransac_line, segment_steps, shared_slope_fit
from wavesync.core.tempo import PRESETS, candidate_tempos, snap_to_preset

ProgressCb = Callable[[float, str], None]


@dataclass(slots=True)
class SyncParams:
    """Tunable analysis parameters (defaults suit feature films)."""

    search_range: float = 60.0  #: ± seconds searched around the expected offset
    n_windows: int = 32  #: ≥ 20 windows spread over the film
    video_window: float = 20.0
    audio_window: float = 60.0  #: audio window for refinement / audio-only mode
    audio_primary_window: float = 30.0  #: window when audio is the primary method
    audio_refine: bool = True
    audio_method: str = "gcc"  #: "gcc" or "envelope"
    audio_mode: str = "sides"  #: "sides" (L−R + attenuated mid) or "full"
    voice_weight: float = 0.2  #: weight of 300–3400 Hz in GCC-PHAT
    audio_refine_range: float = 0.09  #: tight sub-frame range (±90 ms) around video offset
    video_conf_threshold: float = 0.3
    audio_conf_threshold: float = 0.40
    disagreement_threshold: float = 0.065
    max_hypotheses: int = 7
    forced_tempo: float | None = None
    boundary_block: float = 6.0
    edge_margin: float = 20.0
    analysis_workers: int = 2  #: bounded parallelism for independent windows


def _tol(kind: str) -> tuple[float, float]:
    """(fit threshold, segment tolerance) per primary measurer kind."""
    return (0.09, 0.12) if kind == "video" else (0.012, 0.03)


def _offset_from_lag(d: float, c: float, tempo: float, h: float) -> float:
    """Convert a lag measured with hypothesis ``h`` at master time ``c`` into an offset for ``tempo``."""
    return (d - c * (1.0 - tempo / h)) * h / tempo


def _lag_from_offset(off: float, c: float, tempo: float, h: float) -> float:
    return c * (1.0 - tempo / h) + off * tempo / h


def analyze(
    master: MediaInfo,
    source: MediaInfo,
    master_stream: AudioStream | None,
    source_stream: AudioStream,
    params: SyncParams | None = None,
    on_progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
) -> SyncResult:
    """Synchronise ``source`` (B) against ``master`` (A).

    Uses video alignment if both files have video; otherwise audio becomes
    the primary method (with a reliability warning).
    """
    params = params or SyncParams()
    cancel = cancel or CancelToken()
    progress = on_progress or (lambda f, m: None)
    require_audio(source)
    messages: list[str] = []
    messages.append(_("Backend de extracción: {backend}").format(backend=hwaccel_summary()))
    refiner: Measurer | None = None
    if master.has_video and source.has_video:
        progress(0.0, _("Detectando bandas negras…"))
        primary: Measurer = VideoMeasurer(master, source, cancel)
        if params.audio_refine and master_stream is not None:
            refiner = file_audio_measurer(master, master_stream, source, source_stream, mode=params.audio_mode,
                                          method=params.audio_method, voice_weight=params.voice_weight,
                                          cancel=cancel)
        fa = master.video.fps_avg or master.video.fps_real if master.video else 0
        fb = source.video.fps_avg or source.video.fps_real if source.video else 0
        fps_ratio = fa / fb if fa and fb and abs(fa / fb - 1) > 0.002 else None
    else:
        if master_stream is None:
            raise AnalysisError(_("Sin vídeo en ambos archivos hace falta una pista de audio del máster."))
        messages.append(_("B no tiene vídeo: se sincroniza solo por audio. Entre idiomas distintos el "
                          "resultado es menos fiable; revísalo en el comparador."))
        primary = file_audio_measurer(master, master_stream, source, source_stream, mode=params.audio_mode,
                                      method=params.audio_method, voice_weight=params.voice_weight, cancel=cancel)
        fps_ratio = None
    return analyze_with(primary, refiner, master.duration, source.duration, params, fps_ratio=fps_ratio,
                        on_progress=progress, cancel=cancel, messages=messages)


def _measure_safe(m: Measurer, *args: float) -> Measure | None:
    try:
        return m.measure(*args)
    except AnalysisError:
        return None


def _score_probe(lags: list[float], confs: list[float], conf_thr: float) -> tuple[int, float]:
    """Score a tempo hypothesis: (count of confident matches, average confident confidence)."""
    good = [c for c in confs if c >= conf_thr]
    if not good:
        return (0, float(np.mean(confs)) if confs else 0.0)
    return (len(good), float(np.mean(good)))


def _measure_many(
    primary: Measurer,
    jobs: list[tuple[float, float, float, float, float]],
    workers: int,
    cancel: CancelToken,
) -> list[Measure | None]:
    """Measure independent windows concurrently while preserving input order."""
    if workers <= 1 or len(jobs) < 2:
        return [_measure_safe(primary, *job) for job in jobs]
    with ThreadPoolExecutor(max_workers=min(4, workers), thread_name_prefix="wavesync-measure") as pool:
        futures = [pool.submit(_measure_safe, primary, *job) for job in jobs]
        results: list[Measure | None] = []
        for future in futures:
            cancel.raise_if_cancelled()
            results.append(future.result())
        return results


def analyze_with(
    primary: Measurer,
    refiner: Measurer | None,
    dur_a: float,
    dur_b: float,
    params: SyncParams | None = None,
    *,
    fps_ratio: float | None = None,
    on_progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
    messages: list[str] | None = None,
) -> SyncResult:
    """Core algorithm on abstract measurers (see module docstring)."""
    p = params or SyncParams()
    cancel = cancel or CancelToken()
    progress = on_progress or (lambda f, m: None)
    messages = list(messages or [])
    kind = primary.kind
    fit_tol, seg_tol = _tol(kind)
    conf_thr = p.video_conf_threshold if kind == "video" else p.audio_conf_threshold
    S = p.search_range

    win = p.video_window if kind == "video" else p.audio_primary_window
    win = max(2.0, min(win, dur_a * 0.4))
    margin = min(p.edge_margin, 0.03 * dur_a)
    span = dur_a - 2 * margin - win
    if span <= 0:
        margin, span = 0.0, max(0.0, dur_a - win)
    n = max(2, p.n_windows)
    starts = margin + np.linspace(0.0, span, n)

    # --- 1. tempo hypothesis -------------------------------------------------
    if p.forced_tempo:
        cands = [p.forced_tempo]
    else:
        cands = candidate_tempos(dur_b / dur_a if dur_a > 0 else None, fps_ratio)[: p.max_hypotheses]

    # For feature films (> 300s), search a wider intro margin (up to 90s) on initial probe:
    S_probe = max(S, min(90.0, dur_a * 0.3)) if dur_a > 300.0 else S
    probe_idx = sorted({int(round(f * (n - 1))) for f in (0.18, 0.34, 0.50, 0.66, 0.82)})
    best: tuple[tuple[int, float], float, list[float]] = ((-1, -1.0), 1.0, [])
    total_steps = len(cands) * len(probe_idx)
    step = 0
    for h in cands:
        jobs = [(float(starts[i]), win, 0.0, S_probe, h) for i in probe_idx]
        progress(0.15 * step / max(1, total_steps), _("Probando factor de tempo {t:.6f}…").format(t=h))
        measured = _measure_many(primary, jobs, p.analysis_workers, cancel)
        step += len(jobs)
        confs, lags = [], []
        for m in measured:
            confs.append(m.confidence if m else 0.0)
            if m and m.confidence >= conf_thr:
                lags.append(m.lag)
        score = _score_probe(lags, confs, conf_thr)
        if score > best[0]:
            best = (score, h, lags)
    score_h, h, probe_lags = best
    center0 = float(np.median(probe_lags)) if probe_lags else 0.0

    # --- 2. windows ----------------------------------------------------------
    raw: list[Measure | None] = []
    S_win = min(S, 60.0)
    jobs = [(float(t), win, center0, S_win, h) for t in starts]
    progress(0.15, _("Analizando ventanas…"))
    raw = _measure_many(primary, jobs, p.analysis_workers, cancel)
    centers = starts + win / 2
    lag_arr = np.array([m.lag if m else np.nan for m in raw])
    conf_arr = np.array([m.confidence if m else 0.0 for m in raw])
    good = (conf_arr >= conf_thr) & ~np.isnan(lag_arr)

    # Adaptive wide search fallback if normal range found nothing (< 2 good windows)
    # This automatically recovers movies (like Cars) that have > 90-150 s intro differences
    # (FBI warnings, Disney FastPlay, THX trailers, different studio bumpers).
    if good.sum() < 2 and dur_a > 180.0:
        wide_S = min(180.0, dur_a * 0.35)
        progress(0.60, _("Buscando en rango ampliado (±{s:.0f} s) por posibles intros diferentes…").format(s=wide_S))
        wide_probe_idx = sorted({int(round(f * (n - 1))) for f in (0.15, 0.25, 0.38, 0.50, 0.62, 0.75, 0.88)})
        wide_best: tuple[tuple[int, float], float, list[float]] = ((-1, -1.0), h, [])
        for wh in cands:
            wlags, wconfs = [], []
            wide_jobs = [(float(starts[wi]), win, 0.0, wide_S, wh) for wi in wide_probe_idx]
            wide_measured = _measure_many(primary, wide_jobs, p.analysis_workers, cancel)
            for wm in wide_measured:
                wconfs.append(wm.confidence if wm else 0.0)
                if wm and wm.confidence >= conf_thr:
                    wlags.append(wm.lag)
            wscore = _score_probe(wlags, wconfs, conf_thr)
            if wscore > wide_best[0]:
                wide_best = (wscore, wh, wlags)
        if wide_best[2]:
            h = wide_best[1]
            center0 = float(np.median(wide_best[2]))
            raw = _measure_many(
                primary,
                [(float(t), win, center0, S_win, h) for t in starts],
                p.analysis_workers,
                cancel,
            )
            lag_arr = np.array([m.lag if m else np.nan for m in raw])
            conf_arr = np.array([m.confidence if m else 0.0 for m in raw])
            good = (conf_arr >= conf_thr) & ~np.isnan(lag_arr)

    windows = [SyncWindow(float(t), win) for t in starts]

    def unreliable(msg: str) -> SyncResult:
        messages.append(msg)
        for w, m in zip(windows, raw):
            if m is not None:
                _set_primary(w, kind, _offset_from_lag(m.lag, w.center, h, h), m.confidence)
        return SyncResult(center0, h, h, None, [SyncSegment(0.0, dur_a, center0, 0.0, 0)], windows,
                          float(np.median(conf_arr)) if conf_arr.size else 0.0, Verdict.UNRELIABLE, kind,
                          False, messages)

    if good.sum() < 2:
        return unreliable(_("No se encontró correspondencia fiable entre A y B. ¿Son la misma película?"))

    # --- 3. tempo fit --------------------------------------------------------
    x, y, wts = centers[good], lag_arr[good], conf_arr[good]
    fit = ransac_line(x, y, wts, threshold=fit_tol)
    tempo_meas = h * (1.0 - fit.slope)
    tempo = tempo_meas
    preset_key: str | None = None
    if p.forced_tempo:
        tempo = p.forced_tempo
    else:
        options = [(pr.key, pr.tempo) for pr in PRESETS] + [("1:1", 1.0)]
        for key, val in options:
            if abs(val - tempo_meas) > 1.5e-4:
                continue
            fixed = ransac_line(x, y, wts, threshold=fit_tol, fixed_slope=1.0 - val / h)
            if fixed.inliers.sum() >= fit.inliers.sum() and fixed.residual <= fit.residual + 0.25 * fit_tol:
                tempo, preset_key = val, (None if key == "1:1" else key)
                break
    suggestion = snap_to_preset(tempo_meas)
    if preset_key is None and suggestion is not None and tempo != suggestion.tempo:
        messages.append(_("El tempo medido ({m:.6f}) se parece al preajuste {k}; compruébalo.").format(
            m=tempo_meas, k=suggestion.key))

    offs = np.array([_offset_from_lag(d, c, tempo, h) if not np.isnan(d) else np.nan
                     for d, c in zip(lag_arr, centers)])
    for w, o, c, g in zip(windows, offs, conf_arr, good):
        if not np.isnan(o):
            _set_primary(w, kind, float(o), float(c))

    # --- 4. segments ---------------------------------------------------------
    gidx = np.nonzero(good)[0]
    runs_local = segment_steps(offs[gidx], conf_arr[gidx], seg_tol)
    runs = [[int(gidx[j]) for j in r] for r in runs_local]
    if not runs:
        return unreliable(_("Las mediciones son incoherentes entre sí."))
    runs.sort(key=lambda r: r[0])
    for s_id, r in enumerate(runs):
        for i in r:
            windows[i].inlier = True
            windows[i].segment = s_id
    run_off = [float(np.median(offs[r])) for r in runs]

    bounds: list[tuple[float, float]] = []  # (end of run k, start of run k+1)
    for k in range(len(runs) - 1):
        cancel.raise_if_cancelled()
        progress(0.6 + 0.1 * k / max(1, len(runs) - 1), _("Localizando cambio de montaje {i}…").format(i=k + 1))
        i_last, i_first = runs[k][-1], runs[k + 1][0]
        cut, gap = _refine_boundary(primary, float(starts[i_last]), float(starts[i_first] + win),
                                    run_off[k], run_off[k + 1], tempo, h, p.boundary_block, cancel)
        bounds.append((cut, cut + gap))

    segs: list[SyncSegment] = []
    for k, r in enumerate(runs):
        s0 = 0.0 if k == 0 else bounds[k - 1][1]
        s1 = dur_a if k == len(runs) - 1 else bounds[k][0]
        if k > 0 and bounds[k - 1][1] > bounds[k - 1][0] + 1e-3:
            segs.append(SyncSegment(bounds[k - 1][0], bounds[k - 1][1], None, 0.0, 0))
        segs.append(SyncSegment(s0, max(s0, s1), run_off[k], float(np.median(conf_arr[r])), len(r)))

    # --- 5. audio refinement -------------------------------------------------
    refined = False
    if refiner is not None and p.audio_refine:
        segs, tempo, refined, preset_key = _audio_refine(refiner, windows, segs, tempo, preset_key, dur_a, p,
                                                         progress, cancel, messages)

    # --- 6. verdict ----------------------------------------------------------
    inl = [w for w in windows if w.inlier]
    real = [s for s in segs if not s.is_gap]
    frac = len(inl) / len(windows)
    gconf = float(np.median([w.confidence for w in inl])) * float(np.sqrt(frac)) if inl else 0.0
    main = max(real, key=lambda s: s.end - s.start)
    if len(real) == 1 and gconf >= 0.45 and frac >= 0.6:
        verdict = Verdict.SIMPLE
    elif len(real) > 1 and gconf >= 0.4 and all(s.confidence >= 0.3 for s in real):
        verdict = Verdict.SEGMENTS
    else:
        verdict = Verdict.UNRELIABLE
        messages.append(
            _(
                "Confianza baja ({c:.0%}): puede que no sea el mismo vídeo o que haya demasiada diferencia. Revisa el resultado en el comparador."
            ).format(c=gconf)
        )
    if len(real) > 1:
        messages.append(_("Se detectaron {n} tramos con desfases distintos (montajes diferentes).").format(n=len(real)))
    progress(1.0, _("Análisis completado."))
    return SyncResult(main.offset or 0.0, tempo, tempo_meas, preset_key, segs, windows, gconf, verdict, kind,
                      refined, messages)


def _set_primary(w: SyncWindow, kind: str, off: float, conf: float) -> None:
    if kind == "video":
        w.video_offset, w.video_conf = off, conf
    else:
        w.audio_offset, w.audio_conf = off, conf


def _refine_boundary(
    m: Measurer, lo: float, hi: float, off1: float, off2: float, tempo: float, h: float, block: float,
    cancel: CancelToken,
) -> tuple[float, float]:
    """Locate the edit point between two constant-offset stretches.

    Returns ``(cut, gap)``: stretch 1 ends at ``cut``; stretch 2 starts at
    ``cut + gap``. ``gap > 0`` when A contains material missing from B.
    """
    gap = max(0.0, off2 - off1)
    tau = 0.25

    def lags(t: float) -> list[float]:
        return [_lag_from_offset(off1, t, tempo, h), _lag_from_offset(off2, t, tempo, h)]

    while hi - lo > 3 * block:
        cancel.raise_if_cancelled()
        mid = 0.5 * (lo + hi)
        _t, sims = m.profile(mid - block / 2, mid + block / 2, lags(mid), h)
        m1 = float(np.nanmean(sims[0])) if sims.size and np.any(~np.isnan(sims[0])) else 0.0
        m2 = float(np.nanmean(sims[1])) if sims.size and np.any(~np.isnan(sims[1])) else 0.0
        if max(m1, m2) < tau and gap > 0:
            hi = mid  # inside the gap: the cut is earlier
        elif m1 >= m2:
            lo = mid
        else:
            hi = mid
    times, sims = m.profile(lo, hi + gap, lags(0.5 * (lo + hi)), h)
    if times.size < 2:
        return 0.5 * (lo + hi), gap
    s1 = np.nan_to_num(sims[0], nan=tau)
    s2 = np.nan_to_num(sims[1], nan=tau)
    c1 = np.concatenate([[0.0], np.cumsum(s1)])
    c2 = np.concatenate([[0.0], np.cumsum(s2)])
    # Candidate cut positions: before the first frame, between frames, after the last.
    cuts = np.concatenate([[times[0] - 1e-3], 0.5 * (times[1:] + times[:-1]), [times[-1] + 1e-3]])
    i_cut = np.searchsorted(times, cuts)  # frames with t < cut
    i_gap = np.searchsorted(times, cuts + gap)  # frames with t < cut + gap
    score = c1[i_cut] + tau * (i_gap - i_cut) + (c2[-1] - c2[i_gap])
    best = int(np.argmax(score))
    return float(cuts[best]), gap


def _robust_weighted_offset(vals: np.ndarray, weights: np.ndarray) -> float:
    """Confidence-weighted mean of the tight MAD-filtered inlier cluster."""
    if vals.size == 0:
        return 0.0
    med = float(np.median(vals))
    if vals.size <= 2:
        return float(np.average(vals, weights=weights)) if np.sum(weights) > 1e-9 else med
    mad = float(np.median(np.abs(vals - med)))
    inl = np.abs(vals - med) <= max(0.004, 2.5 * mad)
    if np.any(inl) and np.sum(weights[inl]) > 1e-9:
        return float(np.average(vals[inl], weights=weights[inl]))
    return med


def _audio_refine(
    refiner: Measurer,
    windows: list[SyncWindow],
    segs: list[SyncSegment],
    tempo: float,
    preset_key: str | None,
    dur_a: float,
    p: SyncParams,
    progress: ProgressCb,
    cancel: CancelToken,
    messages: list[str],
) -> tuple[list[SyncSegment], float, bool, str | None]:
    """Refine per-segment offsets (and tempo) with language-independent audio cues."""
    real = [(i, s) for i, s in enumerate(segs) if not s.is_gap]
    seg_of_win: list[int | None] = []
    for w in windows:
        idx = next((i for i, s in real if s.start <= w.center < s.end), None)
        seg_of_win.append(idx)
    accepted: list[tuple[int, float, float, int]] = []  # (window idx, centre, offset, seg idx)
    disagree = 0
    n = len(windows)
    for k, (w, si) in enumerate(zip(windows, seg_of_win)):
        cancel.raise_if_cancelled()
        progress(0.7 + 0.28 * k / max(1, n), _("Refinando con audio {i} de {n}…").format(i=k + 1, n=n))
        if si is None:
            continue
        seg = segs[si]
        length = min(p.audio_window, seg.end - seg.start, dur_a)
        if length < 8.0:
            continue
        start = float(np.clip(w.center - length / 2, seg.start, seg.end - length))
        pred = seg.offset if seg.offset is not None else 0.0
        if w.video_offset is not None and w.inlier:
            pred = 0.5 * (w.video_offset + pred) if seg.offset is not None else w.video_offset
        try:
            m = refiner.measure(start, length, pred, p.audio_refine_range, tempo)
        except AnalysisError:
            m = None
        if m is None:
            continue
        w.audio_conf = m.confidence
        if m.confidence >= p.audio_conf_threshold:
            if w.video_offset is not None and w.inlier:
                err_win = abs(m.lag - w.video_offset)
                err_seg = abs(m.lag - seg.offset) if seg.offset is not None else err_win
                if min(err_win, err_seg) > p.disagreement_threshold:
                    # Disagreement indicates vocal timing differences across languages in dubbing.
                    # Reject this audio measurement to preserve the ground-truth video alignment!
                    disagree += 1
                    continue
            w.audio_offset = m.lag  # with h == tempo, lag == offset
            accepted.append((k, start + length / 2, m.lag, si))
    if not accepted:
        messages.append(_("El refinamiento por audio no fue concluyente (probablemente diálogo dominante o "
                          "mezclas muy distintas); se mantiene el resultado del vídeo."))
        return segs, tempo, False, preset_key
    if disagree:
        messages.append(_("Vídeo y audio discrepan más de {ms:.0f} ms en {n} ventanas; revisa el resultado.").format(
            ms=p.disagreement_threshold * 1000, n=disagree))

    xs = np.array([a[1] for a in accepted])
    ys = np.array([a[2] for a in accepted])
    gs = np.array([a[3] for a in accepted])
    ws = np.array([windows[a[0]].audio_conf for a in accepted])

    # Pre-split segments if contiguous zones inside a video segment have a sub-frame/multi-ms
    # step shift (e.g. 15–119 ms reel/scene splice that was below video's 120 ms seg_tol).
    # Only discard true isolated outliers before fitting shared slope.
    keep = np.ones(xs.size, bool)
    for g in np.unique(gs):
        mk = gs == g
        idx_mk = np.nonzero(mk)[0]
        if idx_mk.size >= 4:
            sub_runs = segment_steps(ys[mk], ws[mk], tolerance=0.015, single_min_conf=0.88)
            valid_indices = {j for r in sub_runs if len(r) >= 2 for j in r}
            if valid_indices:
                for local_j, global_j in enumerate(idx_mk):
                    keep[global_j] = local_j in valid_indices
                continue
        med = np.median(ys[mk])
        keep[mk] = np.abs(ys[mk] - med) <= max(0.012, 3 * np.median(np.abs(ys[mk] - med)))

    for (k, *_rest), kp in zip(accepted, keep):
        if not kp:
            windows[k].audio_offset = None
    xs, ys, gs, ws = xs[keep], ys[keep], gs[keep], ws[keep]
    accepted_kept = [a for a, kp in zip(accepted, keep) if kp]
    if xs.size == 0:
        return segs, tempo, False, preset_key

    slope, inter = shared_slope_fit(xs, ys, gs, ws)
    new_tempo = tempo
    if abs(slope) * dur_a > 0.002 and np.ptp(xs) > 0.2 * dur_a:
        cand_tempo = tempo * (1.0 - slope)
        snap = snap_to_preset(cand_tempo, tolerance=5e-4)
        span_x = max(1.0, float(np.ptp(xs)))
        if snap is not None and abs(snap.tempo - cand_tempo) * span_x < 0.015:
            new_tempo, preset_key = snap.tempo, snap.key
        elif abs(cand_tempo - 1.0) * span_x < 0.015:
            new_tempo, preset_key = 1.0, None
        elif xs.size >= 6 and np.unique(gs).size == 1:
            res_fixed = float(np.median(np.abs(ys - np.median(ys))))
            detrended = ys - slope * xs
            res_sloped = float(np.median(np.abs(detrended - np.median(detrended))))
            if res_sloped < 0.0025 and res_sloped < 0.5 * res_fixed:
                new_tempo, preset_key = cand_tempo, None

        if new_tempo != tempo:
            scale = tempo / new_tempo

            def reexpress(off: float, c: float) -> float:
                return off * scale - c * (scale - 1.0)

            ys = np.array([reexpress(float(y), float(x)) for x, y in zip(xs, ys)])
            accepted_kept = [
                (w_idx, cx, reexpress(lag, cx), si)
                for (w_idx, cx, lag, si) in accepted_kept
            ]
            for w in windows:
                if w.audio_offset is not None:
                    w.audio_offset = reexpress(w.audio_offset, w.center)
                if w.video_offset is not None:
                    w.video_offset = reexpress(w.video_offset, w.center)
            messages.append(_("El audio corrigió el tempo a {t:.6f}.").format(t=new_tempo))

    inter = {
        int(g): _robust_weighted_offset(ys[gs == g], ws[gs == g])
        for g in np.unique(gs)
    }

    out: list[SyncSegment] = []
    for i, s in enumerate(segs):
        if i in inter:
            seg_acc = [a for a in accepted_kept if a[3] == i]
            y_seg = np.array([a[2] for a in seg_acc])
            w_seg = np.array([windows[a[0]].audio_conf for a in seg_acc])
            # Check if this coarse segment contains distinct multi-millisecond sub-zones (≥15 ms step)
            sub_runs = (
                [r for r in segment_steps(y_seg, w_seg, tolerance=0.015, single_min_conf=0.90) if len(r) >= 2]
                if y_seg.size >= 4
                else []
            )
            if len(sub_runs) > 1:
                sub_offs = [_robust_weighted_offset(y_seg[r], w_seg[r]) for r in sub_runs]
                # Verify adjacent sub-runs differ by at least 15 ms
                if all(abs(sub_offs[j + 1] - sub_offs[j]) >= 0.015 for j in range(len(sub_offs) - 1)):
                    sub_bounds: list[tuple[float, float]] = []
                    for j in range(len(sub_runs) - 1):
                        w_last = windows[seg_acc[sub_runs[j][-1]][0]]
                        w_first = windows[seg_acc[sub_runs[j + 1][0]][0]]
                        cut, gap = _refine_boundary(
                            refiner,
                            w_last.start,
                            w_first.end,
                            sub_offs[j],
                            sub_offs[j + 1],
                            new_tempo,
                            new_tempo,
                            min(p.boundary_block, 2.0),
                            cancel,
                        )
                        cut = float(np.clip(cut, s.start + 1.0, s.end - 1.0))
                        sub_bounds.append((cut, min(s.end, cut + gap)))
                    for j, r in enumerate(sub_runs):
                        st0 = s.start if j == 0 else sub_bounds[j - 1][1]
                        st1 = s.end if j == len(sub_runs) - 1 else sub_bounds[j][0]
                        if j > 0 and sub_bounds[j - 1][1] > sub_bounds[j - 1][0] + 1e-3:
                            out.append(SyncSegment(sub_bounds[j - 1][0], sub_bounds[j - 1][1], None, 0.0, 0))
                        aconf = float(np.median(w_seg[r]))
                        out.append(SyncSegment(st0, max(st0, st1), sub_offs[j], max(s.confidence, aconf), len(r)))
                    continue

            refined_offset = float(inter[i])
            if s.offset is not None and abs(refined_offset - s.offset) > p.disagreement_threshold:
                # Vocal timing shift across languages in dubbing; preserve video ground truth
                out.append(s)
            else:
                aconf = float(np.median(ws[gs == i])) if np.any(gs == i) else s.confidence
                out.append(SyncSegment(s.start, s.end, refined_offset, max(s.confidence, aconf), s.n_windows))
        elif not s.is_gap and s.offset is not None and new_tempo != tempo:
            sc = tempo / new_tempo
            mid = 0.5 * (s.start + s.end)
            out.append(SyncSegment(s.start, s.end, s.offset * sc - mid * (sc - 1.0), s.confidence, s.n_windows))
        else:
            out.append(s)
    refined = (
        len(out) != len(segs)
        or any(s.offset != orig.offset for s, orig in zip(out, segs))
        or (new_tempo != tempo)
    )
    return out, new_tempo, refined, preset_key
