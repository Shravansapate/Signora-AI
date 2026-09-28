"""Recompute tables from raw evidence. Missing observations never become zero scores."""
import csv
import json
import re
from collections import Counter, defaultdict
from common import ROOT, OUT, records, save
from dataset import CLASSES
import numpy as np

def load(path):return json.loads(path.read_text(encoding='utf-8'))
def stats(values):
    x=np.array([v for v in values if v is not None and np.isfinite(v)],dtype=float)
    return dict(n=len(x),mean=float(x.mean()),median=float(np.median(x)),minimum=float(x.min()),maximum=float(x.max()),std=float(x.std(ddof=1)) if len(x)>1 else 0,p95=float(np.percentile(x,95))) if len(x) else None
def scores(tp,fp,fn):
    p=tp/(tp+fp) if tp+fp else 0;r=tp/(tp+fn) if tp+fn else 0
    return dict(tp=tp,fp=fp,fn=fn,precision=p,recall=r,f1=2*p*r/(p+r) if p+r else 0)
def table(name,rows):
    if not rows:return
    p=OUT/'tables'/f'{name}.csv';p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader();writer.writerows(rows)

SYNONYMS={'ARRIVAL':'ARRIVE','ARRIVES':'ARRIVE','ARRIVING':'ARRIVE','ARRIVED':'ARRIVE','DEPARTURE':'DEPART','DEPARTS':'DEPART','DEPARTING':'DEPART','DEPARTED':'DEPART','CANCELLED':'CANCEL','CANCELED':'CANCEL','DELAYED':'DELAY','MINUTES':'MINUTE','HOURS':'HOUR','BAGGAGE':'LUGGAGE','NOW RIGHT NOW':'NOW','DANGER HAZARD PERIL':'DANGER','RAILWAY TRACK':'TRACK','ANNOUNCE DECLARE RELEASE':'ANNOUNCE'}
def canonical(text):
    value=' '.join(str(text).upper().replace('_',' ').split())
    if re.fullmatch(r'\d+ [A-Z]+',value):value=value.split()[0]
    if value=='ZERO':value='0'
    return SYNONYMS.get(value,value)

def represented(matches,catalog):
    """Decode catalog labels, not the selector's assertion that something matched."""
    groups=defaultdict(list);direct=[]
    for step in matches:
        if step.get('motion_version_id') not in catalog:continue
        c=catalog[step['motion_version_id']];label=canonical(c['canonical_text'])
        if step['method'] in {'ALPHABET','DIGIT'}:groups[(step.get('gloss_token'),step.get('role'))].append(label)
        else:direct.append(label)
    direct.extend(canonical(''.join(chars)) for chars in groups.values())
    return Counter(direct)

