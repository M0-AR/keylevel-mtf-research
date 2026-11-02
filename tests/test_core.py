import numpy as np
import pandas as pd
import pytest

from klab.backtest import simulate_trades
from klab.config import Instrument, load_config
from klab.levels import LevelBook
from klab.pipelines import session_labels
from klab.pivots import find_pivots
from klab.stats import benjamini_hochberg, deflated_sharpe_ratio, pbo_cscv, psr


def _bars(n=6):
    idx = pd.date_range("2020-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open": [1.1000] * n,
            "high": [1.1002] * n,
            "low": [1.0998] * n,
            "close": [1.1000] * n,
            "volume": [0.0] * n,
            "atr14": [0.0020] * n,
        },
        index=idx,
    )


def test_pivot_confirmation_is_causal():
    idx = pd.date_range("2020-01-01", periods=10, freq="1D")
    close = np.array([10, 11, 12, 15, 12, 11, 10, 9, 10, 11], dtype=float)
    df = pd.DataFrame(
        {"open": close, "high": close + 0.1, "low": close - 0.1, "close": close},
        index=idx,
    )
    piv = find_pivots(df, left=2, right=2, price_source="body")
    highs = piv[piv["kind"] == "high"]
    assert len(highs) == 1
    assert highs.iloc[0]["pivot_ts"] == idx[3]
    assert highs.iloc[0]["confirm_ts"] == idx[5]


def test_level_book_waits_for_min_touches():
    cfg = load_config()
    events = pd.DataFrame(
        {
            "ts": [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02")],
            "cid": ["1D-0", "1D-0"],
            "timeframe": ["1D", "1D"],
            "price": [1.10, 1.1001],
            "weight": [1, 2],
            "atr_d1": [0.01, 0.01],
            "pivot_ts": [pd.Timestamp("2019-12-30"), pd.Timestamp("2019-12-31")],
            "kind": ["high", "high"],
        }
    )
    book = LevelBook(events, cfg)
    book.advance(pd.Timestamp("2020-01-01 12:00"))
    assert book.active_levels() == []
    book.advance(pd.Timestamp("2020-01-02 12:00"))
    assert len(book.active_levels()) == 1


def test_stop_first_and_costs():
    cfg = load_config()
    bars = _bars()
    bars.loc[bars.index[1], "high"] = 1.1025
    bars.loc[bars.index[1], "low"] = 1.0955
    bars.loc[bars.index[1], "open"] = 1.1000
    bars.loc[bars.index[1], "close"] = 1.1000
    trend = pd.Series(-1, index=bars.index)
    sig = pd.DataFrame(
        [
            {
                "signal_idx": 0,
                "signal_ts": bars.index[0],
                "entry_idx": 1,
                "side": -1,
                "level": 1.1010,
                "level_tf": "1D",
                "level_weight": 2,
                "touch_count": 2,
                "confluence": 1,
                "stop_raw": 1.1020,
                "target_level": 1.0960,
                "atr": 0.0020,
                "trend": -1,
                "close": 1.1000,
                "risk_proxy": 0.0020,
            }
        ]
    )
    inst = Instrument("EURUSD", "fx_major", "EURUSD", "EURUSD=X", 0.0001, 5, 0.00003, 0.00002)
    t0, d0 = simulate_trades(sig, {"15min": bars}, trend, cfg, inst, cost_multiplier=0.0)
    t1, d1 = simulate_trades(sig, {"15min": bars}, trend, cfg, inst, cost_multiplier=1.0)
    assert len(t0) == 1 and len(t1) == 1
    assert t0.iloc[0]["exit_reason"].startswith("stop")
    assert t1.iloc[0]["r_net"] < t0.iloc[0]["r_net"]


def test_gap_through_target_is_skipped():
    cfg = load_config()
    bars = _bars(n=4)
    bars.loc[bars.index[1], "open"] = 1.0950
    bars.loc[bars.index[1], "high"] = 1.0955
    bars.loc[bars.index[1], "low"] = 1.0945
    bars.loc[bars.index[1], "close"] = 1.0950
    trend = pd.Series(-1, index=bars.index)
    sig = pd.DataFrame(
        [
            {
                "signal_idx": 0,
                "signal_ts": bars.index[0],
                "entry_idx": 1,
                "side": -1,
                "level": 1.1010,
                "level_tf": "1D",
                "level_weight": 2,
                "touch_count": 2,
                "confluence": 1,
                "stop_raw": 1.1020,
                "target_level": 1.0960,
                "atr": 0.0020,
                "trend": -1,
                "close": 1.1000,
                "risk_proxy": 0.0020,
            }
        ]
    )
    inst = Instrument("EURUSD", "fx_major", "EURUSD", "EURUSD=X", 0.0001, 5, 0.00003, 0.00002)
    trades, _ = simulate_trades(sig, {"15min": bars}, trend, cfg, inst, cost_multiplier=0.0)
    assert trades.empty


def test_session_labels_convert_est_to_utc():
    ts = pd.Series(pd.to_datetime(["2020-01-01 09:00", "2020-01-01 12:00", "2020-01-01 14:00", "2020-01-01 23:00"]))
    labels = session_labels(ts, source_offset_hours=5.0).tolist()
    assert labels == ["new_york", "new_york", "new_york", "asia"]


def test_bh_and_pbo_smoke():
    p = [0.001, 0.01, 0.2, 0.9]
    bh = benjamini_hochberg(p, alpha=0.05)
    assert bh["reject"][0]
    rng = np.random.default_rng(0)
    mat = rng.normal(0, 0.01, size=(240, 8))
    out = pbo_cscv(mat, n_slices=6)
    assert 0.0 <= out["pbo"] <= 1.0
    dsr = deflated_sharpe_ratio(mat[:, 0], [0.01] * 10)
    assert 0.0 <= dsr["dsr"] <= 1.0
    assert 0.0 <= psr(mat[:, 0])["psr"] <= 1.0
