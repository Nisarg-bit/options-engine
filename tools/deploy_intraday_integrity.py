"""Apply the verified audit bundle, retaining original files for rollback.

Run only on the collector server after tests and ledger reconstruction.
Only API/publisher services restart; collector is never restarted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path('/home/ubuntu')
AUDIT = ROOT / 'research-audit-20261025'
FILES = ('project_paths.py', 'pricing.py', 'intraday_vrp.py',
         'intraday_richness.py', 'daytrade.py', 'daytrade_api.py')


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def restart():
    subprocess.run(['sudo', '-n', 'systemctl', 'restart', 'options-api', 'options-publisher'], check=True)


def deploy():
    candidate = AUDIT / 'candidate'
    ledger = AUDIT / 'intraday_trades_v2.csv'
    summary = json.loads(Path(str(ledger)+'.summary.json').read_text())
    assert summary['settled'] == 37 and summary['clean_sample']['settled'] == 34, summary
    for name in FILES:
        assert (candidate / name).is_file(), name
        if (AUDIT / name).exists():
            assert digest(ROOT / name) == digest(AUDIT / name), f'production changed: {name}'
    before_pid = subprocess.check_output(['systemctl','show','options-collector','-p','MainPID','--value'], text=True).strip()
    backup = AUDIT / ('deployment-backup-' + str(int(time.time())))
    backup.mkdir()
    targets = [(candidate / name, ROOT / name) for name in FILES]
    targets.append((ledger, ROOT / 'data/journal/intraday_trades_v2.csv'))
    previous = {}
    for source, target in targets:
        previous[str(target)] = target.exists()
        if target.exists():
            shutil.copy2(target, backup / target.name)
    (backup / 'manifest.json').write_text(json.dumps(previous, indent=2))
    try:
        for source, target in targets:
            temporary = target.with_name(target.name + '.audit-new')
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        restart()
        time.sleep(3)
        subprocess.run(['systemctl','is-active','--quiet','options-api','options-publisher','options-collector'], check=True)
        after_pid = subprocess.check_output(['systemctl','show','options-collector','-p','MainPID','--value'], text=True).strip()
        assert before_pid == after_pid, 'collector PID changed unexpectedly'
    except Exception:
        for _, target in targets:
            if previous[str(target)]:
                shutil.copy2(backup / target.name, target)
            elif target.exists():
                target.unlink()
        restart()
        raise
    print(json.dumps({'deployed': list(FILES), 'backup': str(backup),
                      'collector_pid_unchanged': before_pid, 'summary': summary}, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true', required=True)
    p.parse_args()
    deploy()
