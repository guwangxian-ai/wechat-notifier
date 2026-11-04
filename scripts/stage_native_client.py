"""Patch only a project-owned copy of the verified official client; never launch or install it."""
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / '.state/native-staging'
APP = STAGE / 'WeChat.app'
SOURCE = ROOT / '.state/upstream/WeChat-ChatBot-Mac'


def run(args):
    subprocess.run([str(a) for a in args],check=True,capture_output=True,timeout=60)


def main():
    if APP.is_symlink() or not APP.is_dir() or APP.resolve().parent != STAGE.resolve():
        raise RuntimeError('Invalid staging directory')
    info = plistlib.loads((APP/'Contents/Info.plist').read_bytes())
    if (info.get('WeChatBundleVersion'), str(info.get('CFBundleVersion'))) != ('4.1.15.55','270135'):
        raise RuntimeError('Unsupported staging client')
    entitlements = STAGE / 'original-entitlements.plist'
    if not entitlements.exists():
        result = subprocess.run(['/usr/bin/codesign','-d','--entitlements',':-',str(APP)],
                                check=True,capture_output=True,timeout=15)
        plistlib.loads(result.stdout)
        entitlements.write_bytes(result.stdout); entitlements.chmod(0o600)
    gadget = SOURCE / 'hook/gadget/FridaGadget.dylib'
    target = APP / 'Contents/Frameworks/FridaGadget.dylib'
    target.parent.mkdir(exist_ok=True)
    shutil.copy2(gadget,target)
    target.chmod(0o700)
    config = APP / 'Contents/Resources/FridaGadget.config'
    shutil.copy2(SOURCE/'hook/gadget/FridaGadget.config',config)
    link = APP/'Contents/Frameworks/FridaGadget.config'
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.readlink() != Path('../Resources/FridaGadget.config'):
            raise RuntimeError('Unexpected gadget config')
    else:
        link.symlink_to('../Resources/FridaGadget.config')
    run(['/usr/bin/codesign','-f','-s','-','--timestamp=none',target])
    run(['/usr/bin/python3',SOURCE/'tools/inject_load_dylib.py',APP/'Contents/MacOS/WeChat',
         '@executable_path/../Frameworks/FridaGadget.dylib'])
    run(['/usr/bin/codesign','-f','-s','-','--timestamp=none','--entitlements',entitlements,APP])
    run(['/usr/bin/codesign','--verify','--strict',APP])
    plan = {'version':'4.1.15.55','build':'270135',
            'official_url':'https://dldir1v6.qq.com/weixin/Universal/Mac/WeChatMac_4.1.15.dmg',
            'official_dmg_sha256':'ceea355d5c93419658f777fdd1010f9b19d14fab6dc552af9c208cddad7f248f',
            'candidate_signature':'ad-hoc with original entitlements','candidate_signature_valid':True,
            'main_sha256':hashlib.file_digest((APP/'Contents/MacOS/WeChat').open('rb'),'sha256').hexdigest()}
    archive = STAGE/'native-client.zip'
    run(['/usr/bin/ditto','-c','-k','--sequesterRsrc','--keepParent',APP,archive])
    plan.update({'archive':str(archive),'archive_bytes':archive.stat().st_size,
                 'archive_sha256':hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()})
    (STAGE/'install-plan.json').write_text(json.dumps(plan,indent=2))
    print(json.dumps(plan,ensure_ascii=False))


if __name__ == '__main__':
    main()
