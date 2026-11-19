"""Frozen daily spread experiments. Paper only; no broker execution.

Signal uses 09:20..09:44 parity forwards, entry 09:45, exit no later than
15:00. Four predeclared candidates; no parameter search. All amounts are
one instrument-master lot. Positive leg quantity means buy.
"""
import json
import math
import os
from pathlib import Path

import intraday_vrp as V
import pricing as P
import project_paths

VERSION = 'opening_spread_v1'
ENTRY, EXIT = '09:45', '15:00'
CANDIDATES = ('trend', 'reversal', 'iron_fly', 'adaptive')
STOP_R, TARGET_R = 0.35, 0.50
WIDTH_STEPS = 2
TREND_RATIO = 0.60
ACTIVATION = '2026-10-26'
# Frozen before the first prospective session; changed only via a new version.
ACTIVE = 'trend'
JOURNAL = Path(project_paths.ROOT) / 'data/journal/daily_spread_v1.json'


def minute(t):
    h, m = map(int, t.split(':'))
    return h * 60 + m


def quote(chain, strike, kind):
    q = chain.get(strike, {}).get(kind)
    if not q or len(q) != 2 or not all(math.isfinite(x) for x in q):
        return None
    return q if 0 < q[0] <= q[1] else None


def signal(bm, candidate):
    if candidate not in CANDIDATES:
        raise ValueError('unknown candidate')
    times = sorted(t for t in bm if '09:20' <= t <= '09:44')
    if len(times) != 25:
        return None
    fwds = [V.forward(bm[t]) for t in times]
    if any(f is None or not math.isfinite(f) for f in fwds):
        return None
    move = fwds[-1] - fwds[0]
    extent = max(fwds) - min(fwds)
    ratio = abs(move) / extent if extent else 0.0
    direction = 1 if move >= 0 else -1
    shape = candidate
    if candidate == 'adaptive':
        shape = 'trend' if ratio >= TREND_RATIO else 'iron_fly'
    if shape == 'reversal':
        direction *= -1
    chain = bm['09:44']
    atm = V.atm_by_parity(chain)
    if atm is None:
        return None
    step = V.infer_step(chain)
    width = WIDTH_STEPS * step
    if shape == 'iron_fly':
        legs = [(atm, 'CE', -1), (atm, 'PE', -1),
                (atm + width, 'CE', 1), (atm - width, 'PE', 1)]
    elif direction > 0:
        legs = [(atm, 'CE', 1), (atm + width, 'CE', -1)]
    else:
        legs = [(atm, 'PE', 1), (atm - width, 'PE', -1)]
    return {'shape': shape, 'direction': direction, 'move': move,
            'opening_range': extent, 'trend_ratio': ratio, 'atm': atm,
            'width': width, 'legs': legs, 'signal_at': '09:44'}


def execution(chain, legs, lot, opening, extra_slip=0.0):
    """Cash received (negative for paid) and actual individual order fills."""
    cash, fills = 0.0, []
    for strike, kind, qty in legs:
        q = quote(chain, strike, kind)
        if q is None:
            return None
        side = qty if opening else -qty
        price = q[1] + extra_slip if side > 0 else max(0.0, q[0] - extra_slip)
        if price <= 0:
            return None
        cash -= side * price
        fills.append(('buy' if side > 0 else 'sell', price, abs(qty) * lot))
    return cash, fills


def expiry_bounds(legs, entry_cash):
    """Gross expiry payoff envelope; excludes fees and intraday legging risk."""
    ks = sorted({k for k, _, _ in legs})
    if sum(q for _, ot, q in legs if ot == 'CE') != 0:
        raise ValueError('unbounded call exposure')
    payoffs = [entry_cash + sum(q * max((s-k) if ot == 'CE' else (k-s), 0)
               for k, ot, q in legs) for s in [0.0, *ks, 2 * ks[-1]]]
    return -min(payoffs), max(payoffs)


