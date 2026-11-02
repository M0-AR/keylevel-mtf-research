"""End-to-end research pipelines.

Stages are deliberately separated so the sealed holdout can be opened only
after the code freeze:

    development -> validation -> CODE_FREEZE -> holdout

Each stage writes a trial-ledger row for every variant it evaluates.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .backtest import simulate_trade_batch, simulate_trades
from .config import Instrument, resolve, split_dates
from .io import ensure_dir, write_csv, write_json
from .levels import LevelBook
from .stats import (
    alpha_beta,
    benjamini_hochberg,
    bootstrap_sharpe,
    deflated_sharpe_ratio,
    pbo_cscv,
    psr,
    reality_check,
    sharpe_ratio,
    spa_test,
    summary_stats,
    trade_stats,
)
from .strategy import generate_signals, prepare_features
from .timeframes import load_timeframes
from .trend import build_trend

LOG = logging.getLogger(__name__)
_TFS_CACHE: dict[str, dict[str, pd.DataFrame]] = {}
_FEATURE_CACHE: dict[tuple, tuple[pd.DataFrame, pd.Series, dict[str, Any]]] = {}


@dataclass
class StrategyResult:
    name: str
    split: str
    trades: pd.DataFrame
    daily: pd.Series
    per_symbol_daily: pd.DataFrame
    per_symbol: pd.DataFrame
    signals: pd.DataFrame
    params: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0

    @property
    def stats(self) -> dict[str, float]:
        out = summary_stats(self.daily)
        out.update(trade_stats(self.trades))
        return out


def _merge(cfg: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    if not overrides:
        return cfg
    return {**cfg, "strategy": {**cfg["strategy"], **overrides}}


def load_tfs(cfg: dict[str, Any], symbol: str, force: bool = False) -> dict[str, pd.DataFrame]:
    if not force and symbol in _TFS_CACHE:
        return _TFS_CACHE[symbol]
    tfs = load_timeframes(cfg, symbol)
    _TFS_CACHE[symbol] = tfs
    return tfs


def prepare_cached(
    cfg: dict[str, Any], symbol: str, overrides: dict[str, Any]
) -> tuple[pd.DataFrame, pd.Series, dict[str, Any]]:
    cfg2 = _merge(cfg, overrides)
    s = cfg2["strategy"]
    key = (
        symbol,
        float(s["cluster_atr"]),
        str(s["level_price_source"]),
        str(s["trend_method"]),
        int(s["ema_length"]),
        int(s["ema_slope_bars"]),
    )
    if key in _FEATURE_CACHE:
        return _FEATURE_CACHE[key]
    tfs = load_tfs(cfg, symbol)
    events, trend, cfg2 = prepare_features(tfs, cfg2)
    _FEATURE_CACHE[key] = (events, trend, cfg2)
    return events, trend, cfg2


def run_strategy(
    cfg: dict[str, Any],
    name: str,
    split: str,
    symbols: Iterable[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    overrides: dict[str, Any] | None = None,
    cost_multiplier: float = 1.0,
) -> StrategyResult:
    t0 = time.time()
    symbols = list(symbols or cfg["instruments"].keys())
    overrides = overrides or {}
    if start is None or end is None:
        start, end = split_dates(cfg, split)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    all_trades: list[pd.DataFrame] = []
    all_signals: list[pd.DataFrame] = []
    daily_cols: dict[str, pd.Series] = {}
    per_symbol_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            tfs = load_tfs(cfg, symbol)
            events, trend, cfg2 = prepare_cached(cfg, symbol, overrides)
            signals = generate_signals(tfs, events, trend, cfg, overrides=overrides, end_ts=end_ts)
            trades, daily = simulate_trades(
                signals,
                tfs,
                trend,
                cfg2,
                cfg["instruments"][symbol],
                cost_multiplier=cost_multiplier,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("%s %s failed: %s", name, symbol, exc)
            continue
        if not signals.empty:
            sig = signals.copy()
            sig["symbol"] = symbol
            all_signals.append(sig)
        if not trades.empty:
            all_trades.append(trades)
        if not daily.empty:
            daily_cols[symbol] = daily
        row = {"symbol": symbol, "split": split, "variant": name, **summary_stats(daily), **trade_stats(trades)}
        per_symbol_rows.append(row)
        LOG.info("%s %s %s: %d trades, sharpe %.2f", name, split, symbol, len(trades), row["sharpe"])
    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    signals_df = pd.concat(all_signals, ignore_index=True) if all_signals else pd.DataFrame()
    if daily_cols:
        per_symbol_daily = pd.concat(daily_cols, axis=1).fillna(0.0)
        portfolio_daily = per_symbol_daily.sum(axis=1)
    else:
        idx = pd.date_range(start_ts, end_ts, freq="B")
        per_symbol_daily = pd.DataFrame(index=idx)
        portfolio_daily = pd.Series(0.0, index=idx)
    portfolio_daily = portfolio_daily.reindex(pd.date_range(start_ts, end_ts, freq="B"), fill_value=0.0)
    portfolio_daily.index.name = "date"
    elapsed = time.time() - t0
    LOG.info("%s %s: %d trades, %.1fs", name, split, len(trades_df), elapsed)
    return StrategyResult(
        name=name,
        split=split,
        trades=trades_df,
        daily=portfolio_daily,
        per_symbol_daily=per_symbol_daily,
        per_symbol=pd.DataFrame(per_symbol_rows),
        signals=signals_df,
        params=overrides,
        elapsed_s=elapsed,
    )


def _result_row(res: StrategyResult, kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "kind": kind,
        "variant": res.name,
        "split": res.split,
        "params": json.dumps(res.params, sort_keys=True),
        "n_trades": len(res.trades),
        "n_days": len(res.daily),
        "elapsed_s": res.elapsed_s,
        **summary_stats(res.daily),
        **trade_stats(res.trades),
    }
    if extra:
        row.update(extra)
    return row


def run_development(cfg: dict[str, Any], symbols: Iterable[str] | None = None) -> dict[str, Any]:
    symbols = list(symbols) if symbols else None
    start, end = split_dates(cfg, "development")
    base = run_strategy(cfg, "base", "development", symbols, start, end, {})
    ablations = run_ablations(cfg, "development", symbols)
    grid, grid_daily, winner = run_grid(cfg, symbols)
    rows = [_result_row(base, "base"), *ablations["rows"], *grid.to_dict(orient="records")]
    ledger = pd.DataFrame(rows)
    return {
        "base": base,
        "grid": grid,
        "grid_daily": grid_daily,
        "winner": winner,
        "ablations": ablations,
        "ledger": ledger,
    }


def run_validation(cfg: dict[str, Any], winner: dict[str, Any], symbols: Iterable[str] | None = None) -> dict[str, Any]:
    symbols = list(symbols) if symbols else None
    start, end = split_dates(cfg, "validation")
    base = run_strategy(cfg, "base", "validation", symbols, start, end, {})
    wname = "grid_winner"
    win = run_strategy(cfg, wname, "validation", symbols, start, end, winner)
    ablations = run_ablations(cfg, "validation", symbols)
    rows = [_result_row(base, "base"), _result_row(win, "grid_winner"), *ablations["rows"]]
    return {"base": base, "winner": win, "ablations": ablations, "ledger": pd.DataFrame(rows)}


def run_grid(cfg: dict[str, Any], symbols: Iterable[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    start, end = split_dates(cfg, "development")
    g = cfg["grid"]
    rows: list[dict[str, Any]] = []
    daily_cols: dict[str, pd.Series] = {}
    combos = list(product(g["cluster_atr"], g["min_touches"], g["confirmation_closes"]))
    for cluster, min_touch, conf in combos:
        name = f"grid_c{cluster}_m{min_touch}_k{conf}"
        ov = {"cluster_atr": float(cluster), "min_touches": int(min_touch), "confirmation_closes": int(conf)}
        res = run_strategy(cfg, name, "development", symbols, start, end, ov)
        rows.append(_result_row(res, "grid", ov))
        daily_cols[name] = res.daily
    grid = pd.DataFrame(rows)
    grid_daily = pd.concat(daily_cols, axis=1).fillna(0.0)
    if grid.empty:
        return grid, grid_daily, {}
    best = grid.sort_values(["sharpe", "avg_r"], ascending=False).iloc[0]
    winner = json.loads(best["params"])
    return grid, grid_daily, winner


ABLATIONS: dict[str, dict[str, Any]] = {
    "no_trend": {"require_trend": False},
    "no_confirmation": {"confirmation_closes": 0, "require_directional_candle": False},
    "no_directional": {"require_directional_candle": False},
    "ema_trend": {"trend_method": "ema"},
    "wick_levels": {"level_price_source": "wick"},
    "min_touches_3": {"min_touches": 3},
    "cluster_025": {"cluster_atr": 0.25},
    "cluster_100": {"cluster_atr": 1.0},
    "confirmation_1": {"confirmation_closes": 1},
    "confirmation_3": {"confirmation_closes": 3},
}


def run_ablations(cfg: dict[str, Any], split: str, symbols: Iterable[str] | None = None) -> dict[str, Any]:
    start, end = split_dates(cfg, split)
    rows: list[dict[str, Any]] = []
    results: dict[str, StrategyResult] = {}
    for name, ov in ABLATIONS.items():
        res = run_strategy(cfg, name, split, symbols, start, end, ov)
        results[name] = res
        rows.append(_result_row(res, "ablation", ov))
    return {"rows": rows, "results": results}


def run_holdout(
    cfg: dict[str, Any],
    winner: dict[str, Any],
    symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    symbols = list(symbols) if symbols else None
    start, end = split_dates(cfg, "holdout")
    base = run_strategy(cfg, "base", "holdout", symbols, start, end, {})
    win = run_strategy(cfg, "grid_winner", "holdout", symbols, start, end, winner)
    costs: list[dict[str, Any]] = []
    cost_results: dict[str, StrategyResult] = {}
    for mult in [0.0, 1.0, 2.0, 3.0]:
        nm = f"cost_{mult:g}x"
        res = run_strategy(cfg, nm, "holdout", symbols, start, end, {}, cost_multiplier=mult)
        cost_results[nm] = res
        costs.append(_result_row(res, "cost", {"cost_multiplier": mult}))
    return {
        "base": base,
        "winner": win,
        "costs": pd.DataFrame(costs),
        "cost_results": cost_results,
        "ledger": pd.DataFrame([_result_row(base, "base"), _result_row(win, "grid_winner"), *costs]),
    }


def buy_and_hold(cfg: dict[str, Any], symbols: Iterable[str] | None, start: str, end: str) -> pd.Series:
    symbols = list(symbols or cfg["instruments"].keys())
    cols = {}
    for symbol in symbols:
        tfs = load_tfs(cfg, symbol)
        close = tfs["1D"]["close"]
        r = close.pct_change()
        r.index = r.index.normalize()
        cols[symbol] = r
    mat = pd.concat(cols, axis=1).sort_index()
    port = mat.mean(axis=1)
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="B")
    return port.reindex(idx, fill_value=0.0).fillna(0.0)


def trend_only(cfg: dict[str, Any], symbols: Iterable[str] | None, start: str, end: str) -> pd.Series:
    symbols = list(symbols or cfg["instruments"].keys())
    cols = {}
    for symbol in symbols:
        tfs = load_tfs(cfg, symbol)
        events, trend, _ = prepare_cached(cfg, symbol, {})
        bars = tfs["15min"]
        tr = trend.reindex(bars.index, method="ffill").fillna(0)
        pos = tr.shift(1).fillna(0)
        ret = bars["close"].pct_change().fillna(0.0)
        # One unit of turnover is one side of the round trip; the strategy
        # engine charges half_spread + slippage per side.  Do not double-count
        # by multiplying by two here.
        cost_rate = (
            cfg["instruments"][symbol].half_spread_price + cfg["instruments"][symbol].slippage_price
        ) / bars["close"]
        turnover = pos.diff().abs()
        if len(turnover):
            turnover.iloc[0] = abs(pos.iloc[0])
        net = pos * ret - turnover * cost_rate
        daily = net.groupby(net.index.normalize()).sum()
        cols[symbol] = daily
    mat = pd.concat(cols, axis=1).sort_index()
    port = mat.mean(axis=1)
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="B")
    return port.reindex(idx, fill_value=0.0).fillna(0.0)


def sma_crossover(cfg: dict[str, Any], symbols: Iterable[str] | None, start: str, end: str) -> pd.Series:
    symbols = list(symbols or cfg["instruments"].keys())
    cols = {}
    for symbol in symbols:
        tfs = load_tfs(cfg, symbol)
        bars = tfs["4h"]
        fast = bars["close"].rolling(50, min_periods=50).mean()
        slow = bars["close"].rolling(200, min_periods=200).mean()
        pos = pd.Series(0, index=bars.index, dtype=float)
        pos[fast > slow] = 1.0
        pos[fast < slow] = -1.0
        pos = pos.shift(1).fillna(0.0)
        ret = bars["close"].pct_change().fillna(0.0)
        cost_rate = (
            cfg["instruments"][symbol].half_spread_price + cfg["instruments"][symbol].slippage_price
        ) / bars["close"]
        turnover = pos.diff().abs()
        if len(turnover):
            turnover.iloc[0] = abs(pos.iloc[0])
        net = pos * ret - turnover * cost_rate
        daily = net.groupby(net.index.normalize()).sum()
        cols[symbol] = daily
    mat = pd.concat(cols, axis=1).sort_index()
    port = mat.mean(axis=1)
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="B")
    return port.reindex(idx, fill_value=0.0).fillna(0.0)


def donchian(cfg: dict[str, Any], symbols: Iterable[str] | None, start: str, end: str) -> pd.Series:
    symbols = list(symbols or cfg["instruments"].keys())
    cols = {}
    for symbol in symbols:
        tfs = load_tfs(cfg, symbol)
        events, trend, _ = prepare_cached(cfg, symbol, {})
        bars = tfs["15min"]
        tr = trend.reindex(bars.index, method="ffill").fillna(0).to_numpy()
        close = bars["close"].to_numpy(dtype=float)
        high = bars["high"].to_numpy(dtype=float)
        low = bars["low"].to_numpy(dtype=float)
        upper = pd.Series(high).rolling(20, min_periods=20).max().shift(1).to_numpy()
        lower = pd.Series(low).rolling(20, min_periods=20).min().shift(1).to_numpy()
        n = len(bars)
        pos = np.zeros(n)
        state = 0
        for i in range(20, n - 1):
            if tr[i] == 1 and close[i] > upper[i]:
                state = 1
            elif tr[i] == -1 and close[i] < lower[i]:
                state = -1
            elif tr[i] == 0:
                state = 0
            pos[i + 1] = state
        ret = bars["close"].pct_change().fillna(0.0).to_numpy()
        cost_rate = (
            cfg["instruments"][symbol].half_spread_price + cfg["instruments"][symbol].slippage_price
        ) / bars["close"].to_numpy()
        turn = np.abs(np.diff(pos, prepend=0.0))
        net = pd.Series(pos * ret - turn * cost_rate, index=bars.index)
        daily = net.groupby(net.index.normalize()).sum()
        cols[symbol] = daily
    mat = pd.concat(cols, axis=1).sort_index()
    port = mat.mean(axis=1)
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="B")
    return port.reindex(idx, fill_value=0.0).fillna(0.0)


def run_benchmarks(
    cfg: dict[str, Any], split: str, symbols: Iterable[str] | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    start, end = split_dates(cfg, split)
    out: dict[str, pd.Series] = {
        "buy_and_hold": buy_and_hold(cfg, symbols, start, end),
        "trend_only": trend_only(cfg, symbols, start, end),
        "sma_50_200": sma_crossover(cfg, symbols, start, end),
        "donchian_20": donchian(cfg, symbols, start, end),
    }
    rows = []
    for name, daily in out.items():
        row = {"variant": name, "split": split, **summary_stats(daily)}
        rows.append(row)
    return pd.DataFrame(rows), pd.DataFrame(out)


def run_placebos(
    cfg: dict[str, Any],
    result: StrategyResult,
    symbols: Iterable[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, fn in (("random_entry", random_entry_placebo), ("random_level", random_level_placebo)):
        try:
            r = fn(cfg, result, symbols=symbols)
            out[name] = {k: v for k, v in r.items() if k != "draws"}
        except Exception as exc:  # noqa: BLE001
            LOG.warning("placebo %s failed: %s", name, exc)
            out[name] = {"pvalue": float("nan"), "error": str(exc)}
    return out


def random_entry_placebo(
    cfg: dict[str, Any],
    result: StrategyResult,
    symbols: Iterable[str] | None = None,
    n_draws: int | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    n_draws = int(n_draws or cfg["stats"]["n_placebo_draws"])
    seed = int(seed or cfg["stats"]["seed"])
    rng = np.random.default_rng(seed)
    symbols = list(symbols or cfg["instruments"].keys())
    start, end = split_dates(cfg, result.split)
    trades = result.trades
    if trades.empty:
        return {"draws": pd.Series(dtype=float), "pvalue": float("nan"), "observed": float("nan")}
    observed = float(trades["r_net"].mean())
    weight = np.zeros(n_draws)
    acc = np.zeros(n_draws)
    for symbol in symbols:
        sub = trades[trades["symbol"] == symbol]
        if sub.empty:
            continue
        tfs = load_tfs(cfg, symbol)
        bars = tfs["15min"]
        idx = bars.index
        lo = int(np.searchsorted(idx.values, np.datetime64(start), side="left")) + 250
        hi = int(np.searchsorted(idx.values, np.datetime64(end), side="right")) - int(cfg["strategy"]["max_hold_bars"]) - 2
        if hi <= lo:
            continue
        for side in (-1, 1):
            s2 = sub[sub["side"] == side]
            if s2.empty:
                continue
            stop_dist = s2["risk_price"].to_numpy(dtype=float)
            target_dist = (s2["target"] - s2["entry_raw"]).abs().to_numpy(dtype=float)
            cost = float(s2["cost_price"].iloc[0])
            # One vectorised call for all draws is far faster than 1,000
            # Python-level calls and yields the same placebo distribution.
            ent = rng.integers(lo, hi, size=(n_draws, len(s2)))
            r = simulate_trade_batch(
                ent.ravel(),
                side,
                np.tile(stop_dist, n_draws),
                np.tile(target_dist, n_draws),
                bars,
                cost,
                int(cfg["strategy"]["max_hold_bars"]),
            ).reshape(n_draws, len(s2))
            ok = np.isfinite(r)
            acc += np.where(ok, r, 0.0).sum(axis=1)
            weight += ok.sum(axis=1)
    draws = np.where(weight > 0, acc / np.maximum(weight, 1), np.nan)
    valid = draws[np.isfinite(draws)]
    p = float(np.mean(valid >= observed)) if len(valid) else float("nan")
    return {
        "draws": pd.Series(valid),
        "pvalue": p,
        "observed": observed,
        "n_trades": int(len(trades)),
        "mean_placebo": float(np.mean(valid)) if len(valid) else float("nan"),
        "sd_placebo": float(np.std(valid, ddof=1)) if len(valid) > 1 else float("nan"),
    }


def random_level_placebo(
    cfg: dict[str, Any],
    result: StrategyResult,
    symbols: Iterable[str] | None = None,
    n_draws: int = 200,
    seed: int | None = None,
) -> dict[str, Any]:
    """Replace the chosen level with a random active level at the same timestamp.

    Candidate levels are cached once per trade, then all draws are simulated
    in one vectorised call per symbol and side.  The stop is re-derived from
    the sampled level; the target distance and entry timing are held at the
    observed values so that the test isolates level location.
    """
    seed = int(seed or cfg["stats"]["seed"])
    rng = np.random.default_rng(seed)
    symbols = list(symbols or cfg["instruments"].keys())
    trades = result.trades
    if trades.empty:
        return {"draws": pd.Series(dtype=float), "pvalue": float("nan"), "observed": float("nan")}
    trades = trades[trades["symbol"].isin(symbols)]
    if trades.empty:
        return {"draws": pd.Series(dtype=float), "pvalue": float("nan"), "observed": float("nan")}
    observed = float(trades["r_net"].mean())
    weight = np.zeros(n_draws)
    acc = np.zeros(n_draws)
    buffer_atr = float(cfg["strategy"]["stop_buffer_atr"])
    max_chase = float(cfg["strategy"]["max_chase_atr"])
    max_stop = float(cfg["strategy"]["max_stop_atr"])
    for symbol in symbols:
        sub_all = trades[trades["symbol"] == symbol].sort_values("signal_ts")
        if sub_all.empty:
            continue
        tfs = load_tfs(cfg, symbol)
        bars = tfs["15min"]
        events, trend, cfg2 = prepare_cached(cfg, symbol, {})
        book = LevelBook(events, cfg2)
        candidate_lists: list[np.ndarray] = []
        for _, tr in sub_all.iterrows():
            ts = pd.Timestamp(tr["signal_ts"])
            book.advance(ts)
            side = int(tr["side"])
            ref = float(tr["entry_raw"])
            atr = float(tr["atr"])
            cands = []
            for lv in book.active_levels():
                if side < 0 and lv.price > ref and (lv.price - ref) <= max_chase * atr:
                    cands.append(lv.price)
                elif side > 0 and lv.price < ref and (ref - lv.price) <= max_chase * atr:
                    cands.append(lv.price)
            candidate_lists.append(np.asarray(cands if cands else [float(tr["level"])], dtype=float))
        for side in (-1, 1):
            mask = sub_all["side"].to_numpy(dtype=int) == side
            if not mask.any():
                continue
            sub = sub_all[mask]
            entry_idx = sub["entry_idx"].to_numpy(dtype=int)
            atr = sub["atr"].to_numpy(dtype=float)
            target_dist = (sub["target"] - sub["entry_raw"]).abs().to_numpy(dtype=float)
            cost = float(sub["cost_price"].iloc[0])
            level_mat = np.empty((n_draws, len(sub)))
            for k, cands in enumerate([c for c, m in zip(candidate_lists, mask) if m]):
                level_mat[:, k] = cands[rng.integers(0, len(cands), size=n_draws)]
            stop = level_mat - side * buffer_atr * atr[None, :]
            risk_new = np.abs(sub["entry_raw"].to_numpy(dtype=float)[None, :] - stop)
            valid = (risk_new > 0.25 * atr[None, :]) & (risk_new <= max_stop * atr[None, :])
            r = simulate_trade_batch(
                np.tile(entry_idx, n_draws),
                side,
                np.where(valid, risk_new, 1.0).ravel(),
                np.tile(target_dist, n_draws),
                bars,
                cost,
                int(cfg["strategy"]["max_hold_bars"]),
            ).reshape(n_draws, len(sub))
            r = np.where(valid, r, np.nan)
            ok = np.isfinite(r)
            acc += np.where(ok, r, 0.0).sum(axis=1)
            weight += ok.sum(axis=1)
    draws = np.where(weight > 0, acc / np.maximum(weight, 1), np.nan)
    valid_draws = draws[np.isfinite(draws)]
    p = float(np.mean(valid_draws >= observed)) if len(valid_draws) else float("nan")
    return {
        "draws": pd.Series(valid_draws),
        "pvalue": p,
        "observed": observed,
        "mean_placebo": float(np.mean(valid_draws)) if len(valid_draws) else float("nan"),
        "sd_placebo": float(np.std(valid_draws, ddof=1)) if len(valid_draws) > 1 else float("nan"),
        "n_draws": n_draws,
        "note": "random active level at the same timestamp; stop re-derived, target distance fixed",
    }


def session_labels(ts: pd.Series, source_offset_hours: float = 5.0) -> pd.Series:
    """Map timestamps to FX sessions after converting the source clock to UTC.

    HistData timestamps are fixed UTC-5 (EST, no DST), so the default offset
    adds five hours.  Live UTC data should pass ``source_offset_hours=0``.
    Sessions: Asia 00-08 UTC, London 08-13 UTC, New York 13-22 UTC, late.
    """
    utc = pd.to_datetime(ts) + pd.Timedelta(hours=float(source_offset_hours))
    hour = utc.dt.hour
    labels = np.select(
        [(hour < 8), (hour < 13), (hour < 22)],
        ["asia", "london", "new_york"],
        default="late",
    )
    return pd.Series(labels, index=ts.index, name="session")


def trade_event_study(
    trades: pd.DataFrame, source_offset_hours: float = 5.0
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if trades.empty:
        return pd.DataFrame(), pd.DataFrame()
    df = trades.copy()
    df["entry_ts"] = pd.to_datetime(df["entry_ts"])
    df["session"] = session_labels(df["entry_ts"], source_offset_hours=source_offset_hours)
    df["touch_bucket"] = pd.cut(df["touch_count"], bins=[1, 2, 3, 99], labels=["2", "3", "4+"])
    df["confluence_bucket"] = pd.cut(df["confluence"], bins=[0, 1, 2, 99], labels=["1", "2", "3+"])
    df["vol_bucket"] = pd.qcut((df["atr"] / df["entry_raw"]).rank(method="first"), 3, labels=["low", "mid", "high"])
    df["win"] = (df["r_net"] > 0).astype(int)
    rows = []
    for keys, g in df.groupby(["level_tf", "touch_bucket", "confluence_bucket", "session"], observed=True):
        if len(g) < 5:
            continue
        r = g["r_net"].to_numpy(dtype=float)
        se = np.std(r, ddof=1) / np.sqrt(len(r)) if len(r) > 1 else np.nan
        rows.append(
            {
                "level_tf": keys[0],
                "touch_bucket": str(keys[1]),
                "confluence_bucket": str(keys[2]),
                "session": keys[3],
                "n": int(len(g)),
                "mean_r": float(np.mean(r)),
                "win_rate": float(np.mean(r > 0)),
                "t_stat": float(np.mean(r) / se) if se and np.isfinite(se) and se > 0 else np.nan,
            }
        )
    groups = pd.DataFrame(rows).sort_values(["level_tf", "touch_bucket", "session"]).reset_index(drop=True) if rows else pd.DataFrame()
    reg_rows = []
    try:
        import statsmodels.api as sm
        import statsmodels.formula.api as smf

        d = df.dropna(subset=["r_net", "touch_count", "confluence", "atr", "entry_raw"]).copy()
        d["chase_atr"] = (d["level"] - d["entry_raw"]).abs() / d["atr"]
        d["level_tf"] = d["level_tf"].astype(str)
        d["session"] = d["session"].astype(str)
        model = smf.logit("win ~ confluence + touch_count + chase_atr + C(level_tf) + C(session)", data=d).fit(
            disp=False, cov_type="cluster", cov_kwds={"groups": d["entry_ts"].dt.normalize()}
        )
        for term in model.params.index:
            reg_rows.append(
                {
                    "term": term,
                    "coef": float(model.params[term]),
                    "se": float(model.bse[term]),
                    "z": float(model.tvalues[term]),
                    "p": float(model.pvalues[term]),
                    "n": int(model.nobs),
                }
            )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("event-study logit failed: %s", exc)
    return groups, pd.DataFrame(reg_rows)


def save_stage(
    cfg: dict[str, Any],
    stage: str,
    payload: dict[str, Any],
) -> None:
    base = ensure_dir(resolve(cfg, cfg["data"]["results_dir"]) / stage)
    tables = ensure_dir(base / "tables")
    figs = ensure_dir(base / "figures")
    for key in ["ledger", "grid", "costs", "benchmarks", "event_groups", "event_regression"]:
        if key in payload and isinstance(payload[key], pd.DataFrame) and not payload[key].empty:
            write_csv(tables / f"{key}.csv", payload[key])
    if "base" in payload and isinstance(payload["base"], StrategyResult):
        write_csv(tables / "trades_base.csv", payload["base"].trades)
        payload["base"].daily.rename("return").to_csv(tables / "daily_base.csv")
    if "winner" in payload and isinstance(payload["winner"], StrategyResult):
        write_csv(tables / "trades_winner.csv", payload["winner"].trades)
        payload["winner"].daily.rename("return").to_csv(tables / "daily_winner.csv")
    if "grid_daily" in payload and isinstance(payload["grid_daily"], pd.DataFrame):
        payload["grid_daily"].to_csv(tables / "grid_daily.csv")
    # Keep summary.json small and machine-readable.  In particular, never
    # serialise the nested StrategyResult objects (their repr embeds the full
    # trade and signal frames).
    meta: dict[str, Any] = {}
    for key in ("inference", "winner"):
        if key in payload:
            meta[key] = payload[key]
    if isinstance(payload.get("ablations"), dict):
        meta["ablations"] = payload["ablations"].get("rows", [])
    if isinstance(payload.get("placebos"), dict):
        meta["placebos"] = payload["placebos"]
    write_json(base / "summary.json", meta)
    LOG.info("saved %s to %s", stage, base)
