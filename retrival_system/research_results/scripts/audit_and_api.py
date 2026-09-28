"""Read-only catalog audit and actual private-preview HTTP trials; no publication."""
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import time
import uuid
from collections import Counter
from common import ROOT, OUT, append, client, records, save, settings

def audit():
    from sqlalchemy import create_engine, text
    cfg=settings(); engine=create_engine(cfg.database_url.get_secret_value())
    with engine.connect() as c:
        catalog=[dict(x) for x in c.execute(text('''SELECT c.id AS concept_id,c.semantic_key,c.canonical_text,c.gloss,c.level,c.enabled,
            m.id AS motion_version_id,m.version_no,m.storage_key,m.sha256,m.size_bytes,m.duration_seconds,
            m.technical_qc_status,m.linguistic_review_status,m.composition_review_status,m.lifecycle_status,m.deleted_at,m.revoked_at,m.avatar_profile_id
            FROM sign_concepts c JOIN motion_versions m ON m.concept_id=c.id ORDER BY c.semantic_key,m.version_no''')).mappings()]
        counts={table:c.scalar(text(f'SELECT count(*) FROM {table}')) for table in ['sign_concepts','motion_versions','sign_aliases','announcement_templates','eligible_motion_versions','sign_embeddings','retrieval_profiles']}
        db=dict(version=c.scalar(text('SELECT version()')),size_bytes=c.scalar(text('SELECT pg_database_size(current_database())')),schema=c.scalar(text('SELECT version_num FROM alembic_version')))
        aliases=[dict(x) for x in c.execute(text('SELECT concept_id,alias,review_status FROM sign_aliases')).mappings()]
    for r in catalog:
        p=cfg.storage_root / r['storage_key'];r['file_exists']=p.is_file();r['actual_size']=p.stat().st_size if p.is_file() else None
    save(OUT/'raw/catalog.json',catalog);save(OUT/'raw/aliases.json',aliases)
    files={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ['backend/app','frontend/src','backend/alembic','backend/tests','frontend/tests'] for p in (ROOT/folder).rglob('*') if p.is_file() and p.suffix in {'.py','.jsx','.mjs','.js','.json','.css'} and '__pycache__' not in str(p)}
    save(OUT/'raw/source_hashes_before.json',files)
    hardware=subprocess.run([r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe','-NoProfile','-Command',"@{cpu=(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors);gpu=(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM);ram=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory;os=(Get-CimInstance Win32_OperatingSystem).Caption} | ConvertTo-Json -Depth 4"],capture_output=True,text=True).stdout
    with client() as api: caps=api.get('/api/v1/input/capabilities').json();health=api.get('/health/ready').json()
    env=dict(timestamp=time.strftime('%Y-%m-%dT%H:%M:%S%z'),platform=platform.platform(),python=platform.python_version(),hardware=json.loads(hardware),database=db,counts=counts,glb_files=sum(r['file_exists'] for r in catalog),unique_glb_objects=len(set(r['sha256'] for r in catalog)),capabilities=caps,health=health,
        git_head=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True).stdout.strip(),git_status=subprocess.run(['git','status','--short'],cwd=ROOT,capture_output=True,text=True).stdout,
        dependencies={d.metadata['Name']:d.version for d in importlib.metadata.distributions()},frontend_package=json.loads((ROOT/'frontend/package.json').read_text(encoding='utf-8')))
    save(OUT/'raw/environment.json',env)
    print('Audit counts:',counts,flush=True)

def run_api():
    path=OUT/'raw/api_trials.jsonl';done={r['id'] for r in records(path)}
    rows=json.loads((ROOT/'tests/research_evaluation/announcements.json').read_text(encoding='utf-8'))
    rows += [dict(r,category='ROBUSTNESS') for r in json.loads((ROOT/'tests/research_evaluation/robustness.json').read_text(encoding='utf-8'))]
    with client() as api:
        for row in rows:
            if row['id'] in done:continue
            body=dict(request_id=str(uuid.uuid4()),station_id='NAGPUR',input_type='TEXT',text=row['text'],service_date='2026-09-27')
            t=time.perf_counter()
            try:
                response=api.post('/api/v1/translate',json=body)
                result=dict(id=row['id'],request=body,http_status=response.status_code,response=response.json(),api_ms=(time.perf_counter()-t)*1000)
            except Exception as exc:
                result=dict(id=row['id'],request=body,error=type(exc).__name__+': '+str(exc),api_ms=(time.perf_counter()-t)*1000)
            append(path,result)
            print(row['id'],result.get('http_status'),round(result['api_ms']),flush=True)

if __name__=='__main__':
    if not (OUT/'raw/environment.json').exists():audit()
    run_api()
