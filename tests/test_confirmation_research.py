import copy
import pytest
import confirmation_research as R
from test_daily_spread_policy import book


def fixture():
    bm=book()
    meta={(t,k,ot):{'interpolated':False,'ticks':1,'bid_qty':650,'ask_qty':650}
          for t,c in bm.items() for k,qs in c.items() for ot in qs}
    return bm,meta


def shift(bm,t,amount):
    for qs in bm[t].values():
        for ot,sign in [('CE',1),('PE',-1)]:
            qs[ot]=tuple(x+sign*amount/2 for x in qs[ot])


def test_confirmation_waits_and_does_not_look_ahead():
    bm,meta=fixture()
    shift(bm,'09:45',10);shift(bm,'09:46',10)
    sig=R.select(bm,meta,'continuation')
    assert sig['signal_at']=='09:46' and sig['direction']==1
    changed=copy.deepcopy(bm)
    for t in changed:
        if t>'09:46':shift(changed,t,-50)
    assert R.select(changed,meta,'continuation')==sig
    assert R.simulate(bm,meta,'continuation',65)['entry']=='09:47'
    assert R.simulate(bm,meta,'continuation',65,delay=1)['entry']=='09:48'


def test_failed_break_reverses_only_after_return_inside():
    bm,meta=fixture();shift(bm,'09:45',10);shift(bm,'09:46',-10)
    s=R.select(bm,meta,'failed_break')
    assert s=={'signal_at':'09:46','direction':-1,'fallback':False}


def test_daily_fallback_is_explicit():
    bm,meta=fixture()
    r=R.simulate(bm,meta,'continuation',65)
    assert r['fallback'] and r['entry']=='13:30' and r['exit']=='15:00'


def test_stale_signal_and_insufficient_execution_depth_rejected():
    bm,meta=fixture()
    for key in meta:
        if key[0]=='09:30':meta[key]['interpolated']=True
    assert not R.simulate(bm,meta,'baseline',65)['sample_eligible']
    bm,meta=fixture()
    for key in meta:
        if key[0]=='09:45':meta[key]['ask_qty']=0
    assert R.simulate(bm,meta,'baseline',65)['quality_flags']==['invalid_entry_quotes']


def test_missing_held_quotes_cannot_make_clean_result():
    bm,meta=fixture();del bm['10:00']
    r=R.simulate(bm,meta,'baseline_no_target',65)
    assert not r['sample_eligible'] and 'invalid_held_quotes' in r['quality_flags']


def test_baseline_reconciles_and_long_has_bounded_premium_risk():
    from datetime import date
    import daily_spread_policy as S
    bm,meta=fixture()
    r=R.simulate(bm,meta,'baseline',65)
    old=S.simulate(bm,date(2026,10,26),date(2026,10,27),lot=65)
    assert r['net_rs']==pytest.approx(old['net_rs'])
    long=R.simulate(bm,meta,'baseline_no_target',65,'long')
    assert len(long['legs'])==1 and long['risk_rs']>0
    assert long['fees_rs']<r['fees_rs']


def test_no_target_keeps_winner_open_but_preserves_stop():
    bm,meta=fixture()
    k=R.V.atm_by_parity(bm['09:44'])
    for t in bm:
        if t >= '09:46':
            bm[t][k]['CE']=tuple(x+35 for x in bm[t][k]['CE'])
    assert R.simulate(bm,meta,'baseline',65)['exit_reason']=='target'
    assert R.simulate(bm,meta,'baseline_no_target',65)['exit']=='15:00'
    bm['09:47'][k]['CE']=(100,101)
    stopped=R.simulate(bm,meta,'baseline_no_target',65)
    assert stopped['exit']=='09:47' and stopped['exit_reason']=='stop'


def test_extra_slippage_is_charged_on_every_leg():
    bm,meta=fixture()
    normal=R.simulate(bm,meta,'baseline_no_target',65)
    stress=R.simulate(bm,meta,'baseline_no_target',65,slip=1)
    assert stress['exit']==normal['exit']
    assert stress['net_rs']+stress['fees_rs']==pytest.approx(normal['net_rs']+normal['fees_rs']-4*65)
