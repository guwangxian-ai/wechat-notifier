"""Install the prepared client only after explicit approval, preserving the old app and local data."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
APP = Path('/Applications/WeChat.app')
STAGE = ROOT / '.state/native-staging'
OLD_MAIN = '0a6eb182d2ccf2bd005d778d2be8f3e675df3bfa7a7e9c904348e93d0ae8a10a'


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source,'sha256').hexdigest()


def verify(path):
    subprocess.run(['/usr/bin/codesign','--verify','--strict',str(path)],check=True,capture_output=True,timeout=30)


def disable_auto_updates(backup):
    """Pin only WeChat's own Sparkle settings; preserve the prior preferences."""
    consent_path=ROOT/'.state/native-consent.json'
    consent=json.loads(consent_path.read_text()) if consent_path.exists() else {}
    if consent.get('pin_auto_updates') is not True:
        raise RuntimeError('WECHAT_AUTO_UPDATE_PIN_APPROVAL_REQUIRED')
    preferences=Path.home()/'Library/Containers/com.tencent.xinWeChat/Data/Library/Preferences/com.tencent.xinWeChat.plist'
    domains=['com.tencent.xinWeChat']
    if preferences.is_file():
        saved=Path(backup)/'WeChat-preferences-before-update-pin.plist'
        if not saved.exists():
            shutil.copy2(preferences,saved);saved.chmod(0o600)
        domains.append(str(preferences.with_suffix('')))
    for domain in domains:
        for key in ('SUEnableAutomaticChecks','SUAutomaticallyUpdate'):
            result=subprocess.run(['/usr/bin/defaults','write',domain,key,'-bool','false'],capture_output=True,timeout=15)
            if result.returncode:raise RuntimeError('WECHAT_AUTO_UPDATE_PIN_FAILED')
            result=subprocess.run(['/usr/bin/defaults','read',domain,key],capture_output=True,text=True,timeout=15)
            if result.returncode or result.stdout.strip() not in ('0','false'):
                raise RuntimeError('WECHAT_AUTO_UPDATE_PIN_VERIFY_FAILED')
    return True


