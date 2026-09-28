"""Publication-size plots, generated exclusively from saved observations (300 DPI)."""
import json
from common import OUT, records, save
from analyse import load, stats
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.rcParams.update({'font.size':10,'axes.titlesize':12,'axes.labelsize':10,'figure.dpi':120,'savefig.dpi':300,'axes.spines.top':False,'axes.spines.right':False})

def finish(name,fig,data):
    path=OUT/'graphs';path.mkdir(parents=True,exist_ok=True)
    fig.tight_layout();fig.savefig(path/f'{name}.png',bbox_inches='tight');plt.close(fig)
    save(path/f'{name}.json',data)

def bars(name,title,labels,values,subtitle='',ylabel='Performance (%)',ylim=100):
    fig,ax=plt.subplots(figsize=(8,4.6));x=np.arange(len(labels))
    for i,value in enumerate(values):
        if value is None:
            ax.text(i,ylim*.12,'N/A',ha='center',color='#777777',fontweight='bold')
            ax.plot([i-.25,i+.25],[0,0],color='#777777',ls='--')
        else:
            ax.bar(i,value,color='#20665a',width=.62);ax.text(i,min(ylim*.96,value+ylim*.025),f'{value:.1f}',ha='center',fontsize=9)
    ax.set_xticks(x,labels);ax.set_ylim(0,ylim);ax.set_ylabel(ylabel);ax.set_title(title,pad=28);ax.text(.5,1.025,subtitle,ha='center',transform=ax.transAxes,fontsize=8);ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    finish(name,fig,dict(labels=labels,values=values,subtitle=subtitle,ylabel=ylabel))

