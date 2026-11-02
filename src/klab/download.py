"""Data acquisition: HistData M1, Yahoo live bars, and optional Kraken crypto.

Provenance is recorded in `data/raw/download_manifest.json` and
`data/live/download_manifest.json` (URL, retrieval time, SHA-256, size).
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from curl_cffi import requests as ccrequests

from .config import Instrument, resolve
from .io import ensure_dir, sha256_file, utcnow, write_json

LOG = logging.getLogger(__name__)
HISTDATA_PAGE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/{year}"
HISTDATA_POST = "https://www.histdata.com/get.php"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"

_INPUT_RE = re.compile(r'name="(?P<name>[^"]+)"[^>]*value="(?P<value>[^"]*)"')


def _session() -> ccrequests.Session:
    return ccrequests.Session(impersonate="chrome")


def _manifest_path(cfg: dict[str, Any], live: bool = False) -> Path:
    base = resolve(cfg, cfg["data"]["live_dir"] if live else cfg["data"]["raw_dir"])
    return base / "download_manifest.json"


def _update_manifest(cfg: dict[str, Any], record: dict[str, Any], live: bool = False) -> None:
    path = _manifest_path(cfg, live=live)
    records: list[dict[str, Any]] = []
    if path.exists():
        try:
            records = json.loads(path.read_text())
        except Exception:
            records = []
    records = [r for r in records if not (r.get("source") == record.get("source") and r.get("symbol") == record.get("symbol") and r.get("year") == record.get("year") and r.get("interval") == record.get("interval"))]
    records.append(record)
    write_json(path, records)


def _parse_hidden(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in _INPUT_RE.finditer(html):
        fields[m.group("name")] = m.group("value")
    return fields


def download_histdata_year(
    cfg: dict[str, Any],
    symbol: str,
    year: int,
    force: bool = False,
    delay: float = 2.5,
) -> Path:
    inst: Instrument = cfg["instruments"][symbol]
    raw = ensure_dir(resolve(cfg, cfg["data"]["raw_dir"]) / symbol)
    out = raw / f"DAT_ASCII_{inst.histdata_symbol}_M1_{year}.csv"
    if out.exists() and out.stat().st_size > 1_000_000 and not force:
        LOG.info("histdata %s %s: exists", symbol, year)
        return out
    page = HISTDATA_PAGE.format(pair=inst.histdata_symbol.lower(), year=year)
    session = _session()
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = session.get(page, timeout=60)
            if r.status_code != 200 or 'id="file_down"' not in r.text:
                raise RuntimeError(f"page status {r.status_code}, form missing")
            fields = _parse_hidden(r.text)
            required = {"tk", "date", "datemonth", "platform", "timeframe", "fxpair"}
            if not required.issubset(fields):
                raise RuntimeError(f"missing form fields: {required - set(fields)}")
            form = {k: fields[k] for k in required}
            time.sleep(delay)
            resp = session.post(
                HISTDATA_POST,
                data=form,
                headers={"Referer": page, "Origin": "https://www.histdata.com"},
                timeout=180,
            )
            if resp.status_code != 200 or len(resp.content) < 1000:
                raise RuntimeError(f"download status {resp.status_code}, {len(resp.content)} bytes")
            if resp.content[:2] != b"PK":
                raise RuntimeError("response is not a zip archive")
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise RuntimeError("zip has no CSV member")
            data = zf.read(csv_names[0])
            tmp = out.with_suffix(".csv.tmp")
            tmp.write_bytes(data)
            tmp.replace(out)
            zip_dir = ensure_dir(raw / "zips")
            (zip_dir / f"{inst.histdata_symbol}_{year}.zip").write_bytes(resp.content)
            _update_manifest(
                cfg,
                {
                    "source": "histdata",
                    "symbol": symbol,
                    "year": year,
                    "url": page,
                    "retrieved_utc": utcnow(),
                    "sha256": sha256_file(out),
                    "bytes": out.stat().st_size,
                },
            )
            LOG.info("histdata %s %s: %s bytes", symbol, year, out.stat().st_size)
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            LOG.warning("histdata %s %s attempt %d failed: %s", symbol, year, attempt, exc)
            time.sleep(5 * attempt)
    raise RuntimeError(f"histdata {symbol} {year} failed: {last_err}")


def download_histdata(
    cfg: dict[str, Any],
    symbols: Iterable[str] | None = None,
    years: Iterable[int] | None = None,
    force: bool = False,
) -> list[Path]:
    symbols = list(symbols or cfg["instruments"].keys())
    years = list(years or range(int(cfg["data"]["histdata_m1_year_start"]), int(cfg["data"]["histdata_m1_year_end"]) + 1))
    paths: list[Path] = []
    for symbol in symbols:
        for year in years:
            try:
                paths.append(download_histdata_year(cfg, symbol, int(year), force=force))
            except Exception as exc:  # noqa: BLE001
                LOG.error("skip histdata %s %s: %s", symbol, year, exc)
    return paths


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _yahoo_chart(
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    interval: str,
) -> pd.DataFrame:
    params = {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": interval,
        "includePrePost": "false",
        "events": "div,splits",
    }
    r = ccrequests.get(
        YAHOO_URL.format(symbol=symbol),
        params=params,
        impersonate="chrome",
        timeout=45,
    )
    r.raise_for_status()
    payload = r.json()
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(str(chart["error"]))
    result = chart.get("result") or []
    if not result:
        return pd.DataFrame()
    res = result[0]
    ts = res.get("timestamp") or []
    quote = res.get("indicators", {}).get("quote", [{}])[0]
    if not ts or "open" not in quote:
        return pd.DataFrame()
    df = pd.DataFrame(
        {
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
        },
        index=pd.to_datetime(ts, unit="s", utc=True),
    )
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="first")].sort_index()
    if interval.endswith("m") or interval.endswith("h"):
        df = df.between_time("00:00", "23:59")
    return df


def download_yahoo_live(
    cfg: dict[str, Any],
    symbols: Iterable[str] | None = None,
    interval: str | None = None,
    start: str | None = None,
    end: str | None = None,
    force: bool = False,
) -> list[Path]:
    live = cfg["live"]
    symbols = list(symbols or live["yahoo_symbols"])
    interval = interval or live["intraday_interval"]
    start_ts = pd.Timestamp(start or live["start"], tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.now(tz="UTC")
    if interval.endswith("m"):
        # Yahoo enforces a hard ~60-day lookback for intraday bars.  Clamp
        # instead of issuing dozens of guaranteed-422 chunk requests.
        earliest = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=59)
        start_ts = max(start_ts, earliest)
        end_ts = min(end_ts, pd.Timestamp.now(tz="UTC"))
    if start_ts >= end_ts:
        LOG.warning("yahoo %s %s: empty requested range", symbols, interval)
        return []
    outdir = ensure_dir(resolve(cfg, cfg["data"]["live_dir"]) / "yahoo")
    paths: list[Path] = []
    for symbol in symbols:
        out = outdir / f"{_safe(symbol)}_{interval}.parquet"
        if out.exists() and not force:
            LOG.info("yahoo %s %s: exists", symbol, interval)
            paths.append(out)
            continue
        frames: list[pd.DataFrame] = []
        if interval.endswith("m"):
            chunk = pd.Timedelta(days=int(live["intraday_chunk_days"]))
            cur = start_ts
            while cur < end_ts:
                nxt = min(cur + chunk, end_ts)
                try:
                    frames.append(_yahoo_chart(symbol, cur, nxt, interval))
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("yahoo %s %s..%s failed: %s", symbol, cur.date(), nxt.date(), exc)
                cur = nxt
                time.sleep(0.8)
        else:
            frames.append(_yahoo_chart(symbol, start_ts, end_ts, interval))
        frames = [f for f in frames if not f.empty]
        if not frames:
            LOG.error("yahoo %s %s: no data", symbol, interval)
            continue
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        df.to_parquet(out, index=True)
        _update_manifest(
            cfg,
            {
                "source": "yahoo",
                "symbol": symbol,
                "interval": interval,
                "start": str(df.index.min()),
                "end": str(df.index.max()),
                "url": YAHOO_URL.format(symbol=symbol),
                "retrieved_utc": utcnow(),
                "sha256": sha256_file(out),
                "bytes": out.stat().st_size,
                "rows": int(len(df)),
            },
            live=True,
        )
        LOG.info("yahoo %s %s: %d rows", symbol, interval, len(df))
        paths.append(out)
    return paths


def download_kraken_live(
    cfg: dict[str, Any],
    pairs: Iterable[str] | None = None,
    interval: int = 15,
    start: str | None = None,
    force: bool = False,
) -> list[Path]:
    pairs = list(pairs or cfg["live"]["kraken_pairs"])
    start_ts = pd.Timestamp(start or cfg["live"]["start"], tz="UTC")
    since = int(start_ts.timestamp())
    outdir = ensure_dir(resolve(cfg, cfg["data"]["live_dir"]) / "kraken")
    paths: list[Path] = []
    for pair in pairs:
        out = outdir / f"{pair}_{interval}m.parquet"
        if out.exists() and not force:
            LOG.info("kraken %s: exists", pair)
            paths.append(out)
            continue
        rows: list[list[Any]] = []
        cursor = since
        for _ in range(80):
            r = ccrequests.get(
                KRAKEN_URL,
                params={"pair": pair, "interval": interval, "since": cursor},
                impersonate="chrome",
                timeout=30,
            )
            r.raise_for_status()
            payload = r.json()
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            result = payload.get("result", {})
            key = next((k for k in result if k != "last"), None)
            if key is None:
                break
            batch = result[key]
            if not batch:
                break
            rows.extend(batch)
            new_cursor = int(result.get("last", cursor))
            if new_cursor <= cursor or len(batch) < 720:
                break
            cursor = new_cursor
            time.sleep(0.5)
        if not rows:
            LOG.warning("kraken %s: no rows", pair)
            continue
        df = pd.DataFrame(
            rows,
            columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"],
        )
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").drop(columns=["vwap", "count"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"]).sort_index()
        df = df[~df.index.duplicated(keep="first")]
        if len(df) > 1:
            df = df.iloc[:-1]  # last bar is incomplete
        df.to_parquet(out, index=True)
        _update_manifest(
            cfg,
            {
                "source": "kraken",
                "symbol": pair,
                "interval": f"{interval}m",
                "start": str(df.index.min()),
                "end": str(df.index.max()),
                "url": KRAKEN_URL,
                "retrieved_utc": utcnow(),
                "sha256": sha256_file(out),
                "bytes": out.stat().st_size,
                "rows": int(len(df)),
            },
            live=True,
        )
        LOG.info("kraken %s: %d rows", pair, len(df))
        paths.append(out)
    return paths


def download_all(cfg: dict[str, Any], force: bool = False) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    out["histdata"] = [str(p) for p in download_histdata(cfg, force=force)]
    out["yahoo"] = [str(p) for p in download_yahoo_live(cfg, force=force)]
    out["yahoo_daily"] = [str(p) for p in download_yahoo_live(cfg, interval="1d", force=force)]
    try:
        out["kraken"] = [str(p) for p in download_kraken_live(cfg, force=force)]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("kraken optional download failed: %s", exc)
        out["kraken"] = []
    return out
