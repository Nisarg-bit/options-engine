# Stock futures paper system: audit, baseline and daily governance (20 Nov 2026)

> **Superseded in part (21 Nov 2026):** automatic promotion and rollback (section 5) were replaced by an approval workflow. Every activation now needs your approval of the exact version; see `docs/strategy-research.md`. The audit findings, correctness fixes and baseline below still stand.

Paper only. Nothing in this system places a broker order. Evidence here comes from
the live paper ledger (`data/futures/futures.db` on the server, read with
`sqlite mode=ro`), replays of the recorded sessions, and fixtures, and each
table says which.

## 1. What the system is

**It is entirely rule-based.** There is no statistical model, no machine learning
and no training step anywhere in `futures/`. Every decision is a fixed rule with
versioned parameters in `futures/config/default.json` (deployed on the server as
`~/futures_config.json`, hash `52dde64aab2a`, config v1). "Improvement" here
means changing versioned parameters or rules, never retraining a model.

### Data and how often it updates
| Input | Source | Cadence | Used by |
|---|---|---|---|
| NIFTY 50 index 1-min bars | `data/bars_1m/.../underlying=INDEX` (options collector) | per minute | intraday regime (opening range, Fib zone); swing regime (daily bars built from these) |
| NIFTY near-month future 1-min bars | `data/stocks_1m/.../underlying=IDXFUT` | per minute, **since 19 Nov only** | intraday regime VWAP (a **proxy**: the index has no volume) |
| 218 F&O stocks, cash 1-min bars | `stocks_1m/.../EQ` | per minute | sector ranking, stock screening, Desk verification (money flow) |
| Stock futures 1-min bars + last-of-minute depth | `stocks_1m/.../STKFUT` | per minute | contract quotes, paper fills, OI read, exit monitoring |
| Instrument master (expiry, lot, tick, prev close) | `data/instruments/<day>.json` | daily 08:45 | contract selection |
| Sector map | `data/sectors.json` (niftyindices lists) | static, current membership | sector ranking |

