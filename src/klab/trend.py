"""4-hour trend filters.

Base trend: the last two confirmed 4-hour swing highs and lows must both be
rising (up) or both falling (down); otherwise neutral.  The EMA variant is a
pre-specified ablation (close above/below EMA(200) with a positive/negative
slope).  Trend values are indexed by the *close* of the 4-hour bar and are
looked up with `asof`, which guarantees that a 15-minute decision never sees a
4-hour bar that has not closed.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .pivots import find_pivots, last_two_swings


def swing_trend(tfs: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> pd.Series:
    s = cfg["strategy"]
    bars = tfs["4h"]
    piv = find_pivots(
        bars,
        left=int(s["trend_pivot_left"]),
        right=int(s["trend_pivot_right"]),
        price_source="wick",
    )
    sw = last_two_swings(piv, bars.index)
    up = (sw["last_high_1"] > sw["last_high_2"]) & (sw["last_low_1"] > sw["last_low_2"])
    down = (sw["last_high_1"] < sw["last_high_2"]) & (sw["last_low_1"] < sw["last_low_2"])
    out = pd.Series(0, index=bars.index, dtype=int)
    out[up] = 1
    out[down] = -1
    return out


def ema_trend(tfs: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> pd.Series:
    s = cfg["strategy"]
    bars = tfs["4h"]
    ema = bars["close"].ewm(span=int(s["ema_length"]), adjust=False, min_periods=int(s["ema_length"])).mean()
    slope = ema - ema.shift(int(s["ema_slope_bars"]))
    out = pd.Series(0, index=bars.index, dtype=int)
    out[(bars["close"] > ema) & (slope > 0)] = 1
    out[(bars["close"] < ema) & (slope < 0)] = -1
    return out


def build_trend(tfs: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> pd.Series:
    method = cfg["strategy"]["trend_method"]
    if method == "swing":
        return swing_trend(tfs, cfg)
    if method == "ema":
        return ema_trend(tfs, cfg)
    raise ValueError(f"unknown trend_method: {method}")


def trend_asof(series: pd.Series, ts: pd.Timestamp) -> int:
    if series is None or series.empty:
        return 0
    val = series.asof(ts)
    if pd.isna(val):
        return 0
    return int(val)