def main():
    data=load(ROOT/'tests/research_evaluation/announcements.json');raw={r['id']:r for r in records(OUT/'raw/api_trials.jsonl')}
    catalog={r['motion_version_id']:r for r in load(OUT/'raw/catalog.json')}
    cm=np.zeros((len(CLASSES),len(CLASSES)+1),dtype=int);per=[];entities=defaultdict(lambda:Counter(tp=0,fp=0,fn=0,exact=0,n=0));failures=[]
    unit_total=unit_found=spelled=represented_count=0;levels=Counter();unnecessary=[];gloss=[]
    intent={'TRAIN_ARRIVAL':'Arrival','TRAIN_DEPARTURE':'Departure','TRAIN_DELAY':'Delay','TRAIN_CANCELLATION':'Cancellation','PLATFORM_CHANGE':'Platform change'}
    fields={'train_identifier':'train_number','platform_identifier':'platform','train_name':'train_name','source':'source','destination':'destination','delay_duration':'delay_duration','old_platform':'old_platform','new_platform':'new_platform'}
    for row in data:
        r=raw.get(row['id'],{});b=r.get('response',{});meaning=b.get('meaning') or {};pred=intent.get(meaning.get('intent'),'ABSTAIN')
        cm[CLASSES.index(row['category']),CLASSES.index(pred) if pred in CLASSES else len(CLASSES)]+=1
        per.append(dict(id=row['id'],text=row['text'],expected=row['category'],predicted=pred,correct=pred==row['category'],http_status=r.get('http_status')))
        actual={fields[k]:' '.join(v['raw'].lower().split()) for k,v in meaning.get('slots',{}).items() if k in fields}
        if 'clock_time' in meaning.get('slots',{}):actual['arrival_time' if pred=='Arrival' else 'departure_time']=' '.join(meaning['slots']['clock_time']['raw'].lower().split())
        partial=(b.get('retrieval') or {}).get('partial_semantics') or {}
        for key,target in {'train_identifier':'train_number','platform_identifier':'platform','source_station':'source','destination_station':'destination','delay_duration':'delay_duration'}.items():
            values=partial.get('slots',{}).get(key,[])
            if target not in actual and len(values)==1:actual[target]=' '.join(values[0].lower().split())
        clock=partial.get('slots',{}).get('clock_time',[])
        events=partial.get('events',[])
        if len(clock)==1 and len(set(events))==1 and events[0] in {'ARRIVE','DEPART'}:actual['arrival_time' if events[0]=='ARRIVE' else 'departure_time']=' '.join(clock[0].lower().split())
        for key in set(row['entities'])|set(actual):
            expected=row['entities'].get(key);observed=actual.get(key);count=entities[key];count['n']+=1
            if expected==observed:count['tp']+=1;count['exact']+=1
            else:
                if expected is not None:count['fn']+=1
                if observed is not None:count['fp']+=1
                failures.append(dict(id=row['id'],text=row['text'],field=key,expected=expected,actual=observed))
        retrieval=b.get('retrieval') or {};matches=retrieval.get('matches',[]);decoded=represented(matches,catalog);required=Counter(canonical(u) for u in row['required_units']);found=sum((required&decoded).values())
        unit_total+=sum(required.values());unit_found+=found
        spelled_groups={(s.get('gloss_token'),s.get('role')) for s in matches if s['method']=='ALPHABET'}
        spelled+=len(spelled_groups);represented_count+=sum(decoded.values())
        for token,role in spelled_groups:
            if any(canonical(c['canonical_text'])==canonical(token) for c in catalog.values()):unnecessary.append(dict(id=row['id'],token=token,role=role))
        for s in matches:
            if 'motion_version_id' not in s:continue
            m=s['method'];c=catalog.get(s['motion_version_id'],{});numeric=canonical(c.get('canonical_text','')).isdigit()
            level='Fingerspelling' if m=='ALPHABET' else 'Number' if m=='DIGIT' or numeric else 'Phrase' if m=='PHRASE' else 'Word'
            levels[level]+=1
        gloss.append(dict(id=row['id'],input=row['text'],normalized=b.get('normalized_text'),meaning=meaning,gloss=retrieval.get('gloss_tokens'),required_units=row['required_units'],decoded_units=dict(decoded),represented_required=found,total_required=sum(required.values()),fingerspelled=retrieval.get('fingerspelled'),matches=matches))
    classification=[]
    for i,label in enumerate(CLASSES):
        tp=int(cm[i,i]);fn=int(cm[i,:].sum()-tp);fp=int(cm[:,i].sum()-tp)
        classification.append(dict(category=label,support=int(cm[i,:].sum()),**scores(tp,fp,fn)))
    entity_rows=[dict(entity=k,**scores(v['tp'],v['fp'],v['fn']),exact_match=v['exact']/v['n'],evaluated_fields=v['n']) for k,v in entities.items()]
    micro=scores(sum(x['tp'] for x in entity_rows),sum(x['fp'] for x in entity_rows),sum(x['fn'] for x in entity_rows))
    probes=load(OUT/'raw/retrieval_ablations.json');ablation=[];probe_results=[]
    for mode in dict.fromkeys(x['mode'] for x in probes):
        rows=[x for x in probes if x['mode']==mode];correct=covered=0
        for x in rows:
            decoded=represented(x['matches'],catalog);ok=(not decoded) if x['expected'] is None else canonical(x['expected']) in decoded
            correct+=ok;covered+=ok and x['expected'] is not None
            probe_results.append(dict(id=x['id'],mode=mode,query=x['query'],expected=x['expected'],decoded=dict(decoded),correct=ok,clips=x['clip_count']))
        ablation.append(dict(mode=mode,queries=len(rows),correct=correct,retrieval_accuracy=correct/len(rows),coverage=covered/sum(x['expected'] is not None for x in rows),fingerspelling_rate=sum(any(m['method']=='ALPHABET' for m in x['matches']) for x in rows)/max(1,sum(x['clip_count']>0 for x in rows)),mean_clips=float(np.mean([x['clip_count'] for x in rows])),selector_ms=stats([x['selector_ms'] for x in rows]),scope='evaluation-only spelling-disabled selector and restricted catalog; not deployed modes; no sentence assets; at expects omission and is excluded from coverage denominator'))
    guard=load(OUT/'raw/safety_guard.json');tp=sum(r['expected_conflict'] and r['detected'] for r in guard);fn=sum(r['expected_conflict'] and not r['detected'] for r in guard);fp=sum(not r['expected_conflict'] and r['detected'] for r in guard);tn=sum(not r['expected_conflict'] and not r['detected'] for r in guard)
    safety=dict(**scores(tp,fp,fn),tn=tn,false_negative_rate=fn/(tp+fn),scope='candidate-text guard only; no integrated trusted timetable validation')
    browser=records(OUT/'raw/browser_trials.jsonl');latency=[]
    for n in [1,5,10,13,20,30]:
        trials=[r for r in browser if r['kind']=='latency' and r['n']==n];success=[r for r in trials if r['success']]
        latency.append(dict(clips=n,attempts=len(trials),successful=len(success),ttfs_ms=stats([r.get('ttfs_ms') for r in success]),preparation_ms=stats([r.get('prepared_ms') for r in success])))
    consecutive=[r for r in browser if r['kind']=='consecutive'];frames=[f for r in browser if r.get('success') for f in r['measurement']['frames']]
    frame=stats(frames);fps=1000/np.mean(frames) if frames else None
    api=stats([r['api_ms'] for r in raw.values() if r.get('http_status')==200]);full=next(x for x in ablation if x['mode']=='Full hierarchy')
    summary=dict(dataset=dict(cases=len(data),unique_texts=len(set(x['text'] for x in data)),families=len(set(x['family'] for x in data)),distribution=dict(Counter(x['category'] for x in data))),classification=dict(accuracy=float(np.trace(cm[:,:7])/len(data)),macro_f1=float(np.mean([x['f1'] for x in classification])),weighted_f1=sum(x['f1']*x['support'] for x in classification)/len(data),per_class=classification,confusion_matrix=cm.tolist(),predicted_classes=CLASSES+['ABSTAIN']),entities=dict(micro=micro,per_field=entity_rows),safety_integrated=None,safety_guard=safety,retrieval=dict(probe_accuracy=full['retrieval_accuracy'],probe_count=full['queries'],recall_at_1=None,recall_at_3=None,recall_at_5=None,lexical_coverage=unit_found/unit_total,required_units=unit_total,represented_required=unit_found,fingerspelling_rate=spelled/max(1,represented_count),spelled_units=spelled,represented_units=represented_count,unnecessary_fingerspelling=len(unnecessary),clip_level_counts=dict(levels)),ablation=ablation,api_latency_ms=api,browser_latency=latency,consecutive=dict(attempts=len(consecutive),completed=sum(r.get('completed') is True for r in consecutive),errors=[dict(id=r['id'],error=r.get('error'),page_errors=r.get('errors')) for r in browser if not r.get('success') or r.get('errors')]),avatar=dict(mean_fps=float(fps) if fps else None,min_instantaneous_fps=1000/max(frames) if frames else None,frame_ms=frame,estimated_missed_60hz_opportunities=sum(max(0,round(f/16.6667)-1) for f in frames),fps_scope='rAF intervals during PLAYING, headless/software-capable browser; not GPU hardware counter'),missing={'safety_integrated':'No trusted operational timetable comparison service','recall_at_k':'Preview returns a sequence, not ranked alternatives; separate reviewed candidate API has no eligible indexed population','transition_optimization':'No optimizer implemented','sentence_ablation':'No sentence-level catalog content','expert_quality':'Requires human participant evaluation.'})
    save(OUT/'processed/summary.json',summary);save(OUT/'processed/gloss_traces.json',gloss);save(OUT/'processed/probe_scores.json',probe_results);save(OUT/'processed/unnecessary_fingerspelling.json',unnecessary)
    save(OUT/'processed/false_negatives_guard.json',[r for r in guard if r['expected_conflict'] and not r['detected']]);save(OUT/'processed/entity_failures.json',failures)
    table('classification_cases',per);table('classification_metrics',classification);table('entity_metrics',entity_rows);table('entity_failures',failures)
    table('latency_summary',[dict(clips=r['clips'],attempts=r['attempts'],successful=r['successful'],**{f'{kind}_{k}':v for kind in ['ttfs_ms','preparation_ms'] for k,v in (r[kind] or {}).items()}) for r in latency])
    table('robustness',[dict(id=r['id'],http_status=r.get('http_status'),status=r.get('response',{}).get('status'),clips=len((r.get('response',{}).get('manifest') or {}).get('items',[])),error=r.get('error')) for r in raw.values() if r['id'] not in {x['id'] for x in data}])
    print(json.dumps({k:summary[k] for k in ['dataset','entities','retrieval','consecutive']},indent=2))

if __name__=='__main__':main()
