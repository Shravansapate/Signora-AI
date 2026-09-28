/** Actual UI/API/GLBs/Three mixer. Only diagnostics injected, no mocked responses. */
import { createRequire } from 'node:module';
import { mkdir,appendFile,writeFile,readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const require=createRequire(resolve('frontend/package.json'));
const {chromium}=require('playwright');
const out=resolve('research_results/raw');
const token=process.env.SIGNORA_BROWSER_TOKEN;
if(!token)throw new Error('Missing operator credential');
await mkdir(out,{recursive:true});
const diverse=process.env.SIGNORA_EVAL_DIVERSE==='1';
const file=resolve(out,diverse?'browser_diverse.jsonl':'browser_trials.jsonl');
const previous=await readFile(file,'utf8').catch(()=> '');
const priorRows=previous.trim().split('\n').filter(Boolean).map(x=>JSON.parse(x));
const done=new Set(priorRows.filter(x=>x.success).map(x=>x.id));
const browser=await chromium.launch({headless:true,args:['--enable-unsafe-swiftshader']});
const context=await browser.newContext({viewport:{width:1280,height:900}});
const page=await context.newPage();
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('crash',()=>errors.push('BROWSER_PAGE_CRASH'));
await page.goto('http://127.0.0.1:3000/announcements');
await page.locator('#announcement-token').fill(token);
await page.getByRole('button',{name:'Connect',exact:true}).click();
await page.getByRole('button',{name:'Disconnect',exact:true}).waitFor();
const gpu=await page.locator('canvas').evaluate(canvas=>{const gl=canvas.getContext('webgl2');const ext=gl?.getExtension('WEBGL_debug_renderer_info');return {renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null,vendor:ext?gl.getParameter(ext.UNMASKED_VENDOR_WEBGL):null};});
const cdp=await context.newCDPSession(page);
await cdp.send('Performance.enable');
await writeFile(resolve(out,diverse?'browser_diverse_environment.json':'browser_environment.json'),JSON.stringify({version:browser.version(),gpu,headless:true,args:['--enable-unsafe-swiftshader'],viewport:{width:1280,height:900},measurement:'requestAnimationFrame diagnostics; bone pose hash sampled by app every 250ms; warm persistent-avatar session; no playback-rate changes',timing_workload:diverse?'Distinct existing words, 20 trials per length; new assets cold on first occurrence, animation cache warm thereafter':'Repeated TRAIN clips: controlled sequence length, one unique motion; does not estimate diverse-asset cold loading'},null,2));
const cases=[];
const words='train accident platform fire exit ticket water emergency station stop help delay arrive food police clock time minute hour today tomorrow yesterday refund reservation counter window bag luggage cash card'.split(' ');
for(const n of [1,5,10,13,20,30])for(let trial=0;trial<20;trial++)cases.push({id:`${diverse?'diverse':'length'}-${n}-${trial}`,kind:'latency',workload:diverse?'distinct words':'repeated TRAIN',n,trial,text:(diverse?words.slice(0,n):Array(n).fill('train')).join(' ')});
if(!diverse)for(let trial=0;trial<50;trial++)cases.push({id:`consecutive-${trial}`,kind:'consecutive',n:null,trial,text:['Train','Accident','Platform','Train 1201 arrives at platform 2','Train arrives'][trial%5]});
let avatar;
try{
for(const item of cases){
 if(done.has(item.id))continue;
 const errorStart=errors.length;
 const row={...item,attempt:1+priorRows.filter(x=>x.id===item.id).length,browser_session_started:new Date().toISOString()};
 try{
 await page.locator('#announcement-text').fill(item.text);
 await page.evaluate(()=>{
   const canvas=document.querySelector('canvas');
   const m={submitted:performance.now(),states:[],samples:[],frames:[],lastFrame:null,lastPose:null,firstPose:null,firstMotion:null};
   window.__research=m;
   performance.clearResourceTimings();
   function frame(t){
     if(window.__research!==m)return;
     const d={...canvas.dataset};
     if(m.states.at(-1)?.state!==d.playbackState)m.states.push({state:d.playbackState,t:performance.now()});
     if(d.playbackState==='PLAYING'){
       if(m.lastFrame!==null)m.frames.push(t-m.lastFrame);
       m.lastFrame=t;
       if(d.poseHash!==m.lastPose){
         m.samples.push({t:performance.now(),...d});m.lastPose=d.poseHash;
         if(m.firstPose===null)m.firstPose=d.poseHash;
         else if(m.firstMotion===null && d.poseHash!==m.firstPose && Number(d.mixerTime)>0)m.firstMotion=performance.now();
       }
     }else m.lastFrame=null;
     requestAnimationFrame(frame);
   }
   requestAnimationFrame(frame);
 });
 const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/v1/translate')&&r.request().method()==='POST',{timeout:180000});
 await page.getByRole('button',{name:'Play announcement',exact:true}).click();
 const response=await responsePromise;row.http_status=response.status();row.response=await response.json();
 row.api_received=await page.evaluate(()=>performance.now());
 if(!row.response.manifest)throw new Error(`No manifest: ${row.response.status}`);
 row.actual_clips=row.response.manifest.items.length;
 if(item.n!==null&&row.actual_clips!==item.n)throw new Error(`Requested ${item.n} clips; got ${row.actual_clips}`);
 await page.waitForFunction(()=>window.__research.firstMotion!==null||document.querySelector('canvas')?.dataset.playbackState==='ERROR',null,{timeout:180000,polling:'raf'});
 if(await page.locator('canvas').getAttribute('data-playback-state')==='ERROR')throw new Error('Avatar ERROR');
 if(item.kind==='consecutive'){
   await page.waitForFunction(()=>['COMPLETE','ERROR'].includes(document.querySelector('canvas')?.dataset.playbackState),null,{timeout:240000});
 }else await page.waitForTimeout(1000);
 row.measurement=await page.evaluate(()=>({...window.__research,finished:performance.now(),canvas:{...document.querySelector('canvas').dataset},resources:performance.getEntriesByType('resource').filter(r=>r.name.includes('/playback/')||r.name.endsWith('/translate')).map(r=>({url:r.name,startTime:r.startTime,duration:r.duration,transferSize:r.transferSize})),heap:performance.memory?{used:performance.memory.usedJSHeapSize,total:performance.memory.totalJSHeapSize}:null}));
 const m=row.measurement;
 row.ttfs_ms=m.firstMotion-m.submitted;
 row.prepared_ms=m.states.find(x=>['READY','PLAYING'].includes(x.state))?.t-m.submitted;
 row.completed=item.kind==='consecutive'?m.canvas.playbackState==='COMPLETE'&&Number(m.canvas.completedClips)===row.actual_clips:null;
 avatar??=m.canvas.avatarInstance;row.persistent_avatar=m.canvas.avatarInstance===avatar;
 row.movement_verified=m.samples.length>1&&Number(m.samples.at(-1).mixerTime)>Number(m.samples[0].mixerTime);
 row.cdp_metrics=(await cdp.send('Performance.getMetrics')).metrics;
 row.success=row.movement_verified&&row.persistent_avatar&&(item.kind!=='consecutive'||row.completed);
 if(item.id==='length-13-0'||item.id==='consecutive-49')await page.screenshot({path:resolve(out,`${item.id}.png`),fullPage:true});
 if(item.kind!=='consecutive' && await page.getByRole('button',{name:'Stop',exact:true}).isEnabled()){
   try{await page.getByRole('button',{name:'Stop',exact:true}).click({timeout:1500});}
   catch(e){if(await page.locator('canvas').getAttribute('data-playback-state')!=='COMPLETE')throw e;}
 }
 }catch(e){row.success=false;row.error=e.message;await page.screenshot({path:resolve(out,`${item.id}-failure.png`)}).catch(()=>{});}
 row.errors=errors.slice(errorStart);
 await appendFile(file,JSON.stringify(row)+'\n');
 console.log(item.id,row.success,row.actual_clips,Math.round(row.ttfs_ms??0),row.error??'');
 if(row.error)break; // Preserve first failure and debug; never substitute a passing trial.
}
}finally{await context.close();await browser.close();}