The live adapter (`sf_data.RepoData`) re-reads a partition **at most every 20 s**
(`SNAP_TTL_S`). A 1-minute bar counts as available at its minute end. A quote is
the order book at the end of the previous minute (no look-ahead: the 10:55 quote is
the 10:54 minute's closing depth).

### Agents and the exact rules (file references)
1. **Market regime** (`sf_agents.MarketRegime`, `sf_regime.py`)
   - *Intraday* (`classify_intraday`): opening range = completed NIFTY 50 bars in
     [09:15, 09:30); Fib levels measured up from the range low at 0.380 / 0.500 / 0.618,
     frozen for the session.
     - BULLISH: the latest completed 1-minute NIFTY close is > Fib 0.618 **and** the
       NIFTY future's close is > its session VWAP (Σ traded value / Σ volume).
     - BEARISH: close < Fib 0.380 **and** future < VWAP.
     - Otherwise SIDEWAYS (shown as NEUTRAL).
     - UNAVAILABLE while the range forms, or when inputs are stale or misaligned.
     - All comparisons are strict and done in Decimal.
   - *Swing* (`classify_swing`): t = latest completed session.
     - BULLISH: 5-session return Close[t]/Close[t−5] − 1 > +0.5% **and** High[t] > High[t−1].
     - BEARISH: return < −0.5% **and** Low[t] < Low[t−1].
     - An outside day is resolved by the return; any session gap gives UNAVAILABLE.
   - **Both rules you described exist exactly as described.**
2. **Sector ranking** (`SectorRanking`): sectors aggregated from constituents,
   turnover-weighted.
   - Over the window (intraday: last 30 min of today; swing: 5 completed sessions), a
     sector needs ≥ 2 constituents and ≥ 60% fresh coverage.
   - LONG needs absolute return > 0 **and** relative to NIFTY > 0; SHORT needs both < 0.
   - Ranked by relative return; top 3. **Direction-aligned selection exists.**
3. **Stock screening**
   - *Intraday* (`sf_screen.screen_one`), for stocks in qualifying sectors:
     - LONG: the latest completed 1-minute close is > its session VWAP **and** > its
       opening-range high (09:15–09:30) **and** its 30-minute return is > NIFTY's over the
       same bars **and** 30-minute turnover ≥ ₹5 cr.
     - SHORT: the mirror image (below VWAP, below the opening low, return < NIFTY).
     - Ranked by excess return over NIFTY (longs descending, shorts ascending); ties go
       to turnover, then symbol. Top 5.
     - **All four rules you listed exist.** Note that the turnover is the 30-minute
       window's, not the whole session's.
   - *Swing*: 5-session return > +1% and > NIFTY's (longs; mirror for shorts), turnover ≥ ₹5 cr.
4. **Website verification** (`Verification`): the Nifty Desk numbers recomputed from bars.
   - LONG needs turnover ≥ ₹5 cr, net money flow > 0 and OI read "long buildup".
   - SHORT needs flow < 0 and "short buildup".
   - The snapshot must be ≤ 180 s old (intraday) or ≤ 900 s (swing).
   - The first passing candidate in rank order goes on.
   - The money flow is an **estimate** (tick-direction × traded value), and it shares
     its source with screening (flagged `shared_source_with_screening`).
5. **Futures contract + entry** (`FuturesEntry`)
   - Contract: nearest listed FUTSTK with lot, tick and expiry present, and a two-sided
     quote ≤ 90 s old.
     - Spread ≤ 20 ticks, and top-of-book size ≥ 1 lot.
     - Intraday also needs ≥ 1 day to expiry.
     - Swing needs ≥ 5 sessions before its pre-expiry deadline (expiry − 1 session, 15:15).
   - Size is **always exactly one lot**; nothing resizes.
   - Limits, checked in this order:
     - 6 open positions shared, 3 per mode
     - notional ₹60 lakh shared, ₹30 lakh per mode
     - estimated margin ₹12 lakh (20% of notional)
     - daily loss ₹50k
     - one position per underlying, so no reversal
   - Intraday also caps the opening-range stop distance at 2.5%.
   - **LONG = BUY to open, SELL to close; SHORT = SELL to open, BUY to cover.**
6. **Position monitor** (`PositionMonitor`): longs are marked on the bid and shorts on
   the ask (the executable side).
   - Exit precedence: hard risk → stop → pre-expiry → intraday 15:15 → max holding →
     invalidation → target.
   - Intraday stop: the stock's distance from its close to its opening-range edge, as a
     %, applied to the futures fill. Target 2R. Hard cap 2.5%. Flat by 15:15.
   - Swing: 3% stop, 6% target, 5 sessions after entry, plus the pre-expiry deadline.
   - Optional override (stop/target) within 5 / 30 min; the default plan stays active
     meanwhile.
7. **Paper exit** (`PaperExit`, `sf_broker.py`)
   - A single exit claim per trade.
   - A CLOSE order never exceeds the open quantity.
   - BUY fills at ask + 1 tick and SELL at bid − 1 tick, rounded to the tick against the trade.
   - No fill from a missing, stale or crossed quote. A missing quote at the deadline
     becomes an UNRESOLVED exception, never a synthetic price.

**Persistence and recovery:**
- SQLite in WAL mode. Primary keys enforce idempotency: one event id, one claim per
  consumer, one reservation per underlying, one exit claim, one client order id.
- Restart resumes open positions and pending exits.
- Every trade is pinned to the config version it entered under, and exits read that
  version, never the active one.

## 2. Audit findings

### Correctness defects (fixed, measured separately from strategy)
| # | Defect | Evidence | Fix |
|---|---|---|---|
| C1 | **Opening ranges include a price that never traded.** The collector chains each 1-minute bar's open to the previous close, so the session's 09:15 bar "opens" at yesterday's close, and its high or low is often that price. The NIFTY 50 index's 09:15 bar also carries pre-open spikes. | 20 Nov: 82% of stocks' 09:15 bar has high or low = open; NIFTY future 100%. NIFTY 50: 20 Nov OR high 25,514.3 = the synthetic open; 19 Nov OR low 24,664.4, ~2% below every later print. "Opening-range stop beyond 2.5%" was the #1 rejection (77 of 104 on 20 Nov). INDUSINDBK (gap-up +4%) was rejected for a "5.30% stop" because its range low was yesterday's close. | `opening_range(first_bar="close_only")`: the 09:15 bar contributes only its close (`sf_regime.py`). Versioned: v1 keeps `"ohlc"`, so its results stay reproducible. |
| C2 | **Replay saw data earlier than live could.** Live re-reads files at most every 20 s; replay treated each minute as available at its end. | Live exited HDFCBANK on the 15:13 depth at 15:15:04; replay used the 15:14 depth (845.65 vs 845.95). | `RepoData(lag_s=…)`; evaluation replays use 20 s (the live worst case). Live is unchanged. |
| C3 | (audit tool) My first ledger check looked for the quote in the wrong field and flagged every fill. | — | Fixed; it now also checks that no fill is better than its quote. |

**Not defects (verified):**
- **Sides and P&L:** sides, quantities, one lot and P&L recompute exactly for all 6 live trades.
- **Quotes:** no fill without a two-sided quote, and no fill better than its quote.
- **Records:** no duplicate orders, orphan reservations or unresolved exits.
- **Quote timing:** quotes are the previous minute's closing depth, so there's no look-ahead.

### Data-quality and coverage problems (open)
- **Coverage is short:** stock data exists for only **7 sessions** (12–20 Nov). The NIFTY
  future (the VWAP source) exists from **19 Nov**, so only **2 sessions** have complete
  intraday inputs.
- **Swing can't be judged yet:** NIFTY was SIDEWAYS on every session so far (5-day return
  inside ±0.5%), so swing has never been permitted. Zero swing trades.
