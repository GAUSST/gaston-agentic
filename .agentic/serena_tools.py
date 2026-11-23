"""Optional, role-local Serena MCP with a narrow semantic read-only surface.

No global MCP registration or auto-download at runtime. Gaston retains edit
scope and verification controls; native tools remain the fallback.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

SEMANTIC_TOOLS = frozenset({'get_symbols_overview','find_symbol','find_referencing_symbols','find_implementations','get_type_hierarchy','get_incoming_calls','get_outgoing_calls'})


def executable(root):
    path=Path(root)/'.agentic/local/serena-venv'/('Scripts/serena.exe' if os.name=='nt' else 'bin/serena')
    if path.is_file() and (os.name=='nt' or os.access(path,os.X_OK)):return str(path)
    return shutil.which('serena')


def supported(binary):
    if not binary:return False
    try:
        p=subprocess.run([binary,'start-mcp-server','--help'],capture_output=True,text=True,timeout=15)
        return p.returncode==0 and all(x in p.stdout for x in ('--project','--context','--mode','--enable-web-dashboard','--open-web-dashboard'))
    except (OSError,subprocess.TimeoutExpired):return False


def setup(root, *, approved=False):
    root=Path(root); binary=executable(root)
    if supported(binary):return True
    uv=shutil.which('uv')
    if not approved or not uv:return False
    folder=root/'.agentic/local/serena-venv'
    try:
        # This is a workspace-private environment, never a global uv tool.
        if subprocess.run([uv,'venv','--python','3.13',str(folder)],timeout=180).returncode:return False
        python=folder/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if subprocess.run([uv,'pip','install','--python',str(python),'serena-agent'],timeout=600).returncode:return False
        return supported(executable(root))
    except (OSError,subprocess.TimeoutExpired):return False


def config(root, provider):
    binary=executable(root)
    if not binary:return None
    return {'type':'stdio','command':sys.executable,'args':[str(Path(root)/'.agentic/serena_tools.py'),provider,binary]}


def codex_args(root):
    conf=config(root,'codex')
    if not conf:return []
    return ['-c','mcp_servers.gaston_serena.command='+json.dumps(conf['command']),'-c','mcp_servers.gaston_serena.args='+json.dumps(conf['args']),'-c','mcp_servers.gaston_serena.startup_timeout_sec=90','-c','mcp_servers.gaston_serena.tool_timeout_sec=120']


def server_args(root,provider,binary):
    return [binary,'start-mcp-server','--project',str(Path(root).resolve()),'--context','claude-code' if provider=='claude' else 'codex','--mode','no-memories','--enable-web-dashboard','false','--open-web-dashboard','false']


def safe_call(root,params):
    if not isinstance(params,dict) or params.get('name') not in SEMANTIC_TOOLS:return False
    a=params.get('arguments') or {}
    if not isinstance(a,dict):return False
    # Require explicit source scope for symbol search and reject hidden paths,
    # symlink escapes, toolkit and environment files. Reference searches can
    # still follow the language server's legitimate cross-file references.
    relative=a.get('relative_path')
    if not isinstance(relative,str) or not relative or relative=='.':return False
    path=Path(relative)
    if path.is_absolute() or '..' in path.parts or any(p.startswith('.') for p in path.parts):return False
    try:
        p=(Path(root)/path).resolve(); p.relative_to(Path(root).resolve())
        if not p.exists() or any(q.startswith('.') for q in p.relative_to(Path(root).resolve()).parts):return False
    except (OSError,ValueError):return False
    return True


def filter_response(msg, method):
    if method=='tools/list' and isinstance(msg.get('result'),dict):
        msg['result']['tools']=[x for x in msg['result'].get('tools',[]) if x.get('name') in SEMANTIC_TOOLS]
        msg['result'].pop('nextCursor',None)
    elif method=='initialize' and isinstance(msg.get('result'),dict):
        msg['result']['instructions']='Use bounded semantic source retrieval with explicit relative_path. Native tools for edits, non-code files and fallback. Do not use Serena for project memory, edits or verification.'
        msg['result']['capabilities']={'tools':{}}
    elif method=='tools/call' and isinstance(msg.get('result'),dict):
        # Preserve full output on disk only in Serena's native logs; never flood
        # the agent. Requery with a narrower symbol when a result is too broad.
        if len(json.dumps(msg['result'],ensure_ascii=False))>24000:
            msg['result']={'isError':True,'content':[{'type':'text','text':'Semantic result too broad. Narrow relative_path/name_path and request only required symbol bodies; use native tools if needed.'}]}
    return msg


def record_usage(root,provider,tool,outcome,output_bytes,elapsed):
    if tool not in SEMANTIC_TOOLS:return
    path=Path(root)/'.agentic/local/serena-usage.jsonl'
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        row={'provider':provider,'tool':tool,'outcome':outcome,'output_bytes':output_bytes,'elapsed_ms':round(elapsed*1000),'recorded_at':time.time()}
        # Single small append operation; no raw query, source or tool output.
        fd=os.open(path,os.O_APPEND|os.O_CREAT|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
        try:os.write(fd,(json.dumps(row,separators=(',',':'))+'\n').encode())
        finally:os.close(fd)
    except OSError:pass


def usage_summary(root):
    from collections import Counter
    path=Path(root)/'.agentic/local/serena-usage.jsonl';counts=Counter();outcomes=Counter();size=0
    if path.is_file() and not path.is_symlink():
        try:
            with path.open() as stream:
                for line in stream:
                    try:row=json.loads(line)
                    except ValueError:continue
                    if not isinstance(row,dict) or row.get('tool') not in SEMANTIC_TOOLS:continue
                    counts[row['tool']]+=1;outcomes[row.get('outcome','unknown')]+=1;size+=row.get('output_bytes',0) if type(row.get('output_bytes')) is int else 0
        except OSError:pass
    return {'installed':bool(executable(root)),'calls_observed':sum(counts.values()),'tools':dict(counts),'outcomes':dict(outcomes),'returned_output_bytes':size,'note':'Proxy observations only; availability does not prove language-server success.'}


def main():
    root=Path(__file__).resolve().parent.parent
    if len(sys.argv)!=3 or sys.argv[1] not in ('claude','codex'):return 2
    binary=sys.argv[2]
    if not supported(binary):return 2
    env=dict(os.environ)
    for key in ('GASTON_LEAD_TOKEN','GASTON_READONLY_DATABASE_URL','GASTON_DATABASE_ALLOW_FIXTURE_DATA'):env.pop(key,None)
    p=subprocess.Popen(server_args(root,sys.argv[1],binary),cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
    pending={}; lock=threading.Lock(); output_lock=threading.Lock()
    def send(msg):
        with output_lock:
            print(json.dumps(msg,ensure_ascii=False),flush=True)
    def read():
        if p.stdout is None:return
        for raw in p.stdout:
            try:msg=json.loads(raw)
            except ValueError:continue
            if not isinstance(msg,dict):continue
            if 'method' in msg:
                # Do not surface server requests/notifications that can trigger
                # unrelated work or repeated model calls.
                if 'id' in msg and p.stdin:
                    with lock:
                        p.stdin.write(json.dumps({'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32601,'message':'Unsupported server request'}})+'\n');p.stdin.flush()
                continue
            with lock:entry=pending.pop(msg.get('id'),None)
            if entry:
                method,tool,started=entry;out=filter_response(msg,method)
                if method=='tools/call':
                    result=out.get('result',{});record_usage(root,sys.argv[1],tool,'error' if 'error' in out or result.get('isError') else 'returned',len(json.dumps(result,ensure_ascii=False).encode()),time.monotonic()-started)
                send(out)
    reader=threading.Thread(target=read,daemon=True);reader.start()
    try:
        for raw in sys.stdin:
            try:msg=json.loads(raw)
            except ValueError:continue
            if not isinstance(msg,dict):continue
            method=msg.get('method');ident=msg.get('id');params=msg.get('params') or {}
            if method not in ('initialize','notifications/initialized','ping','tools/list','tools/call','notifications/cancelled') or (method=='tools/call' and not safe_call(root,params)):
                if ident is not None:send({'jsonrpc':'2.0','id':ident,'error':{'code':-32602,'message':'Use an approved semantic retrieval tool with explicit workspace source path; native tools remain available.'}})
                continue
            if p.poll() is not None:break
            with lock:
                if ident is not None:pending[ident]=(method,params.get('name') if method=='tools/call' else None,time.monotonic())
                p.stdin.write(json.dumps(msg,ensure_ascii=False)+'\n');p.stdin.flush()
    except (OSError,BrokenPipeError):pass
    finally:
        if p.stdin:p.stdin.close()
        try:p.wait(timeout=3)
        except subprocess.TimeoutExpired:p.terminate();p.wait(timeout=5)
        reader.join(timeout=1)
    return 0

if __name__=='__main__':raise SystemExit(main())
