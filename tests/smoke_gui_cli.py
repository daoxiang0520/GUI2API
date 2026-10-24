"""Actual persistent CLI parsing/recovery, without touching application controls."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
messages = [json.dumps({'id':1,'op':'schema'}), 'not json',
            json.dumps({'id':2,'op':'schema'}), json.dumps({'id':3,'op':'close'})]
process = subprocess.run([sys.executable,str(ROOT/'guictl.py'),'session'],
    input='\n'.join(messages)+'\n',capture_output=True,text=True,encoding='utf-8',timeout=15)
assert process.returncode == 0, process.stderr
rows = [json.loads(line) for line in process.stdout.splitlines()]
assert len(rows)==4 and rows[0]['ok'] and not rows[1]['ok'] and rows[2]['ok'] and rows[3]['ok']
assert rows[0]['data']['protocol']=='appctl.gui/1' and rows[3]['data']['cleanup']==[]
print(json.dumps({'ok':True,'replies':len(rows),'invalid_request_recovered':True,'application_actions':0}))
