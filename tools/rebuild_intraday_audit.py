"""Rebuild a separate corrected ledger; never overwrite the original journal."""
import argparse
import json
from pathlib import Path
import daytrade as D
import intraday_vrp as V
import intraday_richness as R


def rebuild(output):
    if Path(output).resolve() == Path(D.JOURNAL).resolve():
        raise ValueError('audit output must be separate from the active journal')
    rows, history, scores = [], [], []
    for day in V.sessions('NIFTY'):
        book, expiry, error = V.load(day, 'NIFTY')
        if error:
            raise RuntimeError(f'{day}: {error}')
        row = D.evaluate(day, book, expiry, 'NIFTY', history, scores)
        if row:
            rows.append(row)
            print(day, row['status'], row['net_rs'], row['quality_flags'], flush=True)
        rich = R.study(day, book, expiry, D.ENTRY, D.EXIT)
        if rich and rich.get('history_eligible'):
            _, score = D.richness_score(rich['implied'], history)
            history.append(rich['rv15'])
            if score:
                scores.append(score)
    D.write_journal(rows, path=output)
    report = D.summary_data(rows)
    Path(output + '.summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    rebuild(parser.parse_args().output)
