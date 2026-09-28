"""Endpoint kinematic measurements of existing GLBs, not perceptual quality scores."""
import json
import math
import re
import struct
from collections import Counter
from common import OUT, records, save, settings
import numpy as np

def rotation(q):
    x,y,z,w=np.array(q)/np.linalg.norm(q)
    return np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])

def clip_endpoints(path):
    with path.open('rb') as f:
        f.seek(12);length,kind=struct.unpack('<II',f.read(8));doc=json.loads(f.read(length));_,kind=struct.unpack('<II',f.read(8));binary_start=f.tell()
        def accessor(index):
            a=doc['accessors'][index];v=doc['bufferViews'][a['bufferView']]
            if a['componentType']!=5126 or 'sparse' in a:raise ValueError('Unsupported accessor for endpoint metric')
            width={'SCALAR':1,'VEC3':3,'VEC4':4}[a['type']];stride=v.get('byteStride',4*width)
            f.seek(binary_start+v.get('byteOffset',0)+a.get('byteOffset',0));raw=f.read((a['count']-1)*stride+4*width)
            return np.ndarray((a['count'],width),dtype='<f4',buffer=raw,strides=(stride,4)).copy()
        if any('matrix' in n for n in doc['nodes']):raise ValueError('Matrix-node transforms need a separate endpoint evaluator')
        curves=[]
        for channel in doc['animations'][0]['channels']:
            sampler=doc['animations'][0]['samplers'][channel['sampler']]
            if sampler.get('interpolation','LINEAR') not in {'LINEAR','STEP'}:raise ValueError('Unsupported interpolation')
            if channel['target']['path']=='weights':continue
            curves.append((channel['target']['node'],channel['target']['path'],accessor(sampler['input'])[:,0],accessor(sampler['output']),sampler.get('interpolation','LINEAR')))
    duration=max(float(c[2][-1]) for c in curves);dt=min(.04,duration/4)
    parents={child:i for i,n in enumerate(doc['nodes']) for child in n.get('children',[])}
    def pose(t):
        values=[dict(translation=n.get('translation',[0,0,0]),rotation=n.get('rotation',[0,0,0,1]),scale=n.get('scale',[1,1,1])) for n in doc['nodes']]
        for node,path,times,outputs,interpolation in curves:
            j=max(0,min(len(times)-2,int(np.searchsorted(times,t,side='right'))-1));ratio=0 if interpolation=='STEP' else min(1,max(0,(t-float(times[j]))/max(1e-9,float(times[j+1]-times[j]))))
            a,b=outputs[j],outputs[j+1]
            if path=='rotation':
                a=a/np.linalg.norm(a);b=b/np.linalg.norm(b);dot=float(np.dot(a,b))
                if dot<0:b=-b;dot=-dot
                theta=math.acos(min(1,dot))
                value=a*(1-ratio)+b*ratio if dot>.9995 else (a*math.sin((1-ratio)*theta)+b*math.sin(ratio*theta))/math.sin(theta)
                value/=np.linalg.norm(value)
            else:value=a*(1-ratio)+b*ratio
            values[node][path]=value
        world={}
        def transform(i):
            if i not in world:
                v=values[i];m=np.eye(4);m[:3,:3]=rotation(v['rotation'])@np.diag(v['scale']);m[:3,3]=v['translation']
                world[i]=transform(parents[i])@m if i in parents else m
            return world[i]
        joints={j for skin in doc.get('skins',[]) for j in skin['joints']}
        return {doc['nodes'][i].get('name',str(i)):{'rotation':np.asarray(values[i]['rotation']),'world':transform(i)[:3,3]} for i in joints}
    return dict(start=pose(0),after=pose(dt),before=pose(duration-dt),end=pose(duration),dt=dt,duration=duration)

def main():
    catalog={r['motion_version_id']:r for r in json.loads((OUT/'raw/catalog.json').read_text(encoding='utf-8'))}
    pairs=Counter()
    for row in records(OUT/'raw/api_trials.jsonl'):
        items=(row.get('response',{}).get('manifest') or {}).get('items',[])
        pairs.update((a['motion_version_id'],b['motion_version_id']) for a,b in zip(items,items[1:]))
    cache={};errors=[];root=settings().storage_root
    for mid in {m for pair in pairs for m in pair}:
        try:cache[mid]=clip_endpoints(root/catalog[mid]['storage_key'])
        except Exception as exc:errors.append(dict(motion_version_id=mid,error=str(exc)))
    results=[]
    for (a,b),frequency in pairs.items():
        if a not in cache or b not in cache:continue
        left,right=cache[a],cache[b];names=left['end'].keys()&right['start'].keys()
        angles=[];distances=[];hands=[];velocity=[]
        for name in names:
            p,q=left['end'][name],right['start'][name]
            dot=float(np.dot(p['rotation']/np.linalg.norm(p['rotation']),q['rotation']/np.linalg.norm(q['rotation'])))
            angles.append(math.degrees(2*math.acos(min(1,abs(dot)))))
            displacement=float(np.linalg.norm(p['world']-q['world']));distances.append(displacement)
            if re.search(r'(hand|wrist)$',name,re.I):
                hands.append(displacement)
                va=(p['world']-left['before'][name]['world'])/left['dt'];vb=(right['after'][name]['world']-q['world'])/right['dt']
                velocity.append(float(np.linalg.norm(va-vb)))
        results.append(dict(previous=a,next=b,occurrences=frequency,joints=len(names),mean_joint_angle_degrees=float(np.mean(angles)),mean_joint_displacement_model_units=float(np.mean(distances)),mean_hand_displacement_model_units=float(np.mean(hands)) if hands else None,mean_hand_velocity_jump_model_units_per_second=float(np.mean(velocity)) if velocity else None))
    save(OUT/'raw/transition_endpoints.json',dict(scope='Full-clip endpoint cuts in source GLBs. Joint rotations local; positions world/model units; quaternion spherical interpolation for 40ms endpoint velocity estimate. Not an optimized comparison, not perceptual naturalness.',pairs=results,errors=errors,optimized=None))
    print('Measured',len(results),'unique transition pairs; errors',len(errors))

if __name__=='__main__':main()
