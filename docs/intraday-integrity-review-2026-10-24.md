# Intraday integrity changes — 24 October 2026

Update, 25 October: server access was restored and the tested changes were
deployed. The blocked-access status below describes the initial review only.
See [completed audit](intraday-audit-2026-10-25.md) for replay results and deployment.

## Scope and policy

The daily paper baseline still enters once per usable session at 09:20 and exits
at the observed premium stop or 15:15. The weekly gate does not control it.
No real-order execution was added. No replacement strategy has been validated or
promoted. Missing data must not be represented as a successful trade or backtest.

## Implemented locally

- Intraday fees use individual entry/exit order premiums and quantities, including
  brokerage GST on every order. Historical workbook fixture calculations remain
  separate and unchanged. Rates are current tariffs, not a historical tariff table.
- Decay and entry richness use elapsed clock minutes, not the count of stored rows.
- A time exit requires a valid closing quote at the scheduled time. An observed
  stop settles immediately; its fill uses the observed ask, including overshoot.
- Late entry, missing held-position quotes and missing exit are explicit quality
  flags. Legacy records are retained but not certified as clean observations.
- Lot sizes resolve from the same day's instrument master and the traded expiry.
  A fallback is labelled unverified and excluded from the clean sample.
- API journals filter by underlying, and journal updates key by date plus underlying.
- Summaries include clean-sample statistics, average win/loss, profit factor and
  descriptive break-even win rate. Historical totals retain original observations.
- Primary daily P&L is net. Approved live size and binary Kelly sizing are zero
  because no strategy has passed deployment validation. Paper trades continue.
- Website text separates model benchmarks from market-implied forecasts, labels
  the collection milestone correctly, and removes several stale performance claims.
- The default website build preserves existing page fixes and refreshes authored
  scripts. The old classic reconstruction requires --classic-rebuild explicitly.

## Verification

Full suite: 561 passed, 3 skipped. Four existing pandas deprecation warnings.
Added regression coverage includes costs, missing-minute decay, missing closing
quotes, late entries, same-day exits, stop overshoot, clean sample isolation,
cross-index journal retention, missing-data backtest status and browser sizing.

## Strategy experiment

research/intraday_validation.py compares four predeclared daily intraday schedules
on paired, complete sessions. The final chronological third is held out. It never
automatically promotes a strategy. Running it locally returned
blocked_missing_minute_bars: there are no data/bars_1m partitions in this checkout.

The configured SSH server timed out even after network access was approved.
Therefore no recent raw-data replay, historical journal rewrite, live website
deployment or server deployment was performed. Existing production results have
not changed and no improvement in returns is claimed.

## Required next work

Restore access to the collector's minute bars and same-day instrument masters.
Rebuild a separate versioned journal with corrected calculations; reconcile fills
and fees before comparing it to the existing record. Evaluate candidate schedules
on untouched data, including stress costs and tail losses, before adopting one.

One-minute bid/ask replay does not capture intraminute stop touches, depth, queue
position or execution latency. These remain execution limitations. The weekly
probability model, volatility clock and strategy-builder EVs remain unvalidated;
they are not repaired by setting live size to zero. Protective-wing strategies and
a calibrated stochastic-volatility/stop model still require data-backed research.
