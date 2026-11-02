"""Build the public README, interactive report, and GitHub Pages entry point.

``RESEARCH.md`` remains the long-form, reproducible paper.  ``README.md`` and
``preview.html`` explain the same verified artefacts to a non-specialist
audience.  Every number is read from ``results/``; nothing is typed by hand.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

LOG = logging.getLogger(__name__)

INK = "#0b1220"
MUTED = "#64748b"
GRID = "#dbe4f0"
GOLD = "#d79b2b"
TEAL = "#0f766e"
CORAL = "#d94f45"
BLUE = "#3b6ea8"

STAGE_FILES = {
    "Development": "results/development/tables/daily_base.csv",
    "Validation": "results/validation/tables/daily_base.csv",
    "Holdout": "results/holdout/tables/daily_base.csv",
}
STAGE_COLORS = {"Development": GOLD, "Validation": BLUE, "Holdout": CORAL}
BENCH_LABELS = {
    "buy_and_hold": "Buy & hold",
    "sma_50_200": "SMA 50/200",
    "trend_only": "Trend filter",
    "donchian_20": "Donchian 20",
}


def _num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(number):
        return "—"
    return f"{number:,.{digits}f}"


def _signed(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(number):
        return "—"
    return f"{number:+,.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(number):
        return "—"
    return f"{100 * number:.{digits}f}%"


def _p(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(number):
        return "—"
    if number == 0:
        return "0.0000"
    if abs(number) < 1e-3:
        return f"{number:.2e}"
    return f"{number:.4f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:  # noqa: BLE001
        LOG.warning("could not read %s", path)
        return pd.DataFrame()


def _load(root: Path) -> dict[str, Any]:
    report = json.loads((root / "results/report/report.json").read_text())
    return {
        "report": report,
        "grid": _read_csv(root / "results/development/tables/grid.csv"),
        "costs": _read_csv(root / "results/holdout/tables/costs.csv"),
        "bench": _read_csv(root / "results/holdout/tables/benchmarks.csv"),
        "audit": _read_csv(root / "results/audit/tables/level_reaction_summary.csv"),
        "reactions": _read_csv(root / "results/audit/tables/level_reactions.csv"),
        "integrity": _read_csv(root / "results/audit/tables/integrity.csv"),
        "cross": _read_csv(root / "results/audit/tables/cross_validation.csv"),
        "symbols": _read_csv(root / "results/live/tables/per_symbol.csv"),
        "trades": _read_csv(root / "results/holdout/tables/trades_base.csv"),
        "freeze": json.loads((root / "CODE_FREEZE.json").read_text()),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    return value


def _svg(title: str, desc: str, width: int, height: int, body: str, slug: str) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="{slug}-title {slug}-desc" preserveAspectRatio="xMidYMid meet">'
        f'<title id="{slug}-title">{html.escape(title)}</title>'
        f'<desc id="{slug}-desc">{html.escape(desc)}</desc>'
        f"{body}</svg>"
    )


def _text(x: float, y: float, value: str, **attrs: Any) -> str:
    payload = " ".join(f'{key.replace("_", "-")}="{html.escape(str(val))}"' for key, val in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" {payload}>{html.escape(value)}</text>'


def _figure(svg: str, title: str, caption: str) -> str:
    return (
        f'<figure class="chart-figure">{svg}'
        f"<figcaption><strong>{html.escape(title)}</strong> {html.escape(caption)}</figcaption></figure>"
    )


def _svg_equity(root: Path) -> str:
    series: list[tuple[str, np.ndarray]] = []
    for name, rel in STAGE_FILES.items():
        frame = _read_csv(root / rel)
        if frame.empty or "return" not in frame:
            continue
        returns = frame["return"].astype(float).to_numpy()
        equity = np.concatenate([[1.0], np.cumprod(1.0 + returns)])
        series.append((name, equity))
    if not series:
        return ""

    logs = [np.log10(np.clip(equity, 1e-7, None)) for _, equity in series]
    all_logs = np.concatenate(logs)
    low = float(np.floor(all_logs.min() * 2) / 2) - 0.2
    high = float(np.ceil(all_logs.max() * 2) / 2) + 0.2
    width, height = 980, 420
    top, bottom, left, right = 34, 340, 74, 906
    gap = 20
    counts = [max(len(equity) - 1, 1) for _, equity in series]
    usable = (right - left) - gap * (len(series) - 1)
    cursor = left

    def ypos(value: float) -> float:
        return top + (high - value) / (high - low) * (bottom - top)

    body: list[str] = []
    for step in np.linspace(low, high, 6):
        y = ypos(float(step))
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        label = f"{10 ** step:.2g}x"
        body.append(_text(left - 10, y + 4, label, text_anchor="end", font_size="10", fill=MUTED))

    labels: list[tuple[float, float, str, str]] = []
    for index, (name, equity) in enumerate(series):
        span = usable * counts[index] / sum(counts)
        x0 = cursor
        x1 = x0 + span
        xs = np.linspace(x0, x1, len(equity))
        points = " ".join(f"{x:.1f},{ypos(float(value)):.1f}" for x, value in zip(xs, logs[index]))
        color = STAGE_COLORS[name]
        body.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        end = ypos(float(logs[index][-1]))
        body.append(f'<circle cx="{x1:.1f}" cy="{end:.1f}" r="4" fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        labels.append((x1, end, f"{equity[-1]:.3g}x", color))
        body.append(_text((x0 + x1) / 2, bottom + 24, name, text_anchor="middle", font_size="11", font_weight="700", fill=INK))
        cursor = x1 + gap

    labels.sort(key=lambda item: item[1])
    for i in range(1, len(labels)):
        if labels[i][1] < labels[i - 1][1] + 15:
            labels[i] = (labels[i][0], labels[i - 1][1] + 15, labels[i][2], labels[i][3])
    for x, y, value, color in labels:
        body.append(_text(x + 9, y + 4, value, font_size="11", font_weight="700", fill=color))

    body.append(_text(left, 18, "Growth of $1, log scale", font_size="11", font_weight="700", fill=INK))
    svg = _svg(
        "Equity curves for development, validation, and holdout",
        "All three samples lose money. The holdout ends near zero, confirming the development result instead of reversing it.",
        width,
        height,
        "".join(body),
        "equity",
    )
    return _figure(svg, "Equity, 2012–2025", "A $1 stake declines in every sample. The line is plotted on a log scale so the scale of the loss is visible.")


def _svg_grid(grid: pd.DataFrame) -> str:
    if grid.empty:
        return ""
    rows = grid.sort_values("sharpe", ascending=False).to_dict("records")
    width = 980
    height = 104 + 25 * len(rows)
    label_right, zero_x = 250, 720
    values = [float(row["sharpe"]) for row in rows]
    max_abs = max(abs(value) for value in values) or 1.0
    scale = (zero_x - label_right - 22) / max_abs
    body: list[str] = [
        f'<line x1="{zero_x}" y1="42" x2="{zero_x}" y2="{height - 24}" stroke="{INK}" stroke-width="1"/>',
        _text(label_right, 24, "Cluster ATR / touches / confirms", font_size="10", fill=MUTED),
        _text(zero_x + 8, 24, "Net Sharpe", font_size="10", fill=MUTED),
    ]
    for i, row in enumerate(rows):
        y = 50 + i * 25
        value = float(row["sharpe"])
        color = GOLD if i == 0 else CORAL
        left = zero_x - abs(value) * scale
        body.append(f'<rect x="{left:.1f}" y="{y:.1f}" width="{abs(value) * scale:.1f}" height="16" rx="2" fill="{color}" opacity="{0.95 if i == 0 else 0.7}"/>')
        label = f'{float(row["cluster_atr"]):.2f} / {int(row["min_touches"])} / {int(row["confirmation_closes"])}'
        body.append(_text(label_right - 8, y + 12, label, text_anchor="end", font_size="11", fill=INK))
        body.append(_text(zero_x + 8, y + 12, f"{value:+.2f}", font_size="11", font_weight="700" if i == 0 else "400", fill=color if i == 0 else MUTED))
    svg = _svg(
        "Every grid variant has a negative net Sharpe ratio",
        "Horizontal bars show the development-sample net Sharpe ratio for all eighteen pre-registered parameter combinations. The least-bad variant is still negative.",
        width,
        height,
        "".join(body),
        "grid",
    )
    return _figure(svg, "The whole parameter grid fails", "Each row is one pre-registered combination. Gold is the least-bad variant; it still loses after costs.")


def _svg_costs(costs: pd.DataFrame) -> str:
    if costs.empty:
        return ""
    rows = costs.sort_values("cost_multiplier").to_dict("records")
    width, height = 900, 76 + 46 * len(rows)
    label_right, zero_x = 230, 610
    max_abs = max(abs(float(row["avg_r"])) for row in rows) or 1.0
    scale = (zero_x - label_right - 24) / max_abs
    body: list[str] = [
        f'<line x1="{zero_x}" y1="42" x2="{zero_x}" y2="{height - 28}" stroke="{INK}" stroke-width="1"/>',
        _text(label_right, 24, "Cost multiplier", font_size="10", fill=MUTED),
        _text(zero_x + 8, 24, "Mean net R", font_size="10", fill=MUTED),
    ]
    for i, row in enumerate(rows):
        y = 52 + i * 46
        value = float(row["avg_r"])
        mult = float(row["cost_multiplier"])
        color = GOLD if abs(mult - 1.0) < 1e-9 else CORAL
        left = zero_x - abs(value) * scale
        body.append(f'<rect x="{left:.1f}" y="{y:.1f}" width="{abs(value) * scale:.1f}" height="24" rx="3" fill="{color}" opacity="0.86"/>')
        label = f"{mult:g}× modelled cost"
        body.append(_text(label_right - 8, y + 16, label, text_anchor="end", font_size="11", fill=INK))
        body.append(_text(zero_x + 8, y + 16, f"{value:+.3f}R", font_size="11", font_weight="700", fill=color))
        body.append(_text(zero_x + 86, y + 16, f"Sharpe {float(row['sharpe']):+.2f}", font_size="10", fill=MUTED))
    svg = _svg(
        "Cost sensitivity of the holdout result",
        "Mean net R becomes more negative as the cost assumption rises. Even at zero cost the result is negative.",
        width,
        height,
        "".join(body),
        "costs",
    )
    return _figure(svg, "Costs make a weak result worse", "The gold bar is the pre-registered cost assumption. Zero-cost trading is shown only as a stress test, not as a realistic scenario.")


def _svg_hist(trades: pd.DataFrame) -> str:
    if trades.empty or "r_net" not in trades:
        return ""
    values = trades["r_net"].astype(float).clip(-3, 3).to_numpy()
    counts, _ = np.histogram(values, bins=np.linspace(-3, 3, 31))
    width, height = 760, 320
    left, right, top, bottom = 58, 728, 34, 258
    max_count = int(counts.max()) or 1
    bar_width = (right - left) / len(counts)
    body: list[str] = [
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="{INK}" stroke-width="1"/>',
        _text(left, 20, "Holdout trades by net R", font_size="11", font_weight="700", fill=INK),
    ]
    for i, count in enumerate(counts):
        bar_height = (bottom - top) * count / max_count
        x = left + i * bar_width
        body.append(f'<rect x="{x + 1:.1f}" y="{bottom - bar_height:.1f}" width="{bar_width - 2:.1f}" height="{bar_height:.1f}" fill="{TEAL}" opacity="0.72"/>')
    mean = float(trades["r_net"].mean())
    mean_x = left + (mean + 3) / 6 * (right - left)
    body.append(f'<line x1="{mean_x:.1f}" y1="{top}" x2="{mean_x:.1f}" y2="{bottom}" stroke="{CORAL}" stroke-width="2" stroke-dasharray="4 4"/>')
    body.append(_text(mean_x + 6, top + 12, f"mean {mean:+.3f}R", font_size="11", font_weight="700", fill=CORAL))
    for tick in (-3, -2, -1, 0, 1, 2, 3):
        x = left + (tick + 3) / 6 * (right - left)
        body.append(_text(x, bottom + 18, f"{tick:+d}" if tick else "0", text_anchor="middle", font_size="10", fill=MUTED))
    body.append(_text((left + right) / 2, height - 4, "Net R (values clipped at ±3R)", text_anchor="middle", font_size="10", fill=MUTED))
    svg = _svg(
        "Distribution of holdout trade outcomes",
        f"Most holdout trades stop out near minus one R. The mean is negative {abs(mean):.3f} R.",
        width,
        height,
        "".join(body),
        "hist",
    )
    return _figure(svg, "Most trades lose about 1R", "The distribution is centred left of zero. A few large winners do not rescue the average.")


def _svg_bench(bench: pd.DataFrame, holdout_sharpe: float) -> str:
    if bench.empty:
        return ""
    values = {str(row["variant"]): float(row["sharpe"]) for row in bench.to_dict("records")}
    values["key_levels"] = holdout_sharpe
    order = ["buy_and_hold", "sma_50_200", "trend_only", "donchian_20", "key_levels"]
    width, height = 920, 84 + 40 * len(order)
    label_right, zero_x = 240, 600
    max_abs = max(abs(value) for value in values.values()) or 1.0
    scale = (width - zero_x - 30) / max_abs
    body: list[str] = [
        f'<line x1="{zero_x}" y1="40" x2="{zero_x}" y2="{height - 28}" stroke="{INK}" stroke-width="1"/>',
        _text(label_right, 22, "Strategy", font_size="10", fill=MUTED),
        _text(zero_x + 8, 22, "Holdout Sharpe", font_size="10", fill=MUTED),
    ]
    for i, key in enumerate(order):
        value = values.get(key, 0.0)
        y = 50 + i * 40
        color = CORAL if key == "key_levels" else (TEAL if value >= 0 else BLUE)
        if value >= 0:
            left, width_bar = zero_x, value * scale
        else:
            left, width_bar = zero_x - abs(value) * scale, abs(value) * scale
        body.append(f'<rect x="{left:.1f}" y="{y:.1f}" width="{max(width_bar, 1):.1f}" height="22" rx="3" fill="{color}" opacity="0.84"/>')
        name = "Key levels (holdout)" if key == "key_levels" else BENCH_LABELS.get(key, key)
        body.append(_text(label_right - 8, y + 15, name, text_anchor="end", font_size="11", font_weight="700" if key == "key_levels" else "400", fill=INK))
        anchor = "start" if value >= 0 else "end"
        tx = left + width_bar + 8 if value >= 0 else left - 8
        body.append(_text(tx, y + 15, f"{value:+.2f}", text_anchor=anchor, font_size="11", font_weight="700", fill=color))
    svg = _svg(
        "Holdout Sharpe ratios for benchmarks and the key-level rule",
        "Buy and hold and one simple trend benchmark are positive in this sample. The key-level rule is the worst of the five.",
        width,
        height,
        "".join(body),
        "bench",
    )
    return _figure(svg, "Simple benchmarks did better", "The key-level rule is compared with buy-and-hold and three simple technical rules on the same holdout window and cost model.")


def _svg_hold(audit: pd.DataFrame) -> str:
    if audit.empty:
        return ""
    row = audit.iloc[0] if "level_tf" not in audit else audit.loc[audit["level_tf"] == "ALL"].iloc[0]
    items = [
        ("Strict hold (no close through level)", float(row["hold_rate"]), CORAL),
        ("Reached +1R before −1R", float(row["hit_1r_before_stop"]), GOLD),
        ("Reached +2R before −1R", float(row["hit_2r_before_stop"]), TEAL),
        ("Reached +3R before −1R", float(row["hit_3r_before_stop"]), BLUE),
    ]
    width, height = 900, 270
    label_right, zero_x = 430, 610
    scale = (width - zero_x - 40) / 1.0
    body: list[str] = [
        f'<line x1="{zero_x}" y1="38" x2="{zero_x}" y2="{height - 24}" stroke="{INK}" stroke-width="1"/>',
        _text(label_right, 22, "Share of executed signals", font_size="10", fill=MUTED),
    ]
    for i, (label, value, color) in enumerate(items):
        y = 48 + i * 46
        bar_width = max(value * scale, 1.0)
        body.append(f'<rect x="{zero_x}" y="{y}" width="{bar_width:.1f}" height="26" rx="3" fill="{color}" opacity="0.84"/>')
        body.append(_text(label_right - 8, y + 17, label, text_anchor="end", font_size="11", fill=INK))
        body.append(_text(zero_x + bar_width + 8, y + 17, f"{100 * value:.1f}%", font_size="11", font_weight="700", fill=color))
    svg = _svg(
        "Level reaction audit",
        f"Only {100 * float(row['hold_rate']):.1f} percent of executed signals held without a close through the level. Even the weaker one-R measure reached only {100 * float(row['hit_1r_before_stop']):.1f} percent.",
        width,
        height,
        "".join(body),
        "hold",
    )
    return _figure(svg, "The level does not hold", "A strict hold means price never closes through the level during the next 96 bars. The +1R measure is weaker and still close to a coin flip.")


def _tokens(root: Path, data: dict[str, Any]) -> dict[str, str]:
    report = data["report"]
    stages = report["stages"]
    dev, val, hold = stages["development"], stages["validation"], stages["holdout"]
    val_win, hold_win = stages["validation_winner"], stages["holdout_winner"]
    inf = report["holdout_inference"]
    boot = inf["bootstrap"]
    placebo = inf["placebos"]
    grid = data["grid"]
    costs = data["costs"]
    bench = data["bench"]
    audit = data["audit"]
    reactions = data["reactions"]
    integrity = data["integrity"]
    cross = data["cross"]
    symbols = data["symbols"]
    trades = data["trades"]
    freeze = data["freeze"]
    live = report["live_portfolio"]

    raw_generated = str(report["generated_utc"]).strip()
    try:
        generated = datetime.fromisoformat(raw_generated.replace("Z", "+00:00"))
    except ValueError:
        generated = datetime.strptime(raw_generated, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    best = grid.loc[grid["sharpe"].idxmax()] if not grid.empty else None
    rl = placebo["random_level"]
    rl_draws = int(rl.get("n_draws") or 200)
    rl_p = float(rl.get("pvalue") or 0.0)
    rl_display = (
        f"&lt; {1.0 / (rl_draws + 1.0):.4f} ({int(round(rl_p * rl_draws))}/{rl_draws})"
        if rl_p == 0
        else _p(rl_p)
    )
    audit_row = audit.loc[audit["level_tf"] == "ALL"].iloc[0] if not audit.empty and "level_tf" in audit else None
    if audit_row is None and not reactions.empty:
        n_reactions = len(reactions)
        audit_row = {
            "n": n_reactions,
            "hold_rate": float(reactions["hold"].mean()),
            "hit_1r_before_stop": float(reactions["hit_1r"].mean()),
            "hit_2r_before_stop": float(reactions["hit_2r"].mean()),
            "hit_3r_before_stop": float(reactions["hit_3r"].mean()),
            "mean_mfe_r": float(reactions["mfe_r"].mean()),
        }
    if audit_row is None:
        raise RuntimeError("level reaction audit is missing; run `make audit` before `make report`")

    bars = int(integrity["rows"].sum()) if not integrity.empty else 0
    max15 = float(integrity.loc[integrity["timeframe"] == "15min", "max_abs_log_return"].max()) if not integrity.empty else 0.0
    dup = int(integrity["duplicate_ts"].sum()) if not integrity.empty else 0
    viol = int(integrity["ohlc_violations"].sum()) if not integrity.empty else 0
    nonpos = int(integrity["nonpositive"].sum()) if not integrity.empty else 0
    cross_min = float(cross["corr"].min()) if not cross.empty else 0.0
    cross_max = float(cross["corr"].max()) if not cross.empty else 0.0
    nsx = next((row for row in inf["per_instrument_bh"] if row["symbol"] == "NSXUSD"), {})
    n_trials = int(inf.get("n_trials", 0))
    total_trades = int(dev["n_trades"] + val["n_trades"] + hold["n_trades"])
    cost_drag = abs(float(dev["gross_r"]) - float(dev["avg_r"]))

    headline = _table(
        ["Metric", "Development 2012–19", "Validation 2020–22", "Sealed holdout 2023–25"],
        [
            ["Trades", f'{int(dev["n_trades"]):,}', f'{int(val["n_trades"]):,}', f'{int(hold["n_trades"]):,}'],
            ["Mean gross R", _signed(dev["gross_r"], 3), _signed(val["gross_r"], 3), _signed(hold["gross_r"], 3)],
            ["Mean net R", _signed(dev["avg_r"], 3), _signed(val["avg_r"], 3), _signed(hold["avg_r"], 3)],
            ["Annualised net Sharpe", _num(dev["sharpe"]), _num(val["sharpe"]), _num(hold["sharpe"])],
            ["Maximum drawdown", _pct(dev["max_drawdown"], 2), _pct(val["max_drawdown"], 2), _pct(hold["max_drawdown"], 2)],
            ["Win rate", _pct(dev["win_rate"]), _pct(val["win_rate"]), _pct(hold["win_rate"])],
            ["Profit factor", _num(dev["profit_factor"]), _num(val["profit_factor"]), _num(hold["profit_factor"])],
        ],
    )
    grid_table = _table(
        ["Cluster ATR", "Touches", "Confirms", "Trades", "Mean net R", "Net Sharpe"],
        [
            [
                f'{float(row["cluster_atr"]):.2f}',
                str(int(row["min_touches"])),
                str(int(row["confirmation_closes"])),
                f'{int(row["n_trades"]):,}',
                _signed(row["avg_r"], 3),
                _num(row["sharpe"]),
            ]
            for row in grid.sort_values("sharpe", ascending=False).to_dict("records")
        ],
    )
    cost_table = _table(
        ["Cost multiplier", "Trades", "Mean net R", "Net Sharpe", "Maximum drawdown"],
        [
            [
                f'{float(row["cost_multiplier"]):g}×',
                f'{int(row["n_trades"]):,}',
                _signed(row["avg_r"], 3),
                _num(row["sharpe"]),
                _pct(row["max_drawdown"], 2),
            ]
            for row in costs.sort_values("cost_multiplier").to_dict("records")
        ],
    )
    bench_rows = [
        [
            "Key levels (holdout)" if row["variant"] == "key_levels" else BENCH_LABELS.get(row["variant"], row["variant"]),
            _pct(row["ann_return"]),
            _num(row["sharpe"]),
            _pct(row["max_drawdown"], 2),
        ]
        for row in bench.to_dict("records")
    ]
    bench_rows.append(["Key levels (holdout)", _pct(hold["ann_return"]), _num(hold["sharpe"]), _pct(hold["max_drawdown"], 2)])
    bench_table = _table(["Strategy", "Annualised return", "Net Sharpe", "Maximum drawdown"], bench_rows)
    inference_table = _table(
        ["Check", "Result", "What it means"],
        [
            ["Deflated Sharpe, base", _p(inf["dsr_base"]["dsr"]), f"No evidence after {n_trials} trials"],
            ["Deflated Sharpe, grid winner", _p(inf["dsr_winner"]["dsr"]), "No evidence for the best variant"],
            ["Probability of backtest overfitting", _num(inf["pbo"]["pbo"], 4), "The ranking is not a stable edge"],
            ["Reality Check", _p(inf["reality_check"]["pvalue"]), "Cannot reject the benchmark"],
            ["Superior Predictive Ability", _p(inf["spa"]["pvalue"]), "Cannot reject the benchmark"],
            ["Alpha vs equal-weight basket", f'{_pct(inf["alpha_beta_base"]["alpha_ann"])} a year, p = {_p(inf["alpha_beta_base"]["p_alpha"])}', "Negative and significant"],
            ["Bootstrap 95% CI, mean net R", f'[{_num(boot["mean_r"]["ci_low"], 3)}, {_num(boot["mean_r"]["ci_high"], 3)}]', "Entire interval below zero"],
            ["Bootstrap 95% CI, net Sharpe", f'[{_num(boot["sharpe"]["ci_low"])}, {_num(boot["sharpe"]["ci_high"])}]', "Entire interval below zero"],
            ["Random-entry placebo", f'p = {_p(placebo["random_entry"]["pvalue"])}', "The rule is not better than random entries"],
            ["Random-level placebo", rl_display, "Actual levels are less bad than random levels"],
            ["Benjamini–Hochberg, NSXUSD", f'q = {_p(nsx.get("q"))}', "Not significant after correction"],
        ],
    )
    audit_table = _table(
        ["Reaction measure", "Share", "Plain reading"],
        [
            ["Strict hold, 96 bars", _pct(audit_row["hold_rate"]), "Level survived without a close through it"],
            ["+1R before −1R", _pct(audit_row["hit_1r_before_stop"]), "Slightly worse than a coin flip"],
            ["+2R before −1R", _pct(audit_row["hit_2r_before_stop"]), "Roughly one in three"],
            ["+3R before −1R", _pct(audit_row["hit_3r_before_stop"]), "Roughly one in five"],
        ],
    )
    hold_symbols = (
        trades.groupby("symbol")
        .agg(n_trades=("r_net", "size"), mean_r=("r_net", "mean"), win_rate=("r_net", lambda s: float((s > 0).mean())))
        .reset_index()
        .sort_values("mean_r")
    )
    symbol_table = _table(
        ["Symbol", "Holdout trades", "Mean net R", "Win rate"],
        [[row["symbol"], f'{int(row["n_trades"]):,}', _signed(row["mean_r"], 3), _pct(row["win_rate"])] for row in hold_symbols.to_dict("records")],
    )
    live_table = _table(
        ["Symbol", "Live shadow trades", "Mean net R", "Win rate"],
        [[row["symbol"], f'{int(row["n_trades"]):,}', _signed(row["avg_r"], 3), _pct(row["win_rate"])] for row in symbols.to_dict("records")],
    )
    cross_table = _table(
        ["Instrument", "Weeks compared", "Return correlation", "Note"],
        [
            [row["symbol"], f'{int(row["n_weeks"]):,}', f'{float(row["corr"]):.2f}', "Weekly close-to-close"]
            for row in cross.to_dict("records")
        ],
    )
    integrity_table = _table(
        ["Timeframe", "Series", "Bars", "Duplicate timestamps", "OHLC violations", "Non-positive prices"],
        [
            [
                row["timeframe"],
                f'{int(row["series"]):,}',
                f'{int(row["rows"]):,}',
                f'{int(row["duplicate_ts"]):,}',
                f'{int(row["ohlc_violations"]):,}',
                f'{int(row["nonpositive"]):,}',
            ]
            for row in integrity.groupby("timeframe")
            .agg(series=("symbol", "count"), rows=("rows", "sum"), duplicate_ts=("duplicate_ts", "sum"), ohlc_violations=("ohlc_violations", "sum"), nonpositive=("nonpositive", "sum"))
            .reset_index()
            .sort_values("rows", ascending=False)
            .to_dict("records")
        ],
    )

    pages_url = os.environ.get("KL_PAGES_URL", "").strip().rstrip("/")
    preview_url = pages_url or "preview.html"
    preview_label = "Open the live interactive report" if pages_url else "Open the interactive report"

    tokens = {
        "GENERATED_DATE": generated.strftime("%d %B %Y"),
        "GENERATED_ISO": generated.strftime("%Y-%m-%d"),
        "HEADLINE_TABLE": headline,
        "GRID_TABLE": grid_table,
        "COST_TABLE": cost_table,
        "BENCH_TABLE": bench_table,
        "INFERENCE_TABLE": inference_table,
        "AUDIT_TABLE": audit_table,
        "SYMBOL_TABLE": symbol_table,
        "LIVE_TABLE": live_table,
        "CROSS_TABLE": cross_table,
        "INTEGRITY_TABLE": integrity_table,
        "FREEZE_HASH": freeze["hash"],
        "FREEZE_SHORT": freeze["hash"][:12],
        "FREEZE_FILES": str(freeze["n_files"]),
        "DEV_TRADES": f'{int(dev["n_trades"]):,}',
        "DEV_GROSS_R": _signed(dev["gross_r"], 3),
        "DEV_NET_R": _signed(dev["avg_r"], 3),
        "DEV_SHARPE": _num(dev["sharpe"]),
        "DEV_DD": _pct(dev["max_drawdown"], 2),
        "DEV_DD_ABS": _pct(abs(float(dev["max_drawdown"])), 2),
        "DEV_WIN": _pct(dev["win_rate"]),
        "DEV_PF": _num(dev["profit_factor"]),
        "DEV_STOP": _pct(dev["stop_rate"]),
        "DEV_TARGET": _pct(dev["target_rate"]),
        "DEV_TIME": _pct(dev["time_rate"]),
        "COST_DRAG": _num(cost_drag, 3),
        "VAL_TRADES": f'{int(val["n_trades"]):,}',
        "VAL_GROSS_R": _signed(val["gross_r"], 3),
        "VAL_NET_R": _signed(val["avg_r"], 3),
        "VAL_SHARPE": _num(val["sharpe"]),
        "VAL_DD": _pct(val["max_drawdown"], 2),
        "VAL_WIN": _pct(val["win_rate"]),
        "VAL_WINNER_TRADES": f'{int(val_win["n_trades"]):,}',
        "VAL_WINNER_R": _signed(val_win["avg_r"], 3),
        "VAL_WINNER_SHARPE": _num(val_win["sharpe"]),
        "HOLD_TRADES": f'{int(hold["n_trades"]):,}',
        "HOLD_GROSS_R": _signed(hold["gross_r"], 3),
        "HOLD_NET_R": _signed(hold["avg_r"], 3),
        "HOLD_SHARPE": _num(hold["sharpe"]),
        "HOLD_DD": _pct(hold["max_drawdown"], 2),
        "HOLD_DD_ABS": _pct(abs(float(hold["max_drawdown"])), 2),
        "HOLD_WIN": _pct(hold["win_rate"]),
        "HOLD_PF": _num(hold["profit_factor"]),
        "HOLD_STOP": _pct(hold["stop_rate"]),
        "HOLD_TARGET": _pct(hold["target_rate"]),
        "HOLD_TIME": _pct(hold["time_rate"]),
        "HOLD_T": _num(hold["t_stat"]),
        "HOLD_P": _p(hold["p_value"]),
        "HOLD_WINNER_TRADES": f'{int(hold_win["n_trades"]):,}',
        "HOLD_WINNER_R": _signed(hold_win["avg_r"], 3),
        "HOLD_WINNER_SHARPE": _num(hold_win["sharpe"]),
        "GRID_VARIANTS": str(len(grid)),
        "GRID_BEST": _num(best["sharpe"]) if best is not None else "—",
        "GRID_BEST_LABEL": f'{float(best["cluster_atr"]):.2f} / {int(best["min_touches"])} / {int(best["confirmation_closes"])}' if best is not None else "—",
        "BARS": f"{bars:,}",
        "SERIES": str(len(integrity)),
        "MAX15": _pct(max15),
        "DUP": f"{dup:,}",
        "VIOL": f"{viol:,}",
        "NONPOS": f"{nonpos:,}",
        "CROSS_MIN": f"{cross_min:.2f}",
        "CROSS_MAX": f"{cross_max:.2f}",
        "BOOT_MEAN_LO": _num(boot["mean_r"]["ci_low"], 3),
        "BOOT_MEAN_HI": _num(boot["mean_r"]["ci_high"], 3),
        "BOOT_SHARPE_LO": _num(boot["sharpe"]["ci_low"]),
        "BOOT_SHARPE_HI": _num(boot["sharpe"]["ci_high"]),
        "BOOT_N": f'{int(boot["mean_r"]["n_boot"]):,}',
        "BOOT_BLOCK": str(int(boot["mean_r"]["mean_block"])),
        "DSR_BASE": _p(inf["dsr_base"]["dsr"]),
        "DSR_WIN": _p(inf["dsr_winner"]["dsr"]),
        "N_TRIALS": str(n_trials),
        "PBO": _num(inf["pbo"]["pbo"], 4),
        "PBO_SPLITS": f'{int(inf["pbo"]["n_splits"]):,}',
        "RC_P": _p(inf["reality_check"]["pvalue"]),
        "SPA_P": _p(inf["spa"]["pvalue"]),
        "ALPHA": _pct(inf["alpha_beta_base"]["alpha_ann"]),
        "ALPHA_P": _p(inf["alpha_beta_base"]["p_alpha"]),
        "ALPHA_WIN": _pct(inf["alpha_beta_winner"]["alpha_ann"]),
        "ALPHA_WIN_P": _p(inf["alpha_beta_winner"]["p_alpha"]),
        "RE_P": _p(placebo["random_entry"]["pvalue"]),
        "RL_P": _p(placebo["random_level"]["pvalue"]),
        "RL_DISPLAY": rl_display,
        "BH_NSX_RAW": _p(nsx.get("p_one_sided")),
        "BH_NSX_Q": _p(nsx.get("q")),
        "AUDIT_N": f'{int(audit_row["n"]):,}',
        "HOLD_RATE": _pct(audit_row["hold_rate"]),
        "HIT1": _pct(audit_row["hit_1r_before_stop"]),
        "HIT2": _pct(audit_row["hit_2r_before_stop"]),
        "HIT3": _pct(audit_row["hit_3r_before_stop"]),
        "MFE": _signed(audit_row["mean_mfe_r"], 3),
        "LIVE_TRADES": f'{int(live["n_trades"]):,}',
        "LIVE_R": _signed(live["mean_r"], 3),
        "LIVE_WIN": _pct(live["win_rate"]),
        "LIVE_PF": _num(live["profit_factor"]),
        "TOTAL_TRADES": f"{total_trades:,}",
        "PAGES_URL": preview_url,
        "PREVIEW_URL": preview_url,
        "PREVIEW_LABEL": preview_label,
        "PAGES_BADGE": "live site" if pages_url else "static HTML",
    }
    return tokens


def _preview_payload(root: Path, data: dict[str, Any], tokens: dict[str, str]) -> dict[str, Any]:
    report = data["report"]
    stages = report["stages"]
    audit = data["audit"]
    audit_row = audit.loc[audit["level_tf"] == "ALL"].iloc[0].to_dict() if not audit.empty and "level_tf" in audit else {}
    integrity = data["integrity"]
    integrity_rows = (
        integrity.groupby("timeframe")
        .agg(series=("symbol", "count"), rows=("rows", "sum"), duplicate_ts=("duplicate_ts", "sum"), ohlc_violations=("ohlc_violations", "sum"), nonpositive=("nonpositive", "sum"))
        .reset_index()
        .to_dict("records")
        if not integrity.empty
        else []
    )
    trades = data["trades"]
    hold_symbols = (
        trades.groupby("symbol")
        .agg(n_trades=("r_net", "size"), mean_r=("r_net", "mean"), win_rate=("r_net", lambda s: float((s > 0).mean())))
        .reset_index()
        .sort_values("mean_r")
        .to_dict("records")
        if not trades.empty
        else []
    )
    return {
        "generated": report["generated_utc"],
        "freeze": data["freeze"]["hash"][:12],
        "n_trials": int(report["holdout_inference"].get("n_trials", 0)),
        "stages": {
            key: {
                "n_trades": int(value["n_trades"]),
                "gross_r": float(value["gross_r"]),
                "avg_r": float(value["avg_r"]),
                "ann_return": float(value["ann_return"]),
                "sharpe": float(value["sharpe"]),
                "max_drawdown": float(value["max_drawdown"]),
                "win_rate": float(value["win_rate"]),
                "profit_factor": float(value["profit_factor"]),
                "stop_rate": float(value["stop_rate"]),
                "target_rate": float(value["target_rate"]),
                "time_rate": float(value["time_rate"]),
            }
            for key, value in stages.items()
        },
        "grid": [
            {
                "cluster_atr": float(row["cluster_atr"]),
                "min_touches": int(row["min_touches"]),
                "confirmation_closes": int(row["confirmation_closes"]),
                "n_trades": int(row["n_trades"]),
                "avg_r": float(row["avg_r"]),
                "sharpe": float(row["sharpe"]),
            }
            for row in data["grid"].sort_values("sharpe", ascending=False).to_dict("records")
        ],
        "costs": [
            {
                "multiplier": float(row["cost_multiplier"]),
                "n_trades": int(row["n_trades"]),
                "avg_r": float(row["avg_r"]),
                "sharpe": float(row["sharpe"]),
                "max_drawdown": float(row["max_drawdown"]),
            }
            for row in data["costs"].sort_values("cost_multiplier").to_dict("records")
        ],
        "bench": [
            {
                "variant": str(row["variant"]),
                "label": BENCH_LABELS.get(str(row["variant"]), str(row["variant"]).replace("_", " ")),
                "ann_return": float(row["ann_return"]),
                "sharpe": float(row["sharpe"]),
                "max_drawdown": float(row["max_drawdown"]),
            }
            for row in data["bench"].to_dict("records")
        ],
        "audit": {key: (float(value) if isinstance(value, (int, float, np.floating)) else value) for key, value in audit_row.items()},
        "integrity": integrity_rows,
        "hold_symbols": hold_symbols,
        "symbols": data["symbols"].to_dict("records"),
        "live": report["live_portfolio"],
        "tokens": {
            "hold_trades": tokens["HOLD_TRADES"],
            "hold_r": tokens["HOLD_NET_R"],
            "hold_sharpe": tokens["HOLD_SHARPE"],
            "hold_dd": tokens["HOLD_DD_ABS"],
            "hold_win": tokens["HOLD_WIN"],
            "hold_pf": tokens["HOLD_PF"],
            "dsr_base": tokens["DSR_BASE"],
            "pbo": tokens["PBO"],
            "re_p": tokens["RE_P"],
            "rl_p": tokens["RL_P"],
            "bars": tokens["BARS"],
            "series": tokens["SERIES"],
        },
    }


def build_preview(root: Path, data: dict[str, Any], tokens: dict[str, str]) -> Path:
    template = (root / "web/preview_template.html").read_text()
    payload = _json_safe(_preview_payload(root, data, tokens))
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    page = template
    for key, value in {
        **tokens,
        "DATA_JSON": data_json,
        "SVG_EQUITY": _svg_equity(root),
        "SVG_GRID": _svg_grid(data["grid"]),
        "SVG_COST": _svg_costs(data["costs"]),
        "SVG_HIST": _svg_hist(data["trades"]),
        "SVG_BENCH": _svg_bench(data["bench"], float(data["report"]["stages"]["holdout"]["sharpe"])),
        "SVG_HOLD": _svg_hold(data["audit"]),
    }.items():
        page = page.replace("{{" + key + "}}", value)
    missing = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", page)))
    if missing:
        raise RuntimeError(f"unresolved template tokens in preview.html: {missing}")
    for name in ("preview.html", "index.html"):
        (root / name).write_text(page, encoding="utf-8")
    (root / ".nojekyll").touch()
    return root / "preview.html"


def build_readme(root: Path, data: dict[str, Any], tokens: dict[str, str]) -> Path:
    template = (root / "web/readme_template.md").read_text()
    page = template
    for key, value in tokens.items():
        page = page.replace("{{" + key + "}}", value)
    missing = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", page)))
    if missing:
        raise RuntimeError(f"unresolved template tokens in README.md: {missing}")
    out = root / "README.md"
    out.write_text(page, encoding="utf-8")
    return out


def build_site(root: Path) -> Path:
    data = _load(root)
    tokens = _tokens(root, data)
    readme = build_readme(root, data, tokens)
    preview = build_preview(root, data, tokens)
    LOG.info("public README written: %s", readme)
    LOG.info("interactive report written: %s", preview)
    return readme
