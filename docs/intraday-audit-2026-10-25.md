# Intraday audit and deployment — 25 October 2026

## Outcome

Corrected the intraday accounting, quote-coverage checks, elapsed-time calculations,
lot resolution and dashboard risk presentation. Deployed the server modules and
Firebase Hosting website. No profitable replacement strategy was validated.
The application remains paper-only, with one scheduled 09:20 entry per valid
session, an observed 30% premium stop or 15:15 exit, and no overnight holding.
Missing or unusable quotes cannot honestly be represented as a filled trade.

## Evidence from the server data

Replayed 37 NIFTY sessions from 31 August through 23 October 2026. Same-day
instrument masters supported the 65-unit lot assumption used in this comparison.
Three delayed-entry sessions (1, 23 and 25 September) were excluded from the clean
sample. The original ledger was preserved; corrections use intraday_trades_v2.csv.

| Measure | Result |
|---|---:|
| Original 37-session net per lot | -Rs 37,984 |
| Corrected 37-session net per lot | -Rs 38,000 |
| Clean sample | 34 sessions |
| Clean net per lot | -Rs 37,290 |
| Clean winning sessions | 15 / 34 |
| Average clean winner | Rs 1,373.20 |
| Average clean loser, magnitude | Rs 3,046.74 |
| Clean profit factor | 0.356 |
| Clean observed stops | 12 / 34 |
| Mean VIX-driven model stop probability | 16.11% |

Losses are not explained by the fee bug: correcting accounting changed the total
by only Rs 16. In this sample losses were larger and more frequent than winners.
The descriptive break-even win rate at those average payoffs is 68.9%, versus
44.1% observed. These are sample descriptions, not forecasts. The fixed-volatility
stop model understates observed stops, and does not justify Kelly or live sizing.

## Daily schedule comparison

All four candidates use the same 34 paired sessions, actual recorded bid/ask
quotes and estimated order costs. The first 21 sessions form the earlier period;
the final 13, beginning 6 October, form the later period.

| Candidate | Earlier net | Later net |
|---|---:|---:|
| 09:20–15:15 ATM straddle | -Rs 13,224.80 | -Rs 24,064.37 |
| 09:45–15:15 ATM straddle | -Rs 21,703.02 | -Rs 23,983.39 |
| 09:45–14:30 ATM straddle | -Rs 18,679.16 | -Rs 18,667.25 |
| 09:20–15:15 two-strike strangle | -Rs 10,062.34 | -Rs 18,669.06 |

All lost in both periods. Historical outcomes had already been seen during the
review, so this is not untouched or prospective validation, even though the code
labels the chronological later split holdout. No candidate was promoted.

## Validation and rollout

- Full local suite: 561 passed, 3 skipped; four pandas deprecation warnings.
- Candidate server regression suite: 50 passed.
- Final dashboard check: 3 passed, including repeat-build idempotence; all five
  final generated JavaScript blocks parsed.
- Server deployment atomically replaced six modules and installed the separate
  v2 ledger. API and publisher restarted; collector PID 2588285 stayed unchanged.
- All three services were active after deployment. Rollback files are stored at
  /home/ubuntu/research-audit-20261025/deployment-backup-1792853873.
- Website: https://nifty-desk.web.app/ . The Proof page includes the four-candidate
  comparison, clean-sample disclosure and stop-model warning. Live approved size
  is zero; the daily paper baseline remains enabled.
- Final readback confirmed the live database and rendered Proof page use the v2
  ledger: net -Rs 38,000, 34 clean sessions, three excluded and clean net
  -Rs 37,290. The publisher's first cold refresh took approximately ten minutes;
  it completed successfully. The daily journal timer remains active.

## Remaining limitations

One-minute quotes cannot reproduce intraminute stop touches, order depth, queue
position or latency. Fee rates are configured tariffs, not historical rate tables.
Legacy post-mortem and weekly records retain their historical calculations.
The weekly probability model, volatility clock and strategy-builder EVs remain
unvalidated. Setting approved live size to zero does not repair those models.
Protective-wing and directional candidates need separate research and genuinely
future observations before any profitability claim or real-capital use.
