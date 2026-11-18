"""/api/options_view -- the Options page: today's view and fitting strategies,
for NIFTY and BANKNIFTY.

Gathers inputs that already exist elsewhere (index bars, the stock tier for
breadth and money flow, the option chain's OI, the scored catalogue) and hands
them to quant/regime.py. Each index is built in its own try, so one failing
leaves the other on the page with its reason.
"""
from datetime import date

import pandas as pd
from fastapi import APIRouter

import market_pulse as MP
import regime as RG

router = APIRouter()
INDICES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
CAPITAL, RISK_PCT = 2_000_000, 0.01      # the paper book: Rs 20 lakh, 1% per trade
OR_MINUTES = 15                           # opening range = the first 15 one-minute bars


def index_bars(day, sym):
    import api
    df = api.read_parts(day, "INDEX")
    if df is None or df.empty:
        return None
    g = df[df["tradingsymbol"] == sym].sort_values("ts")
    return g if not g.empty else None


def prev_index_close(day, sym):
    """Yesterday's close, without disturbing api.read_parts' cache of today (see pulse_api.closes_on)."""
    import api
    import pulse_api as PA
    days = [d for d in api.sessions() if d < day]
    return PA.closes_on(days[-1]).get(sym) if days else None


def market_breadth_flow(day):
    """F&O-wide breadth and money-flow ratios (-1..+1), or Nones before the stock layer runs."""
    import pulse_api as PA
    import sectors as SEC
    try:
        eq = PA.read_tier(day, "EQ")
        tbl = MP.stock_table(eq, PA.prev_closes(day), {k: v["sector"] for k, v in SEC.load().items()})
        b, f = MP.breadth(tbl), MP.money_flow(tbl)
        return b.get("ratio"), f.get("ratio")
    except Exception:  # noqa: BLE001 -- stock layer missing is not fatal here
        return None, None


def one_index(und, day, breadth_ratio, flow_ratio, vix_pct, day_sigma_pct):
    import api
    st = api.api_structures(underlying=und)
    an = api.api_analytics(underlying=und)
    spot = st.get("spot")
    bars = index_bars(day, INDICES[und])
    ret = or_hi = or_lo = hi = lo = last = None
    if bars is not None:
        last = float(bars["close"].iloc[-1])
        pc = prev_index_close(day, INDICES[und])
        ret = (last / pc - 1) * 100 if pc else None
        first = bars.head(OR_MINUTES)
        if len(bars) > OR_MINUTES:
            or_hi, or_lo = float(first["high"].max()), float(first["low"].min())
        hi, lo = float(bars["high"].max()), float(bars["low"].min())
    lv = an.get("levels") or {}
    sup, res = lv.get("support"), lv.get("resistance")
    ref = last or spot
    inside = (sup < ref < res) if (sup and res and ref) else None
    parts = {
        "trend": RG.trend_score(ret, day_sigma_pct),
        "opening_range": RG.opening_range_score(last, or_hi, or_lo),
        "breadth": None if breadth_ratio is None else max(-1.0, min(1.0, breadth_ratio)),
        "flow": None if flow_ratio is None else max(-1.0, min(1.0, flow_ratio)),
        "oi_flow": RG.oi_flow_score((an.get("pcr") or {}).get("pcr_oi_change")),
    }
    v = RG.view(parts, inside_walls=inside, used=RG.range_used(hi, lo, ref, day_sigma_pct),
                vix_pct=vix_pct, rich_ratio=st.get("ratio"), catalogue=st.get("catalogue"),
                capital=CAPITAL, risk_pct=RISK_PCT)
    v.update({"spot": spot, "last": last, "chg_pct": None if ret is None else round(ret, 2),
              "expiry": st.get("expiry"), "dte": st.get("dte"), "lot": st.get("lot"),
              "support": sup, "resistance": res, "max_pain": an.get("max_pain"),
              "pcr_oi": (an.get("pcr") or {}).get("pcr_oi"),
              "pcr_oi_change": (an.get("pcr") or {}).get("pcr_oi_change"),
              "opening_range": None if or_hi is None else [or_lo, or_hi],
              "as_of": st.get("as_of")})
    return v


@router.get("/api/options_view")
def api_options_view():
    import api
    import pulse_api as PA
    day = api.latest_session()
    vix = PA.vix_history()
    try:
        vb = index_bars(day, "INDIA VIX")
        vlast = float(vb["close"].iloc[-1]) if vb is not None else None
    except Exception:  # noqa: BLE001
        vlast = None
    if not vlast:
        vlast = vix[-1] if vix else None
    vpct = MP.vix_percentile(vix, vlast)
    day_sigma = vlast / (252 ** 0.5) if vlast else None
    b, f = market_breadth_flow(day)
    out = {"session": str(day), "generated": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds"),
           "capital": CAPITAL, "risk_pct": RISK_PCT,
           "vix": {"last": vlast, "percentile": None if vpct is None else round(vpct * 100)},
           "notes": ["Rule-based view: the odds are scores turned into shares of 100, not calibrated probabilities.",
                     "Strategies are matched to the view from a fixed playbook and priced on the live chain. Paper trading only.",
                     f"Lots are sized so the max loss is at most {RISK_PCT:.0%} of the Rs {CAPITAL/1e5:.0f} lakh paper book, and margin at most 50%."],
           "indices": {}}
    for und in INDICES:
        try:
            out["indices"][und] = one_index(und, day, b, f, vpct, day_sigma)
        except Exception as e:  # noqa: BLE001 -- one index failing keeps the other
            out["indices"][und] = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    return out
