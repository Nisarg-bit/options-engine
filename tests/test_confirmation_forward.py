from datetime import date, datetime, timezone
import json
import pytest
import confirmation_forward as F


def test_forward_start_and_close_cutoff():
    now=datetime(2026,10,27,10,14,tzinfo=timezone.utc)
    assert not F.eligible_day(date(2026,10,26),now)
    assert not F.eligible_day(date(2026,10,27),now)
    assert not F.eligible_day(date(2026,10,28),now)
    assert F.eligible_day(date(2026,10,27),now.replace(minute=15))


def test_observation_cannot_be_rewritten(tmp_path):
    p=tmp_path/'journal.json'
    assert F.save_new(p,{'date':'2026-10-27','net':-100})
    assert not F.save_new(p,{'date':'2026-10-27','net':100})
    assert json.loads(p.read_text())[0]['net']==-100


def test_dependency_change_blocks_run(tmp_path):
    source=tmp_path/'strategy.py';source.write_text('original')
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({str(source):F.digest(source)}))
    assert F.verify(manifest)
    source.write_text('changed')
    with pytest.raises(RuntimeError,match='frozen dependency'):
        F.verify(manifest)


def test_failed_days_are_counted_but_not_profit(tmp_path):
    r=F.summary([{'date':'2026-10-27','scenarios':{}}])
    assert r['recorded_sessions']==1
    assert r['scenarios']['quoted']['eligible_sessions']==0
    assert not r['live_ready']
