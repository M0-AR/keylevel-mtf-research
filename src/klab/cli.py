"""Command-line interface for the key-level research pipeline."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .config import load_config, repo_root, resolve, split_dates
from .download import download_all, download_histdata, download_kraken_live, download_yahoo_live
from .io import ensure_dir, hash_files, read_json, utcnow, write_json
from .pipelines import (
    run_benchmarks,
    run_development,
    run_holdout,
    run_placebos,
    run_validation,
    save_stage,
    trade_event_study,
)
from .stats import (
    alpha_beta,
    benjamini_hochberg,
    bootstrap_mean_ci,
    bootstrap_sharpe,
    deflated_sharpe_ratio,
    pbo_cscv,
    reality_check,
    spa_test,
    summary_stats,
)
from .timeframes import build_all

LOG = logging.getLogger(__name__)


def _symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def _years(value: str | None, cfg: dict[str, Any]) -> list[int]:
    if not value:
        return list(range(int(cfg["data"]["histdata_m1_year_start"]), int(cfg["data"]["histdata_m1_year_end"]) + 1))
    out: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def freeze_files(cfg: dict[str, Any]) -> list[Path]:
    root = Path(cfg["_root"])
    files: list[Path] = []
    for pattern in [
        "src/klab/*.py",
        "config/*.yaml",
        "preregistration/*.md",
        "tests/*.py",
        "requirements.txt",
        "pyproject.toml",
        "Dockerfile",
        "docker-compose.yml",
        "Makefile",
    ]:
        files.extend(sorted(root.glob(pattern)))
    return [p for p in files if p.is_file()]


def cmd_download(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    if args.source == "histdata":
        paths = download_histdata(cfg, _symbols(args.symbols), _years(args.years, cfg), force=args.force)
    elif args.source == "yahoo":
        paths = download_yahoo_live(cfg, _symbols(args.symbols), interval=args.interval, start=args.start, end=args.end, force=args.force)
    elif args.source == "kraken":
        paths = download_kraken_live(cfg, pairs=args.symbols.split(",") if args.symbols else None, force=args.force)
    else:
        out = download_all(cfg, force=args.force)
        LOG.info("download summary: %s", {k: len(v) for k, v in out.items()})
        return 0
    LOG.info("downloaded %d files", len(paths))
    return 0


def cmd_freeze(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    files = freeze_files(cfg)
    digest = hash_files(files)
    out = {
        "frozen_utc": utcnow(),
        "hash": digest,
        "files": [str(p.relative_to(Path(cfg["_root"]))) for p in files],
        "n_files": len(files),
        "note": "Holdout may only be opened after this file exists and the hash still matches.",
    }
    path = resolve(cfg, "CODE_FREEZE.json")
    write_json(path, out)
    LOG.info("code frozen: %s (%s files)", digest, len(files))
    return 0


def _check_freeze(cfg: dict[str, Any]) -> dict[str, Any]:
    path = resolve(cfg, "CODE_FREEZE.json")
    if not path.exists():
        raise SystemExit("CODE_FREEZE.json missing; run `klab freeze` before opening the holdout")
    frozen = read_json(path)
    current = hash_files(freeze_files(cfg))
    if current != frozen.get("hash"):
        raise SystemExit(
            "code/config changed since freeze; record a deviation and refreeze BEFORE opening the holdout"
        )
    return frozen


def _inference(cfg: dict[str, Any], holdout: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    base = holdout["base"]
    winner = holdout["winner"]
    start, end = split_dates(cfg, "holdout")
    bench, bench_daily = run_benchmarks(cfg, "holdout")
    buy_hold = bench_daily["buy_and_hold"]
    out["benchmarks"] = bench.to_dict(orient="records")
    out["alpha_beta_base"] = alpha_beta(base.daily, buy_hold)
    out["alpha_beta_winner"] = alpha_beta(winner.daily, buy_hold)
    n_boot = int(cfg["stats"]["n_bootstrap"])
    block = int(cfg["stats"]["block_mean_days"])
    seed = int(cfg["stats"]["seed"])
    r = base.trades["r_net"].to_numpy(dtype=float)
    d = base.daily.to_numpy(dtype=float)
    mr_lo, mr_hi = bootstrap_mean_ci(r, n_boot=n_boot, mean_block=block, seed=seed)
    sr_draws = bootstrap_sharpe(d, n_boot=n_boot, mean_block=block, seed=seed) * np.sqrt(252)
    sr_lo, sr_hi = np.quantile(sr_draws, [0.025, 0.975])
    out["bootstrap"] = {
        "mean_r": {
            "point": float(np.mean(r)),
            "ci_low": float(mr_lo),
            "ci_high": float(mr_hi),
            "n_boot": n_boot,
            "mean_block": block,
            "seed": seed,
        },
        "sharpe": {
            "point": float(np.mean(d) / np.std(d, ddof=1) * np.sqrt(252)),
            "ci_low": float(sr_lo),
            "ci_high": float(sr_hi),
            "n_boot": n_boot,
            "mean_block": block,
            "seed": seed,
        },
    }
    # trial ledger: development grid + ablations
    dev_dir = resolve(cfg, cfg["data"]["results_dir"]) / "development"
    trial_srs: list[float] = []
    grid_daily = None
    try:
        grid = pd.read_csv(dev_dir / "tables" / "grid.csv")
        trial_srs.extend((grid["sharpe"].dropna() / np.sqrt(252)).tolist())
        grid_daily = pd.read_csv(dev_dir / "tables" / "grid_daily.csv", index_col=0, parse_dates=True)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not load development grid for inference: %s", exc)
    try:
        ledger = pd.read_csv(dev_dir / "tables" / "ledger.csv")
        ab = ledger[ledger["kind"] == "ablation"]
        trial_srs.extend((ab["sharpe"].dropna() / np.sqrt(252)).tolist())
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not load development ledger: %s", exc)
    out["dsr_base"] = deflated_sharpe_ratio(base.daily, trial_srs)
    out["dsr_winner"] = deflated_sharpe_ratio(winner.daily, trial_srs)
    out["n_trials"] = len(trial_srs)
    if grid_daily is not None and grid_daily.shape[1] >= 2:
        out["pbo"] = pbo_cscv(grid_daily, n_slices=int(cfg["stats"]["cscv_slices"]))
    else:
        out["pbo"] = {"pbo": float("nan"), "n_splits": 0}
    models = np.column_stack([base.daily.to_numpy(), winner.daily.to_numpy()])
    out["reality_check"] = reality_check(models, buy_hold.to_numpy(), n_boot=int(cfg["stats"]["n_bootstrap"]), mean_block=int(cfg["stats"]["block_mean_days"]), seed=int(cfg["stats"]["seed"]))
    out["spa"] = spa_test(models, buy_hold.to_numpy(), n_boot=int(cfg["stats"]["n_bootstrap"]), mean_block=int(cfg["stats"]["block_mean_days"]), seed=int(cfg["stats"]["seed"]))
    # per-instrument one-sided t-tests, BH-adjusted
    pvals = []
    labels = []
    for symbol, g in base.trades.groupby("symbol"):
        r = g["r_net"].to_numpy(dtype=float)
        if len(r) < 5:
            continue
        from scipy import stats as st
        t, p_two = st.ttest_1samp(r, 0.0)
        p_one = p_two / 2.0 if t > 0 else 1.0 - p_two / 2.0
        pvals.append(float(p_one))
        labels.append(symbol)
    bh = benjamini_hochberg(pvals, alpha=float(cfg["stats"]["alpha"]))
    out["per_instrument_bh"] = [
        {"symbol": s, "p_one_sided": p, "q": q, "reject": bool(r)}
        for s, p, q, r in zip(labels, pvals, bh["qvalues"], bh["reject"])
    ]
    LOG.info("running entry and level placebos")
    out["placebos"] = run_placebos(cfg, base)
    return out


def cmd_run(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    symbols = _symbols(args.symbols)
    if args.stage == "development":
        payload = run_development(cfg, symbols)
        groups, reg = trade_event_study(payload["base"].trades)
        payload["event_groups"] = groups
        payload["event_regression"] = reg
        save_stage(cfg, "development", payload)
        write_json(resolve(cfg, cfg["data"]["results_dir"]) / "development" / "winner.json", {"params": payload["winner"]})
        return 0
    if args.stage == "validation":
        wpath = resolve(cfg, cfg["data"]["results_dir"]) / "development" / "winner.json"
        if not wpath.exists():
            raise SystemExit("development winner missing; run development first")
        winner = read_json(wpath)["params"]
        payload = run_validation(cfg, winner, symbols)
        groups, reg = trade_event_study(payload["winner"].trades)
        payload["event_groups"] = groups
        payload["event_regression"] = reg
        save_stage(cfg, "validation", payload)
        return 0
    if args.stage == "holdout":
        _check_freeze(cfg)
        wpath = resolve(cfg, cfg["data"]["results_dir"]) / "development" / "winner.json"
        if not wpath.exists():
            raise SystemExit("development winner missing")
        winner = read_json(wpath)["params"]
        payload = run_holdout(cfg, winner, symbols)
        groups, reg = trade_event_study(payload["base"].trades)
        payload["event_groups"] = groups
        payload["event_regression"] = reg
        bench, bench_daily = run_benchmarks(cfg, "holdout", symbols)
        payload["benchmarks"] = bench
        payload["bench_daily"] = bench_daily
        inference = _inference(cfg, payload)
        payload["inference"] = inference
        save_stage(cfg, "holdout", payload)
        write_json(resolve(cfg, cfg["data"]["results_dir"]) / "holdout" / "inference.json", inference)
        return 0
    raise SystemExit(f"unknown stage {args.stage}")


def cmd_report(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    from .report import build_report

    path = build_report(cfg)
    LOG.info("wrote %s", path)
    return 0


def cmd_audit(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    from .audit import run_audit

    out = run_audit(
        cfg,
        symbols=_symbols(args.symbols),
        force_yahoo=args.force_yahoo,
    )
    LOG.info("audit: %d integrity rows, %d reaction groups", len(out["integrity"]), len(out["summary"]))
    return 0


def cmd_live(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    from .live import run_live_shadow

    out = run_live_shadow(cfg, symbols=_symbols(args.symbols), force=args.force)
    LOG.info("live shadow: %s", out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="klab", description="Multi-timeframe key-level research pipeline")
    p.add_argument("--config", default=None, help="path to config/base.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="download HistData/Yahoo/Kraken data")
    d.add_argument("--source", choices=["histdata", "yahoo", "kraken", "all"], default="histdata")
    d.add_argument("--symbols", default=None)
    d.add_argument("--years", default=None)
    d.add_argument("--interval", default=None)
    d.add_argument("--start", default=None)
    d.add_argument("--end", default=None)
    d.add_argument("--force", action="store_true")
    d.set_defaults(func=cmd_download)

    b = sub.add_parser("build", help="resample M1 into 15m/4h/1D/1W/1M")
    b.add_argument("--force", action="store_true")
    b.set_defaults(func=lambda cfg, a: (build_all(cfg, force=a.force), 0)[1])

    r = sub.add_parser("run", help="run a research stage")
    r.add_argument("--stage", choices=["development", "validation", "holdout"], required=True)
    r.add_argument("--symbols", default=None)
    r.set_defaults(func=cmd_run)

    f = sub.add_parser("freeze", help="hash code/config before opening the holdout")
    f.set_defaults(func=cmd_freeze)

    rep = sub.add_parser("report", help="generate README/paper artifacts from results")
    rep.set_defaults(func=cmd_report)

    au = sub.add_parser("audit", help="data integrity, vendor cross-check, level reactions")
    au.add_argument("--symbols", default=None)
    au.add_argument("--force-yahoo", action="store_true")
    au.set_defaults(func=cmd_audit)

    lv = sub.add_parser("live", help="run the 2026 live shadow")
    lv.add_argument("--symbols", default=None)
    lv.add_argument("--force", action="store_true")
    lv.set_defaults(func=cmd_live)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_config(args.config)
    return int(args.func(cfg, args) or 0)


if __name__ == "__main__":
    sys.exit(main())
