"""One quota interruption, one explicit decision, one authorized recovery.

Gaston validates time, task and attempt identity before a local deterministic
wakeup can start the existing task.
No timer is started merely because a provider reported a rate limit.
"""
from __future__ import annotations
from datetime import datetime, timezone
import re
import secrets
import time
from task_runtime import read_record
from orchestration import put


def retry_hint(log):
    # Only expose the clock expression, never the original potentially sensitive log.
    match=re.search(r'(?i)(?:try again|retry|reset|available again)[^\n]{0,60}?\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|\d{1,2}:\d{2}\s*(?:AM|PM)?(?:\s*UTC)?)',log)
    return match.group(1).strip() if match else None


def interruption(folder):
    state=read_record(folder/'state.json'); worker=read_record(folder/'worker.json')
    if state.get('stage')!='blocked' or state.get('blocker')!='provider_quota' or not state.get('resumable'):return None
    attempt=worker.get('attempt')
    if not isinstance(attempt,str) or not re.fullmatch(r'[a-f0-9]{32}',attempt):return None
    return attempt


def current(folder):
    attempt=interruption(folder)
    if not attempt:return None
    record=read_record(folder/'quota-recovery.json')
    if record.get('attempt')!=attempt:return {'attempt':attempt,'decision':'pending'}
    # No nonce in routine status responses; only return it directly on consent.
    return {k:v for k,v in record.items() if k!='recovery_token'}


def decision(folder, approved, *, user_request, retry_at=None):
    attempt=interruption(folder)
    if not attempt:raise ValueError('No currently resumable quota interruption with known attempt identity')
    if type(approved) is not bool or not isinstance(user_request,str) or not user_request.strip():raise ValueError('Record explicit developer decision from the current conversation')
    old=read_record(folder/'quota-recovery.json')
    if old.get('attempt')==attempt and old.get('decision')=='consumed':raise ValueError('Recovery already consumed')
    if not approved:
        record={'attempt':attempt,'decision':'declined','recorded_at':time.time()}
        if old.get('attempt')==attempt and old.get('job_id'):record['cancel_job_id']=old['job_id']
        put(folder/'quota-recovery.json',record)
        return {'status':'ready','decision':'declined','cancel_job_id':record.get('cancel_job_id'),'note':'Task preserved for manual resumption. Any previous local wakeup will observe revocation; cancel legacy provider jobs separately.'}
    if not isinstance(retry_at,str):raise ValueError('Provide confirmed retry timestamp including its timezone (ISO 8601)')
    try:when=datetime.fromisoformat(retry_at.replace('Z','+00:00'))
    except ValueError:raise ValueError('Provide confirmed retry timestamp including its timezone (ISO 8601)')
    if when.tzinfo is None or when.timestamp()<=time.time():raise ValueError('Retry timestamp must be future and timezone-aware; do not guess the provider timezone')
    if old.get('attempt')==attempt and old.get('decision')=='approved':
        if old.get('retry_epoch')!=when.timestamp():raise ValueError('Existing approval has a different retry time; cancel it first')
        return {'status':'ready','decision':'approved','retry_at':old['retry_at'],'recovery_token':old['recovery_token'],'job_id':old.get('job_id'),'note':'Do not register a second job; reuse the existing one.' if old.get('job_id') else 'Register exactly one local wakeup.'}
    record={'attempt':attempt,'decision':'approved','retry_at':when.astimezone(timezone.utc).isoformat(),'retry_epoch':when.timestamp(),'recorded_at':time.time(),'recovery_token':secrets.token_hex(24)}
    put(folder/'quota-recovery.json',record)
    return {'status':'ready','decision':'approved','retry_at':record['retry_at'],'recovery_token':record['recovery_token'],'note':'Register the local Builder-only wakeup and report its actual job ID.'}


def record_job(folder, token, job_id):
    record=read_record(folder/'quota-recovery.json')
    if record.get('attempt')!=interruption(folder) or record.get('decision')!='approved' or not isinstance(token,str) or not secrets.compare_digest(record.get('recovery_token',''),token):raise ValueError('No valid current scheduling approval')
    if not isinstance(job_id,str) or not job_id.strip() or len(job_id)>150:raise ValueError('Provide the confirmed job ID')
    if record.get('job_id') and record['job_id']!=job_id:raise ValueError('One job per interruption; cancel duplicates')
    record['job_id']=job_id;put(folder/'quota-recovery.json',record)
    return {'status':'ready','decision':'approved','job_id':job_id,'retry_at':record['retry_at'],'note':'Wakeup registered; execution still subject to host and provider availability.'}


def check_resume(folder, token):
    if not isinstance(token,str):raise ValueError('Automated recovery requires a valid recovery token')
    record=read_record(folder/'quota-recovery.json')
    if record.get('attempt')!=interruption(folder) or record.get('decision')!='approved' or not secrets.compare_digest(record.get('recovery_token',''),token):raise ValueError('Recovery authorization is missing, stale, declined or consumed')
    if not record.get('job_id'):raise ValueError('Scheduling was not confirmed')
    if time.time()<record.get('retry_epoch',float('inf')):raise ValueError('Provider retry time has not arrived')
    return record


def consume(folder, record):
    if record:
        put(folder/'quota-recovery.json',{**record,'decision':'consumed','consumed_at':time.time()})
