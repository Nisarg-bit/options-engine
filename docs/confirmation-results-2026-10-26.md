# Confirmation strategy development results

Research only. No active strategy or broker configuration changed. The candidate definitions were written before this replay, but the historical sample had already been investigated; these results are not an independent holdout.

## One-lot results after estimated charges

| Entry / structure | Eligible days | Quoted net Rs | +1 point/leg/side net Rs | Extra minute delay net Rs | Quoted mean net / premium risk |
|---|---:|---:|---:|---:|---:|
| baseline/spread | 34 / 34 / 34 | -4,136.53 | -11,077.77 | -2,976.33 | -4.76% |
| baseline/long | 34 / 34 / 34 | -3,321.67 | -6,976.13 | -3,792.81 | -2.96% |
| baseline_no_target/spread | 34 / 34 / 34 | -294.65 | -7,937.15 | 1,221.07 | 0.39% |
| baseline_no_target/long | 34 / 34 / 34 | 14,496.30 | 11,250.54 | 19,257.34 | 9.33% |
| continuation/spread | 34 / 34 / 34 | 6,841.20 | -1,224.45 | 5,308.22 | 8.50% |
| continuation/long | 34 / 34 / 34 | 42,371.78 | 36,028.24 | 37,568.20 | 18.65% |
| failed_break/spread | 34 / 34 / 34 | -3,293.25 | -10,057.53 | -3,268.79 | -2.85% |
| failed_break/long | 34 / 34 / 34 | 499.52 | -2,499.71 | -381.43 | 3.01% |

Eligible-day counts are quoted / slippage / delay, respectively. Equal lots are not equal account risk. The last column is an analytical normalization, not a tradable fractional-lot strategy.

## Daily participation and period stability (quoted fills)

| Entry / structure | Earlier net Rs | Later net Rs | Fallback days | Fallback net Rs | Max drawdown Rs |
|---|---:|---:|---:|---:|---:|
| baseline/long | -11,883.93 | 8,562.26 | 0 | 0.00 | 18,186.98 |
| baseline/spread | -6,606.06 | 2,469.53 | 0 | 0.00 | 6,610.88 |
| baseline_no_target/long | -2,016.42 | 16,512.71 | 0 | 0.00 | 10,625.26 |
| baseline_no_target/spread | -4,105.03 | 3,810.39 | 0 | 0.00 | 5,157.08 |
| continuation/long | 2,766.28 | 39,605.50 | 0 | 0.00 | 12,808.00 |
| continuation/spread | -3,353.55 | 10,194.74 | 0 | 0.00 | 4,853.38 |
| failed_break/long | 15,136.56 | -14,637.04 | 5 | 2,868.65 | 16,373.94 |
| failed_break/spread | 3,637.99 | -6,931.24 | 5 | 602.48 | 9,342.19 |

Earlier/later boundary is 6 October, matching the prior diagnostic. Neither period is an untouched holdout. Fallback returns are included in the totals; missing-data days are not silently assigned zero P&L.

## Common-date comparison: 34 sessions

| Experiment | Paired net Rs |
|---|---:|
| baseline/long/delay | -3,792.81 |
| baseline/long/quoted | -3,321.67 |
| baseline/long/slippage | -6,976.13 |
| baseline/spread/delay | -2,976.33 |
| baseline/spread/quoted | -4,136.53 |
| baseline/spread/slippage | -11,077.77 |
| baseline_no_target/long/delay | 19,257.34 |
| baseline_no_target/long/quoted | 14,496.30 |
| baseline_no_target/long/slippage | 11,250.54 |
| baseline_no_target/spread/delay | 1,221.07 |
| baseline_no_target/spread/quoted | -294.65 |
| baseline_no_target/spread/slippage | -7,937.15 |
| continuation/long/delay | 37,568.20 |
| continuation/long/quoted | 42,371.78 |
| continuation/long/slippage | 36,028.24 |
| continuation/spread/delay | 5,308.22 |
| continuation/spread/quoted | 6,841.20 |
| continuation/spread/slippage | -1,224.45 |
| failed_break/long/delay | -381.43 |
| failed_break/long/quoted | 499.52 |
| failed_break/long/slippage | -2,499.71 |
| failed_break/spread/delay | -3,268.79 |
| failed_break/spread/quoted | -3,293.25 |
| failed_break/spread/slippage | -10,057.53 |

## Coverage exceptions

- 2026-09-01: invalid_signal_history
- 2026-09-23: invalid_signal_history
- 2026-09-25: invalid_signal_history

## Decision limits

The continuation/long/no-target candidate merits further paper validation, not live promotion. It earned Rs 42,371.78 at quoted fills, Rs 36,028.24 with an extra adverse point per leg per side, and Rs 37,568.20 with an extra minute entry delay. These stresses were separate scenarios, not simultaneous stresses. All 34 eligible sessions generated a continuation entry, so its fallback was never exercised on this dataset; a daily-trade guarantee cannot be inferred from this coverage.

Risk and concentration: quoted maximum drawdown Rs 12,808.00; worst session -Rs 3,290.50; 18/34 winners. Full premium at risk for one lot ranged from Rs 2,973.75 to Rs 11,232.00, median Rs 7,099.63, before fees. The stop threshold is not a guaranteed maximum loss. The best three sessions contributed Rs 37,050.40 (about 87% of total net); excluding them leaves Rs 5,321.38. This sensitivity check is not a reason to delete those sessions, but shows dependence on large winners.

The earlier 21 sessions earned Rs 2,766.28 at quoted fills but lost Rs 2,060.43 under slippage; the later 13 contributed Rs 39,605.50 at quoted fills. Evidence of stability remains insufficient. Keep the existing production baseline unchanged and retain this candidate as a frozen research implementation until fresh validation and explicit risk limits support a decision.

Validation completed: 20 targeted tests passed, including future-data isolation, execution delay, fallback, stop retention, per-leg slippage, quote-quality failures and baseline reconciliation. All 888 experiment/session records were produced (37 dates times 24 configurations); all configurations share the same 34 eligible dates. The baseline matches the preceding diagnostic to less than one paisa.

Do not activate a candidate solely because it wins this comparison. There are eight related entry/structure experiments and three execution scenarios, all evaluated on previously inspected development data. Minute-book fills do not establish executable simultaneous leg fills or intraminute stop sequencing. Missing quote intervals invalidate eligibility; stops use observed prices and may exceed the threshold.

Next decisions require robustness across periods and execution scenarios, more historical data or locked prospective paper observations, and explicit account capital/daily/monthly loss limits. The daily real-trade objective does not override missing data or risk limits. No real-money profitability claim is made.
