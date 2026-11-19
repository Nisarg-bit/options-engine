# Intraday strategy: diagnosis and proposed research

This is an investigation, not a strategy release. No active trading logic was changed. The current opening-direction debit spread reduces historical losses versus the old straddle but has not demonstrated positive net expectancy. Profitability cannot be promised.

## Scope and method

Reviewed the frozen opening_spread_v1 policy, quote aggregation/loading, charges and historical replays. Replayed 37 recorded sessions, of which 34 have the required opening signal. September 1, 23 and 25 are excluded rather than fabricated. Compared one lot using recorded bid/ask and estimated charges; equal lots are not equal risk. All 34 included sessions have complete minute paths for the exit comparisons below. Reconciled current-strategy net P&L to the previous replay within one paisa.

The rule takes the sign of the parity-forward change from 09:20 to 09:44, enters an ATM directional debit spread around 09:45 with the short strike two steps away, and exits at a net loss of 35% of initial debit risk, a net gain of 50%, or 15:00. It is a deterministic rule, not a calibrated probability or confidence model.

## What is causing the loss?

### 1. The entry signal has no demonstrated predictive edge

On these 34 sessions, its direction was correct on 16 sessions by 15:00; the mean signed forward move from entry was -3.97 points. Mean signed moves were also negative at 10:00, 10:15, 10:45 and noon. This does not establish that the true edge is negative, but provides no evidence for sizing up.

A separate index-close proxy check on 30 earlier sessions, July 20–August 28, was correct 16/30 times by 15:00, with a mean signed move of -1.41 points. This uses index closes rather than the exact parity-forward signal and lacks option quotes; it is not an options P&L backtest.

### 2. A nearly flat gross strategy becomes a loss after costs

| Component, same actual exit times | Total rupees |
|---|---:|
| Hypothetical mid-to-mid gross | +971.75 |
| Bid/ask crossing cost | -936.00 |
| Gross after crossing | +35.75 |
| Estimated explicit charges | -4,172.28 |
| Net | -4,136.53 |

