"""Create a code-and-wheel-only deployment archive, excluding all runtime data."""
import hashlib
import io
import json
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parents[1]
files = []
for directory in ['notifier', 'scripts', 'tests', 'integrations']:
    files.extend(p for p in (root / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
for name in ['README.md', 'LICENSE', 'THIRD_PARTY.md', 'requirements.txt', 'requirements-dev.txt', 'requirements.lock.txt', '打开通知台.command', '.gitignore']:
    files.append(root / name)
wheels = list((root / 'artifacts' / 'wheels').glob('*.whl'))
if not wheels:
    raise SystemExit('请先准备锁定依赖的 wheel')
manifest = {}
target = root / 'artifacts' / 'wechat-notifier-deploy.tar.gz'
with tarfile.open(target, 'w:gz') as archive:
    for path in sorted(files) + sorted(wheels):
        name = 'wheels/' + path.name if path in wheels else str(path.relative_to(root))
        if path.is_symlink():
            raise RuntimeError('部署包不接受符号链接')
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        archive.add(path, arcname=name, recursive=False)
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    entry = tarfile.TarInfo('bundle-manifest.json')
    entry.size, entry.mode = len(payload), 0o600
    archive.addfile(entry, io.BytesIO(payload))
print(json.dumps({'archive': str(target), 'files': len(manifest), 'bytes': target.stat().st_size,
                  'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))
