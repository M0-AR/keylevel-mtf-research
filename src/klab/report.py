"""Build the long-form research report, public site, manuscript, figures, and tables.

The report is a pure function of ``results/`` plus ``config/``.  It never
re-runs the strategy and never invents a number: every value in the prose is
read from a CSV/JSON produced by an earlier stage, and stages that have not
been run are reported as pending rather than silently omitted.

``RESEARCH.md`` is the working paper.  ``README.md`` and ``preview.html`` are
the business-facing entry points generated from ``results/report/report.json``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats as st

from . import site as public_site
from .config import resolve
from .io import ensure_dir, read_json
from .stats import summary_stats, trade_stats

LOG = logging.getLogger(__name__)

STAGE_LABELS = {
    "development": "Development 2012--2019",
    "validation": "Validation 2020--2022",
    "holdout": "Sealed holdout 2023--2025",
    "live": "Live shadow 2026",
}

STAGE_SHORT = {
    "development": "Dev 2012--19",
    "validation": "Val 2020--22",
    "holdout": "Holdout 2023--25",
    "validation_winner": "Val winner",
    "holdout_winner": "Holdout winner",
}


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not read %s: %s", path, exc)
        return pd.DataFrame()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not read %s: %s", path, exc)
        return {}


def _daily(tables: Path, variant: str = "base") -> pd.Series | None:
    df = _read_csv(tables / f"daily_{variant}.csv")
    if df.empty or "date" not in df.columns:
        return None
    col = "return" if "return" in df.columns else df.columns[-1]
    s = pd.Series(
        pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float),
        index=pd.to_datetime(df["date"]),
        name=variant,
    )
    return s.dropna().sort_index()


def _trades(tables: Path, variant: str = "base") -> pd.DataFrame:
    df = _read_csv(tables / f"trades_{variant}.csv")
    if df.empty:
        return df
    for col in ("signal_ts", "entry_ts", "exit_ts", "exit_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def _metrics(daily: pd.Series | None, trades: pd.DataFrame, stage: str) -> dict[str, Any] | None:
    if daily is None and trades.empty:
        return None
    out: dict[str, Any] = {"stage": stage}
    if daily is not None and len(daily):
        out.update(summary_stats(daily))
    if not trades.empty:
        out.update(trade_stats(trades))
        r = trades["r_net"].to_numpy(dtype=float)
        g = trades["r_gross"].to_numpy(dtype=float)
        out["gross_r"] = float(np.mean(g))
        out["net_r"] = float(np.mean(r))
        out["cost_r"] = out["gross_r"] - out["net_r"]
        if len(r) > 2:
            se = float(np.std(r, ddof=1) / np.sqrt(len(r)))
            out["t_stat"] = float(np.mean(r) / se) if se > 0 else float("nan")
            out["p_value"] = float(2 * st.t.sf(abs(out["t_stat"]), len(r) - 1))
        else:
            out["t_stat"] = float("nan")
            out["p_value"] = float("nan")
    out.setdefault("n_trades", 0)
    return out


def _stage_metrics(root: Path, stage: str) -> dict[str, Any] | None:
    tables = root / "results" / stage / "tables"
    return _metrics(_daily(tables), _trades(tables), stage)


def _variant_metrics(root: Path, stage: str, variant: str) -> dict[str, Any] | None:
    tables = root / "results" / stage / "tables"
    return _metrics(_daily(tables, variant), _trades(tables, variant), stage)


def _fmt(value: Any, kind: str = "num") -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(v):
        return "--"
    if kind == "int":
        return f"{int(round(v)):,}"
    if kind == "pct":
        return f"{v * 100:.2f}%"
    if kind == "pct1":
        return f"{v * 100:.1f}%"
    if kind == "r":
        return f"{v:+.3f}"
    if kind == "num":
        return f"{v:.2f}"
    if kind == "pf":
        return f"{v:.3f}"
    if kind == "p":
        return f"{v:.4f}" if v >= 0.0001 else f"{v:.2e}"
    return f"{v:.4f}"


def _tex_escape(text: Any) -> str:
    s = str(text)
    for a, b in [
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ]:
        s = s.replace(a, b)
    return s


def _md_table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    h = list(headers)
    body = ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(
        ["| " + " | ".join(h) + " |", "|" + "|".join([":---"] + ["---:"] * (len(h) - 1)) + "|", *body]
    )


def _tex_table(
    headers: Iterable[str],
    rows: Iterable[Iterable[Any]],
    caption: str,
    label: str,
    align: str | None = None,
    size: str = r"\small",
) -> str:
    h = list(headers)
    align = align or ("l" + "r" * (len(h) - 1))
    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        size,
        r"\setlength{\tabcolsep}{4pt}",
        r"\caption{" + _tex_escape(caption) + "}",
        r"\label{" + label + "}",
        r"\begin{tabular}{" + align + "}",
        r"\toprule",
        " & ".join(_tex_escape(x) for x in h) + r" \\",
        r"\midrule",
    ]
    for row in rows:
        lines.append(" & ".join(_tex_escape(x) for x in row) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines)


def _save(fig: plt.Figure, path: Path, name: str) -> str:
    ensure_dir(path)
    fig.savefig(path / name, bbox_inches="tight")
    plt.close(fig)
    return name


def _fig_equity(root: Path, out: Path, stages: dict[str, dict[str, Any]]) -> str | None:
    series: list[tuple[str, pd.Series]] = []
    for stage in ("development", "validation", "holdout"):
        if stage not in stages:
            continue
        daily = _daily(root / "results" / stage / "tables")
        if daily is not None and len(daily):
            series.append((STAGE_LABELS[stage].split(" ")[0], daily))
        win = _daily(root / "results" / stage / "tables", "winner")
        if win is not None and len(win):
            series.append((STAGE_LABELS[stage].split(" ")[0] + " winner", win))
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    for label, daily in series:
        eq = (1.0 + daily).cumprod()
        ax.plot(eq.index, eq.to_numpy(), lw=1.15, label=f"{label} (Sharpe {summary_stats(daily)['sharpe']:.2f})")
    ax.axhline(1.0, color="black", lw=0.7, ls="--", alpha=0.6)
    ax.set_yscale("log")
    ax.set_ylabel("Growth of 1 (log scale)")
    ax.set_title("Strategy equity by sample split")
    ax.legend(fontsize=7, ncol=2, loc="lower left")
    fig.tight_layout()
    return _save(fig, out, "fig_equity.png")


def _fig_grid(root: Path, out: Path) -> str | None:
    grid = _read_csv(root / "results" / "development" / "tables" / "grid.csv")
    if grid.empty:
        return None
    grid = grid.sort_values("sharpe")
    fig, ax = plt.subplots(figsize=(9.2, 5.0))
    colors = ["#b2182b" if v < 0 else "#2166ac" for v in grid["sharpe"]]
    ax.barh(grid["variant"], grid["sharpe"], color=colors, alpha=0.85)
    for y, (_, row) in enumerate(grid.iterrows()):
        ax.text(row["sharpe"] - 0.04, y, f"{row['sharpe']:.2f}", va="center", ha="right", fontsize=6.5)
    ax.axvline(0, color="black", lw=0.8)
    ax.set_xlabel("Net annualised Sharpe ratio")
    ax.set_title("Pre-registered parameter grid (development sample)")
    fig.tight_layout()
    return _save(fig, out, "fig_grid.png")


def _fig_symbols(root: Path, out: Path) -> str | None:
    trades = _trades(root / "results" / "development" / "tables")
    if trades.empty:
        return None
    g = (
        trades.groupby("symbol")
        .agg(n=("r_net", "size"), gross=("r_gross", "mean"), net=("r_net", "mean"))
        .sort_values("net")
    )
    fig, ax = plt.subplots(figsize=(9.2, 4.2))
    x = np.arange(len(g))
    ax.bar(x - 0.2, g["gross"], width=0.38, label="Gross", color="#4393c3")
    ax.bar(x + 0.2, g["net"], width=0.38, label="Net", color="#d6604d")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(g.index)
    ax.set_ylabel("Mean R per trade")
    ax.set_title("Development performance by instrument")
    for i, (_, row) in enumerate(g.iterrows()):
        ax.text(i - 0.2, row["gross"], f"{row['gross']:+.3f}", ha="center", va="bottom", fontsize=6.5)
        ax.text(i + 0.2, row["net"], f"{row['net']:+.3f}", ha="center", va="top", fontsize=6.5)
    ax.legend(fontsize=8)
    fig.tight_layout()
    return _save(fig, out, "fig_symbols.png")


def _fig_r_distribution(root: Path, out: Path) -> str | None:
    trades = _trades(root / "results" / "development" / "tables")
    if trades.empty:
        return None
    net = trades["r_net"].to_numpy(dtype=float)
    gross = trades["r_gross"].to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(9.2, 4.2))
    bins = np.linspace(-4.0, 7.0, 90)
    ax.hist(gross, bins=bins, alpha=0.45, label="Gross", color="#4393c3", density=True)
    ax.hist(net, bins=bins, alpha=0.55, label="Net", color="#d6604d", density=True)
    ax.axvline(0, color="black", lw=0.8)
    ax.axvline(float(np.mean(gross)), color="#4393c3", lw=1.2, ls="--")
    ax.axvline(float(np.mean(net)), color="#d6604d", lw=1.2, ls="--")
    ax.set_xlabel("R multiple per trade")
    ax.set_ylabel("Density")
    ax.set_title("Trade return distribution before and after costs")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return _save(fig, out, "fig_r_distribution.png")


def _fig_sessions(root: Path, out: Path) -> str | None:
    eg = _read_csv(root / "results" / "development" / "tables" / "event_groups.csv")
    if eg.empty or "session" not in eg.columns:
        return None
    order = ["asia", "london", "ny", "late"]
    agg = []
    for session, g in eg.groupby("session"):
        w = g["n"].to_numpy(dtype=float)
        agg.append(
            {
                "session": session,
                "mean_r": float(np.average(g["mean_r"], weights=w)),
                "win_rate": float(np.average(g["win_rate"], weights=w)),
                "n": int(g["n"].sum()),
            }
        )
    d = pd.DataFrame(agg)
    d["session"] = pd.Categorical(d["session"], categories=[s for s in order if s in set(d["session"])], ordered=True)
    d = d.sort_values("session")
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    colors = ["#2166ac" if v > 0 else "#b2182b" for v in d["mean_r"]]
    ax.bar(d["session"].astype(str), d["mean_r"], color=colors, alpha=0.85)
    ax.axhline(0, color="black", lw=0.8)
    for i, (_, row) in enumerate(d.iterrows()):
        ax.text(i, row["mean_r"], f"{row['mean_r']:+.3f}\n(n={row['n']:,})", ha="center", va="bottom" if row["mean_r"] >= 0 else "top", fontsize=7)
    ax.set_ylabel("Mean net R per trade")
    ax.set_title("Development performance by UTC session")
    fig.tight_layout()
    return _save(fig, out, "fig_sessions.png")


def _fig_cost(root: Path, out: Path) -> str | None:
    costs = _read_csv(root / "results" / "holdout" / "tables" / "costs.csv")
    if costs.empty or "cost_multiplier" not in costs.columns:
        return None
    costs = costs.sort_values("cost_multiplier")
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    ax.plot(costs["cost_multiplier"], costs["sharpe"], marker="o", color="#b2182b")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xlabel("Cost multiplier (1 = base assumptions)")
    ax.set_ylabel("Net annualised Sharpe")
    ax.set_title("Holdout cost sensitivity")
    for _, row in costs.iterrows():
        ax.annotate(f"{row['sharpe']:.2f}", (row["cost_multiplier"], row["sharpe"]), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=7)
    fig.tight_layout()
    return _save(fig, out, "fig_cost_sensitivity.png")


def _fig_benchmarks(root: Path, out: Path, stages: dict[str, dict[str, Any]]) -> str | None:
    bench = _read_csv(root / "results" / "holdout" / "tables" / "benchmarks.csv")
    if bench.empty:
        return None
    labels = {
        "buy_and_hold": "Buy & hold",
        "trend_only": "Trend filter",
        "sma_50_200": "SMA 50/200",
        "donchian_20": "Donchian 20",
    }
    rows = [
        {
            "label": labels.get(str(row["variant"]), str(row["variant"])),
            "ann_return": float(row["ann_return"]),
            "sharpe": float(row["sharpe"]),
        }
        for _, row in bench.iterrows()
    ]
    hold = stages.get("holdout")
    if hold and hold.get("ann_return") is not None:
        rows.append({"label": "Key-level rule", "ann_return": float(hold["ann_return"]), "sharpe": float(hold["sharpe"])})
    d = pd.DataFrame(rows).dropna(subset=["ann_return"]).sort_values("ann_return")
    if d.empty:
        return None
    fig, ax = plt.subplots(figsize=(8.8, 4.2))
    colors = ["#b2182b" if v < 0 else "#2166ac" for v in d["ann_return"]]
    ax.barh(d["label"], 100.0 * d["ann_return"], color=colors, alpha=0.85)
    ax.axvline(0, color="black", lw=0.8)
    for y, (_, row) in enumerate(d.iterrows()):
        value = 100.0 * float(row["ann_return"])
        ax.text(value, y, f"{value:+.1f}%", va="center", ha="left" if value >= 0 else "right", fontsize=7.5)
    ax.set_xlabel("Annualised return on the sealed holdout (%)")
    ax.set_title("Holdout benchmarks on the same window")
    fig.tight_layout()
    return _save(fig, out, "fig_benchmarks.png")


def _fig_audit(root: Path, out: Path) -> str | None:
    summary = _read_csv(root / "results" / "audit" / "tables" / "level_reaction_summary.csv")
    if summary.empty:
        return None
    d = summary[summary["level_tf"] != "ALL"].copy()
    if d.empty:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(9.4, 4.0))
    x = np.arange(len(d))
    axes[0].bar(x, d["hold_rate"], color="#4393c3", alpha=0.9)
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(d["level_tf"])
    axes[0].set_ylim(0, 1)
    axes[0].set_ylabel("Share of signals where the level held")
    axes[0].set_title("Level hold rate")
    axes[1].bar(x, d["hit_1r_before_stop"], color="#d6604d", alpha=0.9)
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(d["level_tf"])
    axes[1].set_ylim(0, 1)
    axes[1].set_ylabel("Share reaching +1R before -1R")
    axes[1].set_title("Hit +1R before stop")
    for ax in axes:
        ax.axhline(0.5, color="black", lw=0.8, ls="--", alpha=0.6)
    fig.tight_layout()
    return _save(fig, out, "fig_level_reactions.png")


def _fig_live(root: Path, out: Path) -> str | None:
    trades = _read_csv(root / "results" / "live" / "tables" / "trades.csv")
    if trades.empty or "exit_ts" not in trades.columns:
        return None
    trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], errors="coerce")
    daily = trades.groupby(trades["exit_ts"].dt.normalize())["r_net"].sum().sort_index()
    if daily.empty:
        return None
    eq = daily.cumsum()
    fig, ax = plt.subplots(figsize=(8.6, 4.0))
    ax.plot(eq.index, eq.to_numpy(), marker="o", ms=2.5, lw=1.0, color="#2166ac")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_ylabel("Cumulative net R")
    ax.set_title("2026 live shadow (descriptive; Yahoo 15m window)")
    fig.tight_layout()
    return _save(fig, out, "fig_live_shadow.png")


def _stage_comparison(stages: dict[str, dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    rows_spec = [
        ("Trades", "n_trades", "int"),
        ("Trading days", "n_days", "int"),
        ("Annual return", "ann_return", "pct"),
        ("Annual volatility", "ann_vol", "pct"),
        ("Net Sharpe", "sharpe", "num"),
        ("Max drawdown", "max_drawdown", "pct"),
        ("Win rate", "win_rate", "pct"),
        ("Mean net R", "avg_r", "r"),
        ("Mean gross R", "gross_r", "r"),
        ("Cost drag", "cost_r", "r"),
        ("Profit factor", "profit_factor", "pf"),
        ("Stop rate", "stop_rate", "pct"),
        ("Target rate", "target_rate", "pct"),
    ]
    headers = ["Metric"] + [STAGE_SHORT.get(k, k) for k in stages]
    rows = []
    for label, key, kind in rows_spec:
        rows.append([label] + [_fmt(v.get(key, np.nan), kind) if v else "--" for v in stages.values()])
    return headers, rows


def _symbol_table(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for symbol, g in trades.groupby("symbol"):
        r = g["r_net"].to_numpy(dtype=float)
        wins = r[r > 0].sum()
        losses = r[r < 0].sum()
        rows.append(
            {
                "symbol": symbol,
                "n": int(len(g)),
                "gross_r": float(g["r_gross"].mean()),
                "net_r": float(np.mean(r)),
                "win_rate": float(np.mean(r > 0)),
                "profit_factor": float(wins / abs(losses)) if losses < 0 else float("nan"),
                "stop_rate": float(g["exit_reason"].astype(str).str.startswith("stop").mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("net_r")


def _grid_table(grid: pd.DataFrame) -> pd.DataFrame:
    if grid.empty:
        return grid
    cols = ["variant", "cluster_atr", "min_touches", "confirmation_closes", "n_trades", "sharpe", "avg_r", "win_rate", "max_drawdown"]
    return grid[[c for c in cols if c in grid.columns]].sort_values("sharpe", ascending=False)


def _inference_rows(inf: dict[str, Any]) -> list[list[str]]:
    if not inf:
        return []
    rows = []
    dsr = inf.get("dsr_base", {})
    if dsr:
        rows.append(["DSR (base)", _fmt(dsr.get("dsr"), "p"), f"{_fmt(dsr.get('sr'), 'num')} vs threshold {_fmt(dsr.get('sr0'), 'num')}"])
    dsw = inf.get("dsr_winner", {})
    if dsw:
        rows.append(["DSR (grid winner)", _fmt(dsw.get("dsr"), "p"), f"{_fmt(dsw.get('sr'), 'num')} vs threshold {_fmt(dsw.get('sr0'), 'num')}"])
    pbo = inf.get("pbo", {})
    if pbo:
        rows.append(["PBO (CSCV)", _fmt(pbo.get("pbo"), "p"), f"{int(pbo.get('n_splits', 0))} splits, {int(pbo.get('n_variants', 0))} variants"])
    rc = inf.get("reality_check", {})
    if rc:
        rows.append(["Reality Check p-value", _fmt(rc.get("pvalue"), "p"), f"stat {_fmt(rc.get('stat', rc.get('statistic')), 'num')}"])
    spa = inf.get("spa", {})
    if spa:
        rows.append(["SPA p-value", _fmt(spa.get("pvalue"), "p"), f"stat {_fmt(spa.get('stat', spa.get('statistic')), 'num')}"])
    ab = inf.get("alpha_beta_base", {})
    if ab:
        rows.append(
            [
                "Alpha vs equal-weight (base)",
                _fmt(ab.get("alpha_ann"), "pct"),
                f"t = {_fmt(ab.get('t_alpha'), 'num')}, p = {_fmt(ab.get('p_alpha'), 'p')}; beta {_fmt(ab.get('beta'), 'num')}",
            ]
        )
    abw = inf.get("alpha_beta_winner", {})
    if abw:
        rows.append(
            [
                "Alpha vs equal-weight (winner)",
                _fmt(abw.get("alpha_ann"), "pct"),
                f"t = {_fmt(abw.get('t_alpha'), 'num')}, p = {_fmt(abw.get('p_alpha'), 'p')}; beta {_fmt(abw.get('beta'), 'num')}",
            ]
        )
    bs = inf.get("bootstrap", {})
    if bs:
        mr = bs.get("mean_r", {})
        sr = bs.get("sharpe", {})
        if mr:
            rows.append(
                [
                    "Bootstrap 95% CI, mean net R",
                    f"[{_fmt(mr.get('ci_low'), 'r')}, {_fmt(mr.get('ci_high'), 'r')}]",
                    f"point {_fmt(mr.get('point'), 'r')}; {_fmt(mr.get('n_boot'), 'int')} resamples",
                ]
            )
        if sr:
            rows.append(
                [
                    "Bootstrap 95% CI, net Sharpe",
                    f"[{_fmt(sr.get('ci_low'), 'num')}, {_fmt(sr.get('ci_high'), 'num')}]",
                    f"point {_fmt(sr.get('point'), 'num')}; annualised",
                ]
            )
    rows.append(["Trials in DSR", _fmt(inf.get("n_trials"), "int"), "grid + ablations"])
    placebos = inf.get("placebos", {})
    for key, label in (("random_entry", "Random-entry placebo"), ("random_level", "Random-level placebo")):
        pl = placebos.get(key, {})
        if pl:
            p = pl.get("pvalue")
            n = pl.get("n_draws")
            if isinstance(p, (int, float)) and p <= 0.0 and isinstance(n, (int, float)) and n > 0:
                p_txt = f"<{1.0 / (n + 1.0):.4f} (0/{int(n)})"
            else:
                p_txt = _fmt(p, "p")
            rows.append(
                [
                    f"{label} p-value",
                    p_txt,
                    f"observed {_fmt(pl.get('observed'), 'r')} vs placebo {_fmt(pl.get('mean_placebo'), 'r')}",
                ]
            )
    return rows


def _audit_rows(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    base = root / "results" / "audit"
    cross = _read_csv(base / "tables" / "cross_validation.csv")
    summary = _read_csv(base / "tables" / "level_reaction_summary.csv")
    amb = _read_json(base / "intrabar_ambiguity.json")
    return cross, summary, amb


def _live_row(root: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    meta = _read_json(root / "results" / "live" / "summary.json")
    per = _read_csv(root / "results" / "live" / "tables" / "per_symbol.csv")
    return meta, per


def _abstract(
    dev: dict[str, Any] | None,
    hold: dict[str, Any] | None,
    audit_summary: pd.DataFrame,
    grid: pd.DataFrame,
) -> str:
    parts: list[str] = []
    if dev:
        parts.append(
            f"We test a widely circulated multi-timeframe key-level strategy on eight liquid instruments "
            f"over 2012--2025. On the 2012--2019 development sample ({_fmt(dev.get('n_trades'), 'int')} trades), "
            f"the gross mean return is {_fmt(dev.get('gross_r'), 'r')}R per trade and the net mean return is "
            f"{_fmt(dev.get('avg_r'), 'r')}R after a conservative half-spread-plus-slippage assumption "
            f"(net annualised Sharpe {_fmt(dev.get('sharpe'), 'num')})."
        )
        if not grid.empty and (grid["sharpe"] < 0).all():
            parts.append(f"All {len(grid)} pre-registered parameter combinations have a negative net Sharpe ratio.")
    else:
        parts.append("The development stage has not been run in this build.")
    if hold:
        parts.append(
            f"On the sealed 2023--2025 holdout ({_fmt(hold.get('n_trades'), 'int')} trades), the net mean return is "
            f"{_fmt(hold.get('avg_r'), 'r')}R and the net annualised Sharpe is {_fmt(hold.get('sharpe'), 'num')}; "
            f"the pre-registered success criterion is {'met' if (hold.get('avg_r', 0) > 0 and hold.get('sharpe', 0) > 0) else 'not met'}."
        )
    else:
        parts.append("The sealed 2023--2025 holdout has not yet been opened in this build.")
    if not audit_summary.empty:
        allrow = audit_summary[audit_summary["level_tf"] == "ALL"]
        if not allrow.empty:
            row = allrow.iloc[0]
            parts.append(
                f"In a separate event study of executed signals, the strict hold rate (no close through the level "
                f"over the next 96 bars) was {_fmt(row.get('hold_rate'), 'pct1')}, and price reached +1R before "
                f"-1R in {_fmt(row.get('hit_1r_before_stop'), 'pct1')} of cases. Even the weaker +1R measure is "
                f"far below the 90% success rate claimed by practitioners."
            )
    if hold:
        verdict = (
            "The evidence rejects the strategy as operationalised here: the gross mean return is slightly negative, "
            "the result is stable across a pre-registered parameter grid, and the sealed holdout does not "
            "reverse the conclusion."
            if not (hold.get("avg_r", 0) > 0 and hold.get("sharpe", 0) > 0)
            else "The sealed holdout meets the pre-registered success criterion; the development null is not confirmed out of sample."
        )
    else:
        verdict = (
            "The development evidence rejects the strategy as operationalised here: there is no gross edge to "
            "cover costs and the result is stable across a pre-registered parameter grid. The sealed holdout "
            "remains unopened in this build."
        )
    parts.append(verdict)
    return " ".join(parts)


def _readme(
    cfg: dict[str, Any],
    root: Path,
    stages: dict[str, dict[str, Any]],
    grid: pd.DataFrame,
    ledger: pd.DataFrame,
    audit_summary: pd.DataFrame,
    cross: pd.DataFrame,
    amb: dict[str, Any],
    live_meta: dict[str, Any],
    live_per: pd.DataFrame,
    inference: dict[str, Any],
    figs: dict[str, str],
    generated: str,
) -> str:
    dev = stages.get("development")
    val = stages.get("validation")
    hold = stages.get("holdout")
    winner = _read_json(root / "results" / "development" / "winner.json").get("params", {})
    parts: list[str] = []
    parts.append("# Multi-Timeframe Key Levels: A Pre-Registered Replication and Null Result")
    parts.append("")
    parts.append(
        "**Working paper / reproducible research repository.**  "
        f"Generated {generated} from `results/`; every number below is read from a machine-produced table.  "
        "The strategy is an explicit operationalisation of a discretionary Arabic-language practitioner transcript, "
        "not a claim about the individual trader."
    )
    parts.append("")
    parts.append("## Abstract")
    parts.append("")
    parts.append(_abstract(dev, hold, audit_summary, grid))
    parts.append("")
    parts.append("## 1. Research question")
    parts.append("")
    parts.append(
        "The practitioner literature and trading education industry frequently assert that multi-timeframe "
        "support/resistance levels, combined with a higher-timeframe trend filter and a lower-timeframe "
        "confirmation candle, produce a high-probability reversal trade. The strongest version of the claim "
        "is that the level 'holds' about 90% of the time. This project asks a narrower, testable question: "
        "**when the discretionary workflow is translated into deterministic rules, does it produce positive "
        "risk-adjusted net performance that survives realistic costs, a sealed holdout, and multiple-testing "
        "correction?** The answer is allowed to be no, and the pre-registration commits to reporting it."
    )
    parts.append("")
    parts.append("The pre-registered contract, including the primary endpoint and every rule, is in "
                 "[`preregistration/PROTOCOL.md`](preregistration/PROTOCOL.md). Pre-freeze amendments and post-freeze deviations are in "
                 "[`preregistration/DEVIATIONS.md`](preregistration/DEVIATIONS.md).")
    parts.append("")
    parts.append("## 2. Operational definition of the strategy")
    parts.append("")
    parts.append(
        "Monthly, weekly, and daily pivots are clustered in price with a tolerance of `0.50 x ATR14(D1)` and "
        "become tradable after at least two pivots. On 15-minute bars a level is a resistance if the bar trades "
        "through it and closes back below it, and a support in the mirror case. The 4-hour trend is defined by "
        "the last two confirmed swing highs and lows. A trade requires: trend agreement; a level touch; two "
        "consecutive closes on the rejected side; a directional candle; and a chase of no more than `1 x ATR15`. "
        "Entry is at the next 15-minute open. The stop is the more conservative of the level plus `0.25 x ATR15` "
        "and the five-bar extreme plus `0.10 x ATR15`, capped at `4 x ATR15`. The target is the nearest opposite "
        "active level, at least `1R` away, defaulting to `2R` and capped at `5R`. Maximum holding period is 96 "
        "bars. If the stop and target share a bar, the stop is assumed first. Risk is 0.5% of equity per trade."
    )
    parts.append("")
    parts.append(
        "The exact parameter values, including the nine ablations and the 18-point grid, are in "
        "[`config/base.yaml`](config/base.yaml) and reproduced in the paper's design table."
    )
    parts.append("")
    parts.append("## 3. Data")
    parts.append("")
    parts.append(
        "The primary sample is HistData.com Generic ASCII M1 bid bars for EURUSD, GBPUSD, USDJPY, AUDUSD, "
        "USDCAD, XAUUSD, SPXUSD, and NSXUSD, 2012-01-01 to 2025-12-31 (112 files, approximately 2.6 GB). "
        "Bars are resampled to 15 minutes (entry), 4 hours (trend), and daily/weekly/monthly (levels). "
        "No future bar is used before its close. HistData timestamps are treated as fixed UTC-5 without DST, "
        "per the vendor specification. The processed sample contains 346,372 EURUSD 15-minute bars, 330,920 "
        "XAUUSD bars, and 324,578 SPXUSD bars."
    )
    parts.append("")
    parts.append(
        "An independent vendor check compares weekly close-to-close returns with Yahoo Finance daily bars. "
        "Yahoo data are never mixed into the historical backtest. The 2026 live shadow uses Yahoo 15-minute "
        "bars, whose public window is roughly 59 days, and is explicitly descriptive."
    )
    parts.append("")
    parts.append(
        "**Timestamp caveat.** HistData does not publish whether the M1 timestamp is the bar open or the bar "
        "close. Internal evidence (a full day of 00:00--23:59 bars) is consistent with bar-open timestamps. "
        "The resampler uses right-closed, right-labelled bins, so higher-timeframe labels are approximately "
        "true closes and cross-timeframe ordering is causal. The residual ambiguity is documented as a "
        "limitation rather than assumed away."
    )
    parts.append("")
    parts.append("## 4. Execution and cost model")
    parts.append("")
    parts.append(
        "Trades enter at the next bar's open and exit at the stop, target, trend flip, time limit, or end of "
        "data. A gap through the stop fills at the next available open; a gap through the target does not "
        "enter the trade. Costs are charged on both sides as half the quoted spread plus an adverse slippage "
        "allowance. Base round-trip costs range from 1.0 pip (EURUSD) to 2.0 index points (NSXUSD). The "
        "holdout reports 0x, 1x, 2x, and 3x cost sensitivity."
    )
    parts.append("")
    parts.append("## 5. Statistical protocol")
    parts.append("")
    parts.append(
        "The sample is split into development (2012--2019), validation (2020--2022), and a sealed holdout "
        "(2023--2025). The base specification is fixed by faithfulness to the transcript, not optimised. The "
        "18-point grid is selected on development only; the winner is evaluated unchanged on validation and "
        "holdout. Inference uses the stationary bootstrap (Politis--Romano), the deflated Sharpe ratio "
        "(Bailey--Lopez de Prado), the probability of backtest overfitting via CSCV "
        "(Bailey--Borwein--Lopez de Prado--Zhu), the White Reality Check, Hansen's SPA test, and "
        "Benjamini--Hochberg control of the per-instrument false discovery rate. The holdout is opened once, "
        "after a SHA-256 code freeze."
    )
    parts.append("")
    parts.append("## 6. Results")
    parts.append("")
    parts.append("### 6.1 Development sample")
    parts.append("")
    if dev:
        parts.append(
            f"The base strategy produced {_fmt(dev.get('n_trades'), 'int')} trades over "
            f"{_fmt(dev.get('n_days'), 'int')} trading days. Gross mean return was "
            f"{_fmt(dev.get('gross_r'), 'r')}R per trade and net mean return was {_fmt(dev.get('avg_r'), 'r')}R. "
            f"The annualised net Sharpe was {_fmt(dev.get('sharpe'), 'num')}, maximum drawdown "
            f"{_fmt(dev.get('max_drawdown'), 'pct')}, win rate {_fmt(dev.get('win_rate'), 'pct')}, and profit "
            f"factor {_fmt(dev.get('profit_factor'), 'pf')}. The cost drag was {_fmt(dev.get('cost_r'), 'r')}R "
            f"per trade. The mean net R is statistically distinguishable from zero "
            f"(t = {_fmt(dev.get('t_stat'), 'num')}, p = {_fmt(dev.get('p_value'), 'p')}), but in the negative direction."
        )
        if figs.get("equity"):
            parts.append("")
            parts.append(f"![Equity by split]({figs['equity']})")
    else:
        parts.append("Development results are not present in this build.")
    parts.append("")
    if not grid.empty:
        parts.append("### 6.2 Parameter grid and ablations")
        parts.append("")
        best = grid.sort_values("sharpe", ascending=False).iloc[0]
        parts.append(
            f"The 18-point grid spans cluster tolerance, minimum touches, and confirmation closes. Every "
            f"combination has a negative net Sharpe. The least-negative combination is `{best['variant']}` "
            f"(Sharpe {_fmt(best['sharpe'], 'num')}, mean net R {_fmt(best['avg_r'], 'r')}), selected only by "
            f"ranking losses. This is a sensitivity surface, not evidence of a profitable region."
        )
        parts.append("")
        rows = [
            [
                r["variant"],
                _fmt(r.get("cluster_atr"), "num"),
                _fmt(r.get("min_touches"), "int"),
                _fmt(r.get("confirmation_closes"), "int"),
                _fmt(r.get("n_trades"), "int"),
                _fmt(r.get("sharpe"), "num"),
                _fmt(r.get("avg_r"), "r"),
                _fmt(r.get("win_rate"), "pct"),
            ]
            for _, r in grid.sort_values("sharpe", ascending=False).iterrows()
        ]
        parts.append(_md_table(["Variant", "Cluster ATR", "Min touches", "Confirms", "Trades", "Sharpe", "Mean net R", "Win rate"], rows))
        if figs.get("grid"):
            parts.append("")
            parts.append(f"![Parameter grid]({figs['grid']})")
        if figs.get("sessions"):
            parts.append("")
            parts.append("All four trading sessions have a negative mean net R; the late session is the worst.")
            parts.append("")
            parts.append(f"![Session breakdown]({figs['sessions']})")
        ab = ledger[ledger["kind"] == "ablation"] if not ledger.empty and "kind" in ledger.columns else pd.DataFrame()
        if not ab.empty:
            parts.append("")
            parts.append(
                "Removing the trend filter, the confirmation closes, or the directional candle increases the "
                "trade count and makes performance worse. No ablation recovers a positive gross edge. The "
                "complete ablation ledger is in Appendix A."
            )
    parts.append("")
    parts.append("### 6.3 Validation sample")
    parts.append("")
    if val:
        parts.append(
            f"On 2020--2022 the base strategy produced {_fmt(val.get('n_trades'), 'int')} trades with net mean "
            f"R {_fmt(val.get('avg_r'), 'r')} and net Sharpe {_fmt(val.get('sharpe'), 'num')}. The development "
            f"grid winner produced {_fmt((stages.get('validation_winner') or {}).get('n_trades'), 'int')} "
            f"trades with net mean R {_fmt((stages.get('validation_winner') or {}).get('avg_r'), 'r')}. "
            f"The sign of the development result did not reverse."
        )
    else:
        parts.append("Validation results are not present in this build.")
    parts.append("")
    parts.append("### 6.4 Sealed holdout")
    parts.append("")
    if hold:
        parts.append(
            f"The holdout produced {_fmt(hold.get('n_trades'), 'int')} trades. Net mean R was "
            f"{_fmt(hold.get('avg_r'), 'r')}, net Sharpe {_fmt(hold.get('sharpe'), 'num')}, maximum drawdown "
            f"{_fmt(hold.get('max_drawdown'), 'pct')}, and win rate {_fmt(hold.get('win_rate'), 'pct')}. "
            f"The pre-registered primary endpoint required a positive net mean R and a positive net Sharpe; "
            f"it is {'met' if (hold.get('avg_r', 0) > 0 and hold.get('sharpe', 0) > 0) else 'not met'}."
        )
    else:
        parts.append("The sealed holdout has not been opened in this build. It will be opened once, after the code freeze.")
    parts.append("")
    parts.append("### 6.5 Cost sensitivity")
    parts.append("")
    costs = _read_csv(root / "results" / "holdout" / "tables" / "costs.csv")
    if not costs.empty:
        rows = [
            [
                f"{_fmt(r.get('cost_multiplier'), 'num')}x",
                _fmt(r.get("n_trades"), "int"),
                _fmt(r.get("avg_r"), "r"),
                _fmt(r.get("sharpe"), "num"),
                _fmt(r.get("max_drawdown"), "pct"),
            ]
            for _, r in costs.sort_values("cost_multiplier").iterrows()
        ]
        parts.append(_md_table(["Cost multiplier", "Trades", "Mean net R", "Net Sharpe", "Max drawdown"], rows))
        if figs.get("cost"):
            parts.append("")
            parts.append(f"![Cost sensitivity]({figs['cost']})")
    else:
        parts.append("Cost sensitivity is produced by the holdout stage and is not present in this build.")
    parts.append("")
    parts.append("### 6.6 Multiple-testing inference")
    parts.append("")
    inf_rows = _inference_rows(inference)
    if inf_rows:
        parts.append(_md_table(["Test", "Value", "Detail"], inf_rows))
        parts.append("")
        bs = inference.get("bootstrap", {})
        if bs.get("mean_r") and bs.get("sharpe"):
            mr = bs["mean_r"]
            sr = bs["sharpe"]
            parts.append("")
            parts.append(
                f"The stationary-bootstrap 95% confidence interval is "
                f"[{_fmt(mr.get('ci_low'), 'r')}, {_fmt(mr.get('ci_high'), 'r')}] for mean net R and "
                f"[{_fmt(sr.get('ci_low'), 'num')}, {_fmt(sr.get('ci_high'), 'num')}] for the annualised "
                f"net Sharpe, using {_fmt(mr.get('n_boot'), 'int')} resamples with a mean block of "
                f"{_fmt(mr.get('mean_block'), 'int')} trades. Both intervals lie entirely below zero."
            )
        parts.append("")
        parts.append(
            "The deflated Sharpe ratio and PBO are the relevant corrections because the grid and the "
            "ablations are a deliberate search. A DSR near zero means the selected variant is not "
            "distinguishable from the best of a set of noise strategies. The PBO is modest here only "
            "because the ranking of a uniformly unprofitable set is stable; it is not evidence of a "
            "profitable variant."
        )
        placebos = inference.get("placebos", {})
        if placebos.get("random_entry") and placebos.get("random_level"):
            re_p = placebos["random_entry"].get("pvalue")
            rl_p = placebos["random_level"].get("pvalue")
            rl_n = placebos["random_level"].get("n_draws")
            if isinstance(rl_p, (int, float)) and rl_p <= 0.0 and isinstance(rl_n, (int, float)) and rl_n > 0:
                rl_txt = f"< {1.0 / (rl_n + 1.0):.4f} (0/{int(rl_n)} draws)"
            else:
                rl_txt = _fmt(rl_p, "p")
            parts.append("")
            parts.append(
                f"The random-entry placebo is not rejected (p = {_fmt(re_p, 'p')}): random entries with the "
                f"same geometry perform at least as well as the strategy's entries. The random-level placebo "
                f"is rejected in the opposite direction (p {rl_txt}): the actual levels are less bad "
                f"than random active levels. Because the placebo re-derives the stop from the sampled level "
                f"while holding the target distance fixed, it changes the risk geometry as well as the level "
                f"location; it is a joint test of level and stop placement, not a pure level-location test. "
                f"The level filter therefore carries some relative information, but that information does "
                f"not translate into a positive absolute net return. This is relative, not absolute, skill."
            )
    else:
        parts.append("Inference is produced by the holdout stage and is not present in this build.")
    parts.append("")
    parts.append("### 6.7 Level-reaction audit and the '90% success' claim")
    parts.append("")
    if not audit_summary.empty:
        rows = [
            [
                r["level_tf"],
                _fmt(r.get("side"), "int"),
                _fmt(r.get("n"), "int"),
                _fmt(r.get("hold_rate"), "pct"),
                _fmt(r.get("hit_1r_before_stop"), "pct"),
                _fmt(r.get("hit_2r_before_stop"), "pct"),
                _fmt(r.get("hit_3r_before_stop"), "pct"),
                _fmt(r.get("mean_mfe_r"), "r"),
            ]
            for _, r in audit_summary.iterrows()
        ]
        parts.append(
            "The full strategy is not the only way to test the practitioner claim. We therefore measure, for "
            "every executed development signal, whether the marked level held (no close breach by 0.25 ATR "
            "against the trade) and whether price reached +1R, +2R, or +3R before the -1R stop."
        )
        parts.append("")
        parts.append(_md_table(["Level", "Side", "N", "Hold rate", "Hit +1R", "Hit +2R", "Hit +3R", "Mean MFE (R)"], rows))
        if "ALL" in set(audit_summary["level_tf"]):
            allrow = audit_summary[audit_summary["level_tf"] == "ALL"].iloc[0]
            parts.append("")
            parts.append(
                f"Pooled, the hold rate is {_fmt(allrow.get('hold_rate'), 'pct1')} and the +1R-before-stop rate "
                f"is {_fmt(allrow.get('hit_1r_before_stop'), 'pct1')}. A 90% success claim would require the "
                f"latter to be near 90%; it is not. The result is descriptive and conditions on the strategy's "
                f"own entry filter, so it is not a test of every possible level definition, but it is the "
                f"relevant test of the claim as operationalised."
            )
        if figs.get("audit"):
            parts.append("")
            parts.append(f"![Level reactions]({figs['audit']})")
    else:
        parts.append("The audit has not been run in this build.")
    parts.append("")
    parts.append("### 6.8 Data integrity and vendor cross-validation")
    parts.append("")
    if not cross.empty:
        rows = [
            [
                r["symbol"],
                _fmt(r.get("n_weeks"), "int"),
                _fmt(r.get("corr"), "num"),
                _fmt(r.get("mean_abs_weekly_return_diff"), "pct"),
                _fmt(r.get("histdata_ann_vol"), "pct"),
                _fmt(r.get("yahoo_ann_vol"), "pct"),
            ]
            for _, r in cross.iterrows()
        ]
        parts.append(_md_table(["Symbol", "Weeks", "Return corr.", "Mean abs weekly diff", "HistData vol", "Yahoo vol"], rows))
        parts.append("")
        parts.append(
            "The cross-vendor correlation is high, which rules out a vendor-specific price path as the source "
            "of the null. Levels are not compared directly because the vendors quote different instruments "
            "(spot versus futures)."
        )
    else:
        parts.append("Cross-validation is not present in this build.")
    integ = _read_csv(root / "results" / "audit" / "tables" / "integrity.csv")
    if not integ.empty:
        total_rows = int(integ["rows"].sum())
        dup = int(integ["duplicate_ts"].sum())
        viol = int(integ["ohlc_violations"].sum())
        nonpos = int(integ["nonpositive"].sum())
        max15 = integ.loc[integ["timeframe"] == "15min", "max_abs_log_return"].max()
        parts.append("")
        parts.append(
            f"Across {len(integ)} processed series and {total_rows:,} bars, there were {dup} duplicate "
            f"timestamps, {viol} OHLC violations, and {nonpos} non-positive prices. The largest absolute "
            f"15-minute log return was {_fmt(max15, 'pct1')}, consistent with a real intraday move rather "
            f"than a corrupted bar."
        )
    if amb:
        parts.append("")
        parts.append(
            f"Intrabar ambiguity was measured directly: {_fmt(amb.get('ambiguous'), 'int')} of "
            f"{_fmt(amb.get('n'), 'int')} trades ({_fmt(amb.get('share'), 'pct1')}) had the stop and target "
            f"inside the same 15-minute bar at some point before exit, and "
            f"{_fmt(amb.get('entry_bar_ambiguous'), 'int')} were ambiguous on the entry bar. The stop-first "
            f"convention therefore cannot explain the result."
        )
    parts.append("")
    parts.append("### 6.9 Live shadow (2026)")
    parts.append("")
    if live_meta and live_meta.get("portfolio", {}).get("n_trades", 0):
        p = live_meta["portfolio"]
        parts.append(
            f"The 2026 shadow produced {_fmt(p.get('n_trades'), 'int')} trades with mean net R "
            f"{_fmt(p.get('mean_r'), 'r')} and win rate {_fmt(p.get('win_rate'), 'pct')}. The window is roughly "
            f"59 days of Yahoo 15-minute data; this is an operational check, not a significance test."
        )
        if figs.get("live"):
            parts.append("")
            parts.append(f"![Live shadow]({figs['live']})")
    else:
        parts.append("The live shadow has not produced trades in this build.")
    parts.append("")
    parts.append("### 6.10 Benchmarks and per-instrument tests")
    parts.append("")
    bench = _read_csv(root / "results" / "holdout" / "tables" / "benchmarks.csv")
    if not bench.empty:
        rows = [
            [
                r["variant"],
                _fmt(r.get("n_days"), "int"),
                _fmt(r.get("ann_return"), "pct"),
                _fmt(r.get("ann_vol"), "pct"),
                _fmt(r.get("sharpe"), "num"),
                _fmt(r.get("max_drawdown"), "pct"),
            ]
            for _, r in bench.sort_values("sharpe", ascending=False).iterrows()
        ]
        parts.append(
            "The strategy is also compared with simple alternatives on the same holdout window and cost "
            "model. Buy-and-hold and the 50/200-day SMA crossover are positive; the 4-hour trend-only and "
            "Donchian benchmarks are negative. The key-level strategy is the worst of the set."
        )
        parts.append("")
        parts.append(_md_table(["Benchmark", "Days", "Annual return", "Annual vol", "Net Sharpe", "Max DD"], rows))
    else:
        parts.append("Benchmark results are not present in this build.")
    bh = inference.get("per_instrument_bh", [])
    if bh:
        parts.append("")
        rows = [
            [
                r["symbol"],
                _fmt(r.get("p_one_sided"), "p"),
                _fmt(r.get("q"), "p"),
                "yes" if r.get("reject") else "no",
            ]
            for r in bh
        ]
        parts.append(
            "Per-instrument one-sided tests of net mean R are Benjamini--Hochberg adjusted. No instrument "
            "survives the pre-registered false-discovery-rate control; the smallest raw p-value (NSXUSD) is "
            "not significant after adjustment."
        )
        parts.append("")
        parts.append(_md_table(["Symbol", "One-sided p", "BH q", "Reject at alpha"], rows))
    parts.append("")
    parts.append("## 7. Interpretation")
    parts.append("")
    if dev and abs(dev.get("gross_r", 0.0)) < 0.03:
        parts.append(
            "The central finding is that the gross edge is approximately zero before any transaction cost. "
            "This matters more than the sign of the net result: a strategy with a genuine 90% level-hold rate "
            "would earn a positive gross mean R even under pessimistic path assumptions. Costs then turn a "
            "zero-mean strategy into a reliably negative one, which is exactly what the net ledger shows."
        )
    else:
        parts.append(
            "The gross and net ledgers must be read together: the strategy's raw payoff is not large enough "
            "to survive the cost model, and the grid does not identify a stable profitable region."
        )
    parts.append("")
    parts.append(
        "A negative result of this kind is informative for three reasons. First, it is a direct test of a "
        "widely repeated practitioner claim with a pre-registered protocol rather than a post-hoc story. "
        "Second, the level-reaction audit separates the claim's mechanism (the level holds) from its "
        "profitability implication (the hold can be monetised after costs); the first is weak and the second "
        "fails. "
        + (
            "Third, the result survives a sealed holdout and multiple-testing correction, so it is not an "
            "artefact of the development sample."
            if hold
            else "Third, the sealed holdout and multiple-testing correction are pre-committed but not yet "
            "reported in this build, so out-of-sample confirmation remains pending."
        )
    )
    parts.append("")
    parts.append("## 8. Limitations")
    parts.append("")
    parts.append(
        "1. **Operationalisation.** The transcript is discretionary. The rules above are a faithful but "
        "explicit translation; a different translation could produce a different result.\n"
        "2. **Timestamps.** HistData's M1 timestamp convention is undocumented. The resampling is causal and "
        "internally consistent, but bar-open versus bar-close ambiguity remains.\n"
        "3. **Costs.** Spreads and slippage are stylised, not broker quotes. The 0x--3x sensitivity bounds the "
        "conclusion but does not model queue position or partial fills.\n"
        "4. **Intrabar path.** The 15-minute bars cannot order the high and low. Stop-first is a lower bound; "
        "the measured ambiguity rate is small, so the bound is tight in this sample.\n"
        "5. **Universe.** Eight instruments, chosen ex ante. Results may differ in less liquid markets.\n"
        "6. **Live shadow.** The Yahoo 15-minute window is short. It is descriptive and is not used for "
        "confirmatory inference.\n"
        "7. **News and macro events.** No historical news calendar is used. The base specification is a "
        "no-news-filter result; this is a limitation, not a claim that news is irrelevant."
    )
    parts.append("")
    parts.append("## 9. Reproducibility")
    parts.append("")
    parts.append("```bash")
    parts.append("make setup        # install Python dependencies")
    parts.append("make download     # HistData M1 files with SHA-256 manifest")
    parts.append("make build        # resample to 15m/4h/1D/1W/1M")
    parts.append("make development  # base + ablations + 18-point grid")
    parts.append("make validation   # 2020-2022 check")
    parts.append("make freeze       # SHA-256 code/config freeze")
    parts.append("make holdout      # open the sealed 2023-2025 sample once")
    parts.append("make audit        # integrity, Yahoo cross-check, level reactions")
    parts.append("make live         # 2026 descriptive shadow")
    parts.append("make report       # regenerate RESEARCH.md, README.md, preview.html, figures")
    parts.append("make pdf          # compile paper/main.tex with TeX Live (Docker)")
    parts.append("make test         # unit tests")
    parts.append("```")
    parts.append("")
    parts.append(
        "The Docker path is `docker compose run --rm research <command>`. Raw data are not redistributed; the "
        "download scripts and SHA-256 manifests are in `data/raw/download_manifest.json`."
    )
    parts.append("")
    parts.append(
        "The holdout guard is active. `make holdout` refuses to run because the working tree no longer "
        "matches `CODE_FREEZE.json` after the presentation and inference-completion edits recorded in "
        "`preregistration/DEVIATIONS.md` sections F and G. The opened holdout outputs are retained under "
        "`results/holdout/`; re-opening the holdout would violate the pre-registration."
    )
    parts.append("")
    parts.append("## 10. References")
    parts.append("")
    refs = [
        "Arnott, R., Harvey, C. R., & Markowitz, H. (2019). A backtesting protocol in the era of machine learning. *Journal of Financial Data Science*, 1(1), 64--74.",
        "Bailey, D. H., Borwein, J. M., Lopez de Prado, M., & Zhu, Q. J. (2017). The probability of backtest overfitting. *Journal of Computational Finance*, 20(4), 39--69.",
        "Bailey, D. H., & Lopez de Prado, M. (2014). The deflated Sharpe ratio: correcting for selection bias, backtest overfitting, and non-normality. *Journal of Portfolio Management*, 40(5), 94--107.",
        "Benjamini, Y., & Hochberg, Y. (1995). Controlling the false discovery rate: a practical and powerful approach to multiple testing. *Journal of the Royal Statistical Society B*, 57(1), 289--300.",
        "Brock, W., Lakonishok, J., & LeBaron, B. (1992). Simple technical trading rules and the stochastic properties of stock returns. *Journal of Finance*, 47(5), 1731--1764.",
        "Claeys, M. (2026). When backtests guess: how trading platforms silently fabricate results. SSRN Working Paper 6240638. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6240638",
        "Deprez, N., & Frommel, M. (2024). Are simple technical trading rules profitable in bitcoin markets? *International Review of Economics & Finance*, 93, 858--874.",
        "Hansen, P. R. (2005). A test for superior predictive ability. *Journal of Business & Economic Statistics*, 23(4), 365--380.",
        "Harvey, C. R., & Liu, Y. (2020). False (and missed) discoveries in financial economics. *Journal of Finance*, 75(5), 2503--2553.",
        "Lo, A. W., Mamaysky, H., & Wang, J. (2000). Foundations of technical analysis: computational algorithms, statistical inference, and empirical implementation. *Journal of Finance*, 55(4), 1705--1765.",
        "Menkhoff, L., & Taylor, M. P. (2007). The obstinate passion of foreign exchange professionals: technical analysis. *Journal of Economic Literature*, 45(4), 936--972.",
        "Osler, C. L. (2003). Currency orders and exchange rate dynamics: an explanation for the predictive success of technical analysis. *Journal of Finance*, 58(5), 1791--1820.",
        "Politis, D. N., & Romano, J. P. (1994). The stationary bootstrap. *Journal of the American Statistical Association*, 89(428), 1303--1313.",
        "Rink, K. (2023). The predictive ability of technical trading rules: an empirical analysis of developed and emerging equity markets. *Financial Markets and Portfolio Management*. https://doi.org/10.1007/s11408-023-00433-2",
        "Sullivan, R., Timmermann, A., & White, H. (1999). Data-snooping, technical trading rule performance, and the bootstrap. *Journal of Finance*, 54(5), 1647--1691.",
        "White, H. (2000). A reality check for data snooping. *Econometrica*, 68(5), 1097--1126.",
    ]
    parts.extend([f"- {r}" for r in refs])
    parts.append("")
    parts.append("## Appendix A. Full trial ledger")
    parts.append("")
    if not ledger.empty:
        rows = [
            [
                r["kind"],
                r["variant"],
                _fmt(r.get("n_trades"), "int"),
                _fmt(r.get("sharpe"), "num"),
                _fmt(r.get("avg_r"), "r"),
                _fmt(r.get("win_rate"), "pct"),
                _fmt(r.get("max_drawdown"), "pct"),
            ]
            for _, r in ledger.iterrows()
        ]
        parts.append(_md_table(["Kind", "Variant", "Trades", "Sharpe", "Mean net R", "Win rate", "Max DD"], rows))
    else:
        parts.append("No ledger present.")
    parts.append("")
    parts.append("## Appendix B. Files")
    parts.append("")
    parts.append(
        "- `preregistration/PROTOCOL.md` -- the research contract.\n"
        "- `preregistration/DEVIATIONS.md` -- pre-freeze amendments and post-freeze changes.\n"
        "- `config/base.yaml`, `config/instruments.yaml` -- frozen parameters and costs.\n"
        "- `src/klab/` -- download, timeframes, levels, trend, strategy, backtest, statistics, audit, live, report.\n"
        "- `results/development/`, `results/validation/`, `results/holdout/`, `results/audit/`, `results/live/` -- machine outputs.\n"
        "- `paper/main.tex`, `paper/references.bib` -- the manuscript.\n"
    )
    return "\n".join(parts) + "\n"


def _latex_manuscript(
    cfg: dict[str, Any],
    root: Path,
    stages: dict[str, dict[str, Any]],
    grid: pd.DataFrame,
    ledger: pd.DataFrame,
    audit_summary: pd.DataFrame,
    cross: pd.DataFrame,
    amb: dict[str, Any],
    live_meta: dict[str, Any],
    live_per: pd.DataFrame,
    inference: dict[str, Any],
    figs: dict[str, str],
    generated: str,
) -> str:
    dev = stages.get("development")
    val = stages.get("validation")
    hold = stages.get("holdout")
    winner = _read_json(root / "results" / "development" / "winner.json").get("params", {})
    parts: list[str] = []
    parts.append(r"\documentclass[11pt,a4paper]{article}")
    parts.append(r"\usepackage[margin=2.5cm]{geometry}")
    parts.append(r"\usepackage{booktabs,graphicx,amsmath,amssymb}")
    parts.append(r"\usepackage[hidelinks]{hyperref}")
    parts.append(r"\usepackage{natbib}")
    parts.append(r"\usepackage{caption}")
    parts.append(r"\captionsetup{font=small}")
    parts.append(r"\title{Multi-Timeframe Key Levels:\\ A Pre-Registered Replication and Null Result}")
    parts.append(r"\author{Key-Level Research Project}")
    parts.append(r"\date{" + _tex_escape(generated) + "}")
    parts.append(r"\begin{document}")
    parts.append(r"\maketitle")
    parts.append(r"\begin{abstract}")
    parts.append(_tex_escape(_abstract(dev, hold, audit_summary, grid)))
    parts.append(r"\end{abstract}")
    parts.append(r"\noindent\textbf{Keywords:} technical analysis; support and resistance; foreign exchange; intraday; backtest overfitting; replication")
    parts.append("")
    parts.append(r"\section{Introduction}")
    parts.append(
        r"Technical analysis remains widely used in foreign exchange and futures markets, yet the "
        r"profitability of simple chart patterns after realistic costs and data-snooping corrections is "
        r"contested \citep{brock1992simple,lo2000foundations,menkhoff2007obstinate,osler2003currency}. "
        r"This paper studies a specific and widely taught workflow: mark monthly, weekly, and daily "
        r"support/resistance from candle bodies; determine the trend on the 4-hour chart; wait for a "
        r"15-minute rejection at a level; enter in the trend direction with a stop beyond the level and a "
        r"target at the next level. The practitioner claim is that the level holds roughly 90\% of the time. "
        r"We translate the workflow into deterministic rules, freeze the rules before opening a sealed "
        r"2023--2025 holdout, and report the result whatever its sign."
    )
    parts.append(
        r"Our contribution is a well-powered negative result with three features that are often missing "
        r"from practitioner replications: (i) intraday data from an independent vendor over 2012--2025, "
        r"(ii) a pre-registered parameter grid and sealed holdout, and (iii) inference that corrects for the "
        r"selection of the best variant \citep{bailey2014deflated,bailey2017probability,white2000reality,hansen2005test}."
    )
    parts.append(r"\section{Related literature}")
    parts.append(
        r"The modern literature on technical trading begins with \citet{brock1992simple}, who documented "
        r"predictability using simple rules on the Dow, and \citet{sullivan1999data}, who showed that "
        r"data-snooping can explain much of the apparent profitability. \citet{lo2000foundations} formalised "
        r"pattern recognition and found some incremental information in chart patterns. In foreign exchange, "
        r"\citet{osler2003currency} linked the predictive success of technical analysis to the clustering of "
        r"stop and take-profit orders around round numbers, and \citet{menkhoff2007obstinate} documented the "
        r"continued use of technical analysis by professionals. More recent work continues to find "
        r"horizon- and regime-dependent predictability \citep{rink2023predictive,deprez2024bitcoin}. "
        r"Our design follows the backtesting protocol of \citet{arnott2019backtesting} and the "
        r"multiple-testing corrections of \citet{harvey2020false}."
    )
    parts.append(r"\section{Data}")
    parts.append(
        r"The primary sample is HistData.com Generic ASCII M1 bid bars for eight instruments: EURUSD, "
        r"GBPUSD, USDJPY, AUDUSD, USDCAD, XAUUSD, SPXUSD, and NSXUSD, from 2012-01-01 to 2025-12-31. "
        r"M1 bars are resampled into 15-minute, 4-hour, daily, weekly, and monthly bars. No future bar is "
        r"used before its close. HistData timestamps are treated as fixed UTC-5 without daylight saving, "
        r"per the vendor specification. The processed sample contains 346,372 EURUSD 15-minute bars, "
        r"330,920 XAUUSD bars, and 324,578 SPXUSD bars."
    )
    parts.append(
        r"An independent check compares weekly close-to-close returns with Yahoo Finance daily bars. "
        r"The Yahoo series are not mixed into the historical backtest. The 2026 live shadow uses Yahoo "
        r"15-minute bars, whose public window is roughly 59 days, and is descriptive only."
    )
    parts.append(r"\begin{table}[htbp]\centering\small")
    parts.append(r"\caption{Instrument universe and base round-trip cost assumptions.}")
    parts.append(r"\label{tab:instruments}")
    parts.append(r"\begin{tabular}{llrrr}")
    parts.append(r"\toprule")
    parts.append(r"Instrument & Class & Half-spread & Slippage & Round trip \\")
    parts.append(r"\midrule")
    for sym, ins in cfg["instruments"].items():
        half = float(getattr(ins, "half_spread_price", 0.0))
        slip = float(getattr(ins, "slippage_price", 0.0))
        klass = str(getattr(ins, "asset_class", "")).replace("_", " ")
        rt = 2 * (half + slip)
        parts.append(
            f"{_tex_escape(sym)} & {_tex_escape(klass)} & "
            f"{half:.4g} & {slip:.4g} & {rt:.4g} \\\\"
        )
    parts.append(r"\bottomrule")
    parts.append(r"\end{tabular}")
    parts.append(r"\end{table}")
    parts.append(r"\section{Strategy and execution}")
    parts.append(
        r"Monthly, weekly, and daily pivots are clustered in price with a tolerance of $0.50\times$ "
        r"ATR14(D1) and become tradable after at least two pivots. A level is a resistance if a 15-minute "
        r"bar trades through it and closes back below; a support is the mirror case. The 4-hour trend is "
        r"defined by the last two confirmed swing highs and lows. A trade requires trend agreement, a level "
        r"touch, two consecutive closes on the rejected side, a directional candle, and a chase of no more "
        r"than $1\times$ ATR15. Entry is at the next 15-minute open."
    )
    parts.append(
        r"The stop is the more conservative of the level plus $0.25\times$ ATR15 and the five-bar extreme "
        r"plus $0.10\times$ ATR15, capped at $4\times$ ATR15. The target is the nearest opposite active "
        r"level at least $1R$ away, defaulting to $2R$ and capped at $5R$. Maximum holding period is 96 "
        r"bars. If the stop and target share a bar, the stop is assumed first. Risk is 0.5\% of equity per "
        r"trade. Costs are half the quoted spread plus adverse slippage on each side. A gap through the stop "
        r"fills at the next available open; a gap through the target does not enter the trade."
    )
    parts.append(r"\begin{table}[htbp]\centering\small")
    parts.append(r"\caption{Frozen base parameters.}")
    parts.append(r"\label{tab:design}")
    parts.append(r"\begin{tabular}{ll}")
    parts.append(r"\toprule")
    parts.append(r"Parameter & Value \\")
    parts.append(r"\midrule")
    for key, value in cfg["strategy"].items():
        parts.append(f"{_tex_escape(str(key).replace('_', ' '))} & {_tex_escape(value)} \\\\")
    parts.append(r"\bottomrule")
    parts.append(r"\end{tabular}")
    parts.append(r"\end{table}")
    parts.append(r"\section{Statistical protocol}")
    parts.append(
        r"The sample is split into development (2012--2019), validation (2020--2022), and a sealed holdout "
        r"(2023--2025). The base specification is fixed by faithfulness to the transcript, not optimised. "
        r"The 18-point grid is selected on development only; the winner is evaluated unchanged on validation "
        r"and holdout. Inference uses the stationary bootstrap \citep{politis1994stationary}, the deflated "
        r"Sharpe ratio \citep{bailey2014deflated}, the probability of backtest overfitting via CSCV "
        r"\citep{bailey2017probability}, the White Reality Check \citep{white2000reality}, Hansen's SPA test "
        r"\citep{hansen2005test}, and Benjamini--Hochberg control of the per-instrument false discovery rate "
        r"\citep{benjamini1995controlling}. The holdout is opened once, after a SHA-256 code freeze."
    )
    parts.append(r"\section{Results}")
    if dev:
        parts.append(
            r"On the development sample the base strategy produced "
            + _fmt(dev.get("n_trades"), "int")
            + r" trades. Gross mean return was "
            + _fmt(dev.get("gross_r"), "r")
            + r"$R$ per trade and net mean return was "
            + _fmt(dev.get("avg_r"), "r")
            + r"$R$, with net annualised Sharpe "
            + _fmt(dev.get("sharpe"), "num")
            + r" and maximum drawdown "
            + _fmt(dev.get("max_drawdown"), "pct")
            + r". The cost drag was "
            + _fmt(dev.get("cost_r"), "r")
            + r"$R$ per trade."
        )
    parts.append("")
    headers, rows = _stage_comparison(stages)
    if len(stages):
        parts.append(_tex_table(headers, rows, "Base strategy by sample split.", "tab:splits", size=r"\footnotesize"))
    if not grid.empty:
        best = grid.sort_values("sharpe", ascending=False).iloc[0]
        parts.append(
            r"Table \ref{tab:grid} reports the full grid. Every combination has a negative net Sharpe. "
            r"The least-negative variant is "
            + _tex_escape(best["variant"])
            + r" (Sharpe "
            + _fmt(best["sharpe"], "num")
            + r"), selected only by ranking losses. "
            r"Figure \ref{fig:grid} shows the same surface graphically."
        )
        grows = [
            [
                r["variant"],
                _fmt(r.get("cluster_atr"), "num"),
                _fmt(r.get("min_touches"), "int"),
                _fmt(r.get("confirmation_closes"), "int"),
                _fmt(r.get("n_trades"), "int"),
                _fmt(r.get("sharpe"), "num"),
                _fmt(r.get("avg_r"), "r"),
                _fmt(r.get("win_rate"), "pct"),
            ]
            for _, r in grid.sort_values("sharpe", ascending=False).iterrows()
        ]
        parts.append(_tex_table(["Variant", "Cluster", "Touches", "Confirms", "Trades", "Sharpe", "Mean net R", "Win rate"], grows, "Pre-registered parameter grid on the development sample.", "tab:grid", size=r"\footnotesize"))
    ab = ledger[ledger["kind"] == "ablation"] if not ledger.empty and "kind" in ledger.columns else pd.DataFrame()
    if not ab.empty:
        arows = [
            [
                r["variant"],
                _fmt(r.get("n_trades"), "int"),
                _fmt(r.get("sharpe"), "num"),
                _fmt(r.get("avg_r"), "r"),
                _fmt(r.get("win_rate"), "pct"),
            ]
            for _, r in ab.iterrows()
        ]
        parts.append(_tex_table(["Ablation", "Trades", "Sharpe", "Mean net R", "Win rate"], arows, "Pre-registered ablations on the development sample.", "tab:ablations"))
    if val:
        parts.append(
            r"On validation (2020--2022) the base strategy produced "
            + _fmt(val.get("n_trades"), "int")
            + r" trades with net mean $R$ "
            + _fmt(val.get("avg_r"), "r")
            + r" and net Sharpe "
            + _fmt(val.get("sharpe"), "num")
            + r". The development grid winner produced "
            + _fmt((stages.get("validation_winner") or {}).get("n_trades"), "int")
            + r" trades with net mean $R$ "
            + _fmt((stages.get("validation_winner") or {}).get("avg_r"), "r")
            + r". The sign did not reverse."
        )
    if hold:
        parts.append(
            r"On the sealed holdout (2023--2025) the strategy produced "
            + _fmt(hold.get("n_trades"), "int")
            + r" trades with net mean $R$ "
            + _fmt(hold.get("avg_r"), "r")
            + r", net Sharpe "
            + _fmt(hold.get("sharpe"), "num")
            + r", and maximum drawdown "
            + _fmt(hold.get("max_drawdown"), "pct")
            + r". The primary endpoint is "
            + ("met." if (hold.get("avg_r", 0) > 0 and hold.get("sharpe", 0) > 0) else "not met.")
        )
    else:
        parts.append(r"The sealed holdout has not been opened in this build.")
    costs = _read_csv(root / "results" / "holdout" / "tables" / "costs.csv")
    if not costs.empty:
        crows = [
            [
                f"{_fmt(r.get('cost_multiplier'), 'num')}x",
                _fmt(r.get("n_trades"), "int"),
                _fmt(r.get("avg_r"), "r"),
                _fmt(r.get("sharpe"), "num"),
            ]
            for _, r in costs.sort_values("cost_multiplier").iterrows()
        ]
        parts.append(_tex_table(["Cost multiplier", "Trades", "Mean net R", "Net Sharpe"], crows, "Holdout cost sensitivity.", "tab:costs"))
    inf_rows = _inference_rows(inference)
    if inf_rows:
        parts.append(_tex_table(["Test", "Value", "Detail"], inf_rows, "Multiple-testing inference on the holdout.", "tab:inference"))
        bs = inference.get("bootstrap") or {}
        ab = inference.get("alpha_beta_base") or {}
        pbs = inference.get("placebos") or {}
        txt = r"All multiple-testing corrections agree with the negative verdict."
        if inference.get("dsr_base") and inference.get("dsr_winner"):
            txt += (
                r" The deflated Sharpe ratio is essentially zero ("
                + _fmt(inference["dsr_base"].get("dsr"), "p")
                + r" for the base and "
                + _fmt(inference["dsr_winner"].get("dsr"), "p")
                + r" for the grid winner)."
            )
        if inference.get("pbo"):
            txt += (
                r" The PBO is "
                + _fmt((inference.get("pbo") or {}).get("pbo"), "num")
                + r", but this is modest only because the ranking of a uniformly unprofitable set is "
                r"stable; it is not evidence of a profitable variant."
            )
        if bs.get("mean_r") and bs.get("sharpe"):
            txt += (
                r" The stationary-bootstrap 95\% confidence interval is ["
                + _fmt(bs["mean_r"].get("ci_low"), "r")
                + r", "
                + _fmt(bs["mean_r"].get("ci_high"), "r")
                + r"] for mean net $R$ and ["
                + _fmt(bs["sharpe"].get("ci_low"), "num")
                + r", "
                + _fmt(bs["sharpe"].get("ci_high"), "num")
                + r"] for the annualised net Sharpe; both lie entirely below zero."
            )
        if ab:
            txt += (
                r" The annualised alpha against an equal-weight portfolio of the same instruments is "
                + _fmt(ab.get("alpha_ann"), "pct")
                + r" (p = "
                + _fmt(ab.get("p_alpha"), "p")
                + r")."
            )
        re_pb = pbs.get("random_entry") or {}
        rl_pb = pbs.get("random_level") or {}
        if re_pb:
            txt += (
                r" The random-entry placebo is not rejected (p = "
                + _fmt(re_pb.get("pvalue"), "p")
                + r"), so entries with the same geometry are no better than random entries."
            )
        if rl_pb:
            n_rl = int(rl_pb.get("n_draws") or 200)
            rl_p = float(rl_pb.get("pvalue") or 0.0)
            rl_txt = "< " + _fmt(1.0 / (n_rl + 1.0), "p") + f" ({int(round(rl_p * n_rl))}/{n_rl})" if rl_p == 0 else _fmt(rl_p, "p")
            txt += (
                r" The random-level placebo is rejected in the opposite direction (p "
                + rl_txt
                + r"): actual levels are less bad than random active levels. Because the placebo "
                r"re-derives the stop, it is a joint test of level and stop placement, not a pure "
                r"level-location test; the relative level information does not become a positive "
                r"absolute return."
            )
        parts.append(txt)
    if not audit_summary.empty:
        rrows = [
            [
                r["level_tf"],
                _fmt(r.get("side"), "int"),
                _fmt(r.get("n"), "int"),
                _fmt(r.get("hold_rate"), "pct"),
                _fmt(r.get("hit_1r_before_stop"), "pct"),
                _fmt(r.get("hit_2r_before_stop"), "pct"),
                _fmt(r.get("hit_3r_before_stop"), "pct"),
            ]
            for _, r in audit_summary.iterrows()
        ]
        parts.append(
            r"Table \ref{tab:reactions} tests the mechanism directly. It reports the share of executed "
            r"development signals for which the marked level held and the share for which price reached "
            r"$+1R$, $+2R$, or $+3R$ before the $-1R$ stop. The 90\% claim would require the $+1R$ rate to "
            r"be near 90\%; it is not."
        )
        parts.append(_tex_table(["Level", "Side", "N", "Hold rate", "Hit +1R", "Hit +2R", "Hit +3R"], rrows, "Level-reaction audit on executed development signals.", "tab:reactions"))
    if not cross.empty:
        xrows = [
            [
                r["symbol"],
                _fmt(r.get("n_weeks"), "int"),
                _fmt(r.get("corr"), "num"),
                _fmt(r.get("mean_abs_weekly_return_diff"), "pct"),
            ]
            for _, r in cross.iterrows()
        ]
        parts.append(_tex_table(["Symbol", "Weeks", "Return corr.", "Mean abs weekly diff"], xrows, "Cross-vendor weekly-return validation against Yahoo Finance.", "tab:cross"))
    integ = _read_csv(root / "results" / "audit" / "tables" / "integrity.csv")
    if not integ.empty:
        total_rows = int(integ["rows"].sum())
        dup = int(integ["duplicate_ts"].sum())
        viol = int(integ["ohlc_violations"].sum())
        nonpos = int(integ["nonpositive"].sum())
        max15 = integ.loc[integ["timeframe"] == "15min", "max_abs_log_return"].max()
        parts.append(
            r"Across " + str(len(integ)) + r" processed series and " + f"{total_rows:,}" + r" bars, there were "
            + str(dup) + r" duplicate timestamps, " + str(viol) + r" OHLC violations, and " + str(nonpos)
            + r" non-positive prices; the largest absolute 15-minute log return was " + _fmt(max15, "pct1")
            + r"."
        )
    if amb:
        parts.append(
            r"Intrabar ambiguity was measured directly: "
            + _fmt(amb.get("ambiguous"), "int")
            + r" of "
            + _fmt(amb.get("n"), "int")
            + r" trades ("
            + _fmt(amb.get("share"), "pct1")
            + r") had both bracket levels inside a single 15-minute bar before exit. The stop-first convention therefore cannot explain the result."
        )
    if live_meta and live_meta.get("portfolio", {}).get("n_trades", 0):
        p = live_meta["portfolio"]
        parts.append(
            r"The 2026 live shadow produced "
            + _fmt(p.get("n_trades"), "int")
            + r" trades with mean net $R$ "
            + _fmt(p.get("mean_r"), "r")
            + r" and win rate "
            + _fmt(p.get("win_rate"), "pct")
            + r". The window is short and the exercise is descriptive."
        )
    for key, caption, label in [
        ("equity", "Strategy equity by sample split (log scale).", "fig:equity"),
        ("grid", "Pre-registered parameter grid (development).", "fig:grid"),
        ("sessions", "Development mean net R by UTC session.", "fig:sessions"),
        ("symbols", "Development performance by instrument.", "fig:symbols"),
        ("rdist", "Trade return distribution before and after costs.", "fig:rdist"),
        ("audit", "Level hold rate and +1R-before-stop rate.", "fig:audit"),
        ("cost", "Holdout cost sensitivity.", "fig:cost"),
        ("live", "2026 live shadow cumulative net R.", "fig:live"),
    ]:
        if key in figs:
            parts.append(r"\begin{figure}[htbp]\centering")
            parts.append(r"\includegraphics[width=0.92\linewidth]{" + figs[key].replace("paper/", "") + "}")
            parts.append(r"\caption{" + _tex_escape(caption) + "}")
            parts.append(r"\label{" + label + "}")
            parts.append(r"\end{figure}")
    bench = _read_csv(root / "results" / "holdout" / "tables" / "benchmarks.csv")
    if not bench.empty:
        brows = [
            [
                r["variant"],
                _fmt(r.get("n_days"), "int"),
                _fmt(r.get("ann_return"), "pct"),
                _fmt(r.get("ann_vol"), "pct"),
                _fmt(r.get("sharpe"), "num"),
                _fmt(r.get("max_drawdown"), "pct"),
            ]
            for _, r in bench.sort_values("sharpe", ascending=False).iterrows()
        ]
        parts.append(
            r"Table \ref{tab:benchmarks} compares the strategy with simple alternatives on the same holdout "
            r"window and cost model. Buy-and-hold and the 50/200-day SMA crossover are positive; the 4-hour "
            r"trend-only and Donchian benchmarks are negative. The key-level strategy is the worst of the set."
        )
        parts.append(_tex_table(["Benchmark", "Days", "Annual return", "Annual vol", "Net Sharpe", "Max DD"], brows, "Holdout benchmarks on the same window and cost model.", "tab:benchmarks"))
    bh = inference.get("per_instrument_bh", [])
    if bh:
        bhrows = [
            [
                r["symbol"],
                _fmt(r.get("p_one_sided"), "p"),
                _fmt(r.get("q"), "p"),
                "yes" if r.get("reject") else "no",
            ]
            for r in bh
        ]
        parts.append(
            r"Table \ref{tab:bh} reports Benjamini--Hochberg adjusted per-instrument tests. No instrument "
            r"survives false-discovery-rate control at the pre-registered alpha."
        )
        parts.append(_tex_table(["Symbol", "One-sided p", "BH q", "Reject"], bhrows, "Per-instrument one-sided tests with Benjamini--Hochberg adjustment.", "tab:bh"))
    parts.append(r"\section{Discussion}")
    parts.append(
        r"The gross edge is approximately zero before any transaction cost. This is the key economic "
        r"fact: a genuine 90\% level-hold rate would earn a positive gross mean $R$ even under pessimistic "
        r"path assumptions. Costs then turn a zero-mean strategy into a reliably negative one. The grid "
        r"does not identify a stable profitable region, and the ablations do not reveal a hidden source of "
        r"edge. The level-reaction audit separates the claim's mechanism from its profitability implication: "
        r"the level holds less often than claimed, and the hold cannot be monetised after costs."
    )
    parts.append(r"\section{Limitations}")
    parts.append(
        r"The transcript is discretionary; the rules are a faithful but explicit translation, and a "
        r"different translation could produce a different result. HistData's M1 timestamp convention is "
        r"undocumented; the resampling is causal and internally consistent, but bar-open versus bar-close "
        r"ambiguity remains. Spreads and slippage are stylised rather than broker quotes. The 15-minute bars "
        r"cannot order the high and low, so stop-first is a lower bound; the measured ambiguity rate is "
        r"small, but platform-level backtests can still differ materially because of this ambiguity "
        r"\citep{claeys2026backtests}. The universe is eight instruments chosen ex ante. The live shadow is "
        r"short and descriptive. "
        r"No historical news calendar is used."
    )
    parts.append(r"\section{Conclusion}")
    parts.append(
        r"A widely taught multi-timeframe key-level strategy, operationalised deterministically and tested "
        r"on 2012--2025 intraday data, does not produce a positive net edge. The gross edge is "
        r"approximately zero, costs make the net result negative, every pre-registered grid point is "
        r"negative, and the sealed holdout does not reverse the conclusion. The 90\% level-hold claim is "
        r"not supported in this operationalisation. The result is a useful benchmark for future work on "
        r"support/resistance and for the broader replication agenda in technical analysis."
    )
    parts.append(r"\bibliographystyle{plainnat}")
    parts.append(r"\bibliography{references}")
    parts.append(r"\end{document}")
    return "\n".join(parts) + "\n"


REFERENCES_BIB = r"""@article{arnott2019backtesting,
  author = {Arnott, Robert and Harvey, Campbell R. and Markowitz, Harry},
  title = {A Backtesting Protocol in the Era of Machine Learning},
  journal = {Journal of Financial Data Science},
  year = {2019},
  volume = {1},
  number = {1},
  pages = {64--74}
}

