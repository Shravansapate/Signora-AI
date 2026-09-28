"""Actual development catalog SELECT on PostgreSQL temporary synthetic tables.

No GLBs are duplicated and no persistent catalog rows are inserted. SQL is taken
from the current prepare_demo function's first assignment, not a substitute query.
"""
import ast
import gc
import inspect
import json
import math
import os
import time
from pathlib import Path
from common import OUT, append, records, settings
import psutil
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app import demo
from app.gloss import GlossUnit

def main():
    # Runtime role intentionally cannot create temp tables. Use the already
    # provisioned local maintenance credential for connection-private tables only.
    admin=json.loads(Path(os.environ.get('SIGNORA_EVAL_DB_ADMIN',r'D:\SignoraData\local-admin.json')).read_text(encoding='utf-8-sig'))
    engine=create_engine(admin['database_url'])
    assignment=ast.parse(inspect.getsource(demo.prepare_demo)).body[0].body[0]
    code=compile(ast.fix_missing_locations(ast.Module(body=[assignment],type_ignores=[])),'<actual-prepare-demo-query>','exec')
    path=OUT/'raw/scalability.jsonl';done={r['requested_rows'] for r in records(path)}
    process=psutil.Process()
    for n in [149,1000,5000,10000,25000,50000,100000]:
        if n in done:continue
        with engine.connect() as con:
            transaction=con.begin()
            try:
                seed=con.scalar(text('SELECT count(*) FROM public.sign_concepts'))
                if n!=149:
                    copies=math.ceil(n/seed)
                    con.execute(text('CREATE TEMP TABLE avatar_profiles ON COMMIT DROP AS SELECT * FROM public.avatar_profiles'))
                    con.execute(text('CREATE TEMP TABLE library_selections ON COMMIT DROP AS SELECT * FROM public.library_selections WHERE false'))
                    con.execute(text('''CREATE TEMP TABLE sign_concepts ON COMMIT DROP AS
                    SELECT (jsonb_populate_record(NULL::public.sign_concepts,to_jsonb(c)||jsonb_build_object(
                    'id',md5(c.id::text||'-'||g)::uuid,'semantic_key',c.semantic_key||'_'||g,'active_motion_version_id',NULL))).*
                    FROM public.sign_concepts c CROSS JOIN generate_series(1,:copies) g ORDER BY g,c.id LIMIT :n'''),dict(copies=copies,n=n))
                    con.execute(text('''CREATE TEMP TABLE motion_versions ON COMMIT DROP AS
                    WITH seed AS MATERIALIZED (SELECT id,concept_id,to_jsonb(m)-'source_metadata'-'technical_report' AS payload FROM public.motion_versions m)
                    SELECT (jsonb_populate_record(NULL::public.motion_versions,m.payload||jsonb_build_object(
                    'id',md5(m.id::text||'-'||g)::uuid,'concept_id',md5(m.concept_id::text||'-'||g)::uuid,
                    'source_metadata','{}'::jsonb,'technical_report','{}'::jsonb))).*
                    FROM seed m CROSS JOIN generate_series(1,:copies) g ORDER BY g,m.concept_id LIMIT :n'''),dict(copies=copies,n=n))
                    con.execute(text('CREATE INDEX ON motion_versions(concept_id)'))
                    con.execute(text('CREATE INDEX ON sign_concepts(id)'))
                    con.execute(text('ANALYZE sign_concepts'));con.execute(text('ANALYZE motion_versions'))
                trials=[]
                for rep in range(5):
                    gc.collect();before=process.memory_info().rss
                    with Session(bind=con) as session:
                        local={'session':session};t=time.perf_counter()
                        exec(code,demo.__dict__,local);rows=local['rows'];sql_ms=(time.perf_counter()-t)*1000
                        t=time.perf_counter()
                        candidates=[demo.Candidate(m.id,c.id,c.semantic_key,demo._terms(c),duration=m.duration_seconds) for m,c,p in rows]
                        build_ms=(time.perf_counter()-t)*1000
                        t=time.perf_counter()
                        motions=demo._select(candidates,[GlossUnit('TRAIN'),GlossUnit('1201','IDENTIFIER'),GlossUnit('PLATFORM'),GlossUnit('2','PLATFORM_IDENTIFIER'),GlossUnit('ARRIVE')])[0]
                        retrieval_ms=(time.perf_counter()-t)*1000
                        trials.append(dict(repetition=rep,actual_rows=len(rows),sql_materialization_ms=sql_ms,candidate_build_ms=build_ms,selector_ms=retrieval_ms,combined_ms=sql_ms+build_ms+retrieval_ms,clips=len(motions),rss_before_bytes=before,rss_loaded_bytes=process.memory_info().rss))
                    del rows,candidates,local,motions
                append(path,dict(requested_rows=n,synthetic=n!=149,seed_rows=seed,trials=trials,api_ms=None,notes='Actual development SELECT + ORM materialization + term construction + selection. Synthetic repeated vocabulary and shared GLB paths; no GLB load, aliases, HTTP or production vector index included.'))
                print(n,'rows',round(sum(t['combined_ms'] for t in trials)/len(trials),2),'ms',flush=True)
            finally:transaction.rollback()

if __name__=='__main__':main()
