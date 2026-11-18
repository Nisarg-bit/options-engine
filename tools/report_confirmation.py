"""Render research results without selecting or activating a winner."""
import argparse
import json
from pathlib import Path


def render(data):
    lines=['# Confirmation strategy development results', '',
           'Research only. No active strategy or broker configuration changed. The candidate definitions were written before this replay, but the historical sample had already been investigated; these results are not an independent holdout.', '',
           '## One-lot results after estimated charges', '',
           '| Entry / structure | Eligible days | Quoted net Rs | +1 point/leg/side net Rs | Extra minute delay net Rs | Quoted mean net / premium risk |',
           '|---|---:|---:|---:|---:|---:|']
    for rule in ('baseline','baseline_no_target','continuation','failed_break'):
        for structure in ('spread','long'):
            prefix=f'{rule}/{structure}'
            a,b,c=[data['summary'][prefix+'/'+s] for s in ('quoted','slippage','delay')]
            ratio=f"{a['mean_r']:.2%}" if a['mean_r'] is not None else 'N/A'
            lines.append(f"| {prefix} | {a['n']} / {b['n']} / {c['n']} | {a['net']:,.2f} | {b['net']:,.2f} | {c['net']:,.2f} | {ratio} |")
    lines += ['', 'Eligible-day counts are quoted / slippage / delay, respectively. Equal lots are not equal account risk. The last column is an analytical normalization, not a tradable fractional-lot strategy.', '',
              '## Daily participation and period stability (quoted fills)', '',
              '| Entry / structure | Earlier net Rs | Later net Rs | Fallback days | Fallback net Rs | Max drawdown Rs |',
              '|---|---:|---:|---:|---:|---:|']
    for key,a in data['summary'].items():
        if key.endswith('/quoted'):
            lines.append(f"| {key.removesuffix('/quoted')} | {a['earlier_net']:,.2f} | {a['later_net']:,.2f} | {a['fallback_n']} | {a['fallback_net']:,.2f} | {a['max_drawdown']:,.2f} |")
    lines += ['', 'Earlier/later boundary is 6 October, matching the prior diagnostic. Neither period is an untouched holdout. Fallback returns are included in the totals; missing-data days are not silently assigned zero P&L.', '',
              f"## Common-date comparison: {len(data['common_dates'])} sessions", '',
              '| Experiment | Paired net Rs |', '|---|---:|']
    for key,a in data['paired_summary'].items():
        lines.append(f"| {key} | {a['net']:,.2f} |")
    failures={}
    for r in data['rows']:
        if not r.get('sample_eligible'):
            failures.setdefault(r['date'],set()).update(r.get('quality_flags',[]))
    lines += ['', '## Coverage exceptions', '']
    lines += [f"- {day}: {', '.join(sorted(flags))}" for day,flags in sorted(failures.items())]
    lines += ['', '## Decision limits', '',
              'Do not activate a candidate solely because it wins this comparison. There are eight related entry/structure experiments and three execution scenarios, all evaluated on previously inspected development data. Minute-book fills do not establish executable simultaneous leg fills or intraminute stop sequencing. Missing quote intervals invalidate eligibility; stops use observed prices and may exceed the threshold.', '',
              'Next decisions require robustness across periods and execution scenarios, more historical data or locked prospective paper observations, and explicit account capital/daily/monthly loss limits. The daily real-trade objective does not override missing data or risk limits. No real-money profitability claim is made.', '']
    return '\n'.join(lines)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('output');a=p.parse_args()
    Path(a.output).write_text(render(json.loads(Path(a.input).read_text())),encoding='utf-8')
