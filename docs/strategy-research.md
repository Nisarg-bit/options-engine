# Stock futures: strategy library, research agent and approvals

This is how strategies get researched, tested and activated. It's paper trading of NSE single-stock
futures only: Intraday and Swing, LONG and SHORT, one lot per entry, shared portfolio limits.

## What runs where

| Process | Unit | What it does |
|---|---|---|
| Paper worker | `options-futures` | Trades the **active** strategies and runs every **SHADOW** strategy beside them, each with its own database (`data/futures/shadow/<version>.db`). |
| Daily checks | `options-futures-eval.timer` (16:10 IST, weekdays) | `sf_daily.py`: reconciliation, data checks, metrics, drift, compliance, and an automatic entry pause on hard-risk failures. It counts each completed session toward the research cadence and **never** activates anything. |
| Research agent | `options-futures-research` | `sf_research.py worker`: research cycles, builder replays, suggestions. Separate process with low priority. |
| Dashboard API | `options-api` | Library, builder, research, approvals (behind the dashboard login). |

All of these keep running with the browser and Claude Code closed.

## Strategy definitions (schema `sf-strategy/1`)

A strategy is versioned JSON. `futures/sf_strategy.py` documents the full schema. One evaluator
(`sf_strategy.evaluate`, used by the engine's `conditions` screener) serves the builder preview,
research replays, shadow and active paper trading.

- **Gates** (`market`, `sector`, `verification`): the existing regime, sector and Nifty Desk checks. Market and sector are **locked** by default.
- **Entry rules**: per direction, as `all` / `any` groups of conditions comparing an indicator with another indicator or a number. Operators: `> >= < <=`, plus `cross_above` and `cross_below`.
- **Ranking, top N, exits and timing**:
  - Stop basis: `opening_range_edge`, `pct` or `atr`.
  - Target basis: `r_multiple`, `pct` or `atr`.
  - Optional trailing stop (%).
  - Intraday: a time exit. Swing: a maximum holding period and the pre-expiry buffer.

**Semantics:**
- **Completed bars only.** Higher timeframes are built from complete buckets aligned to 09:15.
- **Missing values never pass.** A missing value, or one still warming up, makes a condition UNAVAILABLE.
- **Crossovers:** `a[t-1] <= b[t-1]` and `a[t] > b[t]`.
- **Prices aren't mixed.** Equity bars drive the rules; the futures contract is resolved and priced separately.

**Indicators** (`futures/sf_indicators.py`; pure Python, no third-party TA library):

| Group | Indicators |
|---|---|
| Price and moving averages | price, SMA, EMA, session VWAP, anchored VWAP |
| Momentum | RSI, MACD, stochastic, ROC, momentum |
| Volatility and range | ATR, Bollinger Bands, high-low range |
| Trend | ADX/DMI, Supertrend |
| Volume | volume, relative volume, OBV, turnover |
| Session levels | opening-range high/low, Fibonacci level, prior-session high/low/close |
| Relative | return vs NIFTY |
| Derivatives and flow | OI, OI change %, net money flow estimate |

To add an indicator, add one entry to `REGISTRY`. An indicator whose data isn't available
(for example OI for swing) is rejected with a clear message.

**Templates:** the deployed v1 rules are imported as `tpl-intraday-v1` and `tpl-swing-v1`. Where
the template's expression differs from the deployed code, the difference is listed, not silently
changed. Example: the 30-minute window can first pass at 09:46 instead of 09:45.

## Lifecycle and evidence

| | Values | Notes |
|---|---|---|
| Lifecycle | DRAFT → SHADOW → ACTIVE; ACTIVE → DISABLED; DISABLED → SHADOW; any → ARCHIVED; ARCHIVED → DRAFT | **Only an approved activation sets ACTIVE.** |
| Evidence | INSUFFICIENT, EARLY, EVALUATED | Labels only. They never gate research or scheduling. |

- **Edits always create a new version.** An edit of an ACTIVE strategy becomes a separate DRAFT copy.
- **Disabling or archiving** stops new entries only. Open positions keep their entry config and exit plan, and all history stays.

## Research cadence

- **When:** every 3 completed XBOM sessions (`futures/config/research_policy.json` → `cadence.every_sessions`).
- **How it counts:** `sf_daily.py` calls `sf_research.on_session(day)` after each session. The counter
  and last processed session are stored durably, so a session is never counted twice.
- **Late data:** delayed feeds defer the daily job; the session is counted when it completes.
- **Research now:** queues a manual cycle unless one is already queued or running, and doesn't reset the counter.

**What a cycle does:**
1. **Review** the active strategies, available data, diagnostics, history and dismissals.
2. **Tune:** one-parameter moves inside the open areas.
3. **Discover** online and extract rules, when a provider is configured.
4. **Compile and validate**, and record lock changes.
5. **Deduplicate** by rule structure.
6. **Replay** over every complete recorded session. Same costs, fills and 20-second data latency as the active strategy, which runs as the baseline.
7. **Freeze into SHADOW:** forward evidence only counts from that moment.
8. **Create or update suggestions**, and notify once per meaningful change.

**Learned components:** there are none, and the system says so in every suggestion. The
retraining path stays inactive until a trained component exists.

## Online research providers

Set these in `/home/ubuntu/.env` (secrets stay on the server):

```
SF_SEARCH_PROVIDER=anthropic_web   # or: fixture (offline test data, always labelled) / none
SF_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...              # you add this; never paste it in chat
SF_LLM_MODEL=claude-opus-5-5       # optional; default claude-opus-5-5
```

Also install the SDK once: `~/.venv/bin/pip install anthropic`.

**How it works:**
- **Search:** Claude's server-side `web_search` tool (`web_search_20260209`), limited by `web_search_max_uses_per_cycle`.
- **Extraction:** a second call with structured JSON output extracts testable rules.
- **Fallbacks:** both calls opt into server-side refusal fallbacks (`fallbacks: "default"`).
- **Untrusted content:** retrieved pages are data only. The output is validated against the schema and never executed.
- **Status:** the dashboard shows each provider's status, including "not configured" and "SDK not installed".

**Budgets** (`research_policy.json` → `budgets`): queries, ideas, tuned and new candidates per cycle,
replay minutes per cycle, LLM calls and tokens, maximum shadow strategies, one concurrent job.

## Approvals and activation (`futures/sf_approval.py`)

**Every change to future active entries needs your approval.** That includes tuning and rollbacks.

**A suggestion offers five actions:**
1. **Replace current** strategy for a profile.
2. **Run alongside** it, with a priority.
3. **Continue testing.**
4. **Dismiss** (optional reason, and whether to resurface it if new evidence arrives).
5. **Edit a copy** in the builder.

**What activation checks:**
- **Exactness:** the approval is bound to the exact version ID and evidence hash. A changed version or new evidence needs a fresh approval.
- **Re-checked at activation time:** validity, data availability, and locks. A locked gate that differs from the active strategy (any parameter) must be approved explicitly.
- **Hard constraints:** these always come from the portfolio config.

**How it's applied:**
- **Atomic and idempotent:** one transaction in the trading store writes `active_strategies` and the compiled config version.
- **Audited:** `StrategyActivated` events record the before and after lists.

**Several active strategies:**
- **Shared limits:** they share reservations, limits and one position per underlying.
- **Priority:** scans run in visible priority order, so the higher priority reserves first.
- **Rejections recorded:** a candidate rejected for capacity or a conflicting exposure is logged.
- **No reversals:** an opposite signal can't reverse a position.
- **Two numbers per strategy:** standalone potential comes from its shadow database; executed results come from the active portfolio.

**Not approval-gated:**
- **Editing `futures_config.json` on the server** is the operator path for hard limits and costs. A changed file becomes a new config version for future trades only.
- **The old HTTP `POST /api/futures/ops/config`** activation path is closed (returns 410).

**Automatic pauses:** the daily job may pause **new entries** for a ledger failure or the daily
loss limit; monitoring and exits continue, and you get an alert. Pausing and disabling need no
approval: they only stop trading.

## Commands

```
python futures/sf_research.py import-templates     # once, after deploy
python futures/sf_research.py now                  # queue a research cycle (no duplicates)
python futures/sf_research.py status               # providers, cadence, last cycle, jobs
python futures/sf_research.py worker               # the research agent (systemd runs it)
python futures/sf_daily.py run [--day D]           # daily checks + cadence (systemd timer runs it)
python futures/sf_cli.py worker --source repo --mode both   # paper worker incl. shadow strategies
python futures/sf_experiments.py list              # every replay, including failures
```

## Data limits that affect evidence (21 Nov 2026)

- **Intraday:** the NIFTY futures VWAP source exists from 19 Nov only, so intraday replay has 2 complete sessions.
- **Swing:** has 7 sessions of stock data, and only the nearest FUTSTK expiry is collected, so swing entries are blocked near month-end.
- **First-bar defect:** the 09:15 bar's open/high/low are contaminated; indicators default to `first_bar: close_only`.
- **Universe:** sector membership is today's list, with no corporate-action adjustment.
- **What this means:** with fewer than 15 complete sessions there is no chronological train/validation/test split. Every replay is labelled in-sample, and most evidence reads INSUFFICIENT or EARLY until more sessions accumulate.
