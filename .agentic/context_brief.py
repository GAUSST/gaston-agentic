"""Small deterministic handoff context, no extra agent or full log replay.

This complements persistent provider sessions, rather than attempting to replace
Claude/Codex native context management with a misleading generated 'memory'.
"""
from __future__ import annotations
import json
import re


def make(task: dict, cfg: dict, preexisting: dict, *, max_paths: int=30) -> dict:
    goal=str(task.get('goal',''))
    project=cfg.get('project',{}) if isinstance(cfg.get('project',{}),dict) else {}
    roots=project.get('discovered_roots',{})
    if not isinstance(roots,dict):roots={}
    all_roots=[{'type':k,'paths':v[:12]} for k,v in sorted(roots.items()) if isinstance(v,list)]
    hints=re.findall(r'(?<!\w)([\w./-]{2,180}\.(?:java|kt|dart|ts|tsx|jsx|js|yaml|json|sql))\b',goal)
    keys=sorted(str(key) for key in preexisting if isinstance(key,str))
    limited=keys[:max_paths]
    # Goal and acceptance criteria are already in build_prompt: do not pay for
    # them a second time in the supplemental context map.
    return {'project_roots':all_roots[:15], 'named_file_hints':hints[:20],
            'existing_change_paths':limited,
            'existing_changes_not_listed':max(0,len(keys)-len(limited)),
            'source':'local_project_discovery_not_implementation_evidence'}


def render(brief: dict) -> str:
    """Keep the supplemental context bounded without emitting malformed JSON.

    The original instructions and specifications are separate from this brief.
    Explicitly report omissions rather than slicing a serialized object.
    """
    data=dict(brief)
    for key in ('existing_change_paths','named_file_hints','project_roots'):
        if isinstance(data.get(key),list):data[key]=list(data[key])
    def encode():return json.dumps(data,ensure_ascii=False,separators=(',',':'))
    result=encode()
    if len(result)<=6500:return result
    data['context_truncated']=True
    for key in ('existing_change_paths','named_file_hints','project_roots'):
        value=data.get(key)
        while isinstance(value,list) and value and len(encode())>6500:
            value.pop()
            if key=='existing_change_paths':
                data['existing_changes_not_listed']=int(data.get('existing_changes_not_listed',0))+1
            else:
                count_key=key+'_not_listed'
                data[count_key]=int(data.get(count_key,0))+1
    result=encode()
    if len(result)<=6500:return result
    return json.dumps({'source':'local_project_discovery_not_implementation_evidence','context_truncated':True,'existing_changes_not_listed':int(data.get('existing_changes_not_listed',0))},separators=(',',':'))
