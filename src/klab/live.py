"""2026 live shadow: run the frozen rules on data that did not exist when the
rules were written.

The shadow is deliberately *descriptive*.  Yahoo Finance only serves 15-minute
bars for roughly the last 60 days, so the intraday sample is small and no
significance test is claimed.  The point is operational: does the same code,
same levels, same execution assumptions produce trades out of sample, and do
the trades look like the historical distribution?

Data layout
-----------
* Yahoo daily bars from 2012 to now provide the level and weekly/monthly
  history.
* Yahoo 15-minute bars for the last ~59 days provide the 15-minute and 4-hour
  decision timeframes.
* All timestamps are converted to tz-naive UTC so the historical and live
  code paths share the same index semantics.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .backtest import simulate_trades
from .config import Instrument, resolve
from .download import download_yahoo_live
from .io import ensure_dir, write_csv, write_json
from .pipelines import session_labels
from .stats import summary_stats, trade_stats
from .strategy import generate_signals, prepare_features
from .timeframes import add_atr, resample_ohlc

LOG = logging.getLogger(__name__)

LIVE_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "BTCUSD": {
        "asset_class": "crypto",
        "yahoo_symbol": "BTC-USD",
        "pip_size": 1.0,
        "price_digits": 2,
        "half_spread_price": 20.0,
        "slippage_price": 20.0,
    },
    "ETHUSD": {
        "asset_class": "crypto",
        "yahoo_symbol": "ETH-USD",
        "pip_size": 0.1,
        "price_digits": 2,
        "half_spread_price": 1.0,
        "slippage_price": 1.0,
    },
}


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in name)


def _to_naive_utc(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if out.index.tz is not None:
        out.index = out.index.tz_convert("UTC").tz_localize(None)
    return out.sort_index()


def _live_instrument(cfg: dict[str, Any], name: str) -> Instrument:
    if name in cfg["instruments"]:
        return cfg["instruments"][name]
    spec = LIVE_INSTRUMENTS[name]
    return Instrument(
        symbol=name,
        asset_class=spec["asset_class"],
        histdata_symbol="",
        yahoo_symbol=spec["yahoo_symbol"],
        pip_size=float(spec["pip_size"]),
        price_digits=int(spec["price_digits"]),
        half_spread_price=float(spec["half_spread_price"]),
        slippage_price=float(spec["slippage_price"]),
    )


def _live_symbols(cfg: dict[str, Any], symbols: Iterable[str] | None) -> list[str]:
    if symbols:
        return [s.upper() for s in symbols]
    return [*cfg["instruments"].keys(), *LIVE_INSTRUMENTS.keys()]


def _load_yahoo(cfg: dict[str, Any], ysym: str, interval: str) -> pd.DataFrame:
    path = resolve(cfg, cfg["data"]["live_dir"]) / "yahoo" / f"{_safe(ysym)}_{interval}.parquet"
    if not path.exists():
        return pd.DataFrame()
    return _to_naive_utc(pd.read_parquet(path))


def build_live_timeframes(daily: pd.DataFrame, intraday: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Assemble the five decision timeframes from Yahoo daily + 15m bars."""
    d = add_atr(daily.copy(), 14)
    w = add_atr(resample_ohlc(d, "W-FRI", "0h"), 14)
    m = add_atr(resample_ohlc(d, "1ME", "0h"), 14)
    h4 = add_atr(resample_ohlc(intraday, "4h", "0h"), 14)
    m15 = add_atr(intraday.copy(), 14)
    return {"1D": d, "1W": w, "1M": m, "4h": h4, "15min": m15}


