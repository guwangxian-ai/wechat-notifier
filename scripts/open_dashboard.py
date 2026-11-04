import argparse
import json
from pathlib import Path
import urllib.request
import webbrowser
import time
from urllib.parse import urlencode

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--group', default='', help='打开指定群的核对预览，不确认绑定或发送')
args = parser.parse_args()
if len(args.group) > 100 or any(ord(char) < 32 for char in args.group):
    parser.error('群名无效')
url = 'http://127.0.0.1:18790/'
for attempt in range(20):
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + 'healthz', timeout=1) as response:
            if response.status == 200:
                break
    except OSError:
        pass
    time.sleep(.3)
else:
    raise SystemExit('通知服务尚未启动，请先在终端运行 .venv/bin/python -m notifier，或安装独立服务。')
credentials = json.loads((root / '.state' / 'credentials.json').read_text())
parameters = {'key': credentials['admin']}
if args.group:
    parameters['group'] = args.group
webbrowser.open(url + '#' + urlencode(parameters), new=2)
