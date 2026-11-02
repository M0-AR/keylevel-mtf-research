# Pre-registration: Multi-Timeframe Key-Level Strategy Replication

**Status:** frozen before the first full-sample backtest.  
**Frozen at:** 2026-11-02 (UTC), prior to running `klab run`.  
**Principal question:** When a discretionary multi-timeframe support/resistance strategy is translated into deterministic rules, does it produce positive risk-adjusted net performance that survives realistic costs, a sealed holdout, and multiple-testing correction?

This document is the research contract. Any deviation must be recorded in `preregistration/DEVIATIONS.md` with date, reason, and whether it was made before or after seeing holdout results. The holdout is opened once, in the final run, after the base specification and all code are frozen.

## 1. Hypothesis

H1 (primary, two-sided): The pre-specified base strategy has a positive mean net return per trade after costs, and a positive net Sharpe ratio, on the sealed holdout (2023-01-01 to 2025-12-31), pooled across the eight pre-specified instruments.

H0: Net mean return per trade <= 0, or net Sharpe <= 0, or the result is not distinguishable from the placebo/benchmark distributions at the pre-specified alpha.

The strategy is *not* assumed to be profitable. A well-powered null result is a publishable contribution because the practitioner claim is widely repeated and rarely tested with this combination of intraday data, costs, and selection controls.

## 2. Strategy source and operationalisation

The source is a public practitioner interview (Arabic, undated transcript supplied by the project owner) describing a "simple" workflow:

1. Start on monthly, then weekly, then daily charts; mark major support/resistance from candle bodies/closes, not wicks, using repeated reactions (typically two or more).
2. On the 4-hour chart, identify the trend and a diagonal trendline; trade only with the trend ("trend is your friend until the end").
3. On the 15-minute chart, enter at the marked level after confirmation: first close through the level is high risk, second/third close is lower risk; use double/triple tops/bottoms and rejection candles.
4. Use a stop beyond the level/swing, a target at the next level, multiple entries, partial take-profit, move stop to breakeven; risk a fixed fraction; avoid scheduled news; do not predict, follow price.

Because the source is discretionary, the exact rules below are a *faithful but explicit* operationalisation. Results generalise to this operationalisation, not to the individual.

## 3. Instruments and data

- Eight instruments, fixed ex ante: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, XAUUSD, SPXUSD, NSXUSD.
- Primary historical source: HistData.com Generic ASCII M1 bid bars, 2012-01-01 to 2025-12-31. HistData timestamps are treated as fixed UTC-5 (no DST), per the vendor specification.
- Timeframes are built by resampling M1: 15 minutes (entry), 4 hours (trend), 1 day, 1 week, 1 month (levels). No future bar is used before its close.
- 2026 data are used only for the live shadow: Yahoo Finance 15-minute/daily bars obtained through a browser-impersonated HTTP client, and Kraken public 15-minute bars for BTCUSD/ETHUSD. Yahoo data are not mixed into the primary historical sample.
- Raw data are never redistributed in the repository; download scripts and SHA-256 manifests are provided.

## 4. Deterministic rules (base specification)

### 4.1 Pivots and key levels

- A pivot high at bar `i` requires `high[i] > high[i-L:i]` and `high[i] >= high[i+1:i+R+1]`; a pivot low is symmetric. Base `L=R=2` for level timeframes; `L=R=3` for the 4-hour trend.
- A pivot is *confirmed* only at the close of bar `i+R`. The confirmation timestamp is the earliest time the pivot may affect a decision.
- For each level timeframe, pivots are clustered online in price. A new pivot joins an existing cluster if `|price - cluster_price| <= cluster_atr * ATR14_D1`; the cluster price is the touch-weighted mean. A level becomes tradable only after at least `min_touches = 2` pivots have joined it.
- Levels expire after 730 calendar days without an update. Levels are separate by timeframe (monthly, weekly, daily).
- At each 15-minute bar, a level is a *resistance* if `high >= level` and `close < level`; a *support* if `low <= level` and `close > level`.

### 4.2 Trend

- On 4-hour bars, maintain the last two confirmed swing highs and last two confirmed swing lows.
- Up trend: last swing high > previous swing high **and** last swing low > previous swing low.
- Down trend: last swing high < previous swing high **and** last swing low < previous swing low.
- Otherwise neutral. Only trades in the direction of a non-neutral trend are allowed in the base specification.
- The ablation `trend_method=ema` replaces this with 4-hour close > EMA(200) and EMA slope > 0 (up), or the reverse (down).

### 4.3 Entry

For a short (long is symmetric):