def run_live_shadow(
    cfg: dict[str, Any],
    symbols: Iterable[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    names = _live_symbols(cfg, symbols)
    yahoo_syms = []
    for name in names:
        if name in cfg["instruments"]:
            yahoo_syms.append(cfg["instruments"][name].yahoo_symbol)
        elif name in LIVE_INSTRUMENTS:
            yahoo_syms.append(LIVE_INSTRUMENTS[name]["yahoo_symbol"])
    LOG.info("live shadow: downloading %d daily + 15m series", len(yahoo_syms))
    download_yahoo_live(cfg, yahoo_syms, interval="1d", start="2012-01-01", force=force)
    download_yahoo_live(cfg, yahoo_syms, interval="15m", force=force)

    base = ensure_dir(resolve(cfg, cfg["data"]["results_dir"]) / "live")
    tables = ensure_dir(base / "tables")
    per_symbol: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    signal_frames: list[pd.DataFrame] = []
    for name in names:
        ysym = (
            cfg["instruments"][name].yahoo_symbol
            if name in cfg["instruments"]
            else LIVE_INSTRUMENTS[name]["yahoo_symbol"]
        )
        daily = _load_yahoo(cfg, ysym, "1d")
        intraday = _load_yahoo(cfg, ysym, "15m")
        if daily.empty or intraday.empty:
            LOG.warning("live %s: missing daily or 15m data", name)
            per_symbol.append({"symbol": name, "n_trades": 0, "note": "missing data"})
            continue
        tfs = build_live_timeframes(daily, intraday)
        try:
            events, trend, cfg2 = prepare_features(tfs, cfg)
            signals = generate_signals(tfs, events, trend, cfg, end_ts=tfs["15min"].index[-1])
            trades, daily_pnl = simulate_trades(
                signals,
                tfs,
                trend,
                cfg2,
                _live_instrument(cfg, name),
                cost_multiplier=1.0,
                start_ts=tfs["15min"].index[0],
                end_ts=tfs["15min"].index[-1],
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("live %s failed: %s", name, exc)
            per_symbol.append({"symbol": name, "n_trades": 0, "note": f"error: {exc}"})
            continue
        if not signals.empty:
            s = signals.copy()
            s["symbol"] = name
            signal_frames.append(s)
        if not trades.empty:
            t = trades.copy()
            t["symbol"] = name
            t["session"] = session_labels(pd.to_datetime(t["entry_ts"]), source_offset_hours=0.0).to_numpy()
            trade_frames.append(t)
        row = {
            "symbol": name,
            "n_trades": int(len(trades)),
            "n_signals": int(len(signals)),
            "first_15m": str(tfs["15min"].index.min()),
            "last_15m": str(tfs["15min"].index.max()),
            **summary_stats(daily_pnl),
            **trade_stats(trades),
        }
        per_symbol.append(row)
        LOG.info("live %s: %d trades", name, len(trades))

    per = pd.DataFrame(per_symbol)
    write_csv(tables / "per_symbol.csv", per)
    if trade_frames:
        all_trades = pd.concat(trade_frames, ignore_index=True)
        write_csv(tables / "trades.csv", all_trades)
        portfolio = all_trades.groupby(all_trades["exit_ts"].dt.normalize())["r_net"].sum() * float(
            cfg["strategy"]["risk_per_trade"]
        )
        port_summary = {
            "n_trades": int(len(all_trades)),
            "mean_r": float(all_trades["r_net"].mean()),
            "win_rate": float((all_trades["r_net"] > 0).mean()),
            "profit_factor": float(
                all_trades.loc[all_trades["r_net"] > 0, "r_net"].sum()
                / abs(all_trades.loc[all_trades["r_net"] < 0, "r_net"].sum())
            )
            if (all_trades["r_net"] < 0).any()
            else float("nan"),
            "per_symbol_mean_r": per.set_index("symbol")["avg_r"].dropna().to_dict() if "avg_r" in per else {},
        }
    else:
        write_csv(tables / "trades.csv", pd.DataFrame())
        port_summary = {"n_trades": 0}
    if signal_frames:
        write_csv(tables / "signals.csv", pd.concat(signal_frames, ignore_index=True))
    meta = {
        "window": "Yahoo 15m last ~59 days; daily history from 2012",
        "note": "descriptive shadow, not a significance test",
        "portfolio": port_summary,
        "per_symbol": per.to_dict(orient="records"),
    }
    write_json(base / "summary.json", meta)
    LOG.info("live shadow written to %s", base)
    return meta
