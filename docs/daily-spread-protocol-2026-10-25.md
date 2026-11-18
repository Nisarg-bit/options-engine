# Daily spread paper experiment, version 1

Rules frozen before replay of these candidates on 25 October 2026. The existing
historical data has already been inspected in prior work, so none of this replay
is an independent out-of-sample validation. No real-money orders are enabled.

## Fixed rules

- NIFTY only; one instrument-master lot; one entry per data-valid market session.
- Observe parity-implied forward prices from 09:20 through 09:44 IST.
- Opening displacement = final minus first forward. Range = maximum minus minimum.
- Trend strength = absolute displacement / range; zero if the range is zero.
- At 09:45, use strikes identified at 09:44, with width two strike steps.
- Trend candidate: buy ATM call spread if displacement is nonnegative, otherwise
  buy ATM put spread. Reversal candidate takes the opposite direction. The tie
  convention is fixed, not a claim that a flat opening predicts rising prices.
- Neutral candidate: sell ATM call and put; buy call two steps above and put two
  steps below, all same expiry and equal quantity (iron butterfly).
- Adaptive candidate: trend spread if strength is at least 0.60, otherwise the
  iron butterfly. This is the initially designated forward-paper hypothesis;
  the other three are comparisons, not parameters for a broad optimization.
- R = gross maximum loss of the complete structure at expiry, from actual entry
  bid/ask premiums. It excludes charges and is not a guaranteed intraday exit cap.
- Exit on observed net loss of 0.35R, net profit of 0.50R, or at 15:00 IST.
  Stops/targets include estimated order charges. Actual observed close prices are
  used even when a threshold is overshot. No re-entry, averaging or overnight plan.
- Buys at ask, sells at bid. Stress test adds one premium point per leg per side.
- No opening-window/entry quotes means no invented entry. Missing held quotes or
  exit means the result cannot be certified clean. Daily frequency is conditional
  on valid market data, never an excuse to fabricate fills.

## Evaluation and record

Compare all candidates with the old baseline on the intersection of measurable
sessions; retain explicit excluded dates. Report net, worst day and drawdown for
the earlier two thirds and later third, plus stressed costs. Equal lots do not
mean equal risk: all spreads have different payoff envelopes from the old naked
straddle. Do not describe lower rupee losses as superior capital efficiency.

Prospective recording starts 26 October 2026 in a separate versioned journal.
Historical replays cannot enter that journal. The old baseline remains a labelled
control for comparison. Any rule changes require a new version and activation
date. A completed software test or a profitable replay is not live approval.

## Post-replay decision, before prospective activation

The adaptive candidate's later-period profit became a loss under the specified
slippage stress. Its neutral branch and the standalone iron butterfly did not
justify activation. The simpler **trend** debit-spread candidate is the frozen
forward paper policy: call spread for nonnegative displacement, put spread for
negative displacement, with the same entry, width and exits above. This choice
was made after inspecting these historical results and must not be presented as
an unbiased performance estimate. The original candidate definitions are retained
so the rejected alternatives remain visible.

On 34 comparable sessions, trend net was -Rs 4,136.53, including -Rs 6,606.06
earlier and +Rs 2,469.53 later (13 sessions). Later stressed net was +Rs 805.28;
full-period stressed net was -Rs 11,077.77. Worst historical trend day was
-Rs 1,179.33 versus -Rs 7,541.64 for the old baseline. These are rupee comparisons
at equal lots, not equal risk or margin; no profit improvement is established
prospectively. The primary improvement is changing the payoff and limiting the
complete spread's expiry exposure, while collecting new evidence.

Protective-leg payoff mechanics are described in the
[NISM Equity Derivatives FAQ](https://www.nism.ac.in/wp-content/uploads/2021/02/Equity-Derivatives-FAQ-document-1.pdf)
and [Zerodha's bull call spread chapter](https://zerodha.com/varsity/chapter/bull-call-spread/).
These describe structures; they do not validate this entry signal.

## Deployment checks

Full regression suite: 574 passed, 3 skipped, four existing pandas warnings.
Final candidate and API integration tests on the server: 12 passed. All generated
JavaScript blocks passed parsing. The server deployed daily_spread_policy.py,
daytrade.py and daytrade_api.py; the collector retained PID 2588285. Rollback
copies: /home/ubuntu/spread-research-20261025/backup-1792914813.

The existing daily journal timer now also persists the spread result, in
data/journal/daily_spread_v1.json. API responses include paper_policy alongside
the old control. Before activation, the published metadata correctly reports
trend, 09:45 entry, 15:00 exit deadline, zero forward sessions and zero forward
P&L. Metadata was published independently while the ordinary publisher warmed
its cache; it does not substitute historical results for prospective trades.

Firebase Hosting release completed. Browser verification confirmed the new
09:45/15:00 rule card, the zero-session prospective record, and the labelled old
control. Visual evidence: data/daily-spread-policy-live.png. Raw replay rows are
retained in data/daily_spread_research.json; the public website exposes a compact
summary in daily-spread-research.json.
