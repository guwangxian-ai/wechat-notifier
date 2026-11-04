import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import webbrowser

import uvicorn

from .app import create_app
from .driver import DryRunDriver


def main():
    parser = argparse.ArgumentParser(description='独立个人微信群通知应用')
    parser.add_argument('--port', type=int, default=18790)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / '.state')
    parser.add_argument('--dry-run', action='store_true', help='明确预演模式，不操作微信')
    parser.add_argument('--open', action='store_true', help='用管理入口打开浏览器')
    parser.add_argument('--show-url', action='store_true', help='仅显示含管理密钥的入口，勿分享')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port 必须在 1024 到 65535 之间')
    os.umask(0o077)
    app = create_app(args.data_dir, driver=DryRunDriver() if args.dry_run else None)
    url = f'http://127.0.0.1:{args.port}/#key={app.state.credentials.admin}'
    if args.show_url:
        print(url)
        return
    if args.open:
        webbrowser.open(url)
    handler = RotatingFileHandler(app.state.store.directory / 'app.log', maxBytes=2_000_000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    print(f'个人微信群通知：http://127.0.0.1:{args.port}/ （默认暂停发送）', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_config=None, timeout_graceful_shutdown=50)


if __name__ == '__main__':
    main()
