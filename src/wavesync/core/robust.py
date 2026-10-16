"""Robust fitting helpers: RANSAC line fit and step segmentation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class LineFit:
    """``y = intercept + slope * x`` with its inlier mask."""

    intercept: float
    slope: float
    inliers: np.ndarray  #: bool mask
    residual: float  #: median absolute residual of inliers


def _wls(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    if x.size == 1 or np.ptp(x) < 1e-9:
        return float(np.average(y, weights=w)), 0.0
    sw = w.sum()
    mx, my = (w * x).sum() / sw, (w * y).sum() / sw
    sxx = (w * (x - mx) ** 2).sum()
    slope = (w * (x - mx) * (y - my)).sum() / sxx
    return float(my - slope * mx), float(slope)


def ransac_line(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray | None = None,
    threshold: float = 0.1,
    iterations: int = 400,
    seed: int = 1234,
    fixed_slope: float | None = None,
) -> LineFit:
    """Robust line fit; deterministic (fixed seed).

    With ``fixed_slope`` only the intercept is estimated (1-point RANSAC).
    """
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    w = np.ones_like(x) if weights is None else np.maximum(np.asarray(weights, float), 1e-6)
    n = x.size
    if n == 0:
        return LineFit(0.0, fixed_slope or 0.0, np.zeros(0, bool), float("inf"))
    rng = np.random.default_rng(seed)
    best_mask = np.zeros(n, bool)
    best_score = -1.0
    if fixed_slope is not None or n == 1:
        s = fixed_slope or 0.0
        for i in range(n):
            b = y[i] - s * x[i]
            mask = np.abs(y - (b + s * x)) <= threshold
            score = w[mask].sum()
            if score > best_score:
                best_score, best_mask = score, mask
        b = float(np.median(y[best_mask] - s * x[best_mask]))
        res = float(np.median(np.abs(y[best_mask] - (b + s * x[best_mask]))))
        return LineFit(b, s, best_mask, res)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if len(pairs) > iterations:
        idx = rng.choice(len(pairs), iterations, replace=False)
        pairs = [pairs[k] for k in idx]
    for i, j in pairs:
        if abs(x[j] - x[i]) < 1e-9:
            continue
        s = (y[j] - y[i]) / (x[j] - x[i])
        b = y[i] - s * x[i]
        mask = np.abs(y - (b + s * x)) <= threshold
        score = w[mask].sum() - 1e-3 * np.abs(y[mask] - (b + s * x[mask])).sum() / threshold
        if score > best_score:
            best_score, best_mask = score, mask
    if best_mask.sum() < 2:
        best_mask = np.ones(n, bool)
    b, s = _wls(x[best_mask], y[best_mask], w[best_mask])
    # One refinement pass.
    mask = np.abs(y - (b + s * x)) <= threshold
    if mask.sum() >= 2:
        best_mask = mask
        b, s = _wls(x[mask], y[mask], w[mask])
    res = float(np.median(np.abs(y[best_mask] - (b + s * x[best_mask]))))
    return LineFit(b, s, best_mask, res)


def segment_steps(
    values: np.ndarray,
    confidences: np.ndarray,
    tolerance: float,
    single_min_conf: float = 0.75,
) -> list[list[int]]:
    """Group consecutive (time-sorted) values into runs of constant level.

    Returns a list of runs, each a list of indices into ``values``. A run of
    a single element is kept only if its confidence is at least
    ``single_min_conf``; otherwise it is dropped as an outlier. Adjacent runs
    whose medians are within ``tolerance`` are merged.
    """
    v = np.asarray(values, float)
    c = np.asarray(confidences, float)
    runs: list[list[int]] = []
    for i in range(v.size):
        if runs and abs(v[i] - float(np.median(v[runs[-1]]))) <= tolerance:
            runs[-1].append(i)
        else:
            runs.append([i])
    changed = True
    while changed:
        changed = False
        kept = [r for r in runs if len(r) > 1 or c[r[0]] >= single_min_conf]
        if len(kept) != len(runs):
            changed = True
        merged: list[list[int]] = []
        for r in kept:
            if merged and abs(float(np.median(v[merged[-1]])) - float(np.median(v[r]))) <= tolerance:
                merged[-1].extend(r)
                changed = True
            else:
                merged.append(list(r))
        runs = merged
    return runs


def shared_slope_fit(
    x: np.ndarray, y: np.ndarray, groups: np.ndarray, weights: np.ndarray, iterations: int = 6
) -> tuple[float, dict[int, float]]:
    """Fit ``y = b_g + s·x`` with a common slope and per-group intercepts.

    The slope is the weighted within-group least-squares estimate (groups
    with a single point do not constrain it); intercepts are medians of
    ``y - s·x`` per group.
    """
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    w = np.asarray(weights, float)
    num = den = 0.0
    for g in np.unique(groups):
        m = groups == g
        if m.sum() < 2:
            continue
        wm = w[m]
        xm = x[m] - np.average(x[m], weights=wm)
        ym = y[m] - np.average(y[m], weights=wm)
        num += float((wm * xm * ym).sum())
        den += float((wm * xm * xm).sum())
    s = num / den if den > 1e-12 else 0.0
    inter = {int(g): float(np.median(y[groups == g] - s * x[groups == g])) for g in np.unique(groups)}
    return s, inter