- **Only the nearest FUTSTK expiry is collected**, so Swing can't roll to next month.
  Near month-end (25 Nov expiry) Swing is blocked by its own pre-expiry rule.
- **The sector map is today's membership** (survivorship). There's no historical universe,
  and no corporate-action adjustment.
- **The money flow used in verification is an estimate**, and it shares its source with screening.
- **EQ zero-volume minutes:** 3.6% on 20 Nov.
- **The true opening print isn't recorded.** The proper fix is collector-side: store the
  first tick as the 09:15 open. That's a reviewable proposal, not done here.
- **Replay can't reproduce live exactly:** live decisions depend on where a scan lands
  inside the 20 s refresh window. Recording the "data as of" time in live events would
  allow exact replays (proposal).

## 3. Baseline (pinned)
Code `07246b50501aaf1b` (sha256 of `futures/sf_*.py`). Data `35e70cc37be36ab7`
(3,003 input files, 19–20 Nov). Costs and fill model as configured: ₹20/order,
STT 0.02% sell, 1 tick slippage, top-of-book quotes, seed 7. Registry:
`data/futures/experiments/registry.jsonl`.

**Live paper ledger (19–20 Nov)**: all 6 trades were intraday LONG; there were no shorts
and no swing trades.

| | Trades | Net | Gross | Costs | Expectancy | Win rate | Avg win / loss | Profit factor | Max DD | Turnover | Exposure | 80% interval / trade |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Intraday long | 6 | **+₹5,755** | +₹7,140 | ₹1,385 | +₹959 | 50% | +₹4,971 / −₹3,053 | 1.63 | −₹4,749 | ₹41.8 lakh | 1,504 min | −₹1,534 … +₹3,171 |
| Intraday short | 0 | — | | | undefined | undefined | | undefined | | | | |
| Swing long / short | 0 / 0 | — | | | undefined (regime never permitted) | | | | | | | |

Excluded 0, unresolved 0, open 0. Every trade exited at the 15:15 deadline; none
reached its stop or its 2R target.

**Replays of the same 2 sessions** (registry entries; latency-adjusted runs are the
reference):
| Run | Config | Data latency | Trades | Net | Expectancy | Profit factor | Max DD | 80% interval |
|---|---|---|---|---|---|---|---|---|
| baseline-v1 | v1 (deployed) | 0 s | 6 | −₹555 | −₹92 | 0.95 | −₹5,685 | −₹2,378 … +₹2,210 |
| **baseline-v1-lag20** | v1 (deployed) | 20 s | 6 | +₹5,914 | +₹986 | 2.01 | −₹2,953 | −₹551 … +₹2,488 |
| baseline-v1-fix | v1 + C1 | 0 s | 7 | +₹56,758 | +₹8,108 | 8.20 | −₹7,439 | +₹3,319 … +₹13,109 |
| **baseline-v1-fix-lag20** | v1 + C1 | 20 s | 6 | +₹72,297 | +₹12,050 | undefined (no loss) | ₹0 | +₹7,597 … +₹16,797 |