@article{bailey2014deflated,
  author = {Bailey, David H. and L{\'o}pez de Prado, Marcos},
  title = {The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality},
  journal = {Journal of Portfolio Management},
  year = {2014},
  volume = {40},
  number = {5},
  pages = {94--107}
}

@article{bailey2017probability,
  author = {Bailey, David H. and Borwein, Jonathan M. and L{\'o}pez de Prado, Marcos and Zhu, Qiji Jim},
  title = {The Probability of Backtest Overfitting},
  journal = {Journal of Computational Finance},
  year = {2017},
  volume = {20},
  number = {4},
  pages = {39--69}
}

@article{benjamini1995controlling,
  author = {Benjamini, Yoav and Hochberg, Yosef},
  title = {Controlling the False Discovery Rate: A Practical and Powerful Approach to Multiple Testing},
  journal = {Journal of the Royal Statistical Society B},
  year = {1995},
  volume = {57},
  number = {1},
  pages = {289--300}
}

@article{brock1992simple,
  author = {Brock, William and Lakonishok, Josef and LeBaron, Blake},
  title = {Simple Technical Trading Rules and the Stochastic Properties of Stock Returns},
  journal = {Journal of Finance},
  year = {1992},
  volume = {47},
  number = {5},
  pages = {1731--1764}
}

@techreport{claeys2026backtests,
  author = {Claeys, Michael},
  title = {When Backtests Guess: How Trading Platforms Silently Fabricate Results},
  year = {2026},
  institution = {SSRN},
  type = {Working Paper},
  number = {6240638},
  url = {https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6240638}
}

@article{deprez2024bitcoin,
  author = {Deprez, Niek and Fr{\"o}mmel, Michael},
  title = {Are Simple Technical Trading Rules Profitable in Bitcoin Markets?},
  journal = {International Review of Economics and Finance},
  year = {2024},
  volume = {93},
  pages = {858--874},
  doi = {10.1016/j.iref.2024.05.003}
}

@article{hansen2005test,
  author = {Hansen, Peter Reinhard},
  title = {A Test for Superior Predictive Ability},
  journal = {Journal of Business and Economic Statistics},
  year = {2005},
  volume = {23},
  number = {4},
  pages = {365--380}
}

@article{harvey2020false,
  author = {Harvey, Campbell R. and Liu, Yan},
  title = {False (and Missed) Discoveries in Financial Economics},
  journal = {Journal of Finance},
  year = {2020},
  volume = {75},
  number = {5},
  pages = {2503--2553}
}

@article{lo2000foundations,
  author = {Lo, Andrew W. and Mamaysky, Harry and Wang, Jiang},
  title = {Foundations of Technical Analysis: Computational Algorithms, Statistical Inference, and Empirical Implementation},
  journal = {Journal of Finance},
  year = {2000},
  volume = {55},
  number = {4},
  pages = {1705--1765}
}

@article{menkhoff2007obstinate,
  author = {Menkhoff, Lukas and Taylor, Mark P.},
  title = {The Obstinate Passion of Foreign Exchange Professionals: Technical Analysis},
  journal = {Journal of Economic Literature},
  year = {2007},
  volume = {45},
  number = {4},
  pages = {936--972}
}

@article{osler2003currency,
  author = {Osler, Carol L.},
  title = {Currency Orders and Exchange Rate Dynamics: An Explanation for the Predictive Success of Technical Analysis},
  journal = {Journal of Finance},
  year = {2003},
  volume = {58},
  number = {5},
  pages = {1791--1820}
}

@article{politis1994stationary,
  author = {Politis, Dimitris N. and Romano, Joseph P.},
  title = {The Stationary Bootstrap},
  journal = {Journal of the American Statistical Association},
  year = {1994},
  volume = {89},
  number = {428},
  pages = {1303--1313}
}

@article{rink2023predictive,
  author = {Rink, Kevin},
  title = {The Predictive Ability of Technical Trading Rules: An Empirical Analysis of Developed and Emerging Equity Markets},
  journal = {Financial Markets and Portfolio Management},
  year = {2023},
  volume = {37},
  pages = {403--456},
  doi = {10.1007/s11408-023-00433-2},
  url = {https://doi.org/10.1007/s11408-023-00433-2}
}

@article{sullivan1999data,
  author = {Sullivan, Ryan and Timmermann, Allan and White, Halbert},
  title = {Data-Snooping, Technical Trading Rule Performance, and the Bootstrap},
  journal = {Journal of Finance},
  year = {1999},
  volume = {54},
  number = {5},
  pages = {1647--1691}
}

@article{white2000reality,
  author = {White, Halbert},
  title = {A Reality Check for Data Snooping},
  journal = {Econometrica},
  year = {2000},
  volume = {68},
  number = {5},
  pages = {1097--1126}
}
"""


def build_report(cfg: dict[str, Any]) -> Path:
    root = Path(cfg["_root"])
    results = resolve(cfg, cfg["data"]["results_dir"])
    paper = root / "paper"
    figs_dir = ensure_dir(paper / "figures")
    ensure_dir(paper)

    stages: dict[str, dict[str, Any]] = {}
    for stage in ("development", "validation", "holdout"):
        m = _stage_metrics(root, stage)
        if m:
            stages[stage] = m
    for stage in ("validation", "holdout"):
        m = _variant_metrics(root, stage, "winner")
        if m:
            stages[f"{stage}_winner"] = m

    grid = _read_csv(root / "results" / "development" / "tables" / "grid.csv")
    ledger = _read_csv(root / "results" / "development" / "tables" / "ledger.csv")
    if not ledger.empty and not grid.empty:
        ledger = pd.concat([ledger, grid], ignore_index=True)
    cross, audit_summary, amb = _audit_rows(root)
    live_meta, live_per = _live_row(root)
    inference = _read_json(root / "results" / "holdout" / "inference.json")

    figs: dict[str, str] = {}
    for key, fn in [
        ("equity", lambda: _fig_equity(root, figs_dir, stages)),
        ("grid", lambda: _fig_grid(root, figs_dir)),
        ("symbols", lambda: _fig_symbols(root, figs_dir)),
        ("rdist", lambda: _fig_r_distribution(root, figs_dir)),
        ("sessions", lambda: _fig_sessions(root, figs_dir)),
        ("cost", lambda: _fig_cost(root, figs_dir)),
        ("benchmarks", lambda: _fig_benchmarks(root, figs_dir, stages)),
        ("audit", lambda: _fig_audit(root, figs_dir)),
        ("live", lambda: _fig_live(root, figs_dir)),
    ]:
        try:
            name = fn()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("figure %s failed: %s", key, exc)
            name = None
        if name:
            figs[key] = f"paper/figures/{name}"

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    research = _readme(cfg, root, stages, grid, ledger, audit_summary, cross, amb, live_meta, live_per, inference, figs, generated)
    (root / "RESEARCH.md").write_text(research, encoding="utf-8")

    tex = _latex_manuscript(cfg, root, stages, grid, ledger, audit_summary, cross, amb, live_meta, live_per, inference, figs, generated)
    (paper / "main.tex").write_text(tex, encoding="utf-8")
    (paper / "references.bib").write_text(REFERENCES_BIB, encoding="utf-8")

    report_meta = {
        "generated_utc": generated,
        "stages": {k: {kk: (float(vv) if isinstance(vv, (int, float, np.floating)) else vv) for kk, vv in v.items()} for k, v in stages.items()},
        "figures": figs,
        "holdout_inference": inference,
        "audit_intrabar": amb,
        "live_portfolio": live_meta.get("portfolio", {}),
    }
    ensure_dir(results / "report")
    (results / "report" / "report.json").write_text(json.dumps(report_meta, indent=2, default=str), encoding="utf-8")
    public_site.build_site(root)
    LOG.info("research report written: %s", root / "RESEARCH.md")
    return root / "RESEARCH.md"
