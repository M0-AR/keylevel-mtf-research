"""Execution engine with conservative intrabar assumptions.

* Signals are executed at the next 15-minute *open*.
* When a stop and a target are both inside one bar, the stop is assumed to
  fire first (the pessimistic convention; see Claeys, 2026).
* Gaps through a stop fill at the next open; gaps through a target fill at the
  target (never better), so gap luck cannot inflate results.
* Costs are charged on both sides: adverse half-spread plus slippage.
* Open positions are closed at the split boundary so that each split is
  self-contained.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import Instrument
from .trend import trend_asof


def _choose_target(
    side: int,
    entry: float,
    risk: float,
    target_level: float,
    default_rr: float,
    max_rr: float,
) -> float:
    if not np.isfinite(target_level):
        return entry + side * default_rr * risk
    dist = side * (target_level - entry)
    if dist < risk:
        return entry + side * default_rr * risk
    dist = min(dist, max_rr * risk)
    return entry + side * dist


def simulate_trades(
    signals: pd.DataFrame,
    tfs: dict[str, pd.DataFrame],
    trend: pd.Series,
    cfg: dict[str, Any],
    inst: Instrument,
    cost_multiplier: float = 1.0,
    start_ts: pd.Timestamp | None = None,
    end_ts: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    s = cfg["strategy"]
    bars = tfs["15min"]
    idx = bars.index
    opens = bars["open"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    n = len(bars)
    if start_ts is None:
        start_ts = idx[0]
    if end_ts is None:
        end_ts = idx[-1]
    end_idx = int(np.searchsorted(idx.values, np.datetime64(end_ts), side="right") - 1)
    end_idx = min(max(end_idx, 0), n - 1)

    cost = (inst.half_spread_price + inst.slippage_price) * float(cost_multiplier)
    max_hold = int(s["max_hold_bars"])
    default_rr = float(s["default_rr"])
    max_rr = float(s["max_rr"])

    sig = signals.copy()
    if sig.empty:
        return pd.DataFrame(), pd.Series(dtype=float)
    sig = sig[(sig["signal_ts"] >= start_ts) & (sig["signal_ts"] <= end_ts)]
    sig = sig[sig["entry_idx"] <= end_idx]
    sig = sig.sort_values("signal_ts").reset_index(drop=True)

    trades: list[dict[str, Any]] = []
    pos = None
    p = 0
    while p < len(sig):
        if pos is not None:
            p += 1
            continue
        row = sig.iloc[p]
        entry_idx = int(row["entry_idx"])
        if entry_idx > end_idx or entry_idx >= n:
            break
        side = int(row["side"])
        entry_open = opens[entry_idx]
        stop = float(row["stop_raw"])
        risk = abs(entry_open - stop)
        atr = float(row["atr"])
        if not np.isfinite(risk) or risk <= 0 or risk > float(s["max_stop_atr"]) * atr:
            p += 1
            continue
        if side < 0 and entry_open >= stop:
            p += 1
            continue
        if side > 0 and entry_open <= stop:
            p += 1
            continue
        # If the next open has already gapped through the level that was the
        # intended target, the setup no longer exists.  Re-anchoring a fresh
        # 2R target from the gapped open would invent a trade that was never
        # available at the signal close.
        target_level = float(row["target_level"])
        if np.isfinite(target_level) and side * (target_level - entry_open) <= 0:
            p += 1
            continue
        target = _choose_target(
            side, entry_open, risk, float(row["target_level"]), default_rr, max_rr
        )
        entry_fill = entry_open + side * cost
        pos = {
            "signal_ts": row["signal_ts"],
            "entry_idx": entry_idx,
            "entry_ts": idx[entry_idx],
            "side": side,
            "entry_raw": entry_open,
            "entry_fill": entry_fill,
            "stop": stop,
            "target": target,
            "risk": risk,
            "level": row["level"],
            "level_tf": row["level_tf"],
            "touch_count": int(row["touch_count"]),
            "confluence": int(row["confluence"]),
            "atr": atr,
            "trend": int(row["trend"]),
        }
        last_j = min(entry_idx + max_hold, end_idx)
        exit_raw = None
        exit_idx = None
        reason = None
        for j in range(entry_idx, last_j + 1):
            if j > entry_idx:
                # gap handling at the open
                if side < 0:
                    if opens[j] >= pos["stop"]:
                        exit_raw, exit_idx, reason = opens[j], j, "stop_gap"
                        break
                    if opens[j] <= pos["target"]:
                        exit_raw, exit_idx, reason = pos["target"], j, "target_gap"
                        break
                else:
                    if opens[j] <= pos["stop"]:
                        exit_raw, exit_idx, reason = opens[j], j, "stop_gap"
                        break
                    if opens[j] >= pos["target"]:
                        exit_raw, exit_idx, reason = pos["target"], j, "target_gap"
                        break
            # intrabar, stop first
            if side < 0:
                if highs[j] >= pos["stop"]:
                    exit_raw, exit_idx, reason = pos["stop"], j, "stop"
                    break
                if lows[j] <= pos["target"]:
                    exit_raw, exit_idx, reason = pos["target"], j, "target"
                    break
            else:
                if lows[j] <= pos["stop"]:
                    exit_raw, exit_idx, reason = pos["stop"], j, "stop"
                    break
                if highs[j] >= pos["target"]:
                    exit_raw, exit_idx, reason = pos["target"], j, "target"
                    break
            # trend flip is decided at this close and executed at the next open;
            # if that open gaps through the stop/target, the stop/target wins.
            if j < last_j and trend_asof(trend, idx[j]) != side:
                nxt = opens[j + 1]
                if side < 0:
                    if nxt >= pos["stop"]:
                        exit_raw, exit_idx, reason = nxt, j + 1, "stop_gap"
                    elif nxt <= pos["target"]:
                        exit_raw, exit_idx, reason = pos["target"], j + 1, "target_gap"
                    else:
                        exit_raw, exit_idx, reason = nxt, j + 1, "trend_flip"
                else:
                    if nxt <= pos["stop"]:
                        exit_raw, exit_idx, reason = nxt, j + 1, "stop_gap"
                    elif nxt >= pos["target"]:
                        exit_raw, exit_idx, reason = pos["target"], j + 1, "target_gap"
                    else:
                        exit_raw, exit_idx, reason = nxt, j + 1, "trend_flip"
                break
        if exit_raw is None:
            exit_raw, exit_idx, reason = closes[last_j], last_j, "time"
        exit_fill = exit_raw - side * cost
        pnl = side * (exit_fill - entry_fill)
        r_net = pnl / risk
        r_gross = side * (exit_raw - entry_open) / risk
        trades.append(
            {
                "symbol": inst.symbol,
                "asset_class": inst.asset_class,
                "side": side,
                "signal_ts": pos["signal_ts"],
                "entry_ts": pos["entry_ts"],
                "exit_ts": idx[exit_idx],
                "entry_idx": entry_idx,
                "exit_idx": exit_idx,
                "entry_raw": entry_open,
                "stop": pos["stop"],
                "target": pos["target"],
                "exit_raw": exit_raw,
                "exit_reason": reason,
                "risk_price": risk,
                "r_gross": r_gross,
                "r_net": r_net,
                "level": pos["level"],
                "level_tf": pos["level_tf"],
                "touch_count": pos["touch_count"],
                "confluence": pos["confluence"],
                "atr": pos["atr"],
                "trend": pos["trend"],
                "bars_held": int(exit_idx - entry_idx + 1),
                "cost_multiplier": float(cost_multiplier),
                "cost_price": float(cost),
            }
        )
        # advance to the first signal strictly after the exit bar
        while p < len(sig) and int(sig.iloc[p]["entry_idx"]) <= exit_idx:
            p += 1
        pos = None

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return trades_df, pd.Series(dtype=float)
    trades_df["exit_date"] = pd.to_datetime(trades_df["exit_ts"]).dt.normalize()
    daily = trades_df.groupby("exit_date")["r_net"].sum() * float(s["risk_per_trade"])
    all_days = pd.date_range(start=pd.Timestamp(start_ts).normalize(), end=pd.Timestamp(end_ts).normalize(), freq="B")
    daily = daily.reindex(all_days, fill_value=0.0).astype(float)
    daily.index.name = "date"
    return trades_df, daily


def simulate_trade_batch(
    entry_idx: np.ndarray,
    side: int,
    stop_dist: np.ndarray,
    target_dist: np.ndarray,
    bars: pd.DataFrame,
    cost_price: float,
    max_hold: int,
) -> np.ndarray:
    """Vectorised single-trade simulator for placebo distributions."""
    opens = bars["open"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    n = len(bars)
    m = len(entry_idx)
    entry = opens[entry_idx]
    stop = entry - side * stop_dist
    target = entry + side * target_dist
    entry_fill = entry + side * cost_price
    exit_raw = np.full(m, np.nan)
    exit_idx = np.full(m, -1, dtype=int)
    active = np.ones(m, dtype=bool)
    for h in range(max_hold + 1):
        j = entry_idx + h
        valid = active & (j < n)
        if not valid.any():
            break
        jv = j[valid]
        # gap at open for h>0
        if h > 0:
            op = opens[jv]
            if side < 0:
                hit = op >= stop[valid]
                tgt = (~hit) & (op <= target[valid])
            else:
                hit = op <= stop[valid]
                tgt = (~hit) & (op >= target[valid])
            idxs = np.where(valid)[0]
            if hit.any():
                sel = idxs[hit]
                exit_raw[sel] = op[hit]
                exit_idx[sel] = jv[hit]
                active[sel] = False
            if tgt.any():
                sel = idxs[tgt]
                exit_raw[sel] = target[sel]
                exit_idx[sel] = jv[tgt]
                active[sel] = False
        # intrabar, stop first
        valid = active & (j < n)
        if not valid.any():
            continue
        jv = j[valid]
        idxs = np.where(valid)[0]
        if side < 0:
            hit = highs[jv] >= stop[valid]
            tgt = (~hit) & (lows[jv] <= target[valid])
        else:
            hit = lows[jv] <= stop[valid]
            tgt = (~hit) & (highs[jv] >= target[valid])
        if hit.any():
            sel = idxs[hit]
            exit_raw[sel] = stop[sel]
            exit_idx[sel] = jv[hit]
            active[sel] = False
        if tgt.any():
            sel = idxs[tgt]
            exit_raw[sel] = target[sel]
            exit_idx[sel] = jv[tgt]
            active[sel] = False
    # time exit at last available bar
    remaining = np.where(active)[0]
    if len(remaining):
        j = np.minimum(entry_idx[remaining] + max_hold, n - 1)
        exit_raw[remaining] = closes[j]
        exit_idx[remaining] = j
    exit_fill = exit_raw - side * cost_price
    pnl = side * (exit_fill - entry_fill)
    return pnl / stop_dist
