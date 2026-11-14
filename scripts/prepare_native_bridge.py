"""Apply reviewed, reproducible notification-only changes to a pinned upstream checkout."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMMIT = '9094de7859618a8124f9d3162e63c87b0c8c837a'
ADAPTER = ROOT / 'integrations/gusion'

PATCHES = {
    'bridge/script.js': [
        ('        setImmediate(setupSendImgMessageDynamic);\n        setImmediate(r3Init);',
         '        setImmediate(r3Init);\n        setImmediate(contactSetupHooks);\n        return; // Notification-only: text R3 + local target lookup; no incoming/media hooks.'),
        ('SELECT username, nick_name, remark, alias, delete_flag FROM contact;',
         "SELECT username, nick_name, remark, alias, delete_flag FROM contact WHERE username LIKE '%@chatroom';"),
        ('SELECT username, hex(ext_buffer) FROM chat_room WHERE username IS NOT NULL;',
         "SELECT username, '' FROM chat_room WHERE 0;"),
        ('function getContactDirectory(dbStoragePath) {',
         (ADAPTER / 'private_contacts.js').read_text() + '\nfunction getContactDirectory(dbStoragePath, query, exact) {'),
        ('            directory = contactQueryDirectory(tmpPath);',
         '            if (typeof query === "string") {\n'
         '                return JSON.stringify({ ok: true, contacts: contactQueryPeople(tmpPath, query, exact) });\n'
         '            }\n'
         '            directory = contactQueryDirectory(tmpPath);'),
        ('        console.log("[!] [contact] Hook 安装失败: " + e);',
         '        contactState.lastError = String(e);\n        console.log("[!] [contact] Hook 安装失败: " + e);'),
        ('function contactSetupHooks() {',
         (ADAPTER/'contact_compat.js').read_text()+'\nfunction contactSetupHooks() {'),
        ('Interceptor.attach(create, {', 'contactAttach(create, {'),
        ('Interceptor.attach(createMode, {', 'contactAttach(createMode, {'),
        ('Interceptor.attach(crypt, {', 'contactAttach(crypt, {'),
        ('Interceptor.attach(pbkdf2, {', 'contactAttach(pbkdf2, {'),
        ('        contactState.installed = true;\n        console.log("[+] [contact]',
         '        contactState.installed = (contactState.hookCount || 0) > 0;\n        if (!contactState.installed) return;\n        console.log("[+] [contact]'),
        ('            installed: contactState.installed,',
         '            installed: contactState.installed,\n            file_api: typeof File !== "undefined",\n            sqlite_api: typeof SqliteDatabase !== "undefined",\n            crypto_api: !!contactFindExport("CCCrypt"),\n            setup_error: contactState.lastError ? (String(contactState.lastError).indexOf("unable to intercept") !== -1 ? "hook_attach_failed" : "runtime_error") : "",'),
    ],
    'bridge/main.go': [
        ('device, err = mgr.AddRemoteDevice(config.FridaGadgetAddr, frida.NewRemoteDeviceOptions())',
         'remoteOptions := frida.NewRemoteDeviceOptions()\n\tif token := os.Getenv("WECHAT_NOTIFIER_GADGET_TOKEN"); token != "" { remoteOptions.SetToken(token) }\n\tdevice, err = mgr.AddRemoteDevice(config.FridaGadgetAddr, remoteOptions)'),
        ('\t_ "net/http/pprof"\n', ''),
        ('\thttp.HandleFunc("/send_private_msg", sendHandler)',
         '\tif os.Getenv("WECHAT_NOTIFIER_ONLY") == "1" {\n\t\tregisterNotifierRoutes()\n\t} else {\n\thttp.HandleFunc("/send_private_msg", sendHandler)'),
        ('\thttp.HandleFunc("/test_ws", testWebSocket)', '\thttp.HandleFunc("/test_ws", testWebSocket)\n\t}'),
        ('http.ListenAndServe(config.ReceiveHost, nil)',
         '(&http.Server{Addr: config.ReceiveHost, ReadHeaderTimeout: 5*time.Second, ReadTimeout: 10*time.Second, WriteTimeout: 35*time.Second, IdleTimeout: 30*time.Second, MaxHeaderBytes: 16384}).ListenAndServe()'),
        ('\tflag.Parse()\n', '\tflag.Parse()\n\tif token := os.Getenv("WECHAT_NOTIFIER_TOKEN"); token != "" { config.OnebotToken = token }\n'),
        ('fmt.Println("OnebotToken", config.OnebotToken)', 'fmt.Println("OnebotToken", "[redacted]")'),
        ('\t\tmsgType := str(msg, "type")\n',
         '\t\tmsgType := str(msg, "type")\n\t\tif os.Getenv("WECHAT_NOTIFIER_ONLY") == "1" {\n\t\t\tif msgType != "send" { return }\n\t\t\tif p, ok := msg["payload"].(map[string]interface{}); ok {\n\t\t\t\tswitch str(p,"type") { case "r3_seed", "r3_mode": default: return }\n\t\t\t} else { return }\n\t\t}\n'),
        ('\trestoreContactDBKeys()\n', '\tif os.Getenv("WECHAT_NOTIFIER_DIRECTORY") == "1" { restoreContactDBKeys() }\n'),
    ],
    'bridge/r3seed.go': [
        ('func r3SeedFile() string {', 'func r3SeedFile() string {\n\tif base := os.Getenv("WECHAT_NOTIFIER_CACHE_DIR"); base != "" {\n\t\t_ = os.MkdirAll(base, 0o700)\n\t\treturn filepath.Join(base,"r3-seed.json")\n\t}'),
    ],
    'bridge/contact.go': [
        ('func defaultContactDBKeysFilePath() string {', 'func defaultContactDBKeysFilePath() string {\n\tif base := os.Getenv("WECHAT_NOTIFIER_CACHE_DIR"); base != "" { return filepath.Join(base,"db-keys.json") }'),
    ],
    'bridge/logger.go': [
        ('MaxSize:    100,', 'MaxSize:    2,'),
        ('MaxBackups: 10,', 'MaxBackups: 2,'),
    ],
}


def prepare(source):
    source = source.resolve()
    head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if head != COMMIT:
        raise RuntimeError('Upstream commit does not match the reviewed release')
    patch, manifest = [], {'upstream_commit': COMMIT, 'files': {}}
    previous_path = source / 'notifier-adapter-manifest.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    for name, edits in PATCHES.items():
        original = subprocess.check_output(['git', '-C', str(source), 'show', COMMIT + ':' + name]).decode()
        modified = original
        for old, new in edits:
            if modified.count(old) != 1:
                raise RuntimeError('Patch context mismatch: ' + name)
            modified = modified.replace(old, new, 1)
        path = source / name
        if path.read_text() not in (original, modified):
            if hashlib.sha256(path.read_bytes()).hexdigest() != previous.get('files', {}).get(name):
                raise RuntimeError('Preserving unexpected upstream changes: ' + name)
        path.write_text(modified)
        manifest['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        patch.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                                         fromfile='a/' + name, tofile='b/' + name))
    for name in ('notifier.go', 'notifier_test.go'):
        data = (ADAPTER / name).read_bytes()
        path = source / 'bridge' / name
        if path.exists() and path.read_bytes() != data:
            if hashlib.sha256(path.read_bytes()).hexdigest() != previous.get('files', {}).get('bridge/' + name):
                raise RuntimeError('Preserving unexpected adapter changes: ' + name)
        path.write_bytes(data)
        manifest['files']['bridge/' + name] = hashlib.sha256(data).hexdigest()
    (source / 'notifier-adapter-manifest.json').write_text(json.dumps(manifest, indent=2))
    (ADAPTER / 'upstream.patch').write_text(''.join(patch))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / '.state/upstream/WeChat-ChatBot-Mac')
    print(json.dumps(prepare(parser.parse_args().source), indent=2))
