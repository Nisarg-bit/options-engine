"""The seven structures from Daily_Signal columns B-N, ported out of Excel.

Each builder takes the option chain and today's volatility and returns a
Structure carrying strikes, credit, breakevens, POP, expected payoff and net
EV -- every number the workbook's rows 8-42 produce for that column.

Phase 2 rule holds here as in pricing.py: reproduce the workbook exactly,
including its defects, and flag them rather than silently improving. Two are
flagged below (see `max_ev_strangle` and `MAXEV_SEARCH_NOTE`).
"""

from dataclasses import dataclass, field
from typing import List, Optional

import pricing as P


# The chain the workbook builds spans ATM-15 steps to ATM+15 steps
# (Option_Chain P28:P58, where P43 = ATM + 0*step).
CHAIN_STEPS_EACH_SIDE = 15


@dataclass
class Leg:
    strike: float
    kind: str            # "CE" or "PE"
    premium: float
    long: bool = False   # True for a bought wing


@dataclass
class Structure:
    name: str
    label: str
    ce_strike: float
    pe_strike: float
    ce_premium: float
    pe_premium: float
    long_ce_strike: Optional[float] = None
    long_pe_strike: Optional[float] = None
    long_ce_premium: float = 0.0
    long_pe_premium: float = 0.0
    lot_size: int = 65
    lots: int = 1
    wing_width: Optional[float] = None

    # filled by finalise()
    credit_pts: float = 0.0
    credit_rs: float = 0.0
    wing_cost: float = 0.0
    max_loss_pts: float = 0.0
    lower_be: float = 0.0
    upper_be: float = 0.0
    profit_zone: float = 0.0
    pop: float = 0.0
    exp_payoff_pts: float = 0.0
    gross_ev_rs: float = 0.0
    fees_rs: float = 0.0
    slippage_rs: float = 0.0
    net_ev_rs: float = 0.0
    margin: float = 0.0
    legs: List[Leg] = field(default_factory=list)

    @property
    def defined_risk(self):
        return self.long_ce_strike is not None

    @property
    def risk_type(self):
        return "DEFINED" if self.defined_risk else "UNDEFINED"

    def net_ev_per_margin(self):
        return self.net_ev_rs / self.margin if self.margin else 0.0


def finalise(s, spot, sigma, margin_per_lot, costs=P.DEFAULT_COSTS):
    """Compute rows 12-42 for a structure whose strikes and premiums are set."""
    s.legs = [Leg(s.ce_strike, "CE", s.ce_premium),
              Leg(s.pe_strike, "PE", s.pe_premium)]

    if s.defined_risk:
        # row 12 for L:N -- credit is net of the wings bought
        s.wing_cost = s.long_ce_premium + s.long_pe_premium          # row 34
        s.credit_pts = (s.ce_premium + s.pe_premium) - s.wing_cost
        s.max_loss_pts = max(0.0, s.wing_width - s.credit_pts)       # row 35
        s.credit_rs = max(0.0, s.credit_pts) * s.lot_size * s.lots   # row 14
        s.margin = s.max_loss_pts * s.lot_size * s.lots              # row 24
        s.legs += [Leg(s.long_ce_strike, "CE", s.long_ce_premium, long=True),
                   Leg(s.long_pe_strike, "PE", s.long_pe_premium, long=True)]
    else:
        s.credit_pts = s.ce_premium + s.pe_premium                   # row 12
        # row 14 subtracts any strike inversion. Normal strangles have
        # pe_strike < ce_strike so the term is zero; it only bites if a sigma
        # rule ever crosses the strikes over, which row 21's H21 check exists
        # to catch.
        inversion = max(0.0, s.pe_strike - s.ce_strike)
        s.credit_rs = max(0.0, s.credit_pts - inversion) * s.lot_size * s.lots
        s.max_loss_pts = 0.0
        s.margin = margin_per_lot * s.lots * 2                       # row 24

    s.lower_be = s.pe_strike - s.credit_pts                          # row 15
    s.upper_be = s.ce_strike + s.credit_pts                          # row 16
    s.profit_zone = s.upper_be - s.lower_be                          # row 17
    s.pop = P.probability_of_profit(spot, s.lower_be, s.upper_be, sigma)  # row 18

    payoff = (P.expected_call(spot, s.ce_strike, sigma)
              + P.expected_put(spot, s.pe_strike, sigma))
    if s.defined_risk:
        payoff -= P.expected_call(spot, s.long_ce_strike, sigma)
        payoff -= P.expected_put(spot, s.long_pe_strike, sigma)
    s.exp_payoff_pts = payoff                                        # row 36

    s.gross_ev_rs = P.gross_ev(s.credit_pts, payoff, s.lot_size, s.lots)
    s.fees_rs = P.transaction_costs(s.credit_pts, s.lot_size, s.lots,
                                    s.defined_risk, costs)
    s.slippage_rs = P.slippage(s.lot_size, s.lots, s.defined_risk, costs)
    s.net_ev_rs = s.gross_ev_rs - s.fees_rs - s.slippage_rs          # row 40
    return s


