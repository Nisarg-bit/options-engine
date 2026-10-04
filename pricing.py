"""Pricing primitives, ported cell-for-cell from the workbook's Option_Chain
and Daily_Signal sheets.

Phase 2 rule: the workbook is the spec. Every function here reproduces a
specific cell, including the places where the workbook is arguably wrong.
Improvements go behind a flag in 2.4, so their effect can be measured rather
than assumed. Where a formula looks like a defect, it is reproduced faithfully
and flagged in a comment -- see `transaction_costs`.

Model: terminal price is Normal(spot, sigma) -- the workbook's assumption
(NORMDIST with an absolute-points sigma). That is a Bachelier model, not
Black-Scholes. It is a poor fit in the far tails and a reasonable one near
the money over a few days, which is where these structures live. Replacing it
with a lognormal terminal distribution is Phase 2.4.
"""

from math import sqrt, pi, exp, erf, floor
from datetime import datetime

TRADING_DAYS_BASIS = 365.0          # Option_Chain!B16 divides by sqrt(365)
SQRT_2_OVER_PI = sqrt(2.0 / pi)     # 0.797885 -- Daily_Signal!J9


# --------------------------------------------------------------- normal dist

def norm_pdf(z):
    """Standard normal density. Excel: NORMDIST(z, 0, 1, FALSE)."""
    return exp(-0.5 * z * z) / sqrt(2.0 * pi)


def norm_cdf(z):
    """Standard normal CDF. Excel: NORMSDIST(z)."""
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def mround(x, base):
    """Excel MROUND: round to the nearest multiple of `base`.

    Excel rounds halves away from zero; Python's round() rounds half to even.
    NIFTY strikes land on exact multiples often enough for this to matter.
    """
    if base == 0:
        return 0.0
    q = x / base
    return base * floor(q + 0.5) if q >= 0 else -base * floor(-q + 0.5)


# ------------------------------------------------------------------ vol chain

class Volatility:
    """Option_Chain!B16:B19.

    vix is a DECIMAL FRACTION (0.11 for 11%). NSE publishes India VIX as
    11.00, so divide by 100 before passing it in -- the workbook stores the
    already-divided form in Raw_Data!Q2.
    """

    __slots__ = ("daily_vol", "expiry_vol", "daily_stdev", "expiry_stdev")

    def __init__(self, spot, vix, days_to_expiry):
        self.daily_vol = vix / sqrt(TRADING_DAYS_BASIS)              # B16
        # B17 floors the day count at 1 INSIDE the sqrt. On expiry day itself
        # that keeps sigma from collapsing to near zero and producing a POP of
        # ~1.0 on a position that is actually about to be decided.
        self.expiry_vol = self.daily_vol * sqrt(max(days_to_expiry, 1.0))  # B17
        self.daily_stdev = spot * self.daily_vol                     # B18
        self.expiry_stdev = spot * self.expiry_vol                   # B19

    def __repr__(self):
        return (f"Volatility(daily_stdev={self.daily_stdev:.2f}, "
                f"expiry_stdev={self.expiry_stdev:.2f})")


def days_to_expiry(expiry, now=None):
    """Option_Chain!B13 -- fractional days, floored at one hour.

    `expiry` is a datetime at 15:30 IST on expiry day (the workbook builds it
    as DATEVALUE(B10) + 15.5/24).
    """
    now = now or datetime.now()
    return max((expiry - now).total_seconds() / 86400.0, 1.0 / 24.0)


# --------------------------------------------------------- expected payoffs

def expected_call(spot, strike, sigma):
    """E[max(0, S_T - K)] under Normal(spot, sigma).

    Daily_Signal!B36, first half:
      sigma*NORMDIST((K-S)/sigma, 0, 1, FALSE) + (S-K)*NORMSDIST((S-K)/sigma)
    """
    d = (spot - strike) / sigma
    return sigma * norm_pdf(d) + (spot - strike) * norm_cdf(d)


def expected_put(spot, strike, sigma):
    """E[max(0, K - S_T)] under Normal(spot, sigma). B36, second half."""
    d = (strike - spot) / sigma
    return sigma * norm_pdf(d) + (strike - spot) * norm_cdf(d)


