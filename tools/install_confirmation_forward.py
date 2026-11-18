"""Install an isolated paper research timer. Never restarts trading services."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path('/home/ubuntu/confirmation-forward-v1')


def main():
    import confirmation_forward as F
    manifest=ROOT/'manifest.json'
    if manifest.exists():
        F.verify(manifest)
    else:
        paths={Path(m.__file__).resolve() for m in tuple(sys.modules.values())
               if getattr(m,'__file__',None) and str(Path(m.__file__).resolve()).startswith('/home/ubuntu/')
               and '/.venv/' not in str(Path(m.__file__).resolve()) and str(m.__file__).endswith('.py')}
        manifest.write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)},indent=2))
    service='''[Unit]
Description=Frozen confirmation candidate post-session paper validation
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu
Environment=PYTHONPATH=/home/ubuntu
ExecStart=/usr/bin/flock -n /home/ubuntu/confirmation-forward-v1/run.lock /home/ubuntu/.venv/bin/python /home/ubuntu/confirmation-forward-v1/confirmation_forward.py --manifest /home/ubuntu/confirmation-forward-v1/manifest.json --output-dir /home/ubuntu/data/journal/confirmation-forward-v1
Nice=10
TimeoutStartSec=300
NoNewPrivileges=true
'''
    timer='''[Unit]
Description=Evaluate frozen confirmation paper candidate after market close

[Timer]
OnCalendar=Mon..Fri *-*-* 10:20:00 UTC
OnCalendar=Mon..Fri *-*-* 10:40:00 UTC
Persistent=true
AccuracySec=1min
Unit=options-confirmation-forward.service

[Install]
WantedBy=timers.target
'''
    for suffix,content in [('service',service),('timer',timer)]:
        p=ROOT/f'options-confirmation-forward.{suffix}'
        p.write_text(content)
    subprocess.run(['systemd-analyze','verify',str(ROOT/'options-confirmation-forward.service'),str(ROOT/'options-confirmation-forward.timer')],check=True)
    for suffix in ('service','timer'):
        name=f'options-confirmation-forward.{suffix}'
        subprocess.run(['sudo','-n','install','-m','644',str(ROOT/name),'/etc/systemd/system/'+name],check=True)
    subprocess.run(['sudo','-n','systemctl','daemon-reload'],check=True)
    subprocess.run(['sudo','-n','systemctl','enable','--now','options-confirmation-forward.timer'],check=True)
    subprocess.run(['sudo','-n','systemctl','start','options-confirmation-forward.service'],check=True)
    print(json.dumps({'manifest':str(manifest),'dependencies':len(json.loads(manifest.read_text())),'paper_only':True}))


if __name__=='__main__':
    main()