# ------------------------------------------------------------------- chain

class Chain:
    """Premium lookup by strike, mirroring INDEX/MATCH over Option_Chain."""

    def __init__(self, rows):
        self._ce = {float(r["strike"]): float(r["ce"]) for r in rows}
        self._pe = {float(r["strike"]): float(r["pe"]) for r in rows}
        self.strikes = sorted(self._ce)

    def ce(self, strike):
        return self._ce.get(float(strike), 0.0)      # IFERROR(...,0)

    def pe(self, strike):
        return self._pe.get(float(strike), 0.0)

    def window(self, atm, step, lo, hi):
        """Strikes from atm+lo*step to atm+hi*step, inclusive."""
        return [atm + k * step for k in range(lo, hi + 1)
                if (atm + k * step) in self._ce]


# -------------------------------------------------------------- builders

def _otm_pe(spot, sigma_or_offset, step):
    """The workbook's put rule: MROUND(spot - offset, step) - step.

    The trailing '- step' pushes the put one strike further out than symmetry
    would give. It is deliberate in the sheet (every strangle column has it)
    and makes the structure slightly skewed to the downside, which is the
    correct direction for an index that gaps down harder than it gaps up.
    """
    return P.mround(spot - sigma_or_offset, step) - step


def short_straddle(chain, spot, vol, step, lot_size, lots=1, **_):
    """Column B -- both legs at the ATM strike."""
    atm = P.mround(spot, step)
    return Structure("short_straddle", "SHORT STRADDLE", atm, atm,
                     chain.ce(atm), chain.pe(atm), lot_size=lot_size, lots=lots)


def intraday_strangle(chain, spot, vol, step, lot_size, lots=1, **_):
    """Column C -- one DAILY sigma either side."""
    ce = P.mround(spot + vol.daily_stdev, step)
    pe = _otm_pe(spot, vol.daily_stdev, step)
    return Structure("intraday_strangle", "INTRADAY STRANGLE", ce, pe,
                     chain.ce(ce), chain.pe(pe), lot_size=lot_size, lots=lots)


def expiry_strangle(chain, spot, vol, step, lot_size, lots=1, **_):
    """Column D -- one EXPIRY sigma either side."""
    ce = P.mround(spot + vol.expiry_stdev, step)
    pe = _otm_pe(spot, vol.expiry_stdev, step)
    return Structure("expiry_strangle", "EXPIRY STRANGLE", ce, pe,
                     chain.ce(ce), chain.pe(pe), lot_size=lot_size, lots=lots)


MAXEV_SEARCH_NOTE = """\
Column E scans Option_Chain O44:O58 (calls) and Q28:Q42 (puts) -- strictly OTM,
excluding the ATM strike -- for the largest 'EV'. But that EV is

    P(option expires worthless) x premium received

with NO loss term: it never subtracts what you pay when the option finishes
in the money. That is not expected value, it is expected value conditional on
winning. Because the quantity rises monotonically as you move IN toward the
money across the OTM range, the maximum lands on the innermost candidate --
so in practice this 'search' degenerates to a fixed ATM +/- 1 strike strangle.
On the 2026-08-28 fixture it picks exactly that.

Reproduced faithfully here. A corrected selector -- maximising the honest
credit - expected_payoff from pricing.py -- belongs in 2.4, where the
backtest can measure whether it actually picks better strikes."""


