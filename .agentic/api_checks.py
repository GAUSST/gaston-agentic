#!/usr/bin/env python3
"""Small declarative black-box API checks, executed with curl against loopback.

Not a test author: the Builder must derive meaningful cases/assertions from real
contracts. No production hosts, redirects or secrets in the test log.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse


def base_url() -> str:
    value = os.environ.get('AGENTIC_TEST_API_BASE_URL', '').strip().rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Set AGENTIC_TEST_API_BASE_URL to an http(s) loopback API origin')
    hostname = parsed.hostname.lower()
    if hostname != 'localhost':
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError('Only loopback test APIs are allowed; no production or remote services')
        except ValueError as exc:
            if 'Only loopback' in str(exc): raise
            raise ValueError('Only loopback test APIs are allowed; no production or remote services') from exc
    return value


def _lookup(data, path):
    for part in path.split('.'):
        if isinstance(data,dict) and part in data:data=data[part]
        elif isinstance(data,list) and part.isdecimal() and int(part)<len(data):data=data[int(part)]
        else:raise KeyError(path)
    return data

VAR=re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')

def _expand(value, variables):
    if isinstance(value,str):
        exact=VAR.fullmatch(value)
        if exact:return variables[exact.group(1)]
        return VAR.sub(lambda m:str(variables[m.group(1)]),value)
    if isinstance(value,list):return [_expand(x,variables) for x in value]
    if isinstance(value,dict):return {k:_expand(v,variables) for k,v in value.items()}
    return value


def _assert_json(content: str, expected: dict) -> list[str]:
    try:
        data = json.loads(content)
    except ValueError:
        return ['response is not valid JSON']
    failures = []
    for path, expected_value in expected.items():
        item = data
        for part in path.split('.'):
            if isinstance(item, dict) and part in item:
                item = item[part]
            elif isinstance(item, list) and part.isdecimal() and int(part) < len(item):
                item = item[int(part)]
            else:
                item = object()
                break
        if item != expected_value:
            failures.append(f'JSON assertion failed: {path}')
    return failures


def _curl_quote(value: str) -> str:
    return value.replace('\\','\\\\').replace('\"','\\\"')


def run(spec_path: Path) -> int:
    try:
        base = base_url()
        suite = json.loads(spec_path.read_text(encoding='utf-8'))
        cases = suite.get('cases') if isinstance(suite, dict) else None
        if not isinstance(cases, list) or not cases:
            raise ValueError('API check spec must include nonempty cases[]')
    except (ValueError, OSError) as exc:
        print('API checks BLOCKED:', exc)
        return 2
    if not shutil_which_curl():
        print('API checks BLOCKED: curl executable missing')
        return 2
    fails = 0
    variables={}
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            print(f'FAIL case {index}: invalid case object'); fails += 1; continue
        name = str(case.get('name', f'case-{index}'))[:100]
        try:case=_expand(case,variables)
        except KeyError:
            print(f'FAIL {name}: required captured value unavailable');fails+=1;continue
        path = case.get('path', '')
        method = str(case.get('method', 'GET')).upper()
        status = case.get('expected_status')
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '://' in path or '#' in path or any(ord(c)<32 or ord(c)==127 for c in path) or method not in ('GET','POST','PUT','PATCH','DELETE') or type(status) is not int or not 100 <= status <= 599:
            print(f'FAIL {name}: invalid path, method or expected_status'); fails += 1; continue
        if method not in ('GET',) and case.get('allow_local_mutation') is not True:
            print(f'BLOCKED {name}: non-GET test requires allow_local_mutation=true'); fails += 1; continue
        headers = case.get('headers', {})
        env_headers = case.get('header_env', {})
        if not isinstance(headers, dict) or not isinstance(env_headers, dict):
            print(f'FAIL {name}: invalid headers'); fails += 1; continue
        # Curl config arrives over stdin, not argv (test secrets must not appear
        # in the OS process list). A JSON POST body is a private temporary file.
        curl_config = []
        invalid = False
        for key, value in headers.items():
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9-]+',key) or not isinstance(value,str) or '\r' in value or '\n' in value:
                print(f'FAIL {name}: invalid header'); fails += 1; invalid=True; break
            if key.lower() in ('authorization','cookie','proxy-authorization'):
                original=cases[index-1].get('headers',{}).get(key,'')
                if not isinstance(original,str) or not VAR.search(original):
                    print(f'BLOCKED {name}: supply auth headers through captured values or header_env'); fails += 1;invalid=True;break
            curl_config.append(f'header = "{_curl_quote(key+": "+value)}"')
        if invalid: continue
        for key, variable in env_headers.items():
            value = os.environ.get(variable, '') if isinstance(variable, str) else ''
            if not value or not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9-]+',key) or '\r' in value or '\n' in value:
                print(f'BLOCKED {name}: missing/invalid test credential'); fails += 1; invalid=True; break
            curl_config.append(f'header = "{_curl_quote(key+": "+value)}"')
        if invalid: continue
        with tempfile.TemporaryDirectory(prefix='gaston-api-') as directory:
            response = Path(directory)/'response'
            body = case.get('json_body')
            args = ['curl', '--disable', '--silent','--show-error','--noproxy','*', '--max-redirs','0',
                    '--max-time','12', '--connect-timeout','3','--request',method,
                    '--config','-']
            if body is not None:
                if method == 'GET':
                    print(f'FAIL {name}: GET tests cannot include json_body');fails += 1;continue
                body_path=Path(directory)/'body.json'
                body_path.write_text(json.dumps(body),encoding='utf-8')
                body_path.chmod(0o600)
                curl_config.append('header = "Content-Type: application/json"')
                args.extend(['--data-binary','@'+str(body_path)])
            args.extend(['--output',str(response),'--write-out','%{http_code}',base+path])
            try:
                # Never print curl argv, request bodies, credentials or response.
                result = subprocess.run(args, input='\n'.join(curl_config)+'\n',
                                        capture_output=True, text=True, timeout=20, check=False)
            except (OSError, subprocess.TimeoutExpired):
                print(f'BLOCKED {name}: curl did not complete'); fails += 1; continue
            actual_status = int(result.stdout.strip()) if result.stdout.strip().isdecimal() else None
            if result.returncode != 0 or actual_status != status:
                print(f'FAIL {name}: expected HTTP {status}, got {actual_status or "connection failure"}')
                fails += 1; continue
            if 'expect_json' in case:
                expected = case['expect_json']
                if not isinstance(expected,dict):
                    print(f'FAIL {name}: invalid expect_json'); fails += 1; continue
                errs = _assert_json(response.read_text(encoding='utf-8', errors='replace'), expected)
                if errs:
                    print(f'FAIL {name}: {", ".join(errs)}'); fails += 1; continue
            captures=case.get('capture_json',{})
            if captures:
                try:
                    if not isinstance(captures,dict):raise ValueError()
                    data=json.loads(response.read_text(encoding='utf8'))
                    values={}
                    for key,field in captures.items():
                        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key) or not isinstance(field,str):raise ValueError()
                        val=_lookup(data,field)
                        if val is None or not isinstance(val,(str,int,float,bool)):raise ValueError()
                        values[key]=val
                    variables.update(values)
                except (ValueError,KeyError,TypeError):
                    print(f'FAIL {name}: expected captured JSON field missing or invalid');fails+=1;continue
            print(f'PASS {name}: HTTP {actual_status}, assertions satisfied')
    print(f'API checks: {len(cases)-fails}/{len(cases)} passed')
    return 1 if fails else 0


def shutil_which_curl():
    from shutil import which
    return which('curl')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('Usage: python .agentic/api_checks.py path/to/api-checks.json')
        raise SystemExit(2)
    raise SystemExit(run(Path(sys.argv[1])))
