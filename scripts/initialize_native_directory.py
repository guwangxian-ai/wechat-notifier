"""Initialize directory keys before login, using an account verified while logged in.

start normally quits/reopens WeChat after a verified backup and starts a temporary
bridge. finish returns that bridge to launchd after login and directory success.
No credentials, database keys, account paths, drafts or chat content are printed.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.state'
JOURNAL = DATA / 'native-directory-bootstrap.json'


def execute(args, timeout=25):
    return subprocess.run([str(a) for a in args],capture_output=True,text=True,timeout=timeout)


def process_uses(pid, path):
    if not isinstance(pid,int) or pid<=0:return False
    files=execute(['/usr/sbin/lsof','-a','-p',str(pid),'-Fn']).stdout.splitlines()
    return 'n'+str(path) in files


def save_journal(data):
    temporary=JOURNAL.with_suffix('.tmp')
    temporary.write_text(json.dumps(data));temporary.chmod(0o600);temporary.replace(JOURNAL)


def service(action):
    result=execute([ROOT/'.venv/bin/python',ROOT/'scripts/manage_native_service.py',action],40)
    if result.returncode:raise RuntimeError('BRIDGE_SERVICE_ACTION_FAILED')


def stop_temporary(record):
    executable=DATA/'native-runtime/bin/onebot'
    if not process_uses(record['bridge_pid'],executable):
        raise RuntimeError('BOOTSTRAP_BRIDGE_NOT_RUNNING')
    os.kill(record['bridge_pid'],signal.SIGTERM)
    deadline=time.monotonic()+12
    while process_uses(record['bridge_pid'],executable):
        if time.monotonic()>deadline:raise RuntimeError('BOOTSTRAP_BRIDGE_STILL_EXITING')
        time.sleep(.25)


def spawn_bootstrap(args,env,runtime,record):
    deadline=time.monotonic()+25
    while True:
        try:
            with socket.create_connection(('127.0.0.1',27042),timeout=.5):pass
            break
        except OSError:
            if time.monotonic()>deadline:raise RuntimeError('GADGET_NOT_LISTENING')
            time.sleep(.25)
    with (runtime/'bootstrap.log').open('ab') as log:
        bridge=subprocess.Popen(args,env=env,cwd=runtime,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    record['bridge_pid']=bridge.pid
    save_journal(record)
    host=next(a.removeprefix('-receive_host=') for a in args if a.startswith('-receive_host='))
    deadline=time.monotonic()+15
    while True:
        if bridge.poll() is not None:raise RuntimeError('BOOTSTRAP_BRIDGE_START_FAILED')
        try:
            urllib.request.urlopen('http://'+host+'/notifier/status',timeout=.7).close()
        except urllib.error.HTTPError as exc:
            if exc.code==401:break
        except OSError:pass
        if time.monotonic()>deadline:raise RuntimeError('BOOTSTRAP_HTTP_NOT_READY')
        time.sleep(.25)


def resume():
    from run_native_bridge import prepare_runtime
    sys.path.insert(0,str(ROOT))
    from notifier.native import load_config
    record=json.loads(JOURNAL.read_text())
    if record.get('phase')!='awaiting_login':raise RuntimeError('BOOTSTRAP_NOT_PENDING')
    if process_uses(record.get('bridge_pid'),DATA/'native-runtime/bin/onebot'):
        print(json.dumps({'awaiting_login':True,'already_started':True}));return
    app=Path(load_config(DATA)['wechat_app'])
    installation=json.loads((DATA/'native-installation.json').read_text())
    if hashlib.file_digest((app/'Contents/MacOS/WeChat').open('rb'),'sha256').hexdigest()!=installation['candidate_main_sha256']:
        raise RuntimeError('INSTALLED_CLIENT_CHANGED')
    pids=execute(['/usr/bin/pgrep','-x','WeChat']).stdout.split()
    if len(pids)!=1:raise RuntimeError('WECHAT_PROCESS_AMBIGUOUS')
    record['wechat_pid']=int(pids[0]);save_journal(record)
    args,env,runtime=prepare_runtime(bootstrap=True)
    spawn_bootstrap(args,env,runtime,record)
    print(json.dumps({'awaiting_login':True,'resumed':True,'bootstrap_http_ready':True}))


def start():
    from run_native_bridge import prepare_runtime
    sys.path.insert(0,str(ROOT))
    from notifier.store import Store
    from notifier.native import load_config
    consent=json.loads((DATA/'native-consent.json').read_text())
    if not all(consent.get(k) is True for k in ('install_hook','read_group_directory','backup_account_data')):
        raise RuntimeError('DIRECTORY_BOOTSTRAP_APPROVAL_REQUIRED')
    if JOURNAL.exists():
        previous=json.loads(JOURNAL.read_text())
        if previous.get('phase')=='awaiting_login' and process_uses(previous.get('bridge_pid'),DATA/'native-runtime/bin/onebot'):
            print(json.dumps({'awaiting_login':True,'already_started':True}));return
    # Capture only from the current, strictly verified logged-in account. The
    # native HTTP guard will still require this exact account after the restart.
    args,env,runtime=prepare_runtime()
    account_id=next(a.removeprefix('-self_id=') for a in args if a.startswith('-self_id='))
    account_path=Path(next(a.removeprefix('-image_path=') for a in args if a.startswith('-image_path='))).parent
    app=Path(load_config(DATA)['wechat_app'])
    installation=json.loads((DATA/'native-installation.json').read_text())
    if hashlib.file_digest((app/'Contents/MacOS/WeChat').open('rb'),'sha256').hexdigest()!=installation['candidate_main_sha256']:
        raise RuntimeError('INSTALLED_CLIENT_CHANGED')
    store=Store(DATA)
    store.update_settings({'dispatch_enabled':False})
    with store.connect() as db:
        if db.execute("SELECT 1 FROM tasks WHERE status IN ('preparing','dispatching') LIMIT 1").fetchone():
            raise RuntimeError('NOTIFICATION_IN_PROGRESS')
    size_kb=int(execute(['/usr/bin/du','-sk',account_path],45).stdout.split()[0])
    if shutil.disk_usage(ROOT).free<size_kb*1024+5*1024**3:
        raise RuntimeError('BACKUP_DISK_SPACE_INSUFFICIENT')
    backup=DATA/'native-backups'/('directory-init-'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True,mode=0o700)
    from install_native_client import disable_auto_updates
    if consent.get('pin_auto_updates') is True:
        disable_auto_updates(backup)
    old_pid=int(next(a.removeprefix('-wechat_pid=') for a in args if a.startswith('-wechat_pid=')))
    record={'phase':'awaiting_login','wechat_pid':old_pid,'bridge_pid':0,'expected_account':account_id,
            'account_path':str(account_path),'started_at':time.time()}
    save_journal(record)
    service('stop')
    script='ObjC.import("AppKit"); var a=$.NSRunningApplication.runningApplicationsWithBundleIdentifier("com.tencent.xinWeChat"); if(Number(a.count)!==1) throw Error("ambiguous"); a.objectAtIndex(0).terminate;'
    if execute(['/usr/bin/osascript','-l','JavaScript','-e',script]).returncode:
        raise RuntimeError('WECHAT_NORMAL_QUIT_REQUEST_FAILED')
    deadline=time.monotonic()+15
    while execute(['/usr/bin/pgrep','-x','WeChat']).returncode==0:
        if time.monotonic()>deadline:raise RuntimeError('WECHAT_NORMAL_QUIT_REQUIRED')
        time.sleep(.5)
    if execute(['/usr/bin/ditto',account_path,backup/'account-data'],600).returncode:
        raise RuntimeError('ACCOUNT_BACKUP_FAILED')
    if execute(['/usr/bin/pgrep','-x','WeChat']).returncode==0:
        raise RuntimeError('WECHAT_REOPENED_DURING_BACKUP')
    verified=0
    for source in account_path.rglob('*'):
        if source.is_file() and not source.is_symlink() and source.name.endswith(('.db','.db-wal','.db-journal')):
            target=backup/'account-data'/source.relative_to(account_path)
            if not target.is_file() or hashlib.file_digest(source.open('rb'),'sha256').hexdigest()!=hashlib.file_digest(target.open('rb'),'sha256').hexdigest():
                raise RuntimeError('ACCOUNT_BACKUP_VERIFY_FAILED')
            verified+=1
    if execute(['/usr/bin/open','-g','-a',app]).returncode:
        raise RuntimeError('WECHAT_OPEN_FAILED')
    deadline=time.monotonic()+25
    while True:
        pids=execute(['/usr/bin/pgrep','-x','WeChat']).stdout.split()
        if len(pids)==1:
            pid=int(pids[0])
            files=execute(['/usr/sbin/lsof','-a','-p',str(pid),'-Fn']).stdout.splitlines()
            if 'n'+str(app/'Contents/MacOS/WeChat') in files and any(x.endswith('/wechat.dylib') for x in files):break
        if time.monotonic()>deadline:raise RuntimeError('WECHAT_STARTUP_NOT_READY')
        time.sleep(.5)
    args=[('-wechat_pid='+str(pid)) if a.startswith('-wechat_pid=') else a for a in args]
    record.update({'wechat_pid':pid,'backup_database_files_verified':verified});save_journal(record)
    spawn_bootstrap(args,env,runtime,record)
    print(json.dumps({'awaiting_login':True,'bridge_started_before_login':True,'bootstrap_http_ready':True,'backup_database_files_verified':verified}))


def finish():
    sys.path.insert(0,str(ROOT))
    from notifier.native import BridgeHTTP,load_config
    from run_native_bridge import prepare_runtime
    record=json.loads(JOURNAL.read_text())
    if record.get('phase')=='complete':
        print(json.dumps({'bootstrap_complete':True,'already_finished':True}));return
    args,_,_=prepare_runtime()
    if ('-wechat_pid='+str(record['wechat_pid'])) not in args or ('-self_id='+record['expected_account']) not in args:
        raise RuntimeError('BOOTSTRAP_ACCOUNT_OR_PROCESS_CHANGED')
    client=BridgeHTTP(load_config(DATA))
    status=client.request('GET','/notifier/status')['data']
    groups=client.request('GET','/notifier/groups')['data']
    if status['account_id']!=record['expected_account'] or groups['account_id']!=record['expected_account']:
        raise RuntimeError('BOOTSTRAP_ACCOUNT_CHANGED')
    if not (DATA/'native-runtime/cache/db-keys.json').is_file():
        raise RuntimeError('DIRECTORY_KEY_CACHE_NOT_READY')
    stop_temporary(record)
    service('start')
    record['phase']='complete';record['completed_at']=time.time();save_journal(record)
    print(json.dumps({'bootstrap_complete':True,'group_count':len(groups['groups']),'managed_bridge_started':True}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','resume','finish'])
    action=parser.parse_args().action
    if sys.platform!='darwin':parser.error('macOS is required')
    os.umask(0o077)
    try:
        {'start':start,'resume':resume,'finish':finish}[action]()
        return 0
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,RuntimeError) else 'DIRECTORY_BOOTSTRAP_FAILED'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
