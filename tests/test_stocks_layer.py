"""The stock layer added for the website's market pages (11 Nov 2026).

Covers the three places it can go quietly wrong without a Kite session:
choosing the stocks, keeping the websocket under Kite's 3,000 cap, and
writing stock bars into their own tier without disturbing the option files.
"""
import json
import os
from datetime import date, datetime, timezone

import pyarrow.parquet as pq

import instruments as I
import sectors as S
from writer import BarWriter, load_instrument_map

TODAY = date(2026, 11, 11)


def fut(name, exp, tok, lot=500):
    return {"name": name, "instrument_type": "FUT", "expiry": exp,
            "instrument_token": tok, "tradingsymbol": f"{name}{exp:%y%b}FUT".upper(),
            "lot_size": lot, "segment": "NFO-FUT"}


def eq(sym, tok):
    return {"tradingsymbol": sym, "instrument_type": "EQ", "segment": "NSE",
            "instrument_token": tok}


NFO = [fut("RELIANCE", date(2026, 11, 25), 11), fut("RELIANCE", date(2026, 12, 30), 12),
       fut("RELIANCE", date(2026, 10, 28), 10),               # expired: never chosen
       fut("M&M", date(2026, 11, 25), 21),
       fut("NIFTY", date(2026, 11, 25), 99),                  # index future: not a stock
       fut("NOCASH", date(2026, 11, 25), 31)]                 # no NSE cash listing
NSE = [eq("RELIANCE", 1), eq("M&M", 2), eq("NIFTY 50", 3)]


def test_one_cash_share_and_the_nearest_live_future_per_stock():
    rows = I.choose_stocks(NFO, NSE, TODAY)
    by = {(r["name"], r["kind"]): r for r in rows}
    assert set(by) == {("RELIANCE", "stock"), ("RELIANCE", "stock_fut"),
                       ("M&M", "stock"), ("M&M", "stock_fut")}
    assert by[("RELIANCE", "stock_fut")]["instrument_token"] == 11      # Nov, not Oct or Dec
    assert by[("RELIANCE", "stock")]["instrument_token"] == 1


def test_index_futures_and_unlisted_names_are_left_out():
    names = {r["name"] for r in I.choose_stocks(NFO, NSE, TODAY)}
    assert "NIFTY" not in names and "NOCASH" not in names


def test_budget_drops_futures_before_cash_shares():
    stocks = I.choose_stocks(NFO, NSE, TODAY)                  # 2 cash + 2 futures
    opts = [{}] * 10
    assert len(I.fit_budget(opts, stocks, cap=20)) == 4
    kept = I.fit_budget(opts, stocks, cap=13)                  # room for 3
    assert [r["kind"] for r in kept].count("stock") == 2
    assert [r["kind"] for r in kept].count("stock_fut") == 1
    assert all(r["kind"] == "stock" for r in I.fit_budget(opts, stocks, cap=11))
    assert I.fit_budget(opts * 3, stocks, cap=20) == []


def test_stock_bars_land_in_their_own_tier_and_two_partitions(tmp_path):
    master = {"instruments": [
        {"instrument_token": 1, "tradingsymbol": "RELIANCE", "name": "RELIANCE",
         "expiry": None, "strike": None, "instrument_type": "EQ", "lot_size": 500, "kind": "stock"},
        {"instrument_token": 11, "tradingsymbol": "RELIANCE26NOVFUT", "name": "RELIANCE",
         "expiry": "2026-11-25", "strike": None, "instrument_type": "FUT", "lot_size": 500,
         "kind": "stock_fut"},
        {"instrument_token": 7, "tradingsymbol": "NIFTY26N1825000CE", "name": "NIFTY",
         "expiry": "2026-11-18", "strike": 25000.0, "instrument_type": "CE", "lot_size": 65,
         "kind": "option"}]}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(master))
    imap = load_instrument_map(str(p))
    stk = {t: m for t, m in imap.items() if m.get("kind") in ("stock", "stock_fut")}
    opt = {t: m for t, m in imap.items() if t not in stk}
    ts = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    bar = lambda tok, oi=None: {"ts": ts, "instrument_token": tok, "open": 1.0, "high": 1.0,
                                "low": 1.0, "close": 1.0, "volume": 5, "pv": 5.0, "oi": oi,
                                "bid": None, "ask": None, "bid_qty": None, "ask_qty": None,
                                "tick_count": 1, "is_interpolated": False}
    bars = [bar(1), bar(11, 1000), bar(7, 50)]
    w_opt = BarWriter(opt, root=str(tmp_path), flush_every=1)
    w_stk = BarWriter(stk, root=str(tmp_path), tier="stocks_1m", flush_every=1)
    w_opt.add(bars)
    w_stk.add(bars)
    d = tmp_path / "stocks_1m" / "date=2026-11-11"
    assert sorted(os.listdir(d)) == ["underlying=EQ", "underlying=STKFUT"]
    t = pq.read_table(next((d / "underlying=STKFUT").glob("*.parquet"))).to_pylist()
    assert t[0]["underlying"] == "RELIANCE" and t[0]["opt_type"] == "FUT" and t[0]["oi"] == 1000
    # the option tier is untouched by stock rows
    o = tmp_path / "bars_1m" / "date=2026-11-11"
    assert os.listdir(o) == ["underlying=NIFTY"]


SAMPLE500 = "Company Name,Industry,Symbol,Series,ISIN Code\nState Bank,Financial Services,SBIN,EQ,X\nHDFC Bank,Financial Services,HDFCBANK,EQ,X\nL&T,Construction,LT,EQ,X\nSome NBFC,Financial Services,BAJFINANCE,EQ,X\nOdd Co,Unheard Industry,ODD,EQ,X\n"
BANK = "Company Name,Industry,Symbol,Series,ISIN Code\nState Bank,Financial Services,SBIN,EQ,X\nHDFC Bank,Financial Services,HDFCBANK,EQ,X\n"
PSU = "Company Name,Industry,Symbol,Series,ISIN Code\nState Bank,Financial Services,SBIN,EQ,X\n"


def test_narrowest_sector_wins_then_industry_then_others():
    m = S.classify({S.BROAD: SAMPLE500, "ind_niftybanklist": BANK, "ind_niftypsubanklist": PSU})
    assert S.sector_of(m, "SBIN") == "PSU Bank"          # PSU Bank beats Bank
    assert S.sector_of(m, "HDFCBANK") == "Bank"
    assert S.sector_of(m, "LT") == "Infra"                 # from Industry
    assert S.sector_of(m, "BAJFINANCE") == "Financials"
    assert S.sector_of(m, "ODD") == "Others"               # unknown industry
    assert S.sector_of(m, "NOTLISTED") == "Others"
