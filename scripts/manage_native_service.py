"""Manage only this project's per-user native bridge LaunchAgent."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.maowangxian.wechat-native-bridge'
DOMAIN = 'gui/' + str(os.getuid())
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def launch(*args):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install','start','stop','status'])
    action = parser.parse_args().action
    if sys.platform != 'darwin':
        parser.error('macOS is required')
    if action == 'status':
        result = launch('print',DOMAIN+'/'+LABEL)
        print('原生桥服务已加载。' if result.returncode == 0 else '原生桥服务未加载。')
        return result.returncode
    if PLIST.exists():
        if PLIST.is_symlink() or plistlib.loads(PLIST.read_bytes()).get('WorkingDirectory') != str(ROOT):
            parser.error('同名服务来自其他目录，未修改。')
    if action == 'stop':
        result = launch('bootout',DOMAIN+'/'+LABEL)
        print('原生桥已停止。' if result.returncode == 0 else '原生桥未运行。')
        return 0
    consent = ROOT / '.state/native-consent.json'
    if not consent.exists() or json.loads(consent.read_text()).get('install_hook') is not True:
        parser.error('原生桥安装尚未得到用户明确确认。')
    if action == 'install':
        from run_native_bridge import prepare_runtime
        prepare_runtime()
        runtime = ROOT / '.state/native-runtime'
        data = {'Label':LABEL, 'ProgramArguments':[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/run_native_bridge.py')],
                'WorkingDirectory':str(ROOT), 'RunAtLoad':True, 'KeepAlive':{'SuccessfulExit':False},
                'ThrottleInterval':60, 'ExitTimeOut':35,
                'EnvironmentVariables':{'TZ':'Asia/Shanghai','PYTHONUNBUFFERED':'1'},
                'StandardOutPath':str(runtime/'service.log'), 'StandardErrorPath':str(runtime/'service.log')}
        PLIST.parent.mkdir(parents=True,exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(data)); PLIST.chmod(0o600)
    if not PLIST.exists():
        parser.error('请先安装原生桥服务。')
    if launch('print',DOMAIN+'/'+LABEL).returncode == 0:
        print('原生桥已运行，未启动第二个实例。')
        return 0
    result = launch('bootstrap',DOMAIN,str(PLIST))
    print('原生桥服务已启动。' if result.returncode == 0 else '原生桥启动失败，请检查本项目日志。')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