How to read this honestly:
- **Timing alone moves v1 by ₹6,500** on the same two days, because which candidates get
  entered depends on seconds. Replay with 20 s latency reproduced 4 of the 6 live trades
  (PIDILITIND exactly).
- **What the fix does:** it changes which trades happen. It lets gap-up leaders (MOTHERSON,
  APOLLOHOSP, HYUNDAI) through at 09:45 instead of being rejected for a stop distance
  that came from yesterday's price.
- **This is not evidence of profitability:** two sessions, one bullish regime, all longs,
  6–7 trades. C1 is adopted because the v1 inputs were wrong, not because of this P&L.

### Walk-through: a recorded long trade (live, 20 Nov, HDFCBANK)
- **10:55:03, scan.** Regime BULLISH: NIFTY close 25,563.4 > Fib 0.618 = 25,387.76 (range
  25,183.05–25,514.30, where the high is the synthetic open, C1). NIFTY future 25,539.1 >
  VWAP 25,462.89.
- **Sectors, LONG** (NIFTY 30-min return +0.120%): Bank +0.535% (relative +0.415), Power,
  PSU Bank.
- **Candidates:** INDUSINDBK, UNIONBANK, AXISBANK, ICICIBANK, HDFCBANK.
- **Verification and entry checks:**
  - INDUSINDBK passed verification, then was rejected: stop 5.30% > 2.5%.
  - UNIONBANK passed verification, then was rejected: stop 2.92%.
  - AXISBANK and ICICIBANK failed verification on money flow (net −₹537 cr and −₹1,265 cr).
  - HDFCBANK passed: turnover ₹2,973 cr, flow +₹64.8 cr, OI +1.60% with price +1.48% =
    long buildup.
- **Screen values for HDFCBANK:** close 835.70 > VWAP 826.65 and > opening high 830.10;
  30-min return +0.590% vs NIFTY +0.140%.
- **10:55:13, entry:** BUY 650 (one lot) HDFCBANK26NOVFUT. Quote bid 832.90 / ask 832.95,
  so the fill was 833.00 (ask + 1 tick). Costs ₹46.12.
- **Plan:** stop 2.417% (stock 835.70 vs opening low 815.50) = 812.87; target 2R = 873.27;
  deadline 15:15.
- **15:15:04, exit:** intraday deadline. SELL 650 on bid 846.00, filled at 845.95. Costs
  ₹145.45 (STT ₹109.97).
- **Result:** gross +₹8,417.50, costs ₹191.57, **net +₹8,225.93**.

### Walk-through: a short trade (FIXTURE REPLAY, no live short exists)
This is synthetic bearish fixture data (`sf_fixtures.bearish`, 17 Nov), labelled FIXTURE.
- **09:45, regime BEARISH.** Metal sector −0.68% (relative −0.23).
- **Candidate:** TATASTEEL ranked first and was verified: turnover ₹15 cr, flow −₹4.5 cr,
  short buildup.
