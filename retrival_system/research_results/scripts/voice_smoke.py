"""Synthetic Windows speech fixture; not human speech/noise accuracy evidence."""
import os
import subprocess
import time
import uuid
from common import ROOT, OUT, client, credentials, save

def main():
    wav=OUT/'raw/synthetic-voice.wav'
    with client() as api:
        t=time.perf_counter()
        with wav.open('rb') as f:r=api.post('/api/v1/voice/transcribe',files={'audio':('synthetic-voice.wav',f,'audio/wav')})
        elapsed=(time.perf_counter()-t)*1000
        evidence=dict(scope='One synthetic English TTS recording; no natural-speech accuracy claim',spoken_text='Train number one two zero one is arriving at platform two.',http_status=r.status_code,asr_ms=elapsed,response=r.json())
        if r.status_code==201:
            body=r.json();p=api.post('/api/v1/translate',json=dict(request_id=str(uuid.uuid4()),station_id='NAGPUR',input_type='VOICE',text=body['text'],transcript_id=body['transcript_id']))
            evidence['translation_http_status']=p.status_code;evidence['translation']=p.json()
        save(OUT/'raw/voice_api.json',evidence)
    env={**os.environ,'SIGNORA_BROWSER_TOKEN':credentials()['operator']['token'],'SIGNORA_BROWSER_ORIGIN':'http://127.0.0.1:3000','SIGNORA_BROWSER_OUTPUT':str(OUT/'raw/voice_browser'),'SIGNORA_BROWSER_AUDIO':str(wav)}
    result=subprocess.run(['node','scripts/verify-private-preview.mjs'],cwd=ROOT/'frontend',env=env)
    save(OUT/'raw/voice_browser_exit.json',dict(exit_code=result.returncode))
    raise SystemExit(result.returncode)

if __name__=='__main__':main()
