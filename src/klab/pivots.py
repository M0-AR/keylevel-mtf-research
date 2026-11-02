"""Causal pivot detection.

A pivot high at bar i requires it to be strictly above the `left` preceding
bars and at least as high as the `right` following bars.  It is only
*confirmed* at the close of bar i+right; before that moment it must not be
used.  Pivots can be computed on candle bodies (max/min of open and close) or
on wicks (high/low).  The practitioner's claim is that bodies matter, so the
base specification uses bodies.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _body_high(df: pd.DataFrame) -> np.ndarray:
    return np.maximum(df["open"].to_numpy(), df["close"].to_numpy())


def _body_low(df: pd.DataFrame) -> np.ndarray:
    return np.minimum(df["open"].to_numpy(), df["close"].to_numpy())


def find_pivots(
    df: pd.DataFrame,
    left: int = 2,
    right: int = 2,
    price_source: str = "body",
) -> pd.DataFrame:
    if price_source == "body":
        highs = _body_high(df)
        lows = _body_low(df)
    elif price_source == "wick":
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
    else:
        raise ValueError("price_source must be 'body' or 'wick'")

    idx = df.index
    n = len(df)
    rows: list[tuple[pd.Timestamp, pd.Timestamp, str, float, int]] = []
    for i in range(left, n - right):
        h = highs[i]
        l = lows[i]
        if h > np.max(highs[i - left : i]) and h >= np.max(highs[i + 1 : i + right + 1]):
            rows.append((idx[i + right], idx[i], "high", float(h), i))
        if l < np.min(lows[i - left : i]) and l <= np.min(lows[i + 1 : i + right + 1]):
            rows.append((idx[i + right], idx[i], "low", float(l), i))
    out = pd.DataFrame(rows, columns=["confirm_ts", "pivot_ts", "kind", "price", "bar_idx"])
    if not out.empty:
        out = out.sort_values(["confirm_ts", "pivot_ts", "kind"]).reset_index(drop=True)
    return out


def last_two_swings(
    pivots: pd.DataFrame,
    bar_index: pd.DatetimeIndex,
) -> pd.DataFrame:
    """For each bar close, return the last two confirmed swing highs/lows.

    The result is causal: at bar t only pivots with confirm_ts <= t are used.
    """
    n = len(bar_index)
    out = pd.DataFrame(
        {
            "last_high_1": np.nan,
            "last_high_2": np.nan,
            "last_low_1": np.nan,
            "last_low_2": np.nan,
        },
        index=bar_index,
    )
    if pivots.empty:
        return out
    piv = pivots.sort_values(["confirm_ts", "pivot_ts"]).reset_index(drop=True)
    hi: list[float] = []
    lo: list[float] = []
    j = 0
    for k, ts in enumerate(bar_index):
        while j < len(piv) and piv.at[j, "confirm_ts"] <= ts:
            p = float(piv.at[j, "price"])
            if piv.at[j, "kind"] == "high":
                hi.append(p)
                hi = hi[-2:]
            else:
                lo.append(p)
                lo = lo[-2:]
            j += 1
        if len(hi) >= 2:
            out.iat[k, 0] = hi[-1]
            out.iat[k, 1] = hi[-2]
        if len(lo) >= 2:
            out.iat[k, 2] = lo[-1]
            out.iat[k, 3] = lo[-2]
    return out
