"""Run the real browser harness with credentials only in process memory."""
import os
import subprocess
import sys
import time
from common import ROOT, OUT, append, credentials
import psutil

def main():
    diverse='--diverse' in sys.argv
    env={**os.environ,'SIGNORA_BROWSER_TOKEN':credentials()['operator']['token'],'SIGNORA_EVAL_DIVERSE':'1' if diverse else '0'}
    child=subprocess.Popen(['node','research_results/scripts/browser_benchmark.mjs'],cwd=ROOT,env=env)
    root=psutil.Process(child.pid)
    while child.poll() is None:
        processes=[root]
        try:processes+=root.children(recursive=True)
        except psutil.Error:pass
        samples=[]
        for process in processes:
            try:samples.append(dict(pid=process.pid,name=process.name(),rss_bytes=process.memory_info().rss,cpu_seconds=sum(process.cpu_times()[:2])))
            except psutil.Error:pass
        append(OUT/'raw'/('diverse_process_memory.jsonl' if diverse else 'browser_process_memory.jsonl'),dict(timestamp=time.time(),processes=samples,system_available_bytes=psutil.virtual_memory().available))
        time.sleep(1)
    raise SystemExit(child.returncode)

if __name__=='__main__':main()
