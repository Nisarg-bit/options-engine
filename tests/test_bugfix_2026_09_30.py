"""Regression tests for the 30 Sep 2026 website bug-fix pass."""
import chain_metrics as CM
import daytrade_api as DA


def _row(strike, ce_oi, ce_chg, pe_oi, pe_chg):
    return {"strike": strike,
            "CE": {"oi": ce_oi, "oi_chg": ce_chg},
            "PE": {"oi": pe_oi, "oi_chg": pe_chg}}


def test_most_added_ranks_on_contracts_not_percent():
    rows = [
        # far strike: +300% of a tiny base -> 750 contracts added
        _row(24000, 1_000, 3.0, 1_000, 3.0),
        # near strike: +50% of a big base -> 1,000,000 contracts added
        _row(23000, 3_000_000, 0.5, 2_000_000, 0.25),
    ]
    L = CM.levels(rows)
    assert L["max_ce_addition"] == 23000
    assert L["max_pe_addition"] == 23000
    assert L["max_ce_added"] == 1_000_000
    assert L["max_pe_added"] == 400_000


def test_most_added_skips_missing_and_closed_out():
    rows = [_row(22000, None, None, 0, -1), _row(22100, 500, 1.0, 800, 1.0)]
    L = CM.levels(rows)
    assert L["max_ce_addition"] == 22100 and L["max_pe_addition"] == 22100


def test_compact_daytrade_publishes_the_whole_journal(monkeypatch):
    days = ["2026-09-01", "2026-09-02"]
    journal = [{"date": f"2026-08-{d:02d}", "status": "settled", "net_rs": "100"}
               for d in range(1, 26)]                      # 25 rows > old cut of 20
    today = {"date": "2026-09-02", "status": "open", "exit": "12:00",
             "credit": "100", "cover": "90", "pnl_pts": "10", "stop_level": "130"}
    monkeypatch.setattr(DA.V, "sessions", lambda u: days)
    monkeypatch.setattr(DA, "_stats", lambda d, u: None)
    monkeypatch.setattr(DA, "_today_row", lambda *a, **k: dict(today))
    monkeypatch.setattr(DA, "_overnight", lambda stats: ([], {}))
    monkeypatch.setattr(DA.D, "read_journal", lambda *a, **k: list(journal))
    monkeypatch.setattr(DA.D, "summary_data", lambda rows: {"journalled": len(rows)})
    out = DA.api_daytrade(underlying="NIFTY", compact=1, expiry=None)
    assert len(out["journal"]) == 26
    assert out["journal_total"] == 26 == out["summary"]["journalled"]
