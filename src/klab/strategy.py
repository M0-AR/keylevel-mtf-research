"""Deterministic signal generation for the multi-timeframe key-level strategy.

The rules are a faithful operationalisation of the practitioner transcript:

1.  Monthly/weekly/daily pivots are clustered into key levels; a level needs
    at least `min_touches` pivots and two prior 15-minute rejections.
2.  The 4-hour trend must agree with the trade direction.
3.  A 15-minute bar must touch the level, close back through it, and the last
    `confirmation_closes` closes must be on the rejected side.
4.  Entry is at the *next* 15-minute open; same-bar-close execution is
    forbidden.

Everything is causal: pivots are confirmed only at the close of the bar
`right` periods later, higher-timeframe bars are indexed by their close, and
the level book is advanced with event timestamps.
"""
from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd

from .levels import LevelBook, build_level_events
from .trend import build_trend, trend_asof

TF_PRIORITY = {"1D": 1, "1W": 2, "1M": 3}


def _count_rejections(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    i: int,
    level: float,
    side: int,
    lookback: int,
) -> int:
    lo = max(0, i - lookback)
    if side < 0:
        mask = (highs[lo:i] >= level) & (closes[lo:i] < level)
    else:
        mask = (lows[lo:i] <= level) & (closes[lo:i] > level)
    return int(mask.sum())


def prepare_features(
    tfs: dict[str, pd.DataFrame],
    cfg: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.Series, dict[str, Any]]:
    cfg2 = _with_overrides(cfg, overrides or {})
    events = build_level_events(tfs, cfg2)
    trend = build_trend(tfs, cfg2)
    return events, trend, cfg2


def _with_overrides(cfg: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    if not overrides:
        return cfg
    out = {**cfg, "strategy": {**cfg["strategy"], **overrides}}
    return out


def generate_signals(
    tfs: dict[str, pd.DataFrame],
    events: pd.DataFrame,
    trend: pd.Series,
    cfg: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    end_ts: pd.Timestamp | None = None,
) -> pd.DataFrame:
    cfg2 = _with_overrides(cfg, overrides or {})
    s = cfg2["strategy"]
    bars = tfs["15min"]
    idx = bars.index
    opens = bars["open"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    atr = bars["atr14"].to_numpy(dtype=float)
    trend_vals = trend.reindex(idx, method="ffill").fillna(0).astype(int).to_numpy()

    book = LevelBook(events, cfg2)
    pad_mult = 0.15
    min_prior = 2
    rows: list[dict[str, Any]] = []

    n = len(bars)
    if end_ts is not None:
        n = min(n, int(np.searchsorted(idx.values, np.datetime64(end_ts), side="right")))
    for i in range(2, n - 1):
        ts = idx[i]
        tr = int(trend_vals[i])
        require_trend = bool(s.get("require_trend", True))
        if require_trend and tr == 0:
            continue
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        book.advance(ts)
        pad = pad_mult * a
        candidates = book.near(lows[i], highs[i], pad)
        if not candidates:
            continue
        best = None
        best_score = None
        best_side = 0
        for lv in candidates:
            p = lv.price
            if require_trend:
                side = tr
            else:
                side = -1 if p > closes[i] else 1
            if side < 0:
                if not (p > closes[i] and highs[i] >= p - pad):
                    continue
                if p - closes[i] > float(s["max_chase_atr"]) * a:
                    continue
            else:
                if not (p < closes[i] and lows[i] <= p + pad):
                    continue
                if closes[i] - p > float(s["max_chase_atr"]) * a:
                    continue
            touches = _count_rejections(highs, lows, closes, i, p, side, int(s["touch_lookback"]))
            if touches < min_prior:
                continue
            k = int(s["confirmation_closes"])
            if k > 0:
                if i - k + 1 < 0:
                    continue
                window = closes[i - k + 1 : i + 1]
                if side < 0 and not np.all(window < p):
                    continue
                if side > 0 and not np.all(window > p):
                    continue
            if bool(s["require_directional_candle"]):
                if side < 0 and not (closes[i] < opens[i]):
                    continue
                if side > 0 and not (closes[i] > opens[i]):
                    continue
            score = (touches, TF_PRIORITY.get(lv.timeframe, 0), -abs(p - closes[i]))
            if best_score is None or score > best_score:
                best = lv
                best_score = score
                best_side = side
        if best is None:
            continue
        side = best_side

        p = best.price
        lb = int(s["stop_extreme_lookback"])
        if side < 0:
            recent_extreme = float(np.max(highs[max(0, i - lb + 1) : i + 1]))
            stop = max(p + float(s["stop_buffer_atr"]) * a, recent_extreme + float(s["stop_extreme_buffer_atr"]) * a)
            risk_proxy = abs(closes[i] - stop)
            target_level = book.nearest_below(closes[i])
        else:
            recent_extreme = float(np.min(lows[max(0, i - lb + 1) : i + 1]))
            stop = min(p - float(s["stop_buffer_atr"]) * a, recent_extreme - float(s["stop_extreme_buffer_atr"]) * a)
            risk_proxy = abs(closes[i] - stop)
            target_level = book.nearest_above(closes[i])
        if risk_proxy <= 0 or risk_proxy > float(s["max_stop_atr"]) * a:
            continue

        rows.append(
            {
                "signal_idx": i,
                "signal_ts": ts,
                "entry_idx": i + 1,
                "side": side,
                "level": float(p),
                "level_tf": best.timeframe,
                "level_weight": int(best.weight),
                "touch_count": int(best_score[0]),
                "confluence": int(book.confluence(p, 0.5 * a)),
                "stop_raw": float(stop),
                "target_level": float(target_level.price) if target_level is not None else np.nan,
                "atr": float(a),
                "trend": tr,
                "close": float(closes[i]),
                "risk_proxy": float(risk_proxy),
            }
        )

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["signal_ts", "signal_idx"]).reset_index(drop=True)
    return out
