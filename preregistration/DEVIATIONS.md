# Pre-freeze amendments and post-freeze deviations

**Status:** post-holdout.  Development and validation were run, the 2023--2025 holdout was opened
once at 2026-11-02 14:58 UTC under the freeze recorded in `CODE_FREEZE.json`, and all reported
results are final.  This file records every implementation choice that differs from a literal
reading of `PROTOCOL.md`: the execution and data-handling corrections made before the freeze, the
presentation-only changes made after opening, and the post-holdout inference completion.  The
holdout is not re-opened; the guard in `cli.py` now blocks a re-run because the working tree differs
from the freeze hash after the documented edits.

## A. Execution and accounting corrections (pre-freeze)

1. **Gap through the target cancels the entry.**  If the next 15-minute open has already crossed the
   intended target level, a fresh 2R target is not invented from the gapped open; the signal is
   skipped.  A gap through the stop still fills at the next open.  This removes a look-ahead-free but
   economically fictitious fill and is applied identically to every stage.
2. **Gap precedence at the open.**  If the open gaps through both the stop and the target, the stop is
   taken first.  This matches the intrabar stop-first convention and is the conservative assumption.
3. **Benchmark cost accounting.**  The buy-and-hold/trend/SMA/Donchian benchmarks charge the per-side
   rate `(half_spread + slippage) / close` once per side, rather than double-counting a round-trip
   cost on every daily mark.  The strategy ledger is unchanged.
4. **Session labels.**  HistData timestamps are fixed UTC-5 without DST.  Session labels add five
   hours before bucketing into Asia/London/New York/late.  Live UTC data pass a zero offset.
5. **Yahoo intraday window.**  Yahoo rejects 15-minute requests older than roughly 60 days.  The live
   downloader clamps the requested start to the last 59 days instead of failing; the live shadow is
   explicitly descriptive.

## B. Data and timestamp ambiguity

- HistData does not document whether the M1 timestamp is the bar open or the bar close.  The raw
  files contain a full 00:00--23:59 day, which is consistent with bar-open timestamps.  The
  resampler uses right-closed, right-labelled bins, so higher-timeframe labels are approximately true
  closes and cross-timeframe ordering is causal.  The residual ambiguity is reported as a limitation,
  not silently corrected.
- Vendor cross-validation uses weekly close-to-close returns against Yahoo daily bars.  Price levels
  are not compared because the vendors quote different instruments (spot FX versus futures/indices).
  Yahoo data are never mixed into the historical backtest.

## C. Placebos and inference

1. **Random-entry placebo.**  The chosen direction, stop distance, target distance, and cost are held
   fixed at a random bar inside the stage window; 1,000 draws, one vectorised call per symbol and
   side.  The reported p-value is one-sided, `P(placebo mean R >= observed mean R)`.
2. **Random-level placebo.**  The chosen level is replaced by a uniformly sampled **active** level at
   the same signal timestamp, from the same side and within the same chase limit.  Candidate levels
   are cached once per trade; 200 draws are used.  The stop is re-derived from the sampled level plus
   the pre-registered buffer, while the observed target distance and entry timing are held fixed.
   This isolates the level-location contribution.  It is an approximation to a full re-run of the
   signal generator on a synthetic level book, which would be prohibitively slow at 1,000 draws.
3. **Per-instrument tests.**  One-sided t-tests on net R are Benjamini--Hochberg adjusted at the
   pre-registered alpha.  The pooled bootstrap, DSR, PBO, Reality Check, and SPA follow the protocol.
4. **Trial ledger.**  The DSR trial count includes the 18 development grid variants and the nine
   development ablations.  Validation is not used to select the base specification or the winner.

## D. Reporting choices

- Protocol section 6 lists level-only and no-confirmation as benchmarks.  They are reported as the
  `no_trend` and `no_confirmation` ablations in the same ledger, rather than duplicated as separate
  benchmark rows.  The numbers are identical in construction.
- The level-reaction audit uses a strict hold definition: no close through the level by 0.25 ATR
  over the next 96 bars.  It also reports the weaker, more favourable `+1R before -1R` rate so the
  "90% success" claim can be assessed without relying on the strict definition.