def fair_straddle(sigma):
    """Daily_Signal!J9 -- the ATM straddle's value under the normal model.

    E|S_T - S| = sqrt(2/pi) * sigma. This is what 'rich or cheap' is measured
    against, and it is exactly expected_call(S, S, sigma) + expected_put(S, S,
    sigma) -- test_pricing asserts that identity.
    """
    return SQRT_2_OVER_PI * sigma


def rich_cheap_ratio(market_straddle, sigma):
    """Daily_Signal!J10. >= 1.10 rich, <= 0.90 cheap, between = neutral.

    Two years of bhavcopy say the edge actually begins nearer 1.25 (credit
    over sigma of 1.0), not 1.10. That is a threshold question for Phase 3,
    not a reason to change the formula here.
    """
    fair = fair_straddle(sigma)
    return market_straddle / fair if fair else 0.0


# ------------------------------------------------------------ probabilities

def probability_of_profit(spot, lower_be, upper_be, sigma):
    """Daily_Signal row 18 -- P(lower_be < S_T < upper_be)."""
    return norm_cdf((upper_be - spot) / sigma) - norm_cdf((lower_be - spot) / sigma)


def probability_stop_touched(pop):
    """Daily_Signal row 44 -- the reflection-principle approximation, min(1, 2*(1-POP)).

    This is crude: it is the touch probability for a barrier at the breakeven,
    not at the stop, and it ignores that the two legs move together. A
    barrier-correct version is Phase 2.4.
    """
    return min(1.0, 2.0 * (1.0 - pop))


# ------------------------------------------------------------------- costs

class CostModel:
    """Setup!B53:B61. Defaults are the workbook's own values.

    NOTE on `exchange_per_side`: the workbook carries 0.00035. This note used
    to say NSE's "current" rate was 0.0495% -- which stopped being true on
    1 October 2024 and stayed written here for two years. The live rates and
    their history now live in one place, CURRENT_COSTS below. The default
    here reproduces the workbook so the fixtures pass.

    NOTE on `stt_on_sell`: 0.001 was the correct rate until 31 March 2026.
    STT on the sale of options premium is 0.0015 from 1 April 2026. The
    default is left at the workbook's value for the same reason as above --
    test_pricing reproduces the sheet, and a silent change here would hide
    the discrepancy rather than record it. Every live caller passes the
    current rate explicitly.
    """

    def __init__(self, brokerage_per_order=20.0, orders_per_round_trip=4,
                 stt_on_sell=0.001, exchange_per_side=0.00035,
                 sebi_per_side=1e-6, stamp_buy=3e-5, gst=0.18,
                 slippage_per_leg=1.0, legs_per_round_trip=4):
        self.brokerage_per_order = brokerage_per_order
        self.orders_per_round_trip = orders_per_round_trip
        self.stt_on_sell = stt_on_sell
        self.exchange_per_side = exchange_per_side
        self.sebi_per_side = sebi_per_side
        self.stamp_buy = stamp_buy
        self.gst = gst
        self.slippage_per_leg = slippage_per_leg
        self.legs_per_round_trip = legs_per_round_trip


DEFAULT_COSTS = CostModel()


# ------------------------------------------------------- what is billed today
#
# DEFAULT_COSTS reproduces the workbook so test_pricing can hold the sheet to
# the rupee. Everything that prices a real trade uses what follows instead.
#
# ONE definition, on purpose. Until this, seven modules each built their own
# CostModel from literals. When STT rose to 0.15% on 1 April 2026, five were
# updated and two were not: backtest_corrected.py and compare.py ran on the
# old rate for months. And all seven carried an NSE exchange rate that had
# been stale since 1 October 2024. A rate that lives in seven places is a rate
# that is wrong in some of them; test_costs now fails if an eighth appears.
#
# History, so the next revision is a one-line change with a dated trail:
#
#   NSE equity options -- exchange transaction charge, on premium, per side
#     until 30 Sep 2024    0.0495%    slab-based, top slab
#     from   1 Oct 2024    0.03503%   SEBI's flat "true to label" rate
#     from   1 Mar 2026    0.03553%   NSE circular of 27 Feb 2026
#   BSE SENSEX and BANKEX options
#     from   1 Oct 2024    0.0325%    Rs 3,250 per crore of premium
#   BSE SENSEX50 and stock options
#     from   1 Oct 2024    0.005%     Rs 500 per crore of premium
#   STT on the sale of options premium
#     until 31 Mar 2026    0.10%
#     from   1 Apr 2026    0.15%
#
# Current figures are what Zerodha publishes as billed, which is what this
# account actually pays; the October 2024 change is from Zerodha's notice of
# it and the exchanges' SEBI-compliance revisions.
#
# These are TODAY's rates. A study over years of history that prices every
# trade at them is answering "what would this earn if run now", which is the
# question that matters for deciding whether to run it -- but it is not what
# those historical trades were actually charged, and the difference runs the
# other way before October 2024.

