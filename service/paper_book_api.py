"""/api/paper_book and the once-a-minute agent step (driven by publisher.py).

Prices come from the collector's live chain (api.build_chain mids); the views
come from /api/options_view. See tracking/paper_book.py for the rules.
"""
from datetime import date, datetime

from fastapi import APIRouter

import paper_book as PB

router = APIRouter()
_chains = {}            # (und, expiry) -> (minute, {(strike, kind): mid})


def price(und, expiry, strike, kind, now=None):
    import api
    minute = (now or datetime.now(PB.IST)).strftime("%Y-%m-%d %H:%M")
    key = (und, expiry)
    hit = _chains.get(key)
    if not hit or hit[0] != minute:
        rows, _d, _n, _ts = api.build_chain(api.latest_session(), und, date.fromisoformat(expiry))
        book = {}
        for r in rows or []:
            for k in ("CE", "PE"):
                m = (r.get(k) or {}).get("mid")
                if m is not None:
                    book[(float(r["strike"]), k)] = m
        _chains[key] = hit = (minute, book)
    return hit[1].get((float(strike), kind))


def run_step(now=None):
    """Advance the book one minute and save it. Never raises."""
    import market as MK
    import options_view_api as OV
    now = now or datetime.now(PB.IST)
    st = PB.load()
    try:
        events = PB.step(st, now, OV.api_options_view, price, MK.is_trading_day)
    except Exception as e:  # noqa: BLE001 -- the agent must not take the publisher down
        return [f"step failed: {type(e).__name__}: {e}"]
    if events or any(t["status"] == "open" for t in st["trades"]) or st["skips"]:
        PB.save(st)
    for e in events:
        print("paper_book:", e)
    return events


@router.get("/api/paper_book")
def api_paper_book():
    return PB.summary(PB.load())


def publish():
    """What publisher.py writes to /live/paper_book: step, then the summary."""
    ev = run_step()
    out = PB.summary(PB.load())
    out["events"] = ev
    return out
