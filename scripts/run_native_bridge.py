"""Run the reviewed Gusion bridge with private local configuration and strict version checks."""
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from notifier.native import SUPPORTED, UPSTREAM_COMMIT, load_config


def account_identifier(directory):
    account = Path(directory).name
    if account.count('_') >= 2:
        account = account.rsplit('_', 1)[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', account):
        raise RuntimeError('ACTIVE_WECHAT_ACCOUNT_INVALID')
    return account


def active_account(pid, expected_app=None):
    result = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-Fn'],
                            capture_output=True, text=True, timeout=10)
    if expected_app is not None and ('n' + str(Path(expected_app) / 'Contents/MacOS/WeChat')) not in result.stdout.splitlines():
        raise RuntimeError('WECHAT_OTHER_INSTALLATION_RUNNING')
    prefix = str(Path.home() / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files') + '/'
    paths = {line[1:].split('/db_storage/')[0] for line in result.stdout.splitlines()
             if line.startswith('n' + prefix) and '/db_storage/contact/contact.db' in line
             and line.endswith('/contact.db')}
    if not paths:
        raise RuntimeError('ACTIVE_WECHAT_ACCOUNT_NOT_READY')
    if len(paths) != 1:
        raise RuntimeError('ACTIVE_WECHAT_ACCOUNT_AMBIGUOUS')
    directory = Path(paths.pop())
    return directory, account_identifier(directory)


def bootstrap_account(data, pid):
    """Use a previously verified account only for an explicit local login bootstrap.

    The HTTP bridge separately rejects status/directory/send until lsof proves
    that this exact account is actually open in the new WeChat process.
    """
    consent=json.loads((data/'native-consent.json').read_text())
    if not all(consent.get(k) is True for k in ('install_hook','read_group_directory','backup_account_data')):
        raise RuntimeError('DIRECTORY_BOOTSTRAP_APPROVAL_REQUIRED')
    journal=data/'native-directory-bootstrap.json'
    if journal.is_symlink() or journal.stat().st_mode & 0o077:
        raise RuntimeError('DIRECTORY_BOOTSTRAP_JOURNAL_INVALID')
    record=json.loads(journal.read_text())
    if record.get('phase')!='awaiting_login' or record.get('wechat_pid')!=pid:
        raise RuntimeError('DIRECTORY_BOOTSTRAP_PROCESS_CHANGED')
    if record.get('account_path'):
        directory=Path(record['account_path'])
    else:
        directory=Path(json.loads((data/'native-install-account.json').read_text())['account_path'])
    base=Path.home()/'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
    contact=directory/'db_storage/contact/contact.db'
    if directory.parent!=base or directory.is_symlink() or not contact.is_file() or contact.is_symlink():
        raise RuntimeError('DIRECTORY_BOOTSTRAP_ACCOUNT_INVALID')
    account=account_identifier(directory)
    if record.get('expected_account')!=account:
        raise RuntimeError('DIRECTORY_BOOTSTRAP_ACCOUNT_CHANGED')
    return directory,account


def prepare_runtime(root=ROOT, bootstrap=False):
    data = root / '.state'
    runtime = data / 'native-runtime'
    manifest = json.loads((runtime / 'manifest.json').read_text())
    if manifest['upstream_commit'] != UPSTREAM_COMMIT:
        raise RuntimeError('NATIVE_SOURCE_MISMATCH')
    import hashlib
    for name, digest in manifest['files'].items():
        path = runtime / name
        if path.is_symlink() or not path.is_file() or hashlib.file_digest(path.open('rb'), 'sha256').hexdigest() != digest:
            raise RuntimeError('NATIVE_FILE_MISMATCH')
    config = load_config(data)
    if not config['token']:
        raise RuntimeError('NATIVE_TOKEN_REQUIRED')
    app = Path(config['wechat_app'])
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    version, build = str(info.get('WeChatBundleVersion', '')), str(info.get('CFBundleVersion', ''))
    if SUPPORTED.get(version) != build:
        raise RuntimeError('WECHAT_VERSION_UNSUPPORTED')
    pids = subprocess.check_output(['/usr/bin/pgrep', '-x', 'WeChat'], text=True).split()
    if len(pids) != 1:
        raise RuntimeError('WECHAT_PROCESS_AMBIGUOUS')
    pid = int(pids[0])
    loads = subprocess.check_output(['/usr/bin/otool', '-L', str(app/'Contents/MacOS/WeChat')], text=True)
    if '@executable_path/../Frameworks/FridaGadget.dylib' not in loads:
        raise RuntimeError('WECHAT_GADGET_REQUIRED')
    gadget = json.loads((app/'Contents/Resources/FridaGadget.config').read_text())['interaction']
    if (gadget.get('address') != '127.0.0.1' or gadget.get('port') != 27042
            or not isinstance(gadget.get('token'), str) or len(gadget['token']) < 24):
        raise RuntimeError('WECHAT_GADGET_AUTH_REQUIRED')
    consent = data / 'native-consent.json'
    approved = json.loads(consent.read_text()) if consent.exists() else {}
    if not approved.get('install_hook'):
        raise RuntimeError('NATIVE_INSTALL_APPROVAL_REQUIRED')
    try:
        account_path, account_id = active_account(pid, app)
    except RuntimeError as exc:
        if not bootstrap or str(exc)!='ACTIVE_WECHAT_ACCOUNT_NOT_READY':raise
        account_path, account_id = bootstrap_account(data,pid)
    if bootstrap:
        expected_path,expected_id=bootstrap_account(data,pid)
        if (account_path,account_id)!=(expected_path,expected_id):
            raise RuntimeError('DIRECTORY_BOOTSTRAP_ACCOUNT_CHANGED')
    directory_enabled = approved.get('read_group_directory') is True
    profile = json.loads((runtime / 'profiles' / (version.replace('.', '_') + '_mac.json')).read_text())
    profile.update({'enableSend':'true', 'req2bufSendMode':'r3', 'r3ClosureCapture':'true',
                    'r3Slot13Capture':'true', 'r3QueueUntilSeed':'false', 'r3VoiceSend':'false',
                    'contactDirectory':str(directory_enabled).lower(),
                    'notifierVersion':version, 'notifierBuild':build})
    config_path = runtime / 'active-profile.json'
    config_path.write_text(json.dumps(profile))
    config_path.chmod(0o600)
    cache = runtime / 'cache'
    cache.mkdir(mode=0o700, exist_ok=True)
    args = [str(runtime / 'bin/onebot'), '-type=gadget', '-gadget_addr=127.0.0.1:27042',
            '-receive_host=' + config['base_url'].removeprefix('http://'),
            '-wechat_conf=' + str(config_path), '-wechat_pid=' + str(pid), '-self_id=' + account_id,
            '-image_path=' + str(account_path / 'temp'), '-media_dir=' + str(runtime / 'unused-media'),
            '-conn_type=websocket', '-log_level=error', '-log_file=' + str(runtime / 'bridge.log')]
    env = {**os.environ, 'WECHAT_NOTIFIER_ONLY':'1', 'WECHAT_NOTIFIER_TOKEN':config['token'],
           'WECHAT_NOTIFIER_GADGET_TOKEN':gadget['token'],
           'WECHAT_NOTIFIER_DIRECTORY':'1' if directory_enabled else '0',
           'WECHAT_NOTIFIER_CACHE_DIR':str(cache), 'HOOK_SEND_DISABLED':'false'}
    return args, env, runtime


def main():
    os.umask(0o077)
    try:
        args, env, runtime = prepare_runtime()
    except Exception as exc:
        # Never return file contents, account paths, credentials or Frida errors.
        print(str(exc) if isinstance(exc, RuntimeError) else 'NATIVE_STARTUP_CHECK_FAILED', file=sys.stderr)
        return 1
    os.chdir(runtime)
    os.execve(args[0], args, env)


if __name__ == '__main__':
    raise SystemExit(main())
