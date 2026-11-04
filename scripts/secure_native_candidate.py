"""Generate Gadget authentication on the Mac mini, keeping its secret on that host."""
import hashlib
import json
from pathlib import Path
import plistlib
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / '.state/native-staging'


def main():
    app = STAGE/'WeChat.app'
    plan = json.loads((STAGE/'install-plan.json').read_text())
    info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if (info.get('WeChatBundleVersion'),str(info.get('CFBundleVersion'))) != ('4.1.15.55','270135'):
        raise RuntimeError('CANDIDATE_VERSION_MISMATCH')
    config = app/'Contents/Resources/FridaGadget.config'
    data = json.loads(config.read_text())
    data['interaction'].update({'address':'127.0.0.1','port':27042,'on_load':'resume',
                                'token':data['interaction'].get('token') or secrets.token_urlsafe(32)})
    config.write_text(json.dumps(data)); config.chmod(0o600)
    result = subprocess.run(['/usr/bin/codesign','-d','--entitlements',':-',str(app)],
                            capture_output=True,check=True,timeout=15)
    plistlib.loads(result.stdout)
    entitlements = STAGE/'candidate-entitlements.plist'
    entitlements.write_bytes(result.stdout); entitlements.chmod(0o600)
    subprocess.run(['/usr/bin/codesign','-f','-s','-','--timestamp=none','--entitlements',str(entitlements),str(app)],
                   capture_output=True,check=True,timeout=30)
    subprocess.run(['/usr/bin/codesign','--verify','--strict',str(app)],capture_output=True,check=True,timeout=30)
    with (app/'Contents/MacOS/WeChat').open('rb') as source:
        plan['main_sha256']=hashlib.file_digest(source,'sha256').hexdigest()
    plan['gadget_authentication']=True
    plan['secret_host']='Mac mini only'
    (STAGE/'install-plan.json').write_text(json.dumps(plan,indent=2))
    print(json.dumps({'candidate_ready':True,'gadget_authenticated':True,'signature_valid':True}))


if __name__=='__main__':
    main()
