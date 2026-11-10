"""Prepare, exercise and close a pinned AGWF run using host-supplied evidence.

No native tools, model sessions or goal transitions are performed here.
Run one host writer per session. Input receipts are caller-supplied evidence.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


class CheckError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CheckError(message)


def read(path):
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,'duplicate JSON key')
            result[key]=value
        return result
    def finite(value):
        number=float(value)
        require(math.isfinite(number),'nonfinite JSON number')
        return number
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=pairs,
                      parse_float=finite,parse_constant=lambda value: (_ for _ in ()).throw(CheckError('nonfinite JSON')))


def write(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        tmp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(session):
    manifest=Path(session['manifest'])
    require(sha(manifest)==session['manifest_sha256'],'external manifest pin mismatch')
    data=read(manifest)
    require(data['identity']=='agent-workflow-v1-0' and data['version']==session['version'],
            'package identity/version mismatch')
    root=manifest.parent
    for name,pin in data['files'].items():
        path=(root/name).resolve()
        require(path.is_relative_to(root.resolve()) and not (root/name).is_symlink(), 'unsafe payload path')
        require(sha(path)==pin,'payload changed: '+name)
    require(Path(session['candidate']).resolve()==(root/'scripts/candidate.py').resolve()
            and 'scripts/candidate.py' in data['files'],'entry must belong to pinned package')
    return session


def prepare(session_path, candidate, manifest, pin, version, project, run_id):
    require(not Path(session_path).exists(),'session already exists; inspect and reuse it')
    session=dict(schema_version=1,candidate=str(Path(candidate).resolve()),manifest=str(Path(manifest).resolve()),
                 manifest_sha256=pin,version=version,project=str(Path(project).resolve()),run_id=run_id,
                 phase='prepared',prepared_at=time.time(),timed_events=[])
    require(Path(project).is_dir(),'project must already exist')
    verify(session)
    write(session_path,session)
    return dict(result='PREPARED',worker_started=False,session=str(Path(session_path).resolve()))


def load_session(path):
    session=read(path)
    require(session.get('schema_version')==1,'unsupported session schema')
    return verify(session)


def call(session, args, payload=None):
    command=[sys.executable,'-X','utf8','-B',session['candidate'],'--manifest',session['manifest'],
             '--manifest-sha256',session['manifest_sha256'],'--version',session['version'],
             '--project',session['project'],'--run-id',session['run_id'],*args]
    result=subprocess.run(command,input=json.dumps(payload or {},ensure_ascii=False),
                          capture_output=True,text=True,encoding='utf-8',timeout=35)
    if result.returncode:
        error=CheckError(result.stderr or result.stdout or 'candidate failed without output')
        try: error.receipt=json.loads(result.stderr)
        except (ValueError,TypeError): error.receipt={}
        raise error
    return json.loads(result.stdout)


def refresh_by(at, interval):
    return at+interval-min(15,interval/4)


def healthy(status, observed_at):
    health=status.get('health',{})
    return (status.get('at')==observed_at and status.get('action') in {'CONTINUE','CHECKPOINT_SOON'}
            and health.get('identityState')=='matched' and all(health.get(key) is True for key in
            ('healthy','liveIdentity','heartbeatFresh','leaseValid','sampleFresh','workerEpochMatches')))


def usage_path(session_path, data):
    receipt=data['usage']
    require(receipt.get('source')=='mcp__codex_app__get_usage_limits','real native usage source required')
    path=Path(session_path).with_suffix('.usage.json')
    write(path,receipt)
    return str(path.resolve())


def wait_healthy(session, at, allow_save=False):
    deadline=time.monotonic()+8
    while True:
        status=call(session,['watcher','status'])
        if healthy(status,at): return status
        if status.get('action')=='SAVE_AND_STOP' and allow_save and status.get('at')==at: return status
        require(status.get('action')!='SAVE_AND_STOP','watcher requested SAVE_AND_STOP')
        require(time.monotonic()<deadline,'matching fresh worker health not observed')
        time.sleep(.1)


def usage_payload(data):
    receipt=data['usage']; result=receipt['result']
    limits=result['rateLimitsByLimitId']['codex'] if 'rateLimitsByLimitId' in result else result['rateLimits']
    return dict(observed_at=receipt['observedAt'],rateLimits=limits)


def sync_host(session,data):
    call(session,['workflow','app-goal'],dict(app_goal_observation=data['app_goal_observation']))
    call(session,['workflow','inventory'],dict(inventory=data['inventory'],observed_at=data['app_goal_observation']['observed_at']))
    return call(session,['workflow','usage'],usage_payload(data))


def run(session_path,data):
    session=load_session(session_path)
    require(session['phase'] in {'prepared','policy_ready','startup_failed'},'run requires prepared session; use observe or resume')
    receipt=usage_path(session_path,data)
    policy=data.get('weekly_exception')
    if policy and session['phase']=='prepared':
        require(session['phase']=='prepared','configure weekly policy before first activation only')
        call(session,['watcher','start','--usage-input',receipt,'--ignore-weekly-quote',policy['exact_quote'],
                      '--exception-expires-at',str(policy['valid_until'])])
        status=wait_healthy(session,data['usage']['observedAt'])
        session.update(phase='policy_ready',policy_configured_at=status['policy']['exception']['validFrom'])
        write(session_path,session)
        return dict(result='NEED_POLICY_OBSERVATION',watcher=status,
                    reason='Obtain a new actual host observation after bounded policy setup, then run the same session.')
    if session['phase']=='policy_ready':
        require(data['usage']['observedAt']>=session['policy_configured_at'],
                'new usage observation after policy setup required')
    args=['workflow','bind-active-goal','--usage-input',receipt]
    if session.get('startup_attempt_sha256'):
        args+=['--startup-attempt-sha256',session['startup_attempt_sha256']]
    try:
        call(session,args,data['activation'])
    except CheckError as error:
        retry=getattr(error,'receipt',{}).get('startup_retry',{})
        if retry.get('attempt_sha256'):
            session.update(phase='startup_failed',startup_attempt_sha256=retry['attempt_sha256'])
            write(session_path,session)
        raise
    session.update(phase='active',goal_id=data['app_goal_observation']['goal_id'],
                   thread_id=data['app_goal_observation']['thread_id'])
    write(session_path,session)
    sync_host(session,data)
    status=wait_healthy(session,data['usage']['observedAt'])
    return dict(result='ACTIVE',watcher=status)


def halt(session_path,session,data):
    require(data.get('writers_quiescent') is True,'host must confirm all writers quiescent')
    requested=time.time()
    call(session,['workflow','stop'])
    returned=time.time()
    snapshot=call(session,['workflow','snapshot'],dict(files=data['checkpoint_files'],writers_quiescent=True,
                next_action=data['next_action']))
    snapshot_at=time.time()
    result=call(session,['workflow','halt'],dict(inventory=data['inventory'],observed_at=time.time(),
                owned_processes_stopped=True,stop_evidence='Candidate verifies worker exit; host supplied quiescent inventory.'))
    session.update(phase='halted',halted_at=time.time())
    write(session_path,session)
    return dict(result='NEED_POST_HALT_OBSERVATION',snapshot=snapshot,halt=result,
                save_requested_at=requested,save_returned_at=returned,snapshot_verified_at=snapshot_at,
                helper_halted_at=session['halted_at'])


def observe(session_path,data):
    session=load_session(session_path)
    require(session['phase']=='active','observe requires active session')
    status=call(session,['watcher','status'])
    if status.get('action')=='SAVE_AND_STOP': return halt(session_path,session,data)
    receipt=usage_path(session_path,data)
    call(session,['watcher','ingest','--usage-input',receipt])
    status=wait_healthy(session,data['usage']['observedAt'],allow_save=True)
    if status.get('action')=='SAVE_AND_STOP': return halt(session_path,session,data)
    call(session,['watcher','renew'])
    sync_host(session,data)
    status=wait_healthy(session,data['usage']['observedAt'])
    return dict(result='HEALTHY',watcher=status,
                refresh_by=refresh_by(status['at'],status['check_interval_seconds']))


def resume(session_path,data):
    session=load_session(session_path)
    require(session['phase']=='halted','resume requires recorded halt')
    require(data['usage']['observedAt']>session['halted_at']
            and data['app_goal_observation']['observed_at']>session['halted_at'],
            'obtain new host observations strictly after halt')
    # Core validates same-goal authority, actual saved cause, fresh inventory and checkpoint.
    # Preserve the recorded inactive native observation for native UI resume.
    call(session,['workflow','inventory'],dict(inventory=data['inventory'],observed_at=data['app_goal_observation']['observed_at']))
    call(session,['workflow','usage'],usage_payload(data))
    payload=dict(usage_recovery=True,app_goal_observation=data['app_goal_observation'])
    if 'resume' in data: payload=data['resume']
    call(session,['workflow','resume','--usage-input',usage_path(session_path,data)],payload)
    session.update(phase='active'); write(session_path,session)
    return dict(result='ACTIVE',watcher=wait_healthy(session,data['usage']['observedAt']))


def close(session_path,data):
    require(data.get('writers_quiescent') is True,'host must confirm all writers quiescent')
    refreshed=observe(session_path,data)
    if refreshed['result']!='HEALTHY': return refreshed
    session=load_session(session_path)
    call(session,['workflow','snapshot'],dict(files=data['checkpoint_files'],writers_quiescent=True,
                                            next_action=data['next_action']))
    result=call(session,['workflow','complete'])
    status=call(session,['watcher','status'])
    require(result['status']=='complete' and status['health']['identityState']=='absent',
            'completion or owned worker exit unverified')
    require(status.get('action')!='SAVE_AND_STOP','normal close encountered SAVE latch')
    session.update(phase='complete',completed_at=time.time()); write(session_path,session)
    return dict(result='COMPLETE',save_latched=False,workflow=result,watcher=status)


def timed_test(session_path,event,data,delay=10,tolerance=1):
    require(type(delay) in (float,int) and math.isfinite(delay) and delay>0,'positive delay required')
    require(type(tolerance) in (float,int) and math.isfinite(tolerance) and 0<=tolerance<delay,'invalid tolerance')
    now=time.time()
    for key in ('event_at','observed_at'):
        require(type(event[key]) in (int,float) and math.isfinite(event[key]),'finite event times required')
    require(event['event_at']<=event['observed_at']<=now and event.get('source_id'),'past sourced event required')
    deadline=event['event_at']+delay
    if now>deadline+tolerance: return dict(result='INVALID_TIMING',native_event_at=event['event_at'],observed_at=event['observed_at'])
    session=load_session(session_path)
    require(session['phase']=='active' and event['goal_id']==session['goal_id']
            and event['thread_id']==session['thread_id'],'active same-goal timed test required')
    require(event['event_id'] not in session['timed_events'],'event already consumed')
    status=call(session,['watcher','status'])
    require(healthy(status,status.get('at')),'healthy worker required before timed stop')
    worker_healthy_at=time.time()
    session['timed_events'].append(event['event_id']);write(session_path,session)
    target=time.monotonic()+max(0,deadline-time.time())
    while time.monotonic()<target: time.sleep(min(.05,target-time.monotonic()))
    result=halt(session_path,session,data)
    requested=result['save_requested_at']
    return dict(result='SAVE_REQUEST_ON_TIME' if abs(requested-deadline)<=tolerance else 'INVALID_TIMING',
                native_event_at=event['event_at'],observed_at=event['observed_at'],worker_healthy_at=worker_healthy_at,
                save_requested_at=requested,save_returned_at=result['save_returned_at'],
                snapshot_verified_at=result['snapshot_verified_at'],helper_halted_at=result['helper_halted_at'],
                native_paused_at=None,native_pause_verified=False,details=result)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='mode',required=True)
    prep=sub.add_parser('prepare')
    for name in ('candidate','manifest','manifest-sha256','version','project','run-id','session'):
        prep.add_argument('--'+name,required=True)
    for mode in ('run','observe','resume','close','timed-test'):
        command=sub.add_parser(mode);command.add_argument('--session',required=True);command.add_argument('--input',required=True)
        if mode=='timed-test':
            command.add_argument('--event-input',required=True)
            command.add_argument('--delay-seconds',type=float,default=10)
            command.add_argument('--tolerance-seconds',type=float,default=1)
    args=parser.parse_args(argv)
    try:
        if args.mode=='prepare':
            result=prepare(args.session,args.candidate,args.manifest,args.manifest_sha256,args.version,args.project,args.run_id)
        elif args.mode=='timed-test':
            result=timed_test(args.session,read(args.event_input),read(args.input),args.delay_seconds,args.tolerance_seconds)
        else: result=globals()[args.mode](args.session,read(args.input))
        print(json.dumps(result,ensure_ascii=False));return 0
    except (CheckError,OSError,ValueError,KeyError,subprocess.TimeoutExpired) as error:
        print(json.dumps(dict(error=str(error),result='REFUSED'),ensure_ascii=False),file=sys.stderr);return 2


if __name__=='__main__': raise SystemExit(main())
