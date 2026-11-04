"""Package the reviewed GPL bridge separately from notification data and credentials."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '.state/upstream/WeChat-ChatBot-Mac'
COMMIT = '9094de7859618a8124f9d3162e63c87b0c8c837a'
FILES = {'bin/onebot':SOURCE/'bridge/onebot', 'script.js':SOURCE/'bridge/script.js',
         'LICENSE':SOURCE/'bridge/LICENSE', 'NOTICE':SOURCE/'bridge/NOTICE'}
for version in ('4_1_15_55','4_2_0_5','4_2_0_9','4_2_0_12'):
    FILES['profiles/'+version+'_mac.json'] = SOURCE/'hook/versions'/(version+'_mac.json')


def package():
    manifest = {'upstream_commit':COMMIT,'files':{}}
    output = ROOT / 'artifacts/native-bridge-runtime.tar.gz'
    with tarfile.open(output, 'w:gz') as archive:
        for name,path in FILES.items():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Missing regular build artifact: '+name)
            manifest['files'][name] = hashlib.file_digest(path.open('rb'),'sha256').hexdigest()
            archive.add(path,arcname=name,recursive=False)
        data = json.dumps(manifest,sort_keys=True).encode()
        item = tarfile.TarInfo('manifest.json'); item.size=len(data); item.mode=0o600
        archive.addfile(item,io.BytesIO(data))
    # Keep the exact modified corresponding source next to the runtime package.
    # Runtime source tree contains no account configuration or keys.
    source_output = ROOT / 'artifacts/native-bridge-source.tar.gz'
    with tarfile.open(source_output,'w:gz') as archive:
        tracked = subprocess.check_output(['git','-C',str(SOURCE),'ls-files','bridge'],text=True).splitlines()
        for name in tracked + ['bridge/notifier.go','bridge/notifier_test.go']:
            path = SOURCE / name
            if path.is_file() and not path.is_symlink():
                archive.add(path,arcname=name,recursive=False)
        archive.add(ROOT/'integrations/gusion/upstream.patch',arcname='upstream.patch')
        archive.add(SOURCE/'notifier-adapter-manifest.json',arcname='notifier-adapter-manifest.json')
    print(json.dumps({'runtime':str(output),'bytes':output.stat().st_size,
                      'sha256':hashlib.file_digest(output.open('rb'),'sha256').hexdigest(),
                      'source':str(source_output),'source_bytes':source_output.stat().st_size}))


if __name__ == '__main__':
    package()