def main():
    s=load(OUT/'processed/summary.json');pct=lambda x:100*x if x is not None else None
    bars('core_system_performance','Signor AI Core System Performance',['Template\naccuracy','Entity\nF1','Integrated\nsafety F1','Retrieval\nidentity accuracy','Lexical\ncoverage'],[pct(s['classification']['accuracy']),pct(s['entities']['micro']['f1']),None,pct(s['retrieval']['probe_accuracy']),pct(s['retrieval']['lexical_coverage'])],'Synthetic English cases; retrieval probes are a separate catalog-identity task')
    bars('retrieval_performance','Hierarchical Motion Retrieval Performance',['Recall@1','Recall@3','Recall@5','Retrieval\nidentity accuracy','Lexical\ncoverage'],[None,None,None,pct(s['retrieval']['probe_accuracy']),pct(s['retrieval']['lexical_coverage'])],'Ranked retrieval unavailable for current preview/catalog; N/A is not zero')
    bars('performance_by_announcement_type','Performance by Railway Announcement Type',[r['category'].replace(' ','\n') for r in s['classification']['per_class']],[pct(r['f1']) for r in s['classification']['per_class']],'Per-class F1; unparsed messages count as abstentions / false negatives')
    rows=[r for r in s['browser_latency'] if r['ttfs_ms']]
    fig,ax=plt.subplots(figsize=(8,4.6))
    for key,label in [('preparation_ms','Submission to prepared avatar'),('ttfs_ms','Time to first observed sign')]:
        ax.errorbar([r['clips'] for r in rows],[r[key]['mean']/1000 for r in rows],yerr=[r[key]['std']/1000 for r in rows],marker='o',capsize=3,label=label)
    ax.set(xlabel='Number of motion clips',ylabel='Processing time (seconds)',title='Processing Latency vs Motion Sequence Length',xticks=[1,5,10,13,20,30]);ax.grid(alpha=.2);ax.legend();fig.text(.5,.005,'Repeated TRAIN, one unique motion; persistent warm session; first cold trial retained. Error bars: SD.',ha='center',fontsize=8)
    finish('latency_vs_clips',fig,rows)
    diverse_file=OUT/'processed/diverse_latency.json'
    if diverse_file.exists():
        groups=load(diverse_file);fig,ax=plt.subplots(figsize=(8,4.6))
        for key,label in [('all_preparation_ms','Preparation'),('ttfs_ms','First observed sign')]:
            valid=[r for r in groups if r[key]]
            ax.errorbar([r['clips'] for r in valid],[r[key]['mean']/1000 for r in valid],yerr=[r[key]['std']/1000 for r in valid],marker='o',capsize=3,label=label)
        ax.set(xlabel='Distinct motion clips',ylabel='Time (seconds)',title='Distinct-Motion Loading Sensitivity',xticks=[1,5,10,13,20,30]);ax.set_ylim(bottom=0);ax.legend();ax.grid(alpha=.2)
        fig.text(.5,.005,'First use of new motions retained; subsequent trials use the animation cache. Error bars: SD.',ha='center',fontsize=8)
        finish('latency_distinct_motions',fig,groups)
    t=load(OUT/'raw/component_timings.json');mean=lambda key:float(np.mean([x[key] for x in t]))
    # Combined measurements remain combined; do not invent isolated stage durations.
    bars('pipeline_latency_breakdown','Signor AI Processing-Time Breakdown',['Normalize\n(inclusive)','Parse+entities\n+validation','Gloss','Selector\nCPU','Safety\nservice','GLB/stitch/\nrender split'],[mean('normalization_inclusive_ms')+mean('domain_normalization_ms'),mean('parse_combined_ms'),mean('gloss_ms'),mean('lookup_cpu_ms'),None,None],'Component timings overlap; not additive. Browser preparation measured separately.','Time (milliseconds)',max(1,mean('parse_combined_ms')*1.4,mean('lookup_cpu_ms')*1.4))
    profiles=records(OUT/'raw/backend_profile.jsonl')
    if profiles:
        names=['normalize_inclusive_ms','parse_combined_ms','gloss_ms','selector_ms','sql_execute_ms','asset_integrity_ms','manifest_persist_ms','backend_total_ms']
        values=[float(np.mean([r.get(k,0) for r in profiles])) for k in names]
        fig,ax=plt.subplots(figsize=(9,4.6));ax.bar(range(len(names)),values,color='#20665a');ax.set_yscale('log');ax.set_xticks(range(len(names)),['Normalize','Parse +\nentities','Gloss','Selector','SQL\nexecute','GLB disk\nintegrity','Manifest\npersist','Backend\ntotal'],fontsize=8);ax.set(ylabel='Time (ms, logarithmic)',title='Actual Backend Timing: Eight-Clip Announcement')
        fig.text(.5,.005,'20 rolled-back real translations; inclusive scopes overlap and must not be summed.',ha='center',fontsize=8)
        finish('backend_latency_profile',fig,dict(names=names,mean_ms=values,trials=profiles))
    fig,ax=plt.subplots(figsize=(8,4.8));abl=s['ablation'];labels=[r['mode'].replace(' + ','+\n') for r in abl]+['Full + transition\noptimization'];x=np.arange(len(labels));w=.36
    for j,(key,label) in enumerate([('retrieval_accuracy','Catalog-identity accuracy'),('coverage','Expected-unit coverage')]):
        ax.bar(x[:-1]+(j-.5)*w,[pct(r[key]) for r in abl],width=w,label=label,color=['#20665a','#d99b37'][j])
    ax.text(x[-1],12,'N/A',ha='center');ax.set_xticks(x,labels,fontsize=8);ax.set_ylim(0,100);ax.set_ylabel('Performance (%)');ax.set_title('Effect of Hierarchical Retrieval on System Performance');ax.legend(fontsize=8);fig.text(.5,.005,'Evaluation-only restricted retrieval; no sentence assets or transition optimizer. Not ISL quality.',ha='center',fontsize=8)
    finish('ablation_study',fig,abl)
    levels=s['retrieval']['clip_level_counts'];total=sum(levels.values());names=['Sentence','Phrase','Word','Number','Fingerspelling']
    bars('retrieval_level_distribution','Distribution of Motion Retrieval Levels',names,[100*levels.get(n,0)/total for n in names],f'Output clips across {s["dataset"]["cases"]} inputs (n={total}); a spelled letter is one clip','Output clips (%)')
    transition_file=OUT/'raw/transition_endpoints.json';trans=load(transition_file) if transition_file.exists() else {'pairs':[]}
    pairs=trans['pairs'];angle=sum(p['mean_joint_angle_degrees']*p['occurrences'] for p in pairs)/sum(p['occurrences'] for p in pairs) if pairs else None
    bars('transition_quality','Effect of Transition-Aware Motion Selection',['Existing full-clip cuts','Transition-aware selection'],[angle,None],'Source-GLB endpoint rotation discontinuity; optimizer absent, improvement not measurable','Mean joint angle jump (degrees)',max(10,(angle or 0)*1.5))
    scale=records(OUT/'raw/scalability.jsonl');fig,ax=plt.subplots(figsize=(8,4.6))
    if scale:
        scale.sort(key=lambda r:r['requested_rows']);ax.plot([r['requested_rows'] for r in scale],[np.mean([x['combined_ms'] for x in r['trials']]) for r in scale],marker='o',label='Catalog SELECT + build + lookup')
        ax.plot([r['requested_rows'] for r in scale],[np.mean([x['sql_materialization_ms'] for x in r['trials']]) for r in scale],marker='s',label='SQL + ORM only');ax.set_xscale('log');ax.legend(fontsize=8)
    else:ax.text(.5,.5,'Not measured',ha='center',transform=ax.transAxes)
    ax.set(xlabel='Motion entries (log scale)',ylabel='Mean retrieval time (ms)',title='Motion Retrieval Scalability');ax.grid(alpha=.2);fig.text(.5,.005,'149 real entries; other sizes temporary synthetic duplicated vocabulary. Excludes GLB loading and HTTP.',ha='center',fontsize=8)
    finish('scalability',fig,scale)
    g=s['safety_guard'];bars('safety_validation','Safety-Critical Information Validation Performance',['Precision','Recall','F1','False negative\nrate'],[pct(g[k]) for k in ['precision','recall','f1','false_negative_rate']],f'Candidate compatibility guard ONLY: TP={g["tp"]}, TN={g["tn"]}, FP={g["fp"]}, FN={g["fn"]}; integrated safety N/A')
    cm=np.array(s['classification']['confusion_matrix']);fig,ax=plt.subplots(figsize=(8.5,5.5));im=ax.imshow(cm,cmap='Blues',vmin=0);fig.colorbar(im,ax=ax,label='Cases')
    ax.set_xticks(range(8),s['classification']['predicted_classes'],rotation=40,ha='right');ax.set_yticks(range(7),s['classification']['predicted_classes'][:7]);ax.set(xlabel='Predicted class',ylabel='Expected class',title='Railway Announcement Classification Confusion Matrix')
    for i in range(7):
        for j in range(8):ax.text(j,i,str(cm[i,j]),ha='center',va='center',color='white' if cm[i,j]>cm.max()/2 else 'black')
    finish('template_confusion_matrix',fig,dict(counts=cm.tolist(),classes=s['classification']['predicted_classes']))
    print('Generated 13 charts and 13 matching source-data JSON files.')

if __name__=='__main__':main()
