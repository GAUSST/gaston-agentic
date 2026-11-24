"""Real stdio server: a long verification cannot block another tool or ping."""
from pathlib import Path
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

class MCPParallelTests(unittest.TestCase):
    def test_ping_and_inspection_while_verification_in_flight(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        root=Path(td.name);entered=root/'entered';release=root/'release'
        code="""import sys,time
from pathlib import Path
import verification_mcp as vm
vm._recover_abandoned_emulator=lambda:None
vm._cleanup_on_exit=lambda:None
entered,release=map(Path,sys.argv[1:3])
def handle(req,**kwargs):
    if req.get('params',{}).get('name')=='run_task_acceptance_check':
        entered.write_text('running')
        limit=time.monotonic()+12
        while not release.exists() and time.monotonic()<limit:time.sleep(.02)
        return {'completed':release.exists()}
    return {'responsive':True}
vm.handle=handle
vm.main()
"""
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parent)}
        proc=subprocess.Popen([sys.executable,'-c',code,str(entered),str(release)],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
        def cleanup():
            release.touch()
            if proc.stdin:proc.stdin.close()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            proc.stdout.close()
        self.addCleanup(cleanup)
        lines=queue.Queue()
        def read():
            for line in proc.stdout:lines.put(json.loads(line))
        threading.Thread(target=read,daemon=True).start()
        def send(id,method,name=None):
            req={'jsonrpc':'2.0','id':id,'method':method}
            if name:req['params']={'name':name,'arguments':{}}
            proc.stdin.write(json.dumps(req)+'\n');proc.stdin.flush()
        send(1,'tools/call','run_task_acceptance_check')
        limit=time.monotonic()+5
        while not entered.exists() and time.monotonic()<limit:time.sleep(.02)
        self.assertTrue(entered.exists())
        send(2,'ping')
        send(3,'tools/call','work_plan_status')
        self.assertEqual({lines.get(timeout=5)['id'],lines.get(timeout=5)['id']},{2,3})
        release.touch()
        result=lines.get(timeout=5)
        self.assertEqual(result['id'],1)
        self.assertTrue(result['result']['completed'])