1. 4-hour trend is down (up).
2. An active resistance (support) level lies within the current 15-minute bar's high-low range, expanded by `0.15 * ATR14_15m`.
3. The level has at least two prior 15-minute rejection touches in the last 300 bars, where a rejection is `high >= level and close < level` (short) or `low <= level and close > level` (long).
4. The last `confirmation_closes = 2` completed 15-minute closes are below (above) the level; the current candle is bearish (bullish) if `require_directional_candle`.
5. Distance from the level to the current close is at most `max_chase_atr = 1.0 * ATR14_15m`.
6. Entry is at the **next** 15-minute bar open. Same-bar-close execution is forbidden.

### 4.4 Stop, target, and position management

- Stop: `level + stop_buffer_atr * ATR14_15m` for shorts (mirror for longs), or the recent 5-bar extreme plus `0.10 * ATR`, whichever is more conservative. If stop distance > `4 * ATR`, skip.
- Target: nearest active opposite level at least `1R` away; if none, `default_rr = 2R`; cap at `5R`.
- Risk per trade: 0.5% of initial equity. One position per instrument at a time; simultaneous positions across instruments are allowed. PnL is measured in R multiples and converted to portfolio return as `0.005 * R_net`.
- Exit: stop or target (conservative: if both are touched in the same 15-minute bar, the stop is assumed first); trend flip; 96 bars (24 hours); end of data. Gaps fill at the next available open.
- Costs: half-spread plus slippage on entry and exit. Base costs are instrument-specific and listed in `config/instruments.yaml`. A zero-cost and 2x-cost sensitivity is reported.

### 4.5 Filters not in the base specification

The base specification does **not** use a session filter, news blackout, discretionary pattern recognition, or prediction. These are tested only as pre-specified ablations or live-shadow overlays. The historical news calendar is unavailable at daily coverage for 2012-2025, so the base result is a no-news-filter result; this is a limitation, not a claim that news is irrelevant.

## 5. Sample splits

- Development: 2012-01-01 to 2019-12-31 (design and parameter sensitivity only).
- Validation: 2020-01-01 to 2022-12-31 (intermediate check; not used to select the base specification).
- **Sealed holdout:** 2023-01-01 to 2025-12-31. Opened once after code freeze. Results are reported whatever their sign.
- Live shadow: 2026-01-01 to the download date, crypto and EURUSD/gold/index where 15-minute Yahoo data exist. This is descriptive, not a significance test.

The base specification is fixed by theory/faithfulness, not optimised. The grid is a sensitivity/selection experiment; if a grid winner is reported, it is selected on development only and evaluated on validation and holdout with the full trial ledger.

## 6. Benchmarks and placebos

1. Buy-and-hold: daily close-to-close, equal-weight across instruments.
2. Trend-only: 4-hour trend flips, no levels.
3. Level-only: levels and confirmation, no 4-hour trend filter.
4. No-confirmation: first level touch in the trend direction, no close confirmation.
5. Random-level placebo: replace the chosen level with a random active level at the same timestamp.
6. Random-entry placebo: same direction and stop/target geometry at a random bar; 1,000 draws.
7. 15-minute Donchian(20) breakout in the 4-hour trend direction.
8. 4-hour SMA(50/200) crossover.

## 7. Primary and secondary endpoints

Primary: pooled net mean R per trade and net daily Sharpe on the sealed holdout; stationary-bootstrap 95% CI; deflated Sharpe ratio (DSR) with the full trial count; probability of backtest overfitting (PBO) via CSCV on the grid.

Secondary: per-instrument net Sharpe, profit factor, hit rate, average win/loss, maximum drawdown, Calmar, exposure, turnover, trades per year; performance by volatility regime, trend age, level timeframe, touch number, confirmation delay, session, and asset class; cost sensitivity; parameter-surface medians and neighbourhoods.

## 8. Multiple-testing and inference

- All variants considered, including failed and abandoned ones, are written to `results/tables/trial_ledger.csv`.
- DSR uses the number of trials and the cross-trial Sharpe variance, with skewness/kurtosis of the selected strategy.
- PBO uses CSCV with 12 contiguous slices and all 924 balanced splits.
- White/Hansen-style Reality Check uses stationary bootstrap (5,000 resamples) of the maximum excess return over buy-and-hold across variants.
- Per-instrument p-values are Benjamini-Hochberg adjusted.
- Statistical significance is not economic significance: net Sharpe, drawdown, and capacity are reported alongside p-values.

## 9. Stopping and reporting rules

- The pipeline is run to completion; there is no optional stopping based on interim performance.
- The holdout is not used for feature selection, threshold choice, or narrative selection.
- Negative, null, and positive results are reported in the same tables. The abstract states the direction of the primary result.
- Any post-hoc analysis is labelled exploratory and excluded from confirmatory claims.

## 10. Deviations

Recorded in `preregistration/DEVIATIONS.md`. No deviations at freeze time.
