"""Historical M1 loading and causal timeframe construction.

HistData.com Generic ASCII timestamps are US Eastern Standard Time (EST)
without daylight-saving adjustments.  We therefore add five hours to obtain
UTC, and we retain an EST index for chart-aligned resampling.  Every bar is
indexed by its *close* time, which is the earliest moment its OHLC values may
be used by a decision rule.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import Instrument, load_config, resolve
from .io import ensure_dir

LOG = logging.getLogger(__name__)

M1_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]

TF_RULES: dict[str, tuple[str, str]] = {
    "15min": ("15min", "0h"),
    "4h": ("4h", "0h"),
    "1D": ("1D", "17h"),
    "1W": ("1W-FRI", "17h"),
    "1M": ("1ME", "17h"),
}


def read_histdata_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        sep=";",
        header=None,
        names=M1_COLUMNS,
        dtype={"datetime": str, "open": float, "high": float, "low": float, "close": float, "volume": float},
        engine="c",
    )
    df["ts_est"] = pd.to_datetime(df["datetime"], format="%Y%m%d %H%M%S", errors="coerce")
    df = df.dropna(subset=["ts_est"]).drop(columns=["datetime"])
    df = df.set_index("ts_est").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df["ts_utc"] = df.index + pd.Timedelta(hours=5)
    return df


def clean_m1(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Drop impossible OHLC rows and report the count."""
    n0 = len(df)
    finite = np.isfinite(df[["open", "high", "low", "close"]]).all(axis=1)
    positive = (df[["open", "high", "low", "close"]] > 0).all(axis=1)
    upper = df["high"] >= df[["open", "close"]].max(axis=1)
    lower = df["low"] <= df[["open", "close"]].min(axis=1)
    ordered = df["high"] >= df["low"]
    keep = finite & positive & upper & lower & ordered
    out = df.loc[keep].copy()
    stats = {
        "rows_in": int(n0),
        "rows_out": int(len(out)),
        "dropped_nonfinite": int((~finite).sum()),
        "dropped_nonpositive": int((~positive).sum()),
        "dropped_bad_high": int((~upper).sum()),
        "dropped_bad_low": int((~lower).sum()),
        "dropped_inverted": int((~ordered).sum()),
    }
    return out, stats


def load_m1_symbol(cfg: dict[str, Any], symbol: str) -> pd.DataFrame:
    inst: Instrument = cfg["instruments"][symbol]
    raw = resolve(cfg, cfg["data"]["raw_dir"]) / symbol
    files = sorted(raw.glob("*.csv")) + sorted(raw.glob("*.txt"))
    if not files:
        raise FileNotFoundError(
            f"no HistData CSV/TXT found in {raw}; run `klab download` first"
        )
    frames = []
    for f in files:
        if f.name.endswith(".txt") and f.with_suffix(".csv").exists():
            continue
        frames.append(read_histdata_csv(f))
    df = pd.concat(frames, axis=0)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    start = pd.Timestamp(cfg["data"]["start"])
    end = pd.Timestamp(cfg["data"]["end"]) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    df = df.loc[(df.index >= start) & (df.index <= end)]
    df, stats = clean_m1(df)
    LOG.info("%s: %s", symbol, stats)
    if df.empty:
        raise ValueError(f"{symbol}: no usable M1 rows")
    return df


def resample_ohlc(df: pd.DataFrame, rule: str, offset: str = "0h") -> pd.DataFrame:
    out = df.resample(rule, offset=offset, label="right", closed="right").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        n_m1=("close", "count"),
    )
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[out["n_m1"] > 0]
    out = out[(out["high"] >= out["low"]) & (out["high"] > 0)]
    return out


def add_atr(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out = df.copy()
    out["tr"] = tr
    out[f"atr{length}"] = tr.rolling(length, min_periods=length).mean()
    return out


def build_timeframes(m1: pd.DataFrame) -> dict[str, pd.DataFrame]:
    tfs: dict[str, pd.DataFrame] = {}
    for name, (rule, offset) in TF_RULES.items():
        bars = resample_ohlc(m1, rule, offset)
        bars = add_atr(bars, 14)
        tfs[name] = bars
    return tfs


def save_timeframes(cfg: dict[str, Any], symbol: str, tfs: dict[str, pd.DataFrame]) -> None:
    out = ensure_dir(resolve(cfg, cfg["data"]["processed_dir"]) / symbol)
    for name, df in tfs.items():
        df.to_parquet(out / f"{name}.parquet", index=True)


def load_timeframes(cfg: dict[str, Any], symbol: str) -> dict[str, pd.DataFrame]:
    base = resolve(cfg, cfg["data"]["processed_dir"]) / symbol
    tfs: dict[str, pd.DataFrame] = {}
    for name in TF_RULES:
        path = base / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"missing processed timeframe {path}; run `klab build`")
        tfs[name] = pd.read_parquet(path)
    return tfs


def build_all(cfg: dict[str, Any], force: bool = False) -> None:
    for symbol in cfg["instruments"]:
        out = resolve(cfg, cfg["data"]["processed_dir"]) / symbol / "1D.parquet"
        if out.exists() and not force:
            LOG.info("%s: processed data exists; skipping", symbol)
            continue
        LOG.info("%s: loading M1", symbol)
        m1 = load_m1_symbol(cfg, symbol)
        LOG.info("%s: %d M1 rows; resampling", symbol, len(m1))
        tfs = build_timeframes(m1)
        for name, df in tfs.items():
            LOG.info("%s %s: %d bars", symbol, name, len(df))
        save_timeframes(cfg, symbol, tfs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_all(load_config())