def max_ev_strangle(chain, spot, vol, step, lot_size, lots=1, **_):
    """Column E. See MAXEV_SEARCH_NOTE for why this objective is wrong."""
    atm = P.mround(spot, step)
    sigma = vol.expiry_stdev

    calls = chain.window(atm, step, 1, CHAIN_STEPS_EACH_SIDE)
    puts = chain.window(atm, step, -CHAIN_STEPS_EACH_SIDE, -1)

    def ce_ev(k):   # Option_Chain O = L * N
        return P.norm_cdf((k - spot) / sigma) * chain.ce(k) * lot_size

    def pe_ev(k):   # Option_Chain Q = T * R
        return (1.0 - P.norm_cdf((k - spot) / sigma)) * chain.pe(k) * lot_size

    ce = max(calls, key=ce_ev) if calls else atm
    pe = max(puts, key=pe_ev) if puts else atm
    return Structure("maxev_strangle", "MAX-EV STRANGLE", ce, pe,
                     chain.ce(ce), chain.pe(pe), lot_size=lot_size, lots=lots)


def _with_wings(s, chain, wing_width):
    s.wing_width = wing_width
    s.long_ce_strike = s.ce_strike + wing_width       # row 30
    s.long_pe_strike = s.pe_strike - wing_width       # row 31
    s.long_ce_premium = chain.ce(s.long_ce_strike)    # row 32
    s.long_pe_premium = chain.pe(s.long_pe_strike)    # row 33
    return s


def iron_condor(chain, spot, vol, step, lot_size, lots=1, wing_width=200.0, **_):
    """Column L -- the expiry strangle, wings bought."""
    s = expiry_strangle(chain, spot, vol, step, lot_size, lots)
    s.name, s.label = "iron_condor", "IRON CONDOR"
    return _with_wings(s, chain, wing_width)


def iron_butterfly(chain, spot, vol, step, lot_size, lots=1, wing_width=200.0, **_):
    """Column M -- the ATM straddle, wings bought."""
    s = short_straddle(chain, spot, vol, step, lot_size, lots)
    s.name, s.label = "iron_butterfly", "IRON BUTTERFLY"
    return _with_wings(s, chain, wing_width)


def iron_condor_wide(chain, spot, vol, step, lot_size, lots=1, wing_width=200.0,
                     sigma_mult=1.5, **_):
    """Column N -- 1.5 expiry sigma either side, wings bought."""
    ce = P.mround(spot + sigma_mult * vol.expiry_stdev, step)
    pe = _otm_pe(spot, sigma_mult * vol.expiry_stdev, step)
    s = Structure("iron_condor_wide", "IRON CONDOR WIDE", ce, pe,
                  chain.ce(ce), chain.pe(pe), lot_size=lot_size, lots=lots)
    return _with_wings(s, chain, wing_width)


BUILDERS = {
    "short_straddle": short_straddle,
    "intraday_strangle": intraday_strangle,
    "expiry_strangle": expiry_strangle,
    "maxev_strangle": max_ev_strangle,
    "iron_condor": iron_condor,
    "iron_butterfly": iron_butterfly,
    "iron_condor_wide": iron_condor_wide,
}


def build_all(chain_rows, spot, vix, days_to_expiry, step, lot_size, lots=1,
              wing_width=200.0, sigma_mult=1.5, margin_per_lot=120000.0,
              costs=P.DEFAULT_COSTS):
    """Every structure for one session. Mirrors a full Daily_Signal rebuild."""
    chain = Chain(chain_rows)
    vol = P.Volatility(spot, vix, days_to_expiry)
    out = {}
    for name, fn in BUILDERS.items():
        s = fn(chain, spot, vol, step, lot_size, lots,
               wing_width=wing_width, sigma_mult=sigma_mult)
        out[name] = finalise(s, spot, vol.expiry_stdev, margin_per_lot, costs)
    return out


def best(structures):
    """Daily_Signal B48 -- rank on NET EV, not the legacy row-23 EV.

    Returns None when nothing clears zero, which is the sheet's
    'NO TRADE - nothing has positive EV once costs are paid'.
    """
    top = max(structures.values(), key=lambda s: s.net_ev_rs)
    return top if top.net_ev_rs > 0 else None
