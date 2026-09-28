"""Isolated component measurements; never confuse these with full API performance."""
import ast
import inspect
import json
import time
from datetime import date
from collections import defaultdict
from types import SimpleNamespace
from uuid import UUID
from common import ROOT, OUT, save, settings
from app import meaning
from app.announcement_schema import StationDefinition
from app.demo import Candidate, _select, _terms
from app import demo
from app.gloss import GlossUnit, construct_gloss, recover_lexical
from app.retrieval.search import compatible_text

def catalog_candidates():
    rows=json.loads((OUT/'raw/catalog.json').read_text(encoding='utf-8'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    expression=ast.parse(inspect.getsource(demo.prepare_demo)).body[0].body[0]
    with Session(create_engine(settings().database_url.get_secret_value())) as session:
        local={'session':session}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[expression],type_ignores=[])),'<actual-catalog-selection>','exec'),demo.__dict__,local)
        available=local['rows']
        profiles=defaultdict(list)
        for motion,concept,profile in available:profiles[str(profile.id)].append(str(motion.id))
        profile,ids=sorted(profiles.items(),key=lambda x:(-len(x[1]),x[0]))[0]
        available_ids=set(ids)
    save(OUT/'raw/component_catalog_scope.json',dict(avatar_profile_id=profile,motion_ids=ids,selection='Actual prepare_demo catalog query, largest compatible avatar group'))
    aliases=defaultdict(set)
    for a in json.loads((OUT/'raw/aliases.json').read_text(encoding='utf-8')):aliases[a['concept_id']].add(a['alias'].lower())
    seen=set();candidates=[]
    for row in rows:
        if row['concept_id'] in seen or row['motion_version_id'] not in available_ids:continue
        seen.add(row['concept_id'])
        candidates.append(Candidate(UUID(row['motion_version_id']),UUID(row['concept_id']),row['semantic_key'],_terms(SimpleNamespace(**row)),frozenset(aliases[row['concept_id']]),row['duration_seconds']))
    return candidates

