"""Build this app's small native launcher with already-installed Apple tools."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = 'com.maowangxian.wechat-notifier'


def build(desktop=False):
    if sys.platform != 'darwin':
        raise RuntimeError('原生入口仅适用于 macOS')
    source = ROOT / 'scripts/macos/Launcher.swift'
    bundle = ROOT / '微信群通知.app'
    info_path = bundle / 'Contents/Info.plist'
    if bundle.exists() and (not info_path.exists()
            or plistlib.loads(info_path.read_bytes()).get('CFBundleIdentifier') != IDENTIFIER):
        raise RuntimeError('已有同名应用不属于本项目，未覆盖')
    info = {'CFBundleName': '微信群通知', 'CFBundleDisplayName': '微信群通知',
            'CFBundleIdentifier': IDENTIFIER, 'CFBundleExecutable': 'WeChatNotifier',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
            'CFBundleShortVersionString': '1.0.0', 'LSMinimumSystemVersion': '14.0',
            'LSUIElement': True, 'NSAppleEventsUsageDescription': '核对已绑定的个人微信群，并发送你提交的通知。',
            'NSHighResolutionCapable': True}
    config = {'project_root': str(ROOT)}
    fingerprint = hashlib.sha256(source.read_bytes() + Path(__file__).read_bytes()
                                 + json.dumps([info, config], sort_keys=True).encode()).hexdigest()
    marker = bundle / 'Contents/Resources/build.json'
    executable = bundle / 'Contents/MacOS/WeChatNotifier'
    built = (marker.exists() and json.loads(marker.read_text()).get('fingerprint') == fingerprint
             and executable.exists() and subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(bundle)],
                 capture_output=True).returncode == 0)
    if not built:
        developer = subprocess.check_output(['/usr/bin/xcode-select', '-p'], text=True).strip()
        compiler = Path(developer) / 'usr/bin/swiftc'
        if not compiler.is_file():
            raise RuntimeError('现有 Apple 开发工具不含 Swift；未安装或更改系统开发工具')
        cache = ROOT / '.state/build/module-cache'
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        executable.parent.mkdir(parents=True, exist_ok=True)
        marker.parent.mkdir(parents=True, exist_ok=True)
        info_path.write_bytes(plistlib.dumps(info))
        (marker.parent / 'launcher.json').write_text(json.dumps(config))
        sdk = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
        target = platform.machine() + '-apple-macosx14.0'
        subprocess.run([str(compiler), '-sdk', sdk, '-target', target, '-swift-version', '5', '-O', '-module-cache-path', str(cache),
                        str(source), '-o', str(executable), '-framework', 'Cocoa'], check=True)
        marker.write_text(json.dumps({'fingerprint': fingerprint}))
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', IDENTIFIER, str(bundle)], check=True)
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(bundle)], check=True)
    if desktop:
        shortcut = Path.home() / 'Desktop/微信群通知.app'
        if os.path.lexists(shortcut):
            if not shortcut.is_symlink() or shortcut.resolve() != bundle.resolve():
                raise RuntimeError('桌面已有其他同名项目，未覆盖；原生入口已保存在项目目录')
        else:
            shortcut.symlink_to(bundle, target_is_directory=True)
    return {'app': str(bundle), 'rebuilt': not built, 'desktop_shortcut': desktop}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desktop-shortcut', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.desktop_shortcut), ensure_ascii=False))
