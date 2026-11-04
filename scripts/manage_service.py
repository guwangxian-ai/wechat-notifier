"""Manage only this app's per-user LaunchAgent; retain all runtime data."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.maowangxian.wechat-notifier'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
DOMAIN = 'gui/' + str(os.getuid())


def run(*args):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'start', 'stop', 'status'])
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('LaunchAgent 仅支持 macOS')
    if args.action == 'status':
        result = run('print', DOMAIN + '/' + LABEL)
        print(result.stdout or result.stderr)
        return result.returncode
    if args.action == 'stop':
        result = run('bootout', DOMAIN + '/' + LABEL)
        if result.returncode == 0:
            deadline = time.monotonic() + 55
            while run('print', DOMAIN + '/' + LABEL).returncode == 0:
                if time.monotonic() >= deadline:
                    print('服务仍在退出，请检查状态；未启动第二个进程。')
                    return 1
                time.sleep(.2)
        print('服务已停止，数据与配置保留。' if result.returncode == 0 else result.stderr.strip())
        return result.returncode
    if args.action == 'install':
        data = ROOT / '.state'
        data.mkdir(mode=0o700, exist_ok=True)
        python = ROOT / '.venv' / 'bin' / 'python'
        if not python.exists():
            parser.error('请先在项目 .venv 安装依赖')
        if PLIST.exists():
            existing = plistlib.loads(PLIST.read_bytes())
            if existing.get('WorkingDirectory') != str(ROOT):
                parser.error('同名服务来自其他目录，未覆盖')
        info = {'Label': LABEL, 'ProgramArguments': [str(python), '-m', 'notifier', '--data-dir', str(data)],
                'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True,
                'ThrottleInterval': 15, 'EnvironmentVariables': {'TZ': 'Asia/Shanghai', 'PYTHONUNBUFFERED': '1'},
                'StandardOutPath': str(data / 'service.log'), 'StandardErrorPath': str(data / 'service.log')}
        native = ROOT / '微信群通知.app/Contents/MacOS/WeChatNotifier'
        if native.is_file():
            info['ProgramArguments'] = [str(native), '--background']
            info['ExitTimeOut'] = 50
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        if PLIST.is_symlink():
            parser.error('服务文件不能是符号链接')
        PLIST.write_bytes(plistlib.dumps(info))
        os.chmod(PLIST, 0o600)
    if not PLIST.exists():
        parser.error('尚未安装服务，请先运行 install')
    if run('print', DOMAIN + '/' + LABEL).returncode == 0:
        print('服务已经加载；更新代码后先 stop 再 start。')
        return 0
    result = run('bootstrap', DOMAIN, str(PLIST))
    print('服务已启动。' if result.returncode == 0 else result.stderr.strip())
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
