"""Online key-level construction and causal level book.

Levels are built from confirmed pivots, clustered in price, and only become
tradable after `min_touches` pivots have joined the cluster.  A cluster's
price is the touch-weighted mean of its pivots.  Clusters expire after a
fixed number of calendar days without an update.  The book is advanced with
event timestamps, so a level can never be used before the bar that confirmed
the pivot which created or updated it.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .pivots import find_pivots


@dataclass
class LevelState:
    cid: str
    timeframe: str
    price: float
    weight: int
    created: pd.Timestamp
    updated: pd.Timestamp

    def is_expired(self, ts: pd.Timestamp, expiry_days: int) -> bool:
        return (ts - self.updated) > pd.Timedelta(days=expiry_days)


@dataclass
class _Cluster:
    cid: str
    timeframe: str
    price: float
    weight: int
    created: pd.Timestamp
    updated: pd.Timestamp


def _atr_asof(daily_atr: pd.Series, ts: pd.Timestamp) -> float:
    if daily_atr is None or daily_atr.empty:
        return float("nan")
    val = daily_atr.asof(ts)
    if pd.isna(val):
        med = float(daily_atr.dropna().median()) if daily_atr.notna().any() else float("nan")
        return med
    return float(val)


def build_level_events(
    tfs: dict[str, pd.DataFrame],
    cfg: dict[str, Any],
    price_source: str | None = None,
) -> pd.DataFrame:
    """Return one row per level update, timestamped at pivot confirmation."""
    s = cfg["strategy"]
    price_source = price_source or s["level_price_source"]
    daily_atr = tfs["1D"]["atr14"] if "atr14" in tfs["1D"] else None
    rows: list[dict[str, Any]] = []
    for tf in s["level_timeframes"]:
        bars = tfs[tf]
        piv = find_pivots(
            bars,
            left=int(s["pivot_left"]),
            right=int(s["pivot_right"]),
            price_source=price_source,
        )
        clusters: list[_Cluster] = []
        for _, p in piv.iterrows():
            ts = pd.Timestamp(p["confirm_ts"])
            atr = _atr_asof(daily_atr, ts)
            if not np.isfinite(atr) or atr <= 0:
                continue
            tol = float(s["cluster_atr"]) * atr
            best: _Cluster | None = None
            best_dist = float("inf")
            for c in clusters:
                if (ts - c.updated) > pd.Timedelta(days=int(s["level_expiry_days"])):
                    continue
                dist = abs(float(p["price"]) - c.price)
                if dist <= tol and dist < best_dist:
                    best = c
                    best_dist = dist
            if best is None:
                cid = f"{tf}-{len(clusters)}"
                c = _Cluster(
                    cid=cid,
                    timeframe=tf,
                    price=float(p["price"]),
                    weight=1,
                    created=ts,
                    updated=ts,
                )
                clusters.append(c)
                best = c
            else:
                w = best.weight
                best.price = (best.price * w + float(p["price"])) / (w + 1)
                best.weight = w + 1
                best.updated = ts
            rows.append(
                {
                    "ts": ts,
                    "cid": best.cid,
                    "timeframe": tf,
                    "price": float(best.price),
                    "weight": int(best.weight),
                    "atr_d1": float(atr),
                    "pivot_ts": pd.Timestamp(p["pivot_ts"]),
                    "kind": p["kind"],
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=["ts", "cid", "timeframe", "price", "weight", "atr_d1", "pivot_ts", "kind"]
        )
    out = pd.DataFrame(rows)
    out = out.sort_values(["ts", "timeframe", "pivot_ts", "kind"]).reset_index(drop=True)
    return out


class LevelBook:
    """Causal active-level book with O(log n) neighbour queries."""

    def __init__(self, events: pd.DataFrame, cfg: dict[str, Any]):
        self.s = cfg["strategy"]
        self.expiry_days = int(self.s["level_expiry_days"])
        self.min_touches = int(self.s["min_touches"])
        self.events = events.sort_values(["ts", "timeframe", "pivot_ts", "kind"]).reset_index(drop=True)
        self._ptr = 0
        self._active: dict[str, LevelState] = {}
        self._dirty = True
        self._prices: list[float] = []
        self._cids: list[str] = []
        self.ts = pd.Timestamp("1900-01-01")

    def _rebuild(self) -> None:
        if not self._dirty:
            return
        pairs = sorted((v.price, cid) for cid, v in self._active.items())
        self._prices = [p for p, _ in pairs]
        self._cids = [c for _, c in pairs]
        self._dirty = False

    def advance(self, ts: pd.Timestamp) -> None:
        self.ts = ts
        changed = False
        while self._ptr < len(self.events) and self.events.at[self._ptr, "ts"] <= ts:
            e = self.events.iloc[self._ptr]
            cid = str(e["cid"])
            self._active[cid] = LevelState(
                cid=cid,
                timeframe=str(e["timeframe"]),
                price=float(e["price"]),
                weight=int(e["weight"]),
                created=self._active[cid].created if cid in self._active else pd.Timestamp(e["ts"]),
                updated=pd.Timestamp(e["ts"]),
            )
            self._ptr += 1
            changed = True
        if changed:
            self._dirty = True

    def _eligible(self, v: LevelState) -> bool:
        return v.weight >= self.min_touches and not v.is_expired(self.ts, self.expiry_days)

    def active_levels(self) -> list[LevelState]:
        return [v for v in self._active.values() if self._eligible(v)]

    def near(self, low: float, high: float, pad: float) -> list[LevelState]:
        self._rebuild()
        lo = low - pad
        hi = high + pad
        i = bisect_left(self._prices, lo)
        j = bisect_right(self._prices, hi)
        out = []
        for k in range(i, j):
            v = self._active[self._cids[k]]
            if self._eligible(v):
                out.append(v)
        return out

    def nearest_below(self, price: float) -> LevelState | None:
        self._rebuild()
        i = bisect_left(self._prices, price) - 1
        while i >= 0:
            v = self._active[self._cids[i]]
            if self._eligible(v):
                return v
            i -= 1
        return None

    def nearest_above(self, price: float) -> LevelState | None:
        self._rebuild()
        i = bisect_right(self._prices, price)
        while i < len(self._prices):
            v = self._active[self._cids[i]]
            if self._eligible(v):
                return v
            i += 1
        return None

    def confluence(self, price: float, tol: float) -> int:
        self._rebuild()
        i = bisect_left(self._prices, price - tol)
        j = bisect_right(self._prices, price + tol)
        tfs = set()
        for k in range(i, j):
            v = self._active[self._cids[k]]
            if self._eligible(v):
                tfs.add(v.timeframe)
        return len(tfs)
