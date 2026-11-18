"""End-of-day summary and Telegram alerts for the stock-futures system (sf_report.py)."""
import threading
from datetime import date, timedelta

import sf_fixtures as F
import sf_report as R
from sf_api import journal, state
from sf_cli import replay
from sf_core import ReplayClock, ist
from sf_data import FixtureData
from sf_engine import Engine
from sf_store import Store

DAY = F.INTRADAY_DAY


class Outbox:
    def __init__(self, ok=True):
        self.msgs, self.ok = [], ok
        self.lock = threading.Lock()

    def __call__(self, text, silent=False):
        with self.lock:
            self.msgs.append((text, silent))
        return self.ok


def run_day(tmp_path, sends=None, db="r.db"):
    sc = F.bullish()
    clock = ReplayClock(ist(DAY, "09:15"))
    st = Store(str(tmp_path / db))
    out = sends or Outbox()
    e = Engine(st, FixtureData(sc.build()), clock, F.demo_config(), notifier=R.Notifier(st, send=out))
    replay(e, clock, sc.sessions)            # ends with a tick at 15:45
    return st, e, clock, out


def test_end_of_day_summary_once_after_the_close(tmp_path):
    st, e, clock, out = run_day(tmp_path)
    evs = st.events("type='DailySummary'")
    assert len(evs) == 1
    rep = evs[0]["payload"]
    rows = journal(st)
    settled = [r for r in rows if r["class"] == "settled"]
    assert rep["closed"] == len(settled) and abs(rep["net"] - sum(r["net_pnl"] for r in settled)) < 0.01
    assert rep["scans"]["intraday"] > 0 and "BULLISH" in rep["regimes"]["intraday"]
    assert "Net P&amp;L" in rep["text"] and "Paper trading only" in rep["text"]
    assert R.end_of_day(e, clock.now()) is None                    # already done for this session
    assert state(st, clock.now(), e.data, e)["daily_summary"]["session"] == str(DAY)


def test_no_summary_before_the_close_or_on_a_holiday(tmp_path):
    sc = F.bullish()
    st = Store(str(tmp_path / "n.db"))
    e = Engine(st, FixtureData(sc.build()), ReplayClock(ist(DAY, "15:00")), F.demo_config())
    assert R.end_of_day(e, ist(DAY, "15:00")) is None
    assert R.end_of_day(e, ist(date(2026, 11, 24), "16:00")) is None    # exchange holiday
    assert R.end_of_day(e, ist(DAY, "15:40")) is not None


def test_alerts_for_open_close_and_summary_are_sent_once(tmp_path):
    st, e, clock, out = run_day(tmp_path)
    texts = [t for t, _ in out.msgs]
    opened = [t for t in texts if "opened</b>" in t]
    closed = [t for t in texts if "Paper trade closed" in t]
    assert opened and len(opened) == len(st.events("type='PositionOpened'"))
    assert closed and len(closed) == len(st.events("type='PositionClosed'"))
    assert any("BUY" in t for t in opened) and all("net" in t for t in closed)
    summary = [(t, s) for t, s in out.msgs if t.startswith("<b>Stock futures (paper)")]
    assert len(summary) == 1 and summary[0][1] is True                  # summary is a silent message
    n = len(out.msgs)
    e.notifier.poll(clock.now())
    assert len(out.msgs) == n                                            # nothing re-sent


def test_competing_notifiers_send_each_alert_once(tmp_path):
    sc = F.bullish()
    clock = ReplayClock(ist(DAY, "09:15"))
    st = Store(str(tmp_path / "c.db"))
    e = Engine(st, FixtureData(sc.build()), clock, F.demo_config())
    replay(e, clock, sc.sessions)
    out = Outbox()
    ns = [R.Notifier(Store(str(tmp_path / "c.db")), send=out) for _ in range(4)]
    b = threading.Barrier(4)

    def go(n):
        b.wait()
        n.poll(clock.now())
    th = [threading.Thread(target=go, args=(n,)) for n in ns]
    [t.start() for t in th]
    [t.join() for t in th]
    expected = len(st.events("type IN ('PositionOpened','PositionClosed','DailySummary')"))
    assert len(out.msgs) == expected


def test_restart_does_not_resend_old_events_and_failed_send_never_raises(tmp_path):
    sc = F.bullish()
    clock = ReplayClock(ist(DAY, "09:15"))
    st = Store(str(tmp_path / "s.db"))
    e = Engine(st, FixtureData(sc.build()), clock, F.demo_config())
    replay(e, clock, sc.sessions)
    out = Outbox(ok=False)                                              # Telegram down
    n = R.Notifier(st, send=out, since=clock.now() + timedelta(minutes=1))
    n.poll(clock.now())
    assert out.msgs == []                                               # backlog from before the start skipped
    assert st.q("SELECT COUNT(*) n FROM consumed WHERE consumer='notifier'")[0]["n"] > 0


def test_unresolved_and_worker_messages():
    m = R.message({"type": "ExitUnresolved", "mode": "intraday", "direction": "LONG", "trade_id": "t1",
                   "payload": {"reason": "intraday_cutoff_exception: no usable quote"}})
    assert "Unresolved" in m and "t1" in m
    w = R.message({"type": "WorkerStarted", "mode": None, "direction": None,
                   "payload": {"worker": "box:1", "gate": "current-session entries disabled: feeds not ready"}})
    assert "feeds not ready" in w
    assert R.message({"type": "Other", "payload": {}}) is None
    # HTML is escaped
    assert "&lt;" in R.message({"type": "ExitUnresolved", "mode": "x", "direction": "y", "trade_id": "<t>",
                                "payload": {"reason": "<b>"}})
