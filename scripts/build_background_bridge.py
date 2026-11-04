"""Build the background helper without changing the app bundle/TCC identity."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def build():
    if sys.platform != 'darwin':
        raise RuntimeError('后台执行器仅支持 macOS')
    source = ROOT / 'notifier/macos/background.m'
    directory = ROOT / '.state/bin'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    executable = directory / 'background-bridge'
    marker = directory / 'background-build.json'
    digest = hashlib.sha256(source.read_bytes() + Path(__file__).read_bytes()).hexdigest()
    if executable.is_file() and marker.is_file():
        old = json.loads(marker.read_text())
        if old.get('source') == digest and old.get('binary') == hashlib.sha256(executable.read_bytes()).hexdigest():
            return {'rebuilt': False}
    sdk = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='background-build-', dir=directory) as temporary:
        output = Path(temporary) / 'background-bridge'
        subprocess.run(['/usr/bin/clang', '-fobjc-arc', '-O2', '-Wall', '-Wextra',
                        '-isysroot', sdk, '-mmacosx-version-min=14.0', '-framework', 'AppKit',
                        '-framework', 'ApplicationServices', '-framework', 'ScreenCaptureKit',
                        '-framework', 'Vision', '-framework', 'CoreML', str(source), '-o', str(output)], check=True)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(output)], check=True, capture_output=True)
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(output)], check=True)
        binary_digest = hashlib.sha256(output.read_bytes()).hexdigest()
        os.chmod(output, 0o700)
        os.replace(output, executable)
        marker.write_text(json.dumps({'source': digest, 'binary': binary_digest}))
        os.chmod(marker, 0o600)
    return {'rebuilt': True}


if __name__ == '__main__':
    print(json.dumps(build()))