- **Entry:** SELL 5,500 (one lot) TATASTEEL26NOVFUT at 148.95 (bid − 1 tick).
- **15:15, exit:** deadline. BUY 5,500 at 146.80 (ask + 1 tick).
- **Result:** gross +₹11,825.00, costs ₹262.32, **net +₹11,562.68**.
- **Duplicate check:** at 09:50 a second TATASTEEL signal was refused ("already has an
  active position"), so a duplicate signal can't add a lot.

## 4. Experiments
**Budget:** at most 3 candidates per cycle and 24 per 30 days. Every candidate goes in the
registry, including failed and rejected ones. **Split:** chronological 60/20/20 with a
Swing embargo of max-holding + 1 sessions; a test window is used once (burned). **Minimum
to evaluate anything:** 15 clean sessions.

**Run so far:** only the four baselines above. **No strategy candidate was evaluated.**
With 2 complete sessions, the minimum isn't met, and searching parameters on 2 days would
only fit noise. **Decision: retain the v1 rules.** Apply C1 as a correctness fix (config
v2), with v1 kept as last-known-good and run in shadow on the same sessions.

**Evidence still needed before any strategy change:**
- **Intraday:** 15+ sessions with complete inputs (from 19 Nov, so about the second week
  of December) and at least one non-bullish stretch.
- **Shorts:** they need a BEARISH regime to occur at all.
- **Swing:** it needs a trending 5-day NIFTY move, and next-month contracts (collector
  change) to trade near month-end.

## 5. Daily automated process (`futures/sf_daily.py`)
- **Schedule:** `options-futures-eval.timer`, Mon–Fri 16:10 IST. The job checks the XBOM
  calendar and records holidays as SKIPPED.
- **Steps:** reconcile → validate and quarantine → metrics by mode/direction → compliance
  vs the trade's own config version → drift (`robust_z/1`) and deterioration
  (`expectancy_ci/1`) → experiment eligibility → chronological evaluation → shadow →
  promotion / rollback assessment → `data/futures/eval/<session>.json`, `latest.json` and
  `audit.jsonl`, shown in Stock Futures → Health.
- **Robustness:**
  - Idempotent per session.
  - A file lock prevents parallel jobs.
  - Late feeds give exit 75; systemd retries every 20 min, up to 6 times.
  - A crash leaves no partial record.
  - Activation checks the live active version, so a rerun can't promote twice.
- **Champion/challenger:**
  - Challengers are immutable configs, stored by hash. They run in **shadow** inside the
    worker, each with its own database (`data/futures/shadow/`), with no alerts.
  - **Promotion:** an atomic `active_config` switch in one locked transaction. It applies
    to future entries only; open trades keep their entry version.
  - The previous champion becomes last-known-good and keeps shadow-trading.
  - **Rollback triggers:** drawdown > 1.5× the shadow max or > ₹50k; last-known-good
    beating the champion (80% lower bound > 0 over 5+ sessions); a ledger or spec failure.
  - **Operator controls:** `python sf_daily.py auto-promotion --off` keeps decisions
    computed and recorded but takes no action. Entry pauses stop entries; monitoring and
    exits continue.
- **Promotion requirements** (all provisional and pre-registered in
  `futures/config/governance.json`):
  - ≥ 15 forward shadow sessions and ≥ 20 settled trades
  - paired daily P&L vs the champion with an 80% lower bound > 0 (stricter when more
    candidates were compared: 1 − 0.20/k)
  - still net positive with +1 tick per side
  - drawdown ≤ 1.25× the champion's and ≤ ₹50k
  - ahead in 2 of 3 sub-periods
  - no direction materially worse
  - no ledger, spec or quarantine failure
  - With insufficient evidence the decision is CONTINUE_SHADOW. A challenger that fails
    with twice the evidence is REJECTED.
- **Search space** (the only thing automation may change): intraday stop cap, R
  multiple, turnover floor, screen lookback, sector top-N and opening-range first-bar
  mode; swing threshold, stop, target and holding period. **One lot, portfolio limits,
  costs, the fill model, the calendar and direction maps are outside it.** Broader changes
  (like the collector open fix) are written up as reviewable proposals.

## 6. Commands
```
python futures/sf_audit.py all --day 2026-11-20                  # read-only audit (ledger, funnel, data)
python futures/sf_experiments.py replay --config futures/config/default.json --sessions 2026-11-19,2026-11-20 --name my-run
python futures/sf_experiments.py list                            # the registry
python futures/sf_daily.py run [--day D] [--force] [--no-experiments]
python futures/sf_daily.py status
python futures/sf_daily.py auto-promotion --off | --on
python futures/sf_cli.py worker --source repo --mode both        # champion + shadow challengers
python futures/sf_cli.py replay --source repo --date 2026-11-20 --mode both
```
On the server everything sits in `/home/ubuntu`. Environment: `SF_DB` (optional),
`SF_GOVERNANCE` (optional), and `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` for alerts.