def check():
    plan = json.loads((STAGE/'install-plan.json').read_text())
    current = plistlib.loads((APP/'Contents/Info.plist').read_bytes())
    candidate = STAGE/'WeChat.app'
    info = plistlib.loads((candidate/'Contents/Info.plist').read_bytes())
    if (info.get('WeChatBundleVersion'),str(info.get('CFBundleVersion'))) != ('4.1.15.55','270135'):
        raise RuntimeError('CANDIDATE_VERSION_MISMATCH')
    if digest(candidate/'Contents/MacOS/WeChat') != plan['main_sha256']:
        raise RuntimeError('CANDIDATE_HASH_MISMATCH')
    if digest(APP/'Contents/MacOS/WeChat') != OLD_MAIN:
        raise RuntimeError('CURRENT_WECHAT_CHANGED')
    verify(APP); verify(candidate)
    return {'current_version':current.get('WeChatBundleVersion'),'current_build':current.get('CFBundleVersion'),
            'candidate_version':'4.1.15.55','candidate_build':'270135','candidate_signature_valid':True,
            'candidate_main_sha256':plan['main_sha256']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','install'])
    action=parser.parse_args().action
    if sys.platform!='darwin':parser.error('macOS is required')
    os.umask(0o077)
    report=check()
    if action=='check':print(json.dumps(report));return
    consent_path=ROOT/'.state/native-consent.json'
    approved=json.loads(consent_path.read_text()) if consent_path.exists() else {}
    if approved.get('install_hook') is not True or approved.get('backup_account_data') is not True:
        raise RuntimeError('NATIVE_INSTALL_APPROVAL_REQUIRED')
    sys.path.insert(0,str(ROOT))
    from notifier.store import Store
    from run_native_bridge import active_account
    store=Store(ROOT/'.state');store.update_settings({'dispatch_enabled':False})
    with store.connect() as db:
        if db.execute("SELECT 1 FROM tasks WHERE status IN ('preparing','dispatching') LIMIT 1").fetchone():
            raise RuntimeError('NOTIFICATION_IN_PROGRESS')
    pids=subprocess.run(['/usr/bin/pgrep','-x','WeChat'],capture_output=True,text=True).stdout.split()
    journal=ROOT/'.state/native-install-account.json'
    if len(pids)==1:
        account,_=active_account(int(pids[0]))
        journal.write_text(json.dumps({'account_path':str(account)}));journal.chmod(0o600)
    elif not pids and journal.exists():
        account=Path(json.loads(journal.read_text())['account_path'])
        base=Path.home()/'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
        if account.parent!=base or not (account/'db_storage/contact/contact.db').is_file():
            raise RuntimeError('BACKUP_ACCOUNT_JOURNAL_INVALID')
    else:raise RuntimeError('WECHAT_PROCESS_AMBIGUOUS')
    size_kb=int(subprocess.check_output(['/usr/bin/du','-sk',str(account)],stderr=subprocess.PIPE,text=True,timeout=45).split()[0])
    if size_kb*1024 > shutil.disk_usage(ROOT).free-5*1024**3:
        raise RuntimeError('ACCOUNT_BACKUP_DISK_SPACE_INSUFFICIENT')
    # Normal termination only; no keystrokes, activation, force kill or draft edits.
    script='ObjC.import("AppKit"); var a=$.NSRunningApplication.runningApplicationsWithBundleIdentifier("com.tencent.xinWeChat"); if(Number(a.count)!==1) throw Error("ambiguous"); a.objectAtIndex(0).terminate;'
    if pids:
        result=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',script],capture_output=True,timeout=15)
        if result.returncode:raise RuntimeError('WECHAT_NORMAL_QUIT_REQUEST_FAILED')
    deadline=time.monotonic()+12
    while subprocess.run(['/usr/bin/pgrep','-x','WeChat'],capture_output=True).returncode==0:
        if time.monotonic()>deadline:raise RuntimeError('WECHAT_NORMAL_QUIT_REQUIRED')
        time.sleep(.5)
    if digest(APP/'Contents/MacOS/WeChat')!=OLD_MAIN:raise RuntimeError('CURRENT_WECHAT_CHANGED')
    backup=ROOT/'.state/native-backups'/datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True,mode=0o700)
    print(json.dumps({'stage':'account_backup','allocated_bytes':size_kb*1024}),flush=True)
    copied=subprocess.run(['/usr/bin/ditto',str(account),str(backup/'account-data')],capture_output=True,timeout=600)
    if copied.returncode:raise RuntimeError('ACCOUNT_BACKUP_COPY_FAILED')
    if subprocess.run(['/usr/bin/pgrep','-x','WeChat'],capture_output=True).returncode==0:
        raise RuntimeError('WECHAT_REOPENED_DURING_BACKUP')
    verified=0
    for database in account.rglob('*.db'):
        if database.is_file() and not database.is_symlink():
            saved=backup/'account-data'/database.relative_to(account)
            if not saved.is_file() or digest(database)!=digest(saved):
                raise RuntimeError('ACCOUNT_BACKUP_VERIFY_FAILED')
            verified+=1
    print(json.dumps({'stage':'account_backup_verified','databases':verified}),flush=True)
    update_pin_applied=False
    if approved.get('pin_auto_updates') is True:
        update_pin_applied=disable_auto_updates(backup)
    # App Store apps can be root-owned. Use a standard user installation without
    # changing that original or requesting additional administrator privileges.
    target=APP if os.access(APP,os.W_OK) else Path.home()/'Applications/WeChat.app'
    target.parent.mkdir(exist_ok=True)
    if target!=APP and (target.exists() or target.is_symlink()):
        raise RuntimeError('USER_WECHAT_PATH_ALREADY_EXISTS')
    if target.parent.stat().st_dev!=backup.stat().st_dev or target.parent.stat().st_dev!=STAGE.stat().st_dev:
        raise RuntimeError('ATOMIC_INSTALL_FILESYSTEM_MISMATCH')
    if target==APP:
        os.rename(APP,backup/'WeChat-original.app')
    else:
        original=subprocess.run(['/usr/bin/ditto',str(APP),str(backup/'WeChat-original.app')],capture_output=True,timeout=180)
        if original.returncode:raise RuntimeError('ORIGINAL_APP_BACKUP_FAILED')
        verify(backup/'WeChat-original.app')
    try:
        os.rename(STAGE/'WeChat.app',target)
        verify(target)
    except Exception:
        if target.exists():os.rename(target,STAGE/'WeChat.app')
        if target==APP:os.rename(backup/'WeChat-original.app',APP)
        raise
    from notifier.native import load_config,save_config
    config=load_config(ROOT/'.state')
    save_config(ROOT/'.state',config['base_url'],wechat_app=str(target))
    shortcut=Path.home()/'Desktop/微信（通知专用）.app'
    if not shortcut.exists() and not shortcut.is_symlink():shortcut.symlink_to(target)
    report.update({'installed':True,'backup':str(backup),'account_data_backed_up':True,
                   'installed_path':str(target),'system_original_preserved':target!=APP,
                   'auto_update_pin_applied':update_pin_applied})
    report['backup_databases_verified']=verified
    (ROOT/'.state/native-installation.json').write_text(json.dumps(report,indent=2))
    subprocess.run(['/usr/bin/open','-g','-a',str(target)],check=True,timeout=15)
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()
