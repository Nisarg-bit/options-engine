"""Execution and sample integrity: failures here can change trading conclusions."""
from datetime import date
import pytest
import pricing as P
import intraday_vrp as V
import daytrade as D
import intraday_validation as IV


def test_actual_exit_turnover_and_all_order_gst():
    c = P.CURRENT_COSTS
    fills = [('sell', 100, 65), ('sell', 100, 65),
             ('buy', 150, 65), ('buy', 150, 65)]
    turnover = 32500
    exchange = turnover * c.exchange_per_side
    sebi = turnover * c.sebi_per_side
    brokerage = 4 * c.brokerage_per_order
    expected = (brokerage + exchange + sebi) * (1+c.gst)
    expected += 13000*c.stt_on_sell + 19500*c.stamp_buy
    assert P.fill_costs(fills) == pytest.approx(expected)
    assert P.fill_costs(fills + fills) == pytest.approx(2*expected)


@pytest.mark.parametrize('fill', [('sell', float('nan'), 65), ('buy', 2, 0), ('bad', 2, 65)])
def test_invalid_fills_rejected(fill):
    with pytest.raises(ValueError):
        P.fill_costs([fill])


def quote(p=100):
    return {23000: {'CE': (p, p+0.1), 'PE': (p, p+0.1)},
            22950: {'CE': (p+25, p+25.1), 'PE': (p-25, p-24.9)},
            23050: {'CE': (p-25, p-24.9), 'PE': (p+25, p+25.1)}}


def sim(book):
    return V.simulate(book, date(2026,9,21), date(2026,9,22), 0)


def test_missing_minutes_do_not_slow_the_decay_clock():
    r = sim({'09:20': quote(), '15:15': quote(90)})
    assert r['s_in'] == pytest.approx(1 + 370/375)
    assert r['s_out'] == pytest.approx(1 + 15/375)
    assert not r['sample_eligible']


def test_missing_exit_quote_is_not_a_completed_time_exit():
    r = sim({'09:20': quote(), '15:14': quote(90), '15:15': {}})
    assert not r['complete']


def test_late_entry_is_explicitly_excluded():
    r = sim({'12:07': quote(), '15:15': quote(90)})
    assert 'late_entry' in r['quality_flags']
    assert not r['sample_eligible']


def test_valid_daily_session_keeps_one_trade_and_same_day_exit():
    book = {f'{m//60:02d}:{m%60:02d}': quote(100-(m-560)/100)
            for m in range(560, 916)}
    r = sim(book)
    assert r['sample_eligible']
    assert r['entry'] == '09:20' and r['exit'] == '15:15'
    assert r['complete'] and r['exit_reason'] == 'time'
    assert r['net_rs'] == pytest.approx(r['pnl_rs'] - r['fees_rs'])


def test_stop_settles_before_close_without_future_bars():
    r = sim({'09:20': quote(), '09:21': quote(140)})
    assert r['complete'] and r['sample_eligible']
    assert r['exit_reason'] == 'stop'
    assert r['cover'] > r['credit'] * 1.3  # actual observed fill, not capped stop


def test_clean_summary_does_not_certify_legacy_rows():
    base = dict(status='settled', net_rs=100, ratio='', exit_reason='time', entry='09:20')
    rows = [base, dict(base, sample_eligible=True),
            dict(base, sample_eligible=True, entry='12:07')]
    result = D.summary_data(rows)
    assert result['total_net'] == 300
    assert result['clean_sample']['total_net'] == 100
    assert result['unverified_or_excluded'] == 2


def test_no_bars_never_creates_a_successful_backtest(monkeypatch):
    monkeypatch.setattr(V, 'sessions', lambda _: [])
    report = IV.run()
    assert report['status'] == 'blocked_missing_minute_bars'
    assert report['live_approved'] is False
    assert all(x['holdout']['sessions'] == 0 for x in report['candidates'].values())


def test_upsert_keeps_other_index_on_same_date():
    n = dict(date='2026-10-23', underlying='NIFTY', net_rs=1)
    b = dict(n, underlying='BANKNIFTY', net_rs=2)
    rows = D.upsert([n, b], dict(n, net_rs=3))
    assert len(rows) == 2
    assert sum(r['net_rs'] for r in rows) == 5
