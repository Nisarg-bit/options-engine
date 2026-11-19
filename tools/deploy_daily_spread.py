"""Deploy the tested paper-only rules; preserve rollback and collector process."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path('/home/ubuntu')
STAGE = ROOT/'spread-research-20261025'
FILES = ('daily_spread_policy.py', 'daytrade.py', 'daytrade_api.py')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    report = json.loads((STAGE/'results.json').read_text())
    assert report['candidates']['trend']['all']['sessions'] == 34
    assert report['candidates']['trend']['later_stress']['sessions'] == 13
    for name in FILES:
        assert (STAGE/name).is_file()
    for name in ('daytrade.py','daytrade_api.py'):
        original = ROOT/'research-audit-20261025/candidate'/name
        assert digest(ROOT/name) == digest(original), f'production changed: {name}'
    pid = lambda: subprocess.check_output(['systemctl','show','options-collector','-p','MainPID','--value'],text=True).strip()
    before = pid()
    backup = STAGE/f'backup-{int(time.time())}'
    backup.mkdir()
    existed = {name:(ROOT/name).exists() for name in FILES}
    for name, exists in existed.items():
        if exists:
            shutil.copy2(ROOT/name,backup/name)
    (backup/'manifest.json').write_text(json.dumps(existed))
    def restart():
        subprocess.run(['sudo','-n','systemctl','restart','options-api','options-publisher'],check=True)
    try:
        for name in FILES:
            temporary = ROOT/(name+'.spread-new')
            shutil.copy2(STAGE/name,temporary)
            os.replace(temporary,ROOT/name)
        restart()
        time.sleep(3)
        subprocess.run(['systemctl','is-active','--quiet','options-api','options-publisher','options-collector'],check=True)
        assert before == pid(), 'collector changed'
    except Exception:
        for name, exists in existed.items():
            if exists:
                shutil.copy2(backup/name,ROOT/name)
            elif (ROOT/name).exists():
                (ROOT/name).unlink()
        restart()
        raise
    print(json.dumps({'backup':str(backup),'collector_pid':before,'files':FILES}))


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--apply',action='store_true',required=True)
    p.parse_args()
    main()