NSE_OPTION_EXCHANGE = 0.0003553
BSE_OPTION_EXCHANGE = 0.000325
BSE_SMALL_OPTION_EXCHANGE = 0.00005     # SENSEX50 and BSE stock options
STT_ON_SELL = 0.0015

CURRENT_COSTS = CostModel(exchange_per_side=NSE_OPTION_EXCHANGE,
                          stt_on_sell=STT_ON_SELL)

# Only the exceptions. Anything unclassified is priced on NSE's rate, the
# DEAREST of the three, so a missing entry makes a trade look worse than it
# is rather than better.
_EXCHANGE_RATE_FOR = {
    "SENSEX": BSE_OPTION_EXCHANGE,
    "BANKEX": BSE_OPTION_EXCHANGE,
    "SENSEX50": BSE_SMALL_OPTION_EXCHANGE,
}
_MODELS = {NSE_OPTION_EXCHANGE: CURRENT_COSTS}


def current_costs_for(underlying):
    """Today's cost model for this underlying's exchange tariff."""
    rate = _EXCHANGE_RATE_FOR.get(underlying, NSE_OPTION_EXCHANGE)
    if rate not in _MODELS:
        _MODELS[rate] = CostModel(exchange_per_side=rate,
                                  stt_on_sell=STT_ON_SELL)
    return _MODELS[rate]


def transaction_costs(credit_pts, lot_size, lots=1, defined_risk=False,
                      costs=DEFAULT_COSTS):
    """Daily_Signal row 38. Returns rupees.

    `defined_risk` doubles the leg count (a condor has four legs, a straddle
    two), which doubles brokerage.

    WORKBOOK DEFECT, reproduced deliberately: the GST term uses
    `brokerage * orders` WITHOUT the defined-risk multiplier, while the
    brokerage term itself uses `* 2`. So a condor pays GST on half the
    brokerage it actually incurred -- understating costs by about Rs 14 per
    condor. Fixing it belongs in 2.4, where the effect can be measured; fixing
    it here would fail the fixtures and hide the discrepancy.
    """
    mult = 2 if defined_risk else 1
    turnover = credit_pts * lot_size * lots

    rate_sum = (costs.stt_on_sell + costs.exchange_per_side * 2
                + costs.sebi_per_side * 2 + costs.stamp_buy)
    statutory = turnover * rate_sum

    brokerage = costs.brokerage_per_order * costs.orders_per_round_trip * mult

    gstable = (costs.brokerage_per_order * costs.orders_per_round_trip
               + turnover * (costs.exchange_per_side * 2 + costs.sebi_per_side * 2))
    gst = gstable * costs.gst

    return statutory + brokerage + gst


def slippage(lot_size, lots=1, defined_risk=False, costs=DEFAULT_COSTS):
    """Daily_Signal row 39. Returns rupees."""
    mult = 2 if defined_risk else 1
    return (costs.slippage_per_leg * costs.legs_per_round_trip * mult
            * lot_size * lots)


# -------------------------------------------------------------------- EV

def gross_ev(credit_pts, expected_payoff_pts, lot_size, lots=1):
    """Daily_Signal row 37 -- expected value before costs, in rupees."""
    return (credit_pts - expected_payoff_pts) * lot_size * lots


def net_ev(credit_pts, expected_payoff_pts, lot_size, lots=1,
           defined_risk=False, costs=DEFAULT_COSTS):
    """Daily_Signal row 40 -- gross EV less charges and slippage, in rupees.

    This is the number the workbook's corrected verdict (B48) ranks on, and
    the one Phase 2's exit gate requires within 0.5%.
    """
    return (gross_ev(credit_pts, expected_payoff_pts, lot_size, lots)
            - transaction_costs(credit_pts, lot_size, lots, defined_risk, costs)
            - slippage(lot_size, lots, defined_risk, costs))
