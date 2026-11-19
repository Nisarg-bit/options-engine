"""Frozen, research-only intraday confirmation experiments. No persistence/orders."""
import math
import daily_spread_policy as S
import intraday_vrp as V
import pricing as P

RULES = ('baseline', 'baseline_no_target', 'continuation', 'failed_break')


def hm(n):
    return f'{n//60:02d}:{n%60:02d}'


def fresh(meta, t, k, ot, side=None, lot=1):
    m = meta.get((t, k, ot))
    if not m or m.get('interpolated') is not False or not m.get('ticks', 0) > 0:
        return False
    if side:
        size = m.get('ask_qty' if side > 0 else 'bid_qty')
        return size is not None and math.isfinite(size) and size >= lot
    return True


def forward(bm, meta, t):
    chain = bm.get(t, {})
    k = V.atm_by_parity(chain)
    if k is None or any(not fresh(meta, t, k, ot) for ot in ('CE', 'PE')):
        return None
    f = V.forward(chain)
    return f if f is not None and math.isfinite(f) else None


def select(bm, meta, rule):
    if rule not in RULES:
        raise ValueError('unknown rule')
    opening = [forward(bm, meta, hm(n)) for n in range(S.minute('09:20'), S.minute('09:44')+1)]
    if any(f is None for f in opening):
        return None
    if rule.startswith('baseline'):
        return {'signal_at': '09:44', 'direction': 1 if opening[-1] >= opening[0] else -1, 'fallback': False}
    low, high = min(opening), max(opening)
    prev = 0
    for n in range(S.minute('09:45'), S.minute('13:29')+1):
        t = hm(n)
        f = forward(bm, meta, t)
        if f is None:
            return None
        outside = 1 if f > high else (-1 if f < low else 0)
        if rule == 'continuation' and outside and outside == prev:
            return {'signal_at': t, 'direction': outside, 'fallback': False}
        if rule == 'failed_break' and prev and low < f < high:
            return {'signal_at': t, 'direction': -prev, 'fallback': False}
        prev = outside
    return {'signal_at': '13:29', 'direction': 1 if f >= (low+high)/2 else -1, 'fallback': True}


def simulate(bm, meta, rule, lot, structure='spread', slip=0., delay=0):
    if lot <= 0 or slip < 0 or delay not in (0, 1) or structure not in ('spread','long'):
        raise ValueError('invalid experiment configuration')
    base = {'rule': rule, 'structure': structure, 'slip': slip, 'delay': delay, 'sample_eligible': False}
    sig = select(bm, meta, rule)
    if sig is None:
        return {**base, 'quality_flags': ['invalid_signal_history']}
    base.update(sig)
    chain = bm[sig['signal_at']]
    k = V.atm_by_parity(chain)
    ot = 'CE' if sig['direction'] > 0 else 'PE'
    legs = [(k, ot, 1)]
    if structure == 'spread':
        legs.append((k + sig['direction'] * 2 * V.infer_step(chain), ot, -1))
    entry = hm(S.minute(sig['signal_at'])+1+delay)

    def execute(t, opening):
        if any(not fresh(meta, t, strike, kind, qty if opening else -qty, lot) for strike,kind,qty in legs):
            return None
        return S.execution(bm.get(t, {}), legs, lot, opening, slip)

    opened = execute(entry, True)
    if opened is None:
        return {**base, 'entry': entry, 'quality_flags': ['invalid_entry_quotes']}
    cash, fills = opened
    risk = -cash * lot
    if risk <= 0 or (structure == 'spread' and risk >= abs(legs[1][0]-k)*lot):
        return {**base, 'quality_flags': ['invalid_debit']}
    flags = set()
    result = {**base, 'entry': entry, 'risk_rs': risk, 'legs': legs}
    for n in range(S.minute(entry)+1, S.minute('15:00')+1):
        t = hm(n)
        closed = execute(t, False)
        if closed is None:
            flags.add('invalid_held_quotes')
            continue
        fees = P.fill_costs(fills+closed[1], costs=P.current_costs_for('NIFTY'))
        net = (cash+closed[0])*lot-fees
        reason = 'stop' if net <= -.35*risk else ('target' if rule == 'baseline' and net >= .5*risk else ('time' if t == '15:00' else None))
        if reason:
            return {**result, 'exit': t, 'exit_reason': reason, 'net_rs': net, 'fees_rs': fees,
                    'net_r': net/risk, 'quality_flags': sorted(flags), 'sample_eligible': not flags}
    return {**result, 'quality_flags': sorted(flags | {'missing_exit'})}