Mid prices are an attribution reference, not executable fills. Average gross after crossing is only Rs 1.05 per session against Rs 122.71 explicit charges. Four executed orders at Rs 20 plus brokerage GST account for Rs 94.40 per session before other charges. The fee assumptions were checked against [Zerodha's published charges](https://zerodha.com/charges/). Broker rounding and tiny IPFT differences do not explain this loss.

The observed win rate is 38.24%; with the observed average winner and loser, descriptive break-even win rate is about 44.67%. Those payoff averages are sample estimates, not future constants.

### 3. The fixed target clips gains; removing protection makes results worse

Same entry, structure and recorded paths; only exit rules changed for this diagnostic:

| Exit policy | Earlier 21 | Later 13 | All 34 net Rs |
|---|---:|---:|---:|
| Current stop + target + time | -6,606.06 | +2,469.53 | -4,136.53 |
| Keep stop, remove target, keep time | -4,105.03 | +3,810.39 | -294.65 |
| Time only | -5,859.24 | +3,705.45 | -2,153.79 |
| Keep target, remove stop, keep time | -8,360.26 | +1,107.20 | -7,253.06 |

Removing the target improves both periods but still loses overall. This merits a preregistered test, not immediate deployment. Of 16 stopped trades, only two would finish positive at 15:00 and only one subsequently reached the original target on the observed minute path. The evidence does not support blaming most losses on tight stops.

### 4. Weak openings and directional bias matter, but are not validated filters

Opening net displacement divided by opening range below 0.30: ten sessions, net -6,179.77. Remaining 24: +2,043.24. Absolute opening moves below 25 points: 17 sessions, -6,868.27. These thresholds are descriptive, selected after observing results, and overlapping; their effects cannot be added.

Call signals lost Rs 5,282.29 over 17 sessions; put signals gained Rs 1,145.76 over 17. Always buying the put spread would have gained Rs 6,713.15 versus always buying the call spread losing Rs 7,867.66. This reveals a possible sample-direction bias, not a recommendation to always buy puts.

Simply reversing the signal gained Rs 2,982.02 overall but lost Rs 321.26 in the later period. Previously tested additional one-point adverse execution per leg per side turns reversal into -Rs 5,450.72 and current trend into -Rs 11,077.77. A superficially profitable historical variant is not enough.

### 5. Data guard gaps exist, but did not explain these observed losses

No interpolated execution quotes, insufficient displayed entry/exit quantity for one lot, or stale opening signal quotes were detected in the audited trades. Zero of 12,422 held-leg-minute records were flagged stale. However, the replay loader ignores interpolation/depth fields; minute bars cannot establish simultaneous two-leg fills, exact quote age within the minute, or intraminute stop ordering. Add explicit checks and execution-delay tests before treating simulated results as tradable. Displayed quantity is not a fill guarantee.

### 6. The evidence is too small and already inspected

The earlier period loses while the later period profits. Descriptive five-day block resampling gives an approximate 95% interval of -Rs 387.88 to +Rs 166.32 for mean daily P&L. This is not independent validation or a probability of future profitability. The 34 days have been used to inspect/select ideas; calling a subset an untouched holdout would be misleading.

## Proposed changes — before implementation

1. **Replace the unqualified opening-sign entry.** Define a small number of testable hypotheses: confirmed opening-range continuation versus failed-break reversal. Use only information available at the decision time; test whether waiting for confirmation improves net returns. Do not fit a complex model or search hundreds of thresholds on 34 days. Compare with simple always-call/always-put controls to expose market-direction bias.
2. **Preserve daily intraday participation as an explicit constraint.** The user requires a real trade each trading day. Research a flexible entry window and a separately measured cutoff fallback if no qualified setup appears. Do not silently drop difficult days. A forced fallback may have negative expectancy and must be reported separately. A daily-trade objective cannot override exchange closures, unavailable data, broker failures or hard loss limits; do not force an unsafe order to satisfy a counter.
3. **Test exits without the fixed profit cap first.** Keep bounded risk and a hard intraday time exit. Compare the existing stop plus time exit with one prespecified structural/trailing exit. Do not remove stops on the assumption that losing trades usually recover. All changes require new chronological or prospective evidence.
4. **Compare option structures at equal risk.** Compare a long option (fewer orders, uncapped upside, greater premium exposure) with a debit spread (bounded debit and capped upside). Evaluate strike width, expiry and liquidity with net charges and stress. Do not switch automatically, add naked shorts or use martingale sizing.
5. **Separate account risk from signal confidence.** Start research comparisons at one lot. Account capital, maximum daily loss and monthly drawdown require explicit user limits. The strategy's 35% trade-debit stop is not a 35% account-loss limit and is not a guaranteed fill. Size using complete-position worst-case loss plus execution/fee allowance, subject to hard account caps. A confidence score cannot supply missing capital limits.
6. **Strengthen validation and execution accounting.** Enforce quote-quality checks, model delayed fills and adverse slippage, and test individual-leg execution risk. Freeze each hypothesis before evaluation; use a longer chronological sample, rolling evaluation and then genuinely unseen paper sessions. Report all days, fallback contribution, net P&L, drawdown, worst day, costs and period stability. No live rollout solely because the best of many historical variants profits.

## Decision and remaining information

Do not promote the current rule as a profitable model. The most promising isolated exit change still loses, so entry research remains necessary. The daily real-trade preference and Zerodha broker are confirmed. Maximum allocated capital, daily account-loss percentage and monthly drawdown percentage remain unspecified; the existing model cannot safely infer them.

No strategy implementation or deployment was performed during this investigation. Diagnostic artifacts: `tools/diagnose_daily_spread.py`, `data/deep-diagnostic.json`, `data/opening-signal-history-audit.json`; previous comparison: `docs/old-vs-new-pnl-2026-10-25.md`.
