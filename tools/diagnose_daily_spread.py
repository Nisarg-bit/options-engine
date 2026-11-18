"""Read-only loss attribution and counterfactual audit. Does not select rules.

Counterfactuals share frozen entry legs; exits are diagnostic, not fitted.
Historical sample is already inspected and cannot be reused as a clean holdout.
"""
import argparse
from collections import Counter, defaultdict
import glob
import json
import math
import os
import numpy as np
import pandas as pd
import daily_spread_policy as S
import daytrade as D
import intraday_vrp as V
import pricing as P


def totals(rows, field='net_rs'):
    xs = [r[field] for r in rows if r.get(field) is not None]
    win = [x for x in xs if x>0]
    loss = [x for x in xs if x<0]
    aw = sum(win)/len(win) if win else None
    al = -sum(loss)/len(loss) if loss else None
    return {'n':len(xs),'net':sum(xs),'mean':sum(xs)/len(xs) if xs else None,
            'wins':len(win),'avg_win':aw,'avg_loss':al,
            'breakeven_win_rate': al/(aw+al) if aw and al else None}


def mid_cash(chain,legs,opening):
    return sum((-q if opening else q)*sum(chain[k][ot])/2 for k,ot,q in legs)


def metadata(day,expiry):
    files=sorted(glob.glob(os.path.join(V.BARS,f'date={day}','underlying=NIFTY','*.parquet')))
    cols=['ts','expiry','strike','opt_type','is_interpolated','tick_count','bid_qty','ask_qty']
    df=pd.concat([pd.read_parquet(f,columns=cols) for f in files],ignore_index=True)
    df=df[pd.to_datetime(df.expiry).dt.date==expiry].copy()
    df['hm']=pd.to_datetime(df.ts,utc=True).dt.tz_convert('Asia/Kolkata').dt.strftime('%H:%M')
    return {(r.hm,float(r.strike),str(r.opt_type)):
            {'interpolated': bool(r.is_interpolated), 'ticks': int(r.tick_count),
             'bid_qty':r.bid_qty,'ask_qty':r.ask_qty} for r in df.itertuples()}


def path(bm,r,lot):
    opened=S.execution(bm[S.ENTRY],r['legs'],lot,True)
    out=[]
    for n in range(S.minute(S.ENTRY)+1,S.minute(S.EXIT)+1):
        t=f'{n//60:02d}:{n%60:02d}'
        closed=S.execution(bm.get(t,{}),r['legs'],lot,False)
        if closed is None:
            continue
        fees=P.fill_costs(opened[1]+closed[1],costs=P.current_costs_for('NIFTY'))
        out.append({'time':t,'net':(opened[0]+closed[0])*lot-fees})
    return out


def first_exit(marks,r,stop=True,target=True):
    for p in marks:
        if (stop and p['net']<=r['stop_net_rs']) or (target and p['net']>=r['target_net_rs']):
            return p
    return marks[-1]


