"""Data audit, external cross-validation, and level-reaction diagnostics.

The audit has three jobs:

1.  Prove that the processed bars are internally coherent (no duplicates,
    monotone timestamps, valid OHLC, plausible gaps).
2.  Cross-validate the HistData series against an independent vendor
    (Yahoo Finance daily bars) on weekly returns, so a vendor-specific
    artefact cannot masquerade as a result.
3.  Test the practitioner's central claim directly: does a marked level
    actually "hold" after a rejection, and is the subsequent move large
    enough to pay for a 1R stop?  This is separate from the full strategy
    backtest and is reported as a descriptive event study.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .config import resolve
from .download import download_yahoo_live
from .io import ensure_dir, write_csv, write_json
from .pipelines import load_tfs, session_labels
from .timeframes import TF_RULES

LOG = logging.getLogger(__name__)


def _symbols(cfg: dict[str, Any], symbols: Iterable[str] | None) -> list[str]:
    return list(symbols) if symbols else list(cfg["instruments"].keys())


def integrity_report(cfg: dict[str, Any], symbols: Iterable[str] | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol in _symbols(cfg, symbols):
        tfs = load_tfs(cfg, symbol)
        for tf, bars in tfs.items():
            o = bars["open"].to_numpy(dtype=float)
            h = bars["high"].to_numpy(dtype=float)
            l = bars["low"].to_numpy(dtype=float)
            c = bars["close"].to_numpy(dtype=float)
            r = np.diff(np.log(np.maximum(c, 1e-12)))
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "rows": int(len(bars)),
                    "start": str(bars.index.min()),
                    "end": str(bars.index.max()),
                    "duplicate_ts": int(bars.index.duplicated().sum()),
                    "monotone": bool(bars.index.is_monotonic_increasing),
                    "ohlc_violations": int(
                        ((h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l)).sum()
                    ),
                    "nonpositive": int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum()),
                    "atr_nan": int(bars["atr14"].isna().sum()) if "atr14" in bars else -1,
                    "zero_range_share": float(np.mean(np.isclose(h, l))) if len(bars) else np.nan,
                    "max_abs_log_return": float(np.max(np.abs(r))) if len(r) else np.nan,
                }
            )
    return pd.DataFrame(rows)


def _weekly_close_from_daily(daily: pd.DataFrame) -> pd.Series:
    s = daily["close"].copy()
    s.index = pd.to_datetime(s.index)
    # Normalise to a tz-naive calendar date, then take the last observation
    # in each ISO week.  This is vendor-agnostic and avoids session offsets.
    s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
    return s.groupby(s.index.to_period("W-FRI")).last()


def cross_validate_yahoo(
    cfg: dict[str, Any],
    symbols: Iterable[str] | None = None,
    start: str = "2012-01-01",
    end: str | None = None,
    force: bool = False,
) -> pd.DataFrame:
    """Compare weekly returns of HistData and Yahoo daily bars."""
    symbols = _symbols(cfg, symbols)
    yahoo_symbols = [cfg["instruments"][s].yahoo_symbol for s in symbols]
    try:
        download_yahoo_live(cfg, yahoo_symbols, interval="1d", start=start, end=end, force=force)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("yahoo daily download failed: %s", exc)
    outdir = resolve(cfg, cfg["data"]["live_dir"]) / "yahoo"
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        ysym = cfg["instruments"][symbol].yahoo_symbol
        safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in ysym)
        path = outdir / f"{safe}_1d.parquet"
        if not path.exists():
            rows.append({"symbol": symbol, "n_weeks": 0, "corr": np.nan, "note": "yahoo file missing"})
            continue
        yd = pd.read_parquet(path)
        hd = load_tfs(cfg, symbol)["1D"]
        a = _weekly_close_from_daily(hd)
        b = _weekly_close_from_daily(yd)
        a = a[~a.index.duplicated(keep="last")]
        b = b[~b.index.duplicated(keep="last")]
        ra = a.pct_change()
        rb = b.pct_change()
        joined = pd.concat({"histdata": ra, "yahoo": rb}, axis=1).dropna()
        # The two vendors can disagree on the calendar week around holidays;
        # require at least 52 overlapping weeks before reporting a correlation.
        if len(joined) < 52:
            rows.append({"symbol": symbol, "n_weeks": int(len(joined)), "corr": np.nan, "note": "too few overlapping weeks"})
            continue
        corr = float(joined["histdata"].corr(joined["yahoo"]))
        mad = float(np.mean(np.abs(joined["histdata"] - joined["yahoo"])))
        rows.append(
            {
                "symbol": symbol,
                "n_weeks": int(len(joined)),
                "corr": corr,
                "mean_abs_weekly_return_diff": mad,
                "histdata_ann_vol": float(joined["histdata"].std() * np.sqrt(52)),
                "yahoo_ann_vol": float(joined["yahoo"].std() * np.sqrt(52)),
                "note": "weekly close-to-close returns; levels are not compared because vendors quote different instruments (spot vs futures)",
            }
        )
    return pd.DataFrame(rows)


def intrabar_ambiguity(cfg: dict[str, Any], trades: pd.DataFrame) -> dict[str, Any]:
    """How often can a single 15m bar contain both the stop and the target?"""
    if trades.empty:
        return {"n": 0, "ambiguous": 0, "share": float("nan")}
    total = 0
    amb = 0
    entry_bar_amb = 0
    for symbol, g in trades.groupby("symbol"):
        bars = load_tfs(cfg, symbol)["15min"]
        h = bars["high"].to_numpy(dtype=float)
        l = bars["low"].to_numpy(dtype=float)
        for _, r in g.iterrows():
            i0 = int(r["entry_idx"])
            i1 = int(r["exit_idx"])
            side = int(r["side"])
            stop = float(r["stop"])
            target = float(r["target"])
            total += 1
            if side < 0:
                both = (h[i0 : i1 + 1] >= stop) & (l[i0 : i1 + 1] <= target)
            else:
                both = (l[i0 : i1 + 1] <= stop) & (h[i0 : i1 + 1] >= target)
            if bool(np.any(both)):
                amb += 1
            if bool(both[0]):
                entry_bar_amb += 1
    return {
        "n": int(total),
        "ambiguous": int(amb),
        "share": float(amb / total) if total else float("nan"),
        "entry_bar_ambiguous": int(entry_bar_amb),
        "note": "both stop and target inside the same 15-minute bar at some point before exit",
    }


def level_reaction_study(
    cfg: dict[str, Any],
    trades: pd.DataFrame,
    horizon: int | None = None,
) -> pd.DataFrame:
    """Measure level holds and maximum favourable excursion (MFE).

    For each executed signal we ask, over the next `horizon` 15m bars:

    * `breach`: did the close move beyond the level by 0.25 ATR against the
      intended direction?  (A breach means the level failed.)
    * `hold`: the complement of `breach`.
    * `mfe_r` / `mae_r`: maximum favourable / adverse excursion in R units.
    * `hit_1r`, `hit_2r`, `hit_3r`: did price reach +1R/+2R/+3R before the
      stop was hit?  This is the practitioner's "90% success" claim tested
      in its own units, independent of the chosen target.
    """
    if trades.empty:
        return pd.DataFrame()
    horizon = int(horizon or cfg["strategy"]["max_hold_bars"])
    rows: list[dict[str, Any]] = []
    for symbol, g in trades.groupby("symbol"):
        bars = load_tfs(cfg, symbol)["15min"]
        h = bars["high"].to_numpy(dtype=float)
        l = bars["low"].to_numpy(dtype=float)
        c = bars["close"].to_numpy(dtype=float)
        n = len(bars)
        for _, tr in g.iterrows():
            i0 = int(tr["entry_idx"])
            i1 = min(i0 + horizon, n - 1)
            side = int(tr["side"])
            level = float(tr["level"])
            entry = float(tr["entry_raw"])
            risk = float(tr["risk_price"])
            atr = float(tr["atr"])
            if risk <= 0 or i1 <= i0:
                continue
            hh = h[i0 : i1 + 1]
            ll = l[i0 : i1 + 1]
            cc = c[i0 : i1 + 1]
            if side < 0:
                mfe = (entry - ll.min()) / risk
                mae = (hh.max() - entry) / risk
                breach = bool(np.any(cc > level + 0.25 * atr))
            else:
                mfe = (hh.max() - entry) / risk
                mae = (entry - ll.min()) / risk
                breach = bool(np.any(cc < level - 0.25 * atr))
            # stop-first path for the +kR milestones
            stop_idx = np.argmax((ll <= entry - side * risk) if side > 0 else (hh >= entry + risk))
            stopped = bool(((ll <= entry - risk) if side > 0 else (hh >= entry + risk)).any())
            first_stop = int(stop_idx) if stopped else len(hh)
            hits = {}
            for k in (1, 2, 3):
                if side < 0:
                    hit = np.argmax(ll <= entry - k * risk) if np.any(ll <= entry - k * risk) else len(ll)
                else:
                    hit = np.argmax(hh >= entry + k * risk) if np.any(hh >= entry + k * risk) else len(hh)
                hits[f"hit_{k}r"] = bool(hit < first_stop)
            rows.append(
                {
                    "symbol": symbol,
                    "level_tf": tr["level_tf"],
                    "side": side,
                    "session": session_labels(pd.Series([pd.Timestamp(tr["entry_ts"])])).iloc[0],
                    "breach": breach,
                    "hold": not breach,
                    "mfe_r": float(mfe),
                    "mae_r": float(mae),
                    "stopped": stopped,
                    **hits,
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df


def summarise_reactions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rows = []
    for keys, g in df.groupby(["level_tf", "side"], dropna=False):
        rows.append(
            {
                "level_tf": keys[0],
                "side": int(keys[1]),
                "n": int(len(g)),
                "hold_rate": float(g["hold"].mean()),
                "hit_1r_before_stop": float(g["hit_1r"].mean()),
                "hit_2r_before_stop": float(g["hit_2r"].mean()),
                "hit_3r_before_stop": float(g["hit_3r"].mean()),
                "mean_mfe_r": float(g["mfe_r"].mean()),
                "median_mfe_r": float(g["mfe_r"].median()),
                "mean_mae_r": float(g["mae_r"].mean()),
            }
        )
    rows.append(
        {
            "level_tf": "ALL",
            "side": 0,
            "n": int(len(df)),
            "hold_rate": float(df["hold"].mean()),
            "hit_1r_before_stop": float(df["hit_1r"].mean()),
            "hit_2r_before_stop": float(df["hit_2r"].mean()),
            "hit_3r_before_stop": float(df["hit_3r"].mean()),
            "mean_mfe_r": float(df["mfe_r"].mean()),
            "median_mfe_r": float(df["mfe_r"].median()),
            "mean_mae_r": float(df["mae_r"].mean()),
        }
    )
    return pd.DataFrame(rows)


def run_audit(
    cfg: dict[str, Any],
    symbols: Iterable[str] | None = None,
    trades: pd.DataFrame | None = None,
    force_yahoo: bool = False,
) -> dict[str, Any]:
    base = ensure_dir(resolve(cfg, cfg["data"]["results_dir"]) / "audit")
    tables = ensure_dir(base / "tables")
    integrity = integrity_report(cfg, symbols)
    write_csv(tables / "integrity.csv", integrity)
    cross = cross_validate_yahoo(cfg, symbols, force=force_yahoo)
    write_csv(tables / "cross_validation.csv", cross)
    if trades is None:
        path = resolve(cfg, cfg["data"]["results_dir"]) / "development" / "tables" / "trades_base.csv"
        trades = pd.read_csv(path, parse_dates=["signal_ts", "entry_ts", "exit_ts"]) if path.exists() else pd.DataFrame()
    amb = intrabar_ambiguity(cfg, trades)
    write_json(base / "intrabar_ambiguity.json", amb)
    reactions = level_reaction_study(cfg, trades)
    write_csv(tables / "level_reactions.csv", reactions)
    summary = summarise_reactions(reactions)
    write_csv(tables / "level_reaction_summary.csv", summary)
    meta = {
        "n_integrity_rows": int(len(integrity)),
        "cross_validation": cross.to_dict(orient="records"),
        "intrabar_ambiguity": amb,
        "level_reaction_summary": summary.to_dict(orient="records") if not summary.empty else [],
    }
    write_json(base / "audit.json", meta)
    LOG.info("audit written to %s", base)
    return {"integrity": integrity, "cross_validation": cross, "intrabar": amb, "reactions": reactions, "summary": summary}
