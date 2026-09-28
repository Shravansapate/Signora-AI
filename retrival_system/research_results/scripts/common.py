"""Evaluation helpers. Credentials are read locally and never written to evidence."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'research_results'
sys.path.insert(0, str(ROOT / 'artifacts/research-python'))
sys.path.insert(0, str(ROOT / 'backend'))

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding='utf-8')

def credentials():
    return json.JSONDecoder().raw_decode(Path(os.environ.get('SIGNORA_EVAL_CREDENTIALS', r'D:\SignoraData\local-credentials.json')).read_text(encoding='utf-8-sig').lstrip())[0]

def settings():
    from app.config import Settings
    return Settings(_env_file=ROOT / 'backend/.env')

def client():
    import httpx
    return httpx.Client(base_url=os.environ.get('SIGNORA_EVAL_API', 'http://127.0.0.1:8000'), headers={'Authorization': 'Bearer '+credentials()['operator']['token']}, timeout=180)

def append(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, default=str)+'\n')

def records(path):
    path = Path(path)
    return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()] if path.exists() else []