def run():
    rows=[];excluded=[]
    for day in V.sessions('NIFTY'):
        bm,expiry,error=V.load(day,'NIFTY')
        if error:
            excluded.append({'date':str(day),'reason':error});continue
        lot,source=D.session_lot(day,expiry,'NIFTY')
        r=S.simulate(bm,day,expiry,'trend',lot)
        if not r.get('sample_eligible') or source=='unverified fallback':
            excluded.append({'date':str(day),'reason':r.get('quality_flags')});continue
        row=dict(r)
        row['dte_calendar']=(expiry-day).days
        row['gross_after_spread']=r['net_rs']+r['fees_rs']
        row['mid_gross_same_exit']=(mid_cash(bm[S.ENTRY],r['legs'],True)+mid_cash(bm[r['exit']],r['legs'],False))*lot
        row['spread_cost']=row['mid_gross_same_exit']-row['gross_after_spread']
        row['hold_minutes']=S.minute(r['exit'])-S.minute(S.ENTRY)
        row['debit_fraction_width']=-r['entry_cash_pts']/r['width']
        row['risk_multiple']=r['net_rs']/r['gross_expiry_max_loss_rs']
        row['fee_fraction_risk']=r['fees_rs']/r['gross_expiry_max_loss_rs']
        row['entry_spread_cost']=(-mid_cash(bm[S.ENTRY],r['legs'],True)+r['entry_cash_pts'])*-lot
        f0=V.forward(bm[S.ENTRY])
        row['horizons']={}
        for t in ('10:00','10:15','10:45','12:00','15:00'):
            f=V.forward(bm.get(t,{}))
            row['horizons'][t]=None if f is None else (f-f0)*r['direction']
        rev=S.simulate(bm,day,expiry,'reversal',lot)
        row['reverse_net']=rev.get('net_rs') if rev.get('sample_eligible') else None
        row['always_call_net']=r['net_rs'] if r['direction']>0 else row['reverse_net']
        row['always_put_net']=r['net_rs'] if r['direction']<0 else row['reverse_net']
        marks=path(bm,r,lot)
        row['full_path']=len(marks)==S.minute(S.EXIT)-S.minute(S.ENTRY)
        if row['full_path']:
            row['time_only_net']=marks[-1]['net']
            row['stop_only_net']=first_exit(marks,r,True,False)['net']
            row['target_only_net']=first_exit(marks,r,False,True)['net']
            row['best_observed_net']=max(p['net'] for p in marks)
            row['worst_observed_net']=min(p['net'] for p in marks)
            row['both_thresholds_seen']=any(p['net']>=r['target_net_rs'] for p in marks) and any(p['net']<=r['stop_net_rs'] for p in marks)
            assert abs(first_exit(marks,r)['net']-r['net_rs'])<1e-6
        meta=metadata(day,expiry)
        issues=[]; illiquid=[]; used=0; stale=0; stale_signal=0
        for t in sorted(t for t in bm if '09:20'<=t<='09:44'):
            k=V.atm_by_parity(bm[t])
            for ot in ('CE','PE'):
                m=meta.get((t,k,ot),{})
                stale_signal+=bool(m.get('interpolated') or m.get('ticks')==0)
        for t,opening in ((S.ENTRY,True),(r['exit'],False)):
            for k,ot,q in r['legs']:
                m=meta.get((t,k,ot),{})
                if m.get('interpolated') or m.get('ticks')==0:
                    issues.append(f'{t}:{k}:{ot}')
                size=m.get('ask_qty' if (q>0)==opening else 'bid_qty')
                if size is None or not math.isfinite(size) or size<lot:
                    illiquid.append(f'{t}:{k}:{ot}')
        for t in sorted(t for t in bm if S.ENTRY<=t<=r['exit']):
            for k,ot,q in r['legs']:
                m=meta.get((t,k,ot),{})
                used+=1;stale+=bool(m.get('interpolated') or m.get('ticks')==0)
        row['data_audit']={'stale_execution_quotes':issues,'insufficient_displayed_quantity':illiquid,
                           'stale_signal_quotes':stale_signal,'held_leg_minutes':used,'stale_held_leg_minutes':stale}
        rows.append(row)
        print(str(day),'audited',flush=True)
    summary={k:totals(rows,k) for k in ('net_rs','gross_after_spread','mid_gross_same_exit','fees_rs','spread_cost',
             'reverse_net','always_call_net','always_put_net','time_only_net','stop_only_net','target_only_net')}
    groups={}
    classifiers={'direction':lambda r:'call' if r['direction']>0 else 'put',
      'exit_reason':lambda r:r['exit_reason'],
      'dte':lambda r:str(r['dte_calendar']),
      'strength':lambda r:'<0.30' if r['trend_ratio']<.3 else ('0.30-0.60' if r['trend_ratio']<.6 else '>=0.60'),
      'opening_move':lambda r:'<25' if abs(r['move'])<25 else ('25-75' if abs(r['move'])<75 else '>=75'),
      'period':lambda r:'earlier' if r['date']<'2026-10-06' else 'later'}
    for key,fn in classifiers.items():
        groups[key]={v:totals([r for r in rows if fn(r)==v]) for v in sorted({fn(r) for r in rows})}
    horizons={t:{'n':len(xs),'correct':sum(x>0 for x in xs),'mean_signed_move':float(np.mean(xs))}
              for t in ('10:00','10:15','10:45','12:00','15:00')
              for xs in [[r['horizons'][t] for r in rows if r['horizons'][t] is not None]]}
    rng=np.random.default_rng(20261025); xs=np.array([r['net_rs'] for r in rows]);n=len(xs)
    iid=rng.choice(xs,(20000,n),replace=True).mean(axis=1)
    starts=rng.integers(0,n,(20000,math.ceil(n/5)))
    indices=((starts[:,:,None]+np.arange(5))%n).reshape(20000,-1)[:,:n]
    block=xs[indices].mean(axis=1)
    stoprows=[r for r in rows if r['exit_reason']=='stop' and r['full_path']]
    data={'execution_stale_days':sum(bool(r['data_audit']['stale_execution_quotes']) for r in rows),
          'insufficient_depth_days':sum(bool(r['data_audit']['insufficient_displayed_quantity']) for r in rows),
          'signal_stale_days':sum(r['data_audit']['stale_signal_quotes']>0 for r in rows),
          'held_stale_days':sum(r['data_audit']['stale_held_leg_minutes']>0 for r in rows),
          'stale_held_leg_minutes':sum(r['data_audit']['stale_held_leg_minutes'] for r in rows),
          'held_leg_minutes':sum(r['data_audit']['held_leg_minutes'] for r in rows)}
    return {'summary':summary,'groups':groups,'horizons':horizons,'data_audit':data,
            'uncertainty':{'iid_mean_95':np.quantile(iid,[.025,.975]).tolist(),
                           'five_day_block_mean_95':np.quantile(block,[.025,.975]).tolist(),
                           'warning':'Descriptive resampling, small previously inspected sample, not validation'},
            'exit_audit':{'stopped_full_path':len(stoprows),
               'stopped_but_positive_at_1500':sum(r['time_only_net']>0 for r in stoprows),
               'stopped_then_target_seen':sum(r['best_observed_net']>=r['target_net_rs'] for r in stoprows),
               'median_hold_minutes':float(np.median([r['hold_minutes'] for r in rows])),
               'median_fee_fraction_risk':float(np.median([r['fee_fraction_risk'] for r in rows]))},
            'excluded':excluded,'rows':rows}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()
    result=run()
    with open(a.output,'w') as f:json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
