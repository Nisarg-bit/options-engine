"""A position that knows what it is worth and when to get out.

Nothing in the stack does this today. `daily_signal.py` writes a row and
`journal.score()` settles it at expiry from put-call parity -- entry and
outcome, with nothing in between. There is no object that can answer "what is
this worth right now, and should I still be in it", which is exactly what an
85%-of-credit exit rule requires.

SIGN CONVENTION, once, so nothing downstream has to think about it

    side = -1  sold (short)
    side = +1  bought (long)

    value_pts(prices) = sum(-side * price)

At entry that sum IS the net credit: short legs contribute their premium as
income, long legs subtract theirs. At any later moment the same sum is what
it would cost to close. So one function serves both, and

    pnl_pts = credit - value_now

falls out with no special cases for condors, ratios or multi-lot.

THE EXIT RULES, in the order they are checked

    decay   value has fallen to (1 - capture) of the credit. Checked first
            because it is the outcome you actually want.
    eod     intraday only: flat by a wall-clock time, no exceptions.
    time    dte has reached the hard floor. Nisarg's rule is out 1-2 days
            before expiry; the default is 2.
    expiry  dte <= 0 and nothing else fired. Settled, not closed.

MAXIMUM ADVERSE EXCURSION is tracked throughout, because the difference
between terminal POP and no-touch POP is only meaningful if you also record
how far underwater the trade actually went. A position that finished inside
having been 300 points offside is not the same trade as one that never
moved, and averaging them together is how a record stops telling the truth.

Deliberately pure: it consumes (timestamp, prices, dte) and knows nothing
about parquet, Kite or the journal. The bars adapter is separate so this can
be tested in memory, which is also how it gets backtested over all ten soaked
sessions without a fixture directory.
"""

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional

import pricing as P

CE, PE = "CE", "PE"


# ----------------------------------------------------------------- the parts

@dataclass(frozen=True)
class Leg:
    strike: float
    opt_type: str               # CE or PE
    side: int                   # -1 sold, +1 bought
    entry_px: float

    def key(self):
        return (float(self.strike), self.opt_type)

    def __post_init__(self):
        if self.opt_type not in (CE, PE):
            raise ValueError(f"opt_type must be CE or PE, got {self.opt_type!r}")
        if self.side not in (-1, 1):
            raise ValueError(f"side must be -1 or +1, got {self.side!r}")


@dataclass(frozen=True)
class ExitRule:
    """Nisarg's rule: 80-90% of premium decayed, out 1-2 days before expiry.

    `capture` is the fraction of the credit banked. 0.85 sits in the middle of
    the band he gave and is the default; the band itself is the point, so it
    is a parameter rather than a constant.
    """
    capture: float = 0.85
    hard_exit_dte: int = 2
    flat_by: Optional[dtime] = None      # intraday: square off by this time


@dataclass
class Exit:
    ts: datetime
    reason: str                 # decay | eod | time | expiry
    value_pts: float            # cost to close at the moment of exit
    pnl_pts: float
    captured: float             # fraction of credit banked
    mae_pts: float              # worst mark-to-market, in points, negative
    bars: int

    def pnl_rs(self, lot_size, lots=1, defined_risk=False,
               costs=P.DEFAULT_COSTS, credit_pts=None):
        """Realised rupees after round-trip costs.

        `transaction_costs` is priced off the CREDIT, not the P&L -- charges
        scale with turnover, and turnover is set when the position is opened.
        """
        gross = self.pnl_pts * lot_size * lots
        base = self.pnl_pts if credit_pts is None else credit_pts
        fees = P.transaction_costs(abs(base), lot_size, lots,
                                   defined_risk=defined_risk, costs=costs)
        return gross - fees


# -------------------------------------------------------------- the position