def main():
    cfg=json.loads((OUT/'raw/environment.json').read_text(encoding='utf-8'))['capabilities']['stations'][0]['definition']
    station=StationDefinition.model_validate(cfg)
    data=json.loads((ROOT/'tests/research_evaluation/announcements.json').read_text(encoding='utf-8'))
    timings=[]
    # Instrument only this evaluation Python process. Restore original functions below.
    original_normalize, original_slot, original_domain=meaning.normalize,meaning._slot,meaning.normalize_domain
    measured=defaultdict(float)
    def wrap(fn,key):
        def run(*a,**kw):
            t=time.perf_counter_ns()
            try:return fn(*a,**kw)
            finally:measured[key]+=(time.perf_counter_ns()-t)/1e6
        return run
    meaning.normalize=wrap(original_normalize,'normalization_inclusive_ms')
    meaning.normalize_domain=wrap(original_domain,'domain_normalization_ms')
    meaning._slot=wrap(original_slot,'entity_extraction_and_validation_inclusive_ms')
    candidates=catalog_candidates()
    try:
        for row in data:
            measured.clear();t=time.perf_counter_ns()
            normalized, parsed, issues=meaning.parse_meaning(row['text'],'NAGPUR',station,date(2026,9,27),development=True)
            parse_ms=(time.perf_counter_ns()-t)/1e6
            t=time.perf_counter_ns();units=construct_gloss(parsed,station.development_gloss) if parsed else recover_lexical(normalized)[0]
            gloss_ms=(time.perf_counter_ns()-t)/1e6
            trace=[];t=time.perf_counter_ns();_select(candidates,units,trace);lookup_ms=(time.perf_counter_ns()-t)/1e6
            timings.append(dict(id=row['id'],parse_combined_ms=parse_ms,gloss_ms=gloss_ms,lookup_cpu_ms=lookup_ms,**measured))
    finally:meaning.normalize,meaning._slot,meaning.normalize_domain=original_normalize,original_slot,original_domain
    save(OUT/'raw/component_timings.json',timings)
    conflicts=json.loads((ROOT/'tests/research_evaluation/safety_conflicts.json').read_text(encoding='utf-8'))
    safety=[]
    for r in conflicts:
        t=time.perf_counter_ns();detected=not compatible_text(r['announcement'],r['trusted_statement'])
        safety.append(dict(r,detected=detected,guard_ms=(time.perf_counter_ns()-t)/1e6,scope='candidate compatibility guard, not integrated trusted timetable validation'))
    save(OUT/'raw/safety_guard.json',safety)
    # Intended labels authored independently of selector outputs. Case variants are not new semantic concepts.
    probes=[('train','train'),('arrive','arrive'),('arrival','arrive'),('arriving','arrive'),('departure','depart'),('delayed','delay'),('cancelled','cancel'),('platform','platform'),('ticket','ticket'),('baggage','luggage'),('exit','exit'),('emergency','emergency'),('fire','fire'),('help','help'),('station','station'),('water','water'),('local train','local train'),('express train','express train'),('waiting room','waiting room'),('new delhi','new delhi'),('identity card','identity card'),('time table','time table'),('1201','1201'),('00120','00120'),('22120','22120'),('2','2'),('10','10'),('30','30'),('xylophonex','xylophonex'),('quizzleville','quizzleville'),('evacuate','evacuate'),('not','not'),('future','future'),('at','at'),('railway track','railway track')]
    labels=[dict(id=f'probe-{i}',query=q,expected=None if q=='at' else target,kind='IDENTIFIER' if q in {'1201','00120','22120'} else 'NUMBER' if q.isdigit() else 'LEXICAL') for i,(q,target) in enumerate(probes)]
    save(ROOT/'tests/research_evaluation/retrieval_ground_truth.json',labels)
    # Restrict the real algorithm only in this harness: disable the nested spelling
    # helper for A/B/C. Preserve the production function and its runtime module.
    tree=ast.parse(inspect.getsource(_select))
    nested=next(n for n in tree.body[0].body if isinstance(n,ast.FunctionDef) and n.name=='spell')
    nested.body=[ast.Return(value=ast.Constant(value=None))]
    namespace=dict(demo.__dict__)
    exec(compile(ast.fix_missing_locations(tree),'<evaluation-no-spelling>','exec'),namespace)
    no_spell=namespace['_select']
    selected=[]
    for row in labels:
        for mode in ['Word only','Phrase + Word','Sentence + Phrase + Word','Full hierarchy']:
            pool=candidates if mode!='Word only' else [c for c in candidates if all(' ' not in t for t in c.terms)]
            units=[GlossUnit(row['query'].upper(),row['kind'],'probe')] if row['kind'] in {'IDENTIFIER','NUMBER'} else recover_lexical(row['query'])[0]
            if mode!='Full hierarchy' and row['kind'] in {'IDENTIFIER','NUMBER'}:units=[]
            selector=_select if mode=='Full hierarchy' else no_spell
            trace=[];t=time.perf_counter_ns();motions,_,_,missing=selector(pool,units,trace);elapsed=(time.perf_counter_ns()-t)/1e6
            selected.append(dict(row,mode=mode,matches=trace,clip_count=len(motions),missing=missing,selector_ms=elapsed,latency_scope='evaluation-only spelling-disabled selector and candidate restriction; production function unchanged'))
    save(OUT/'raw/retrieval_ablations.json',selected)
    # Genuine phrase->word->alphabet fallback with existing motions; no invented GLB.
    fallbacks=[]
    for name,query,excluded in [('phrase_present','local train',set()),('phrase_removed','local train',{'ISL_LOCAL_TRAIN_01'}),('word_present','train',set()),('word_removed','train',{'ISL_TRAIN_01'}),('number','1201',set())]:
        trace=[];units=[GlossUnit(query.upper(),'IDENTIFIER' if query.isdigit() else 'LEXICAL','probe')]
        pool=[c for c in candidates if c.semantic_key not in excluded]
        # lexical recovery tokenizes words before longest phrase matching
        if not query.isdigit():units=recover_lexical(query)[0]
        motions,_,_,missing=_select(pool,units,trace)
        fallbacks.append(dict(name=name,input=query,excluded=sorted(excluded),matches=trace,clip_count=len(motions),missing=missing))
    save(OUT/'raw/fallbacks.json',fallbacks)
    print('Saved component timings, actual conflict-guard tests, labelled retrieval probes, ablations and fallback experiments.')

if __name__=='__main__':main()
