"""Attach status publication after successful frozen-paper evaluation."""
from pathlib import Path
import subprocess
p=Path('/home/ubuntu/confirmation-ui.conf')
p.write_text('[Service]\nExecStartPost=/home/ubuntu/.venv/bin/python /home/ubuntu/publish_confirmation_ui.py\n')
subprocess.run(['sudo','-n','mkdir','-p','/etc/systemd/system/options-confirmation-forward.service.d'],check=True)
subprocess.run(['sudo','-n','install','-m','644',str(p),'/etc/systemd/system/options-confirmation-forward.service.d/website.conf'],check=True)
subprocess.run(['sudo','-n','systemctl','daemon-reload'],check=True)
subprocess.run(['/home/ubuntu/.venv/bin/python','/home/ubuntu/publish_confirmation_ui.py'],check=True)