@dataclass
class Position:
    legs: tuple
    lot_size: int
    entry_ts: datetime
    expiry: object                       # date
    rule: ExitRule = field(default_factory=ExitRule)
    lots: int = 1
    label: str = ""

    # running state
    bars: int = 0
    mae_pts: float = 0.0                 # most negative P&L seen, <= 0
    mfe_pts: float = 0.0                 # most positive, >= 0
    best_captured: float = 0.0
    last_value: Optional[float] = None
    exit: Optional[Exit] = None

    @property
    def credit_pts(self):
        """Net premium taken in at entry. Negative means a net debit."""
        return sum(-leg.side * leg.entry_px for leg in self.legs)

    @property
    def defined_risk(self):
        return any(leg.side == 1 for leg in self.legs)

    @property
    def is_open(self):
        return self.exit is None

    def value_pts(self, prices):
        """Cost to close, in points, from a {(strike, opt_type): px} map.

        Raises on a missing leg rather than guessing. A position that cannot
        be marked is not worth zero -- it is unknown, and silently treating
        unknown as zero would report a full-profit exit on a data gap.
        """
        total = 0.0
        for leg in self.legs:
            px = prices.get(leg.key())
            if px is None:
                raise KeyError(f"no price for {leg.strike:.0f} {leg.opt_type}")
            total += -leg.side * float(px)
        return total

    def captured(self, value):
        c = self.credit_pts
        if c == 0:
            return 0.0
        return (c - value) / c

    def step(self, ts, prices, dte):
        """Mark against one bar. Returns an Exit the first time one fires."""
        if self.exit is not None:
            return self.exit

        value = self.value_pts(prices)
        pnl = self.credit_pts - value
        cap = self.captured(value)

        self.bars += 1
        self.last_value = value
        self.mae_pts = min(self.mae_pts, pnl)
        self.mfe_pts = max(self.mfe_pts, pnl)
        self.best_captured = max(self.best_captured, cap)

        reason = None
        if cap >= self.rule.capture:
            reason = "decay"
        elif self.rule.flat_by is not None and ts.time() >= self.rule.flat_by:
            reason = "eod"
        elif dte <= self.rule.hard_exit_dte:
            reason = "time"
        elif dte < 0:
            # STRICTLY less than zero. On expiry day itself dte is 0 and the
            # contract trades until 15:30 -- an intraday position opened that
            # morning is not settled, it is open. Writing this as `dte <= 0`
            # closed every intraday trade on its first bar at exactly zero
            # P&L, which looked like a flat strategy rather than a broken
            # loop.
            reason = "expiry"

        if reason is None:
            return None

        self.exit = Exit(ts=ts, reason=reason, value_pts=value, pnl_pts=pnl,
                         captured=cap, mae_pts=self.mae_pts, bars=self.bars)
        return self.exit

    def run(self, stream):
        """Walk a stream of (ts, prices, dte) until an exit fires.

        Returns the Exit, or None if the stream ran out first -- which is a
        real outcome, not an error: it means the position was still open when
        the data ended, and treating that as a closed trade is how an open
        position quietly becomes a fake win.
        """
        for ts, prices, dte in stream:
            e = self.step(ts, prices, dte)
            if e is not None:
                return e
        return None


# --------------------------------------------------------------- convenience

def short_straddle(strike, ce_px, pe_px, **kw):
    return Position(legs=(Leg(strike, CE, -1, ce_px),
                          Leg(strike, PE, -1, pe_px)), **kw)


def short_strangle(ce_strike, pe_strike, ce_px, pe_px, **kw):
    return Position(legs=(Leg(ce_strike, CE, -1, ce_px),
                          Leg(pe_strike, PE, -1, pe_px)), **kw)


def iron_condor(ce_strike, pe_strike, long_ce, long_pe,
                ce_px, pe_px, long_ce_px, long_pe_px, **kw):
    return Position(legs=(Leg(ce_strike, CE, -1, ce_px),
                          Leg(pe_strike, PE, -1, pe_px),
                          Leg(long_ce, CE, 1, long_ce_px),
                          Leg(long_pe, PE, 1, long_pe_px)), **kw)
