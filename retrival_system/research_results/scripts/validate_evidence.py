"""Fail on incomplete evidence, mismatched counts, unreadable charts or leaked tokens."""
import hashlib
import json
from common import ROOT, OUT, credentials, records, save
from PIL import Image

data=json.loads((ROOT/'tests/research_evaluation/announcements.json').read_text(encoding='utf-8'))
api={r['id']:r for r in records(OUT/'raw/api_trials.jsonl')}
assert len({r['text'] for r in data})>=200
assert all(r['id'] in api for r in data)
assert all(api[r['id']]['http_status']==200 for r in data)
checks={}
for file in ['browser_trials.jsonl','browser_diverse.jsonl']:
    trials=records(OUT/'raw'/file)
    for n in [1,5,10,13,20,30]:
        passing=[r for r in trials if r['kind']=='latency' and r['n']==n and r['success']]
        assert len({r['trial'] for r in passing})==20,(file,n)
        assert all(r['actual_clips']==n and r['movement_verified'] for r in passing)
    checks[file]=dict(attempts=len(trials),failed_attempts=sum(not r['success'] for r in trials))
consecutive=[r for r in records(OUT/'raw/browser_trials.jsonl') if r['kind']=='consecutive']
assert len(consecutive)==50 and all(r['completed'] and r['persistent_avatar'] for r in consecutive)
scales=records(OUT/'raw/scalability.jsonl');assert {r['requested_rows'] for r in scales}=={149,1000,5000,10000,25000,50000,100000}
assert all(t['actual_rows']==r['requested_rows'] for r in scales for t in r['trials'])
pngs=list((OUT/'graphs').glob('*.png'));assert len(pngs)>=11
for path in pngs:
    assert path.with_suffix('.json').exists()
    with Image.open(path) as image:assert image.width>=1000 and image.info.get('dpi',(0,0))[0]>=299
tokens=[r['token'] for r in credentials().values() if isinstance(r,dict) and 'token' in r]
for folder in [OUT,ROOT/'tests/research_evaluation']:
    for path in folder.rglob('*'):
        if path.is_file() and path.suffix in {'.json','.jsonl','.csv','.md','.txt','.xml'}:
            content=path.read_text(encoding='utf-8',errors='replace')
            assert all(token not in content for token in tokens),f'Credential found in {path}'
save(OUT/'processed/evidence_validation.json',dict(status='PASSED',announcements=len(data),unique_texts=len({r['text'] for r in data}),consecutive_complete=50,browser=checks,graphs=len(pngs),scalability_sizes=sorted(r['requested_rows'] for r in scales)))
index={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.rglob('*') if p.is_file() and '__pycache__' not in str(p)}
save(ROOT/'tests/research_evaluation/evidence_index.json',dict(description='Authoritative evidence files are linked by repository-relative path and SHA-256; no duplicate output copies.',files=index))
print('Evidence complete; chart files valid; no local credentials in report/evidence.')
