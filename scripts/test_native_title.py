"""Compile/run synthetic title identity tests without capturing the desktop."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sdk = subprocess.check_output(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
with tempfile.TemporaryDirectory(prefix='wechat-title-test-') as directory:
    output = str(Path(directory) / 'title-test')
    command = ['/usr/bin/clang', '-fobjc-arc', '-O2', '-Wall', '-Wextra', '-isysroot', sdk,
               '-mmacosx-version-min=14.0']
    for framework in ('AppKit', 'ApplicationServices', 'ScreenCaptureKit', 'Vision', 'CoreML'):
        command += ['-framework', framework]
    subprocess.run(command + [str(ROOT / 'tests/native_title_match.m'), '-o', output], check=True)
    subprocess.run([output], check=True)
