"""Publish sanitized forward-validation status; no frozen research changes."""
import json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv('/home/ubuntu/.env')
import publisher as P

def main():
    path=Path('/home/ubuntu/data/journal/confirmation-forward-v1/status.json')
    status=json.loads(path.read_text())
    allowed=('version','paper_only','evaluation_mode','start','recorded_sessions','scenarios',
             'live_ready','review_after_sessions','note','last_run_utc','latest_available_session')
    public={k:status[k] for k in allowed if k in status}
    P.write('confirmation_forward',public)
    print('Published sanitized forward status')

if __name__=='__main__':main()
