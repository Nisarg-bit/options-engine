from datetime import date
import pytest
import daily_spread_policy as S

DAY = date(2026, 10, 26)


def book(end='15:00', falling=False):
    bm = {}
    for n in range(S.minute('09:20'), S.minute(end)+1):
        t = f'{n//60:02d}:{n%60:02d}'
        f = 10000 + (-1 if falling else 1) * min(n-S.minute('09:20'), 24)
        bm[t] = {k: {'CE': (200+(f-k)/2,201+(f-k)/2),
                     'PE': (200+(k-f)/2,201+(k-f)/2)}
                 for k in range(9800,10201,50)}
    return bm


def test_signal_cannot_see_entry_or_later_prices():
    bm = book()
    expected = S.signal(bm, 'adaptive')
    for t in list(bm):
        if t > '09:44':
            bm[t] = {1: {'CE': (999,1000),'PE': (1,2)}}
    assert S.signal(bm, 'adaptive') == expected
    assert expected['shape'] == 'trend'


def test_direction_and_reversal():
    bm = book(falling=True)
    assert S.signal(bm,'trend')['legs'][0][1] == 'PE'
    assert S.signal(bm,'reversal')['legs'][0][1] == 'CE'


def test_adaptive_neutral_branch_is_not_the_active_policy():
    bm = book()
    bm['09:44'] = bm['09:20']
    assert S.signal(bm,'adaptive')['shape'] == 'iron_fly'
    assert S.ACTIVE == 'trend'
    assert S.simulate(book(),DAY,DAY)['candidate'] == 'trend'


def test_round_trip_uses_ask_to_buy_bid_to_sell_and_charges_all_orders():
    r = S.simulate(book(), DAY, DAY, 'trend')
    assert r['sample_eligible']
    assert r['entry_cash_pts'] == -51
    assert r['exit_cash_pts'] == 49
    assert r['net_rs'] == pytest.approx(-2*65-r['fees_rs'])
    assert r['fees_rs'] > 0
    assert r['exit'] == '15:00'
    assert r['exit_reason'] == 'time'
    assert r['gross_expiry_max_loss_rs'] == 51*65


def test_stop_uses_observed_worse_price_and_no_reentry():
    bm = book()
    k,ot,_ = S.signal(bm,'trend')['legs'][0]
    bm['09:46'][k][ot] = (100,101)
    r = S.simulate(bm,DAY,DAY,'trend')
    assert r['exit_reason'] == 'stop' and r['exit'] == '09:46'
    assert r['net_rs'] < r['stop_net_rs']


def test_target_and_stop_are_based_on_net_after_fees():
    bm = book()
    k,ot,_ = S.signal(bm,'trend')['legs'][0]
    b,a = bm['09:46'][k][ot]
    bm['09:46'][k][ot] = (b+35,a+35)
    r = S.simulate(bm,DAY,DAY,'trend')
    assert r['exit_reason'] == 'target'
    assert r['net_rs'] >= r['target_net_rs']


def test_missing_signal_entry_and_held_quotes_not_fabricated():
    bm = book()
    del bm['09:30']
    assert S.simulate(bm,DAY,DAY)['status'] == 'unavailable'
    bm = book()
    del bm['09:45']
    assert S.simulate(bm,DAY,DAY)['quality_flags'] == ['missing_entry_quotes']
    bm = book()
    del bm['10:00']
    assert not S.simulate(bm,DAY,DAY,'trend')['sample_eligible']


def test_open_session_and_missing_exit_are_distinct():
    assert S.simulate(book('12:00'),DAY,DAY,'trend')['status'] == 'open'
    bm = book('15:01')
    del bm['15:00']
    r = S.simulate(bm,DAY,DAY,'trend')
    assert r['status'] == 'unresolved' and not r['sample_eligible']
    assert 'missing_exit' in r['quality_flags']


def test_payoff_envelope_and_input_validation():
    assert S.expiry_bounds([(100,'CE',1),(110,'CE',-1)],-4) == (4,6)
    assert S.expiry_bounds([(100,'CE',-1),(100,'PE',-1),
                            (110,'CE',1),(90,'PE',1)],6) == (4,6)
    with pytest.raises(ValueError):
        S.expiry_bounds([(100,'CE',-1)],5)
    with pytest.raises(ValueError):
        S.simulate(book(),DAY,DAY,lot=0)


def test_journal_starts_prospectively_and_upserts(tmp_path):
    path = tmp_path/'paper.json'
    row = S.simulate(book(),DAY,DAY,'trend')
    old = dict(row,date='2026-10-23')
    S.persist(old,path)
    assert not path.exists()
    S.persist(row,path)
    S.persist(row,path)
    assert len(S.read_journal(path)) == 1
    assert S.payload(row,path)['forward_sessions'] == 1


def test_stress_fill_and_rejection_of_crossed_quotes():
    bm = book()
    regular = S.simulate(bm,DAY,DAY,'trend')
    stress = S.simulate(bm,DAY,DAY,'trend',extra_slip=1)
    assert stress['net_rs'] < regular['net_rs'] - 250
    assert S.quote({1:{'CE':(3,2)}},1,'CE') is None


def test_api_exposes_new_policy_without_replacing_baseline(monkeypatch, tmp_path):
    import daytrade_api as API
    bm = book()
    monkeypatch.setattr(API, '_sig', lambda *a: ('new-policy-test',))
    monkeypatch.setattr(API.V, 'load', lambda *a: (bm, DAY, None))
    monkeypatch.setattr(API.D, 'evaluate', lambda *a: {'structure':'atm_straddle'})
    monkeypatch.setattr(API.D, 'session_lot', lambda *a: (65,'master test'))
    API._cache.clear()
    row = API._today_row(DAY,'NIFTY',[],[])
    assert row['structure'] == 'atm_straddle'
    assert row['experimental_spread']['candidate'] == 'trend'
    assert row['experimental_spread']['sample_eligible']
    p = S.payload(row['experimental_spread'],tmp_path/'new.json')
    assert p['paper_only'] and p['today']['date'] == str(DAY)
    API._cache.clear()
