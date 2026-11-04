"""Example integration client; only the standard library is required."""
import argparse
import json
import os
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description='向本机微信通知服务提交任务')
    parser.add_argument('--event-id', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--title', default='')
    parser.add_argument('--body', required=True)
    parser.add_argument('--port', type=int, default=18790)
    args = parser.parse_args()
    token = os.getenv('WECHAT_NOTIFY_TOKEN')
    if not token:
        parser.error('请通过 WECHAT_NOTIFY_TOKEN 环境变量提供接口密钥，不要写入源码')
    data = {'event_id': args.event_id, 'target_code': args.target, 'title': args.title, 'body': args.body}
    request = urllib.request.Request(f'http://127.0.0.1:{args.port}/api/v1/notifications',
        data=json.dumps(data, ensure_ascii=False).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            print(response.read(32768).decode())
    except urllib.error.HTTPError as exc:
        print(exc.read(32768).decode())
        raise SystemExit(1) from None
    except OSError:
        raise SystemExit('提交未确认。可使用相同事件编号再次提交，服务会去重。') from None


if __name__ == '__main__':
    main()