- `report.py` is a pure formatter.  It reads `results/` and `config/` and writes `README.md`,
  `paper/main.tex`, `paper/references.bib`, figures, and `results/report/report.json`.  No number in
  the paper is typed by hand.

## E. Development and validation status

- Development (2012--2019) and validation (2020--2022) have been run with the corrected execution
  model.  Validation was not used to change any parameter, rule, or grid point.
- The development grid winner was selected on development only.  Its validation net Sharpe is
  negative, so the sign of the development null does not reverse out of sample.
- The 2023--2025 holdout was opened once, after `CODE_FREEZE.json` was created and matched the
  working tree.  It is now closed.  The current tree no longer matches that hash because of the
  presentation and inference-completion edits recorded in sections F and G; re-running the holdout
  is therefore refused.

## F. Post-holdout presentation changes

The holdout was opened once, at 2026-11-02 14:58 UTC, using the code frozen under
`CODE_FREEZE.json` hash `bb94af28645d759661b58669a0d39288c62408f28d78be8ad70f6bf2f5c0af60`.
After seeing the holdout output, four files were changed for presentation and tooling only:

1. `src/klab/report.py`: fixed the display key for the Reality Check/SPA statistic (`stat` vs
   `statistic`), formatted a zero placebo p-value as `< 1/(n+1)` with the draw count, added the
   placebo interpretation paragraph, added the session figure to the paper, cited Claeys (2026) in
   the intrabar-ambiguity limitation, added the holdout benchmark and per-instrument
   Benjamini--Hochberg tables (already computed by the frozen inference code), added the data-integrity
   summary, added the corresponding inference narrative and placebo-geometry caveat to the LaTeX
   body, and corrected the reproducibility command names. No data, strategy, backtest, statistics, or
   placebo code was changed.
2. `Makefile`: added a `development` alias for the existing development target, made `setup`
   idempotent (`uv venv --clear`), replaced the missing `pip` shim with `uv pip --python`, moved the
   global `--config` flag before the subcommand, and added an optional Docker TeX Live `pdf` target.
3. `Dockerfile`: changed the default command from `make run` (which assumed an absent `.venv`) to the
   installed `klab run --stage development` entry point.
4. `.dockerignore` (new): excludes raw/processed data, results, logs, the virtual environment, and
   generated report artefacts from the build context, so the published image is the code and its
   dependencies rather than a 4.3 GB copy of the local data.

The working-tree hash therefore differs from the pre-holdout freeze.  Because this file is itself part
of the freeze set, the post-change hash changes whenever this note is edited, so it is not quoted
here.  The original `CODE_FREEZE.json` is retained unchanged as the commitment that was in force when
the holdout was opened.  None of these edits can change a holdout number; the reported results are
those produced by the frozen analysis code.

## G. Post-holdout inference completion

Two pre-registered inference requirements were not fully satisfied by the original holdout run.
Neither change re-runs the strategy or alters any trade:

1. **Stationary-bootstrap confidence interval.** PROTOCOL section 7 requires a stationary-bootstrap
   95% CI for the primary endpoint. The original `inference.json` reported DSR, PBO, Reality Check,
   SPA, BH, and the placebos but not the CI. It was computed after opening the holdout from the saved
   `trades_base.csv` and `daily_base.csv`, using the frozen `bootstrap_mean_ci` and
   `bootstrap_sharpe` functions with the pre-registered seed, 5,000 resamples, and a mean block of 20.
   The result is stored under `bootstrap` in `results/holdout/inference.json` and reported in the
   paper. The interval is entirely below zero for both mean net R and net Sharpe. The same calculation
   is now part of `_inference` in `cli.py`, so a future re-run produces the interval as part of the
   pipeline rather than as a one-off repair.  A `n_draws` field was also added to the random-entry
   placebo result for reporting; it does not change any draw or p-value.
2. **Reality Check and SPA scope.** The protocol describes the Reality Check as spanning variants.
   The holdout run evaluated the base and the development-selected winner only, so the RC and SPA
   statistics use two models rather than the full 18-point grid. The full trial count still enters the
   DSR, and the PBO uses all 18 grid variants. With every variant negative, widening the RC/SPA model
   set can only raise the p-value, which is already 1.0; the conclusion is unaffected. This narrower
   scope is recorded as a deviation from the literal protocol wording.
