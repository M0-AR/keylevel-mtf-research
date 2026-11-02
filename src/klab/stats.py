"""Statistical inference for a large strategy search.

Implements the pre-registered battery:

* annualised performance and drawdown statistics;
* stationary bootstrap (Politis & Romano, 1994);
* Probabilistic and Deflated Sharpe Ratios (Bailey & López de Prado, 2012,
  2014);
* Probability of Backtest Overfitting via CSCV (Bailey et al., 2017);
* White (2000) Reality Check and Hansen (2005) SPA;
* Benjamini-Hochberg (1995) false-discovery control;
* HAC alpha/beta regressions.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy import stats

try:
    import statsmodels.api as sm
except Exception:  # pragma: no cover
    sm = None

EULER_GAMMA = 0.5772156649015329


def _clean(returns: pd.Series | np.ndarray) -> np.ndarray:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    return r


def sharpe_ratio(returns: pd.Series | np.ndarray, annualization: int = 252) -> float:
    r = _clean(returns)
    if len(r) < 2:
        return float("nan")
    sd = float(np.std(r, ddof=1))
    if sd == 0:
        return float("nan")
    return float(np.mean(r) / sd * np.sqrt(annualization))


def sortino_ratio(returns: pd.Series | np.ndarray, annualization: int = 252) -> float:
    r = _clean(returns)
    if len(r) < 2:
        return float("nan")
    downside = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2))
    if downside == 0:
        return float("nan")
    return float(np.mean(r) / downside * np.sqrt(annualization))


def max_drawdown(returns: pd.Series | np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return float("nan")
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    return float(np.min(dd))


def summary_stats(returns: pd.Series | np.ndarray, annualization: int = 252) -> dict[str, float]:
    r = _clean(returns)
    n = len(r)
    if n == 0:
        return {k: float("nan") for k in ["n_days", "ann_return", "ann_vol", "sharpe", "sortino", "max_drawdown", "calmar", "skew", "kurtosis"]}
    equity = np.cumprod(1.0 + r)
    ann_ret = float(equity[-1] ** (annualization / n) - 1.0) if equity[-1] > 0 else float("nan")
    ann_vol = float(np.std(r, ddof=1) * np.sqrt(annualization)) if n > 1 else float("nan")
    mdd = max_drawdown(r)
    return {
        "n_days": float(n),
        "ann_return": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": sharpe_ratio(r, annualization),
        "sortino": sortino_ratio(r, annualization),
        "max_drawdown": mdd,
        "calmar": float(ann_ret / abs(mdd)) if mdd < 0 else float("nan"),
        "skew": float(stats.skew(r, bias=False)) if n > 2 else float("nan"),
        "kurtosis": float(stats.kurtosis(r, fisher=False, bias=False)) if n > 3 else float("nan"),
    }


def trade_stats(trades: pd.DataFrame) -> dict[str, Any]:
    if trades is None or trades.empty:
        return {
            "n_trades": 0,
            "win_rate": float("nan"),
            "avg_r": float("nan"),
            "median_r": float("nan"),
            "profit_factor": float("nan"),
            "avg_bars": float("nan"),
        }
    r = trades["r_net"].to_numpy(dtype=float)
    wins = r[r > 0]
    losses = r[r < 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    return {
        "n_trades": int(len(trades)),
        "win_rate": float((r > 0).mean()),
        "avg_r": float(np.mean(r)),
        "median_r": float(np.median(r)),
        "profit_factor": pf,
        "avg_bars": float(trades["bars_held"].mean()),
        "stop_rate": float((trades["exit_reason"].str.startswith("stop")).mean()),
        "target_rate": float((trades["exit_reason"].str.startswith("target")).mean()),
        "time_rate": float((trades["exit_reason"] == "time").mean()),
        "trend_flip_rate": float((trades["exit_reason"] == "trend_flip").mean()),
    }


def _sharpe_columns(mat: np.ndarray) -> np.ndarray:
    mu = np.mean(mat, axis=0)
    sd = np.std(mat, axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 0, mu / sd, 0.0)


def stationary_bootstrap_indices(
    n: int,
    n_boot: int = 5000,
    mean_block: int = 20,
    seed: int = 20261102,
) -> np.ndarray:
    """Politis-Romano stationary bootstrap indices, shape (n_boot, n)."""
    rng = np.random.default_rng(seed)
    idx = np.empty((n_boot, n), dtype=np.int32)
    cur = rng.integers(0, n, size=n_boot)
    p = 1.0 / max(1, int(mean_block))
    for t in range(n):
        new = rng.random(n_boot) < p
        cur = np.where(new, rng.integers(0, n, size=n_boot), (cur + 1) % n)
        idx[:, t] = cur
    return idx


def bootstrap_sharpe(
    returns: pd.Series | np.ndarray,
    n_boot: int = 5000,
    mean_block: int = 20,
    seed: int = 20261102,
) -> np.ndarray:
    r = _clean(returns)
    n = len(r)
    if n < 10:
        return np.array([])
    idx = stationary_bootstrap_indices(n, n_boot, mean_block, seed)
    means = r[idx].mean(axis=1)
    sds = r[idx].std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sds > 0, means / sds, np.nan)


def bootstrap_mean_ci(
    returns: pd.Series | np.ndarray,
    n_boot: int = 5000,
    mean_block: int = 20,
    seed: int = 20261102,
    alpha: float = 0.05,
) -> tuple[float, float]:
    r = _clean(returns)
    if len(r) < 10:
        return (float("nan"), float("nan"))
    idx = stationary_bootstrap_indices(len(r), n_boot, mean_block, seed)
    means = r[idx].mean(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def psr(returns: pd.Series | np.ndarray, sr_benchmark: float = 0.0) -> dict[str, float]:
    r = _clean(returns)
    n = len(r)
    if n < 3:
        return {"psr": float("nan"), "sr": float("nan"), "n": float(n)}
    sr = float(np.mean(r) / np.std(r, ddof=1))
    skew = float(stats.skew(r, bias=False))
    kurt = float(stats.kurtosis(r, fisher=False, bias=False))
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return {"psr": float("nan"), "sr": sr, "n": float(n)}
    z = (sr - sr_benchmark) * np.sqrt(n - 1) / np.sqrt(denom)
    return {"psr": float(stats.norm.cdf(z)), "sr": sr, "n": float(n), "skew": skew, "kurtosis": kurt}


def deflated_sharpe_ratio(
    returns: pd.Series | np.ndarray,
    trial_srs: np.ndarray | list[float],
    sr_benchmark: float = 0.0,
) -> dict[str, float]:
    r = _clean(returns)
    n = len(r)
    trials = np.asarray(trial_srs, dtype=float)
    trials = trials[np.isfinite(trials)]
    if n < 3 or len(trials) == 0:
        return {"dsr": float("nan"), "sr0": float("nan"), "sr": float("nan"), "n_trials": float(len(trials))}
    sr = float(np.mean(r) / np.std(r, ddof=1))
    if len(trials) > 1:
        var_trials = float(np.var(trials, ddof=1))
        sd_trials = np.sqrt(var_trials)
        n_trials = len(trials)
        sr0 = sd_trials * (
            (1.0 - EULER_GAMMA) * stats.norm.ppf(1.0 - 1.0 / n_trials)
            + EULER_GAMMA * stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
        )
        sr0 = max(sr0, 0.0)
    else:
        sr0 = float(sr_benchmark)
    skew = float(stats.skew(r, bias=False))
    kurt = float(stats.kurtosis(r, fisher=False, bias=False))
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return {"dsr": float("nan"), "sr0": sr0, "sr": sr, "n_trials": float(len(trials))}
    z = (sr - sr0) * np.sqrt(n - 1) / np.sqrt(denom)
    return {
        "dsr": float(stats.norm.cdf(z)),
        "sr0": float(sr0),
        "sr": sr,
        "n": float(n),
        "n_trials": float(len(trials)),
        "skew": skew,
        "kurtosis": kurt,
    }


def pbo_cscv(returns_matrix: pd.DataFrame | np.ndarray, n_slices: int = 12) -> dict[str, Any]:
    """Probability of Backtest Overfitting using combinatorially symmetric CV."""
    mat = np.asarray(returns_matrix, dtype=float)
    if mat.ndim != 2:
        raise ValueError("returns_matrix must be 2-D")
    if mat.shape[1] < 2:
        return {"pbo": float("nan"), "n_splits": 0, "n_variants": int(mat.shape[1])}
    mat = np.nan_to_num(mat, nan=0.0)
    T = mat.shape[0]
    s = int(n_slices)
    if s % 2 != 0:
        s -= 1
    s = max(2, min(s, T // 2 * 2))
    block = T // s
    if block < 2:
        return {"pbo": float("nan"), "n_splits": 0, "n_variants": int(mat.shape[1])}
    mat = mat[T - block * s :]
    blocks = [mat[i * block : (i + 1) * block] for i in range(s)]
    n_variants = mat.shape[1]
    ranks: list[int] = []
    logits: list[float] = []
    is_best: list[int] = []
    all_idx = list(range(s))
    for combo in combinations(all_idx, s // 2):
        is_mask = np.zeros(s, dtype=bool)
        is_mask[list(combo)] = True
        is_data = np.concatenate([blocks[i] for i in all_idx if is_mask[i]], axis=0)
        oos_data = np.concatenate([blocks[i] for i in all_idx if not is_mask[i]], axis=0)
        is_sr = _sharpe_columns(is_data)
        oos_sr = _sharpe_columns(oos_data)
        best = int(np.nanargmax(is_sr))
        is_best.append(best)
        order = np.argsort(-oos_sr, kind="mergesort")
        rank = int(np.where(order == best)[0][0]) + 1
        ranks.append(rank)
        omega = rank / (n_variants + 1.0)
        logits.append(float(np.log(omega / (1.0 - omega))))
    ranks_arr = np.array(ranks)
    return {
        "pbo": float(np.mean(ranks_arr > (n_variants + 1) / 2.0)),
        "n_splits": int(len(ranks)),
        "n_variants": int(n_variants),
        "n_obs": int(mat.shape[0]),
        "median_oos_rank": float(np.median(ranks_arr)),
        "mean_logit": float(np.mean(logits)),
        "is_best_counts": pd.Series(is_best).value_counts().to_dict(),
    }


def benjamini_hochberg(pvals: np.ndarray | list[float], alpha: float = 0.05) -> dict[str, Any]:
    p = np.asarray(pvals, dtype=float)
    ok = np.isfinite(p)
    out = np.ones_like(p)
    q = np.ones_like(p)
    m = int(ok.sum())
    if m == 0:
        return {"reject": np.zeros_like(p, dtype=bool), "qvalues": q, "n_tests": 0}
    idx = np.where(ok)[0]
    order = idx[np.argsort(p[idx])]
    ranked = p[order]
    qv = ranked * m / np.arange(1, m + 1)
    qv = np.minimum.accumulate(qv[::-1])[::-1]
    q[order] = np.minimum(qv, 1.0)
    reject = q <= alpha
    return {"reject": reject, "qvalues": q, "n_tests": m}


def _bootstrap_max_stats(
    d: np.ndarray,
    idx: np.ndarray,
    studentize: bool,
) -> tuple[float, np.ndarray]:
    T, k = d.shape
    f_bar = np.mean(d, axis=0)
    se = np.std(d, axis=0, ddof=1)
    se = np.where(se > 0, se, np.nan)
    if studentize:
        obs = float(np.nanmax(np.sqrt(T) * f_bar / se)) if k else float("nan")
    else:
        obs = float(np.sqrt(T) * np.max(f_bar)) if k else float("nan")
    boot = np.empty(len(idx))
    for b in range(len(idx)):
        fb = np.mean(d[idx[b]], axis=0)
        if studentize:
            vals = np.sqrt(T) * (fb - f_bar) / se
            boot[b] = np.nanmax(vals) if np.isfinite(vals).any() else np.nan
        else:
            boot[b] = np.sqrt(T) * np.max(fb - f_bar)
    if studentize:
        obs = max(0.0, obs)
        boot = np.maximum(0.0, boot)
    return obs, boot


def reality_check(
    returns_matrix: pd.DataFrame | np.ndarray,
    benchmark: pd.Series | np.ndarray,
    n_boot: int = 5000,
    mean_block: int = 20,
    seed: int = 20261102,
) -> dict[str, float]:
    r = np.asarray(returns_matrix, dtype=float)
    b = np.asarray(benchmark, dtype=float).reshape(-1)
    if r.ndim == 1:
        r = r.reshape(-1, 1)
    d = r - b[:, None]
    d = np.nan_to_num(d, nan=0.0)
    idx = stationary_bootstrap_indices(len(d), n_boot, mean_block, seed)
    obs, boot = _bootstrap_max_stats(d, idx, studentize=False)
    return {
        "stat": float(obs),
        "pvalue": float(np.mean(boot >= obs)) if len(boot) else float("nan"),
        "n_models": int(d.shape[1]),
    }


def spa_test(
    returns_matrix: pd.DataFrame | np.ndarray,
    benchmark: pd.Series | np.ndarray,
    n_boot: int = 5000,
    mean_block: int = 20,
    seed: int = 20261102,
) -> dict[str, float]:
    r = np.asarray(returns_matrix, dtype=float)
    b = np.asarray(benchmark, dtype=float).reshape(-1)
    if r.ndim == 1:
        r = r.reshape(-1, 1)
    d = r - b[:, None]
    d = np.nan_to_num(d, nan=0.0)
    idx = stationary_bootstrap_indices(len(d), n_boot, mean_block, seed)
    obs, boot = _bootstrap_max_stats(d, idx, studentize=True)
    return {
        "stat": float(obs),
        "pvalue": float(np.mean(boot >= obs)) if len(boot) else float("nan"),
        "n_models": int(d.shape[1]),
    }


def alpha_beta(
    returns: pd.Series | np.ndarray,
    benchmark: pd.Series | np.ndarray,
    annualization: int = 252,
    maxlags: int = 5,
) -> dict[str, float]:
    y = np.asarray(returns, dtype=float)
    x = np.asarray(benchmark, dtype=float)
    ok = np.isfinite(y) & np.isfinite(x)
    y, x = y[ok], x[ok]
    if len(y) < 10:
        return {"alpha_ann": float("nan"), "beta": float("nan"), "t_alpha": float("nan"), "p_alpha": float("nan")}
    X = sm.add_constant(x) if sm is not None else np.column_stack([np.ones(len(x)), x])
    if sm is not None:
        model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
        alpha_daily = float(model.params[0])
        beta = float(model.params[1])
        t_alpha = float(model.tvalues[0])
        p_alpha = float(model.pvalues[0])
    else:  # pragma: no cover
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        alpha_daily, beta = float(coef[0]), float(coef[1])
        resid = y - X @ coef
        s2 = np.sum(resid**2) / (len(y) - 2)
        cov = s2 * np.linalg.inv(X.T @ X)
        t_alpha = alpha_daily / np.sqrt(cov[0, 0])
        p_alpha = float(2 * (1 - stats.t.cdf(abs(t_alpha), len(y) - 2)))
    return {
        "alpha_ann": float((1.0 + alpha_daily) ** annualization - 1.0),
        "beta": beta,
        "t_alpha": t_alpha,
        "p_alpha": p_alpha,
    }