def simulate(bm, day, expiry, candidate=ACTIVE, lot=65, extra_slip=0.0,
             underlying='NIFTY'):
    if lot <= 0 or extra_slip < 0:
        raise ValueError('invalid size or slippage')
    base = {'date': str(day), 'expiry': str(expiry), 'candidate': candidate,
            'version': VERSION, 'lot': lot, 'underlying': underlying,
            'sample_eligible': False, 'status': 'unavailable'}
    sig = signal(bm, candidate)
    if sig is None:
        return {**base, 'quality_flags': ['missing_signal_window']}
    opened = execution(bm.get(ENTRY, {}), sig['legs'], lot, True, extra_slip)
    if opened is None:
        return {**base, **sig, 'quality_flags': ['missing_entry_quotes']}
    cash, entry_fills = opened
    risk, reward = expiry_bounds(sig['legs'], cash)
    if not (0 < risk and 0 < reward):
        return {**base, **sig, 'quality_flags': ['invalid_payoff_after_spread']}
    costs = P.current_costs_for(underlying)
    flags, marks = [], []
    last = max(bm) if bm else ''
    reason = 'open' if last < EXIT else 'missing_exit'
    for n in range(minute(ENTRY) + 1, min(minute(last), minute(EXIT)) + 1):
        t = f'{n//60:02d}:{n%60:02d}'
        close = execution(bm.get(t, {}), sig['legs'], lot, False, extra_slip)
        if close is None:
            if 'missing_held_quotes' not in flags:
                flags.append('missing_held_quotes')
            continue
        close_cash, exit_fills = close
        fees = P.fill_costs(entry_fills + exit_fills, costs=costs)
        net = (cash + close_cash) * lot - fees
        marks.append((t, close_cash, fees, net))
        if net <= -STOP_R * risk * lot:
            reason = 'stop'
            break
        if net >= TARGET_R * risk * lot:
            reason = 'target'
            break
        if t == EXIT:
            reason = 'time'
    settled = reason in ('stop', 'target', 'time')
    if last >= EXIT and not settled:
        flags.append('missing_exit')
    t, close_cash, fees, net = marks[-1] if marks else (None, None, None, None)
    return {**base, **sig, 'status': 'settled' if settled else ('open' if last < EXIT else 'unresolved'),
            'sample_eligible': settled and not flags, 'quality_flags': flags,
            'entry': ENTRY, 'exit': t, 'exit_reason': reason,
            'entry_cash_pts': cash, 'exit_cash_pts': close_cash,
            'gross_expiry_max_loss_rs': risk * lot,
            'gross_expiry_max_profit_rs': reward * lot,
            'stop_net_rs': -STOP_R * risk * lot, 'target_net_rs': TARGET_R * risk * lot,
            'fees_rs': fees, 'net_rs': net,
            'extra_slippage_per_leg_per_side': extra_slip}


def read_journal(path=None):
    p = Path(path or JOURNAL)
    return json.loads(p.read_text()) if p.exists() else []


def persist(row, path=None):
    """Only prospective sessions; a replay cannot overwrite the baseline."""
    if row['date'] < ACTIVATION:
        return
    p = Path(path or JOURNAL)
    rows = read_journal(p)
    key = lambda r: (r['date'], r['underlying'], r['version'])
    kept = {key(r): r for r in rows}
    kept[key(row)] = row
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix('.tmp')
    temporary.write_text(json.dumps(sorted(kept.values(), key=key), indent=2))
    os.replace(temporary, p)


def payload(row=None, path=None):
    rows = read_journal(path)
    if row and row['date'] >= ACTIVATION:
        rows = [r for r in rows if (r['date'], r['underlying']) !=
                (row['date'], row['underlying'])] + [row]
    clean = [r for r in rows if r.get('sample_eligible')]
    return {'version': VERSION, 'candidate': ACTIVE, 'activation': ACTIVATION,
            'paper_only': True, 'entry': ENTRY, 'exit_by': EXIT,
            'today': row if row and row['date'] >= ACTIVATION else None,
            'forward_sessions': len(clean),
            'forward_net_rs': sum(r['net_rs'] for r in clean),
            'journal': rows[-120:]}
