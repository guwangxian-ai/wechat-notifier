"""Client for the pinned Gusion native bridge. No desktop or clipboard access."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from .errors import AppError

UPSTREAM_COMMIT = '9094de7859618a8124f9d3162e63c87b0c8c837a'
SUPPORTED = {'4.1.15.55': '270135', '4.2.0.5': '270341',
             '4.2.0.9': '270345', '4.2.0.12': '270348'}
PROTOCOL = 'wechat-notifier-gusion-v1'
GROUP_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}@chatroom$')
USER_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
FILE_HELPER = {'user_id': 'filehelper', 'display_name': '文件传输助手'}
DEFAULT_WECHAT_APP = '/Applications/WeChat.app'


def personal_id(value):
    return isinstance(value, str) and USER_ID.fullmatch(value) and not value.lower().startswith('gh_')


def client_app_path(value):
    if value not in (DEFAULT_WECHAT_APP, str(Path.home() / 'Applications/WeChat.app')):
        raise AppError('WECHAT_PATH_INVALID', '微信路径须为系统或当前用户应用程序目录中的 WeChat.app。', 422)
    return value


def bridge_url(value):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                 and parsed.username is None and parsed.password is None
                 and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
                 and parsed.port is not None and 1024 <= parsed.port <= 65535)
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise AppError('BRIDGE_URL_INVALID', '发送桥地址须为 http://127.0.0.1:端口。', 422)
    return f'http://127.0.0.1:{parsed.port}'


def load_config(directory):
    path = Path(directory) / 'native-bridge.json'
    if path.is_symlink():
        raise AppError('BRIDGE_CONFIG_INVALID', '发送桥配置不能是符号链接。', 503)
    if not path.exists():
        return {'base_url': 'http://127.0.0.1:58080', 'token': '', 'wechat_app': DEFAULT_WECHAT_APP}
    try:
        if path.stat().st_size > 8192 or path.stat().st_mode & 0o077:
            raise ValueError
        data = json.loads(path.read_text())
        token = data['token']
        if not isinstance(token, str) or not 24 <= len(token) <= 256 or re.search(r'[\x00-\x20\x7f]', token):
            raise ValueError
        return {'base_url': bridge_url(data['base_url']), 'token': token,
                'wechat_app': client_app_path(data.get('wechat_app', DEFAULT_WECHAT_APP))}
    except (KeyError, ValueError, TypeError, OSError):
        raise AppError('BRIDGE_CONFIG_INVALID', '发送桥配置无效或文件权限过宽。', 503) from None


def save_config(directory, base_url, token=None, wechat_app=None):
    current = load_config(directory)
    token = token or current['token'] or secrets.token_urlsafe(32)
    if not 24 <= len(token) <= 256 or re.search(r'[\x00-\x20\x7f]', token):
        raise AppError('BRIDGE_TOKEN_INVALID', '发送桥密钥需为 24 到 256 个不含空白的字符。', 422)
    data = {'base_url': bridge_url(base_url), 'token': token,
            'wechat_app': client_app_path(wechat_app or current['wechat_app'])}
    path = Path(directory) / 'native-bridge.json'
    temporary = path.with_name('.native-bridge-' + secrets.token_hex(8))
    try:
        with temporary.open('x', encoding='utf-8') as output:
            os.chmod(temporary, 0o600)
            json.dump(data, output)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return data


def installed_version(app=DEFAULT_WECHAT_APP):
    try:
        path = Path(app) / 'Contents/Info.plist'
        data = plistlib.loads(path.read_bytes())
        return str(data.get('WeChatBundleVersion', '')), str(data.get('CFBundleVersion', ''))
    except (OSError, ValueError, plistlib.InvalidFileException):
        return '', ''


class BridgeHTTP:
    def __init__(self, config):
        self.port = urlsplit(bridge_url(config['base_url'])).port
        self.token = config['token']

    def request(self, method, path, payload=None):
        if not self.token:
            raise AppError('BRIDGE_NOT_CONFIGURED', '请先在连接设置保存原生发送桥配置。', 503)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30 if payload else 4)
        try:
            body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
            connection.request(method, path, body=body, headers={
                'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(2_000_001)
            if response.status == 401:
                raise AppError('BRIDGE_UNAUTHORIZED', '发送桥密钥不匹配，请核对连接设置。', 503)
            if response.status != 200 or len(raw) > 2_000_000:
                if response.status == 503 and path == '/notifier/groups' and len(raw) <= 2_000_000:
                    try:
                        reason = json.loads(raw).get('reason')
                    except (ValueError, AttributeError):
                        reason = None
                    messages = {
                        'key_missing': ('GROUP_KEY_MISSING', '群目录密钥尚未捕获，请在原生桥就绪后打开一次通讯录；仍失败时查看桥诊断。'),
                        'db_missing': ('GROUP_DATABASE_UNAVAILABLE', '当前账号群目录文件不可读，请检查微信登录及本机文件权限。'),
                        'decrypt_failed': ('GROUP_DIRECTORY_DECRYPT_FAILED', '群目录解密或查询失败，请检查原生桥版本适配及诊断。'),
                        'unsupported': ('GROUP_DIRECTORY_UNSUPPORTED', '当前群目录 Hook 尚未安装成功，请检查原生桥运行时诊断。'),
                    }
                    if isinstance(reason, str) and reason in messages:
                        code, message = messages[reason]
                        raise AppError(code, message, 503)
                if response.status == 503 and path == '/notifier/contacts':
                    raise AppError('CONTACT_LOOKUP_UNAVAILABLE',
                                   '暂时无法读取本机联系人，请检查微信登录和原生通讯录连接。', 503)
                raise AppError('BRIDGE_RESPONSE_INVALID', '发送桥请求未成功，请检查原生桥运行状态。', 503)
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('status') != 'ok':
                raise ValueError
            return data
        except AppError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise AppError('BRIDGE_UNAVAILABLE', '原生发送桥未连接或响应异常。', 503) from None
        finally:
            connection.close()


class NativeDriver:
    mode = 'live'
    backend = 'onebot'
    execution_mode = 'native'

    def __init__(self, directory, transport=None, version_reader=None):
        self.directory = Path(directory)
        self.config = load_config(directory)
        self.transport = transport or BridgeHTTP(self.config)
        self.version_reader = version_reader or (lambda: installed_version(self.config['wechat_app']))
        self._cache_lock = threading.Lock()
        self._health, self._health_at = None, 0
        self._prepared = None

    def _status(self):
        version, build = self.version_reader()
        if SUPPORTED.get(version) != build:
            raise AppError('WECHAT_VERSION_UNSUPPORTED',
                           f'当前微信 {version or "未安装"}（{build or "未知构建"}）尚未适配；请使用发送桥支持的精确版本。', 503)
        data = self.transport.request('GET', '/notifier/status').get('data', {})
        if (not isinstance(data, dict) or data.get('protocol') != PROTOCOL or data.get('upstream_commit') != UPSTREAM_COMMIT
                or data.get('version') != version or data.get('build') != build
                or not isinstance(data.get('session_id'), str) or not data['session_id']
                or not isinstance(data.get('account_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data['account_id'])):
            raise AppError('BRIDGE_IDENTITY_INVALID', '发送桥版本、微信构建或账号身份无法核对。', 503)
        return data

    def health(self, fresh=False):
        with self._cache_lock:
            if not fresh and self._health and time.monotonic() - self._health_at < 15:
                return self._health
            version, build = self.version_reader()
            checks = [{'name': '微信版本', 'ok': SUPPORTED.get(version) == build,
                       'detail': f'{version or "未检测到微信"} / {build or "未知构建"}；须与原生桥适配表一致。'}]
            try:
                data = self._status()
                checks.extend([
                    {'name': '原生发送桥', 'ok': True, 'detail': '本机鉴权连接和程序版本已核对。'},
                    {'name': '发送上下文', 'ok': data.get('ready') is True,
                     'detail': '原生发送上下文已就绪。' if data.get('ready') is True else
                               '等待微信登录及发送上下文；首次或微信重启后需手动发送一条消息。'}])
            except AppError as exc:
                checks.append({'name': '原生发送桥', 'ok': False, 'detail': exc.message})
            result = {'mode': self.mode, 'backend': self.backend, 'execution_mode': self.execution_mode,
                      'wechat_version': version, 'wechat_build': build, 'checks': checks,
                      'ready': all(c['ok'] for c in checks), 'checked_at': time.time(),
                      'note': '通过微信内部发送通道提交文本，不操作聊天窗口、键盘、鼠标或剪贴板。'
                              '“已提交微信”表示本地任务提交成功，可在对应会话用通知编号核对结果。'}
            self._health, self._health_at = result, time.monotonic()
            return result

    def background_check(self, query='', verify_layout=False):
        result = self.health(fresh=True)
        return {**result, 'read_only': True}

    def _groups(self, status):
        reply = self.transport.request('GET', '/notifier/groups').get('data', {})
        if (not isinstance(reply, dict) or reply.get('account_id') != status['account_id']
                or reply.get('session_id') != status['session_id']):
            raise AppError('ACCOUNT_CHANGED', '读取群目录期间账号或微信进程发生变化，请重新检查。')
        rows = reply.get('groups')
        if not isinstance(rows, list) or len(rows) > 10000:
            raise AppError('GROUP_DIRECTORY_INVALID', '发送桥未返回有效群目录。', 503)
        result, seen = [], set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('group_id'), str)
                    or not GROUP_ID.fullmatch(row['group_id']) or row['group_id'] in seen
                    or not isinstance(row.get('group_name'), str) or not 1 <= len(row['group_name']) <= 200):
                raise AppError('GROUP_DIRECTORY_INVALID', '群目录条目无效或重复，未选择目标群。', 503)
            seen.add(row['group_id'])
            result.append(row)
        return result

    def search(self, query):
        status = self._status()
        rows = self._groups(status)
        return [{'group_name': row['group_name'], 'group_id': row['group_id'],
                 'selection_source': 'native_directory', 'requires_id_confirmation': True,
                 '_account_id': status['account_id']} for row in rows
                if query.casefold() in row['group_name'].casefold() or query == row['group_id']][:20]

    @staticmethod
    def require_private_support(status):
        if status.get('private_send_enabled') is not True:
            raise AppError('PRIVATE_UNSUPPORTED', '当前原生发送桥需要升级后才能发送个人通知。', 503)

    def _contacts(self, status, query, exact=False):
        reply = self.transport.request('POST', '/notifier/contacts', {
            'query': query, 'exact': exact, 'allow_contact_lookup': True,
            'account_id': status['account_id'], 'session_id': status['session_id'],
        }).get('data', {})
        if (not isinstance(reply, dict) or reply.get('account_id') != status['account_id']
                or reply.get('session_id') != status['session_id']):
            raise AppError('ACCOUNT_CHANGED', '查询联系人期间微信账号或进程变化，请重新搜索。')
        rows = reply.get('contacts')
        if not isinstance(rows, list) or len(rows) > 20:
            raise AppError('CONTACT_DIRECTORY_INVALID', '发送桥未返回有效联系人结果。', 503)
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not personal_id(row.get('user_id'))
                    or row['user_id'] in seen or (exact and row['user_id'] != query)
                    or not isinstance(row.get('display_name'), str)
                    or not 1 <= len(row['display_name']) <= 200 or not row['display_name'].strip()):
                raise AppError('CONTACT_DIRECTORY_INVALID', '联系人身份无法核对，请重新搜索。', 503)
            seen.add(row['user_id'])
        return rows

    def search_person(self, query, allow_contact_lookup=False):
        builtin = query.casefold() in (FILE_HELPER['user_id'], FILE_HELPER['display_name'])
        if not builtin and not allow_contact_lookup:
            raise AppError('CONTACT_LOOKUP_CONSENT_REQUIRED', '请先勾选允许本机查找微信联系人。', 422)
        status = self._status()
        self.require_private_support(status)
        rows = [FILE_HELPER] if builtin else self._contacts(status, query)
        return [{'group_name': row['display_name'], 'user_id': row['user_id'],
                 'selection_source': 'native_directory', 'requires_id_confirmation': True,
                 '_account_id': status['account_id'], '_session_id': status['session_id'],
                 '_contact_lookup': not builtin} for row in rows]

    def _verify_person(self, row, confirmed):
        if not confirmed:
            raise AppError('PRIVATE_CONFIRMATION_REQUIRED', '请核对收件人和唯一微信 ID。')
        status = self._status()
        self.require_private_support(status)
        if status['account_id'] != row['_account_id'] or status['session_id'] != row['_session_id']:
            raise AppError('ACCOUNT_CHANGED', '微信账号或进程已变化，请重新搜索联系人。')
        user_id = row['user_id']
        # Only candidates created by an authorized search can re-read contact.db.
        matches = self._contacts(status, user_id, exact=True) if row['_contact_lookup'] else [FILE_HELPER]
        if len(matches) != 1 or matches[0]['user_id'] != user_id or matches[0]['display_name'] != row['group_name']:
            raise AppError('CONTACT_CHANGED', '联系人信息已变化，请重新搜索并核对。')
        return {'group_name': row['group_name'], 'backend': self.backend,
                'user_id': user_id, 'account_id': status['account_id']}

    def verify_candidate(self, row, confirmed):
        if row.get('user_id'):
            return self._verify_person(row, confirmed)
        if not confirmed:
            raise AppError('GROUP_CONFIRMATION_REQUIRED', '请核对群名和唯一群 ID，再确认绑定。')
        status = self._status()
        if status['account_id'] != row['_account_id']:
            raise AppError('ACCOUNT_CHANGED', '微信账号已变化，请重新选择目标群。')
        matches = [g for g in self._groups(status) if g['group_id'] == row['group_id']]
        if len(matches) != 1 or matches[0]['group_name'] != row['group_name']:
            raise AppError('GROUP_CHANGED', '目标群已变化，请重新搜索并核对。')
        return {'group_name': row['group_name'], 'backend': self.backend,
                'group_id': row['group_id'], 'account_id': status['account_id']}

    def prepare(self, task):
        self._prepared = None
        status = self._status()
        if status.get('ready') is not True:
            raise AppError('NATIVE_NOT_READY', '原生发送上下文尚未就绪，任务已保留。', 503, retryable=True)
        group_id, user_id = task.get('group_id', ''), task.get('user_id', '')
        if user_id and not group_id and personal_id(user_id):
            self.require_private_support(status)
            recipient = {'user_id': user_id}
        elif group_id and not user_id and GROUP_ID.fullmatch(group_id):
            recipient = {'group_id': group_id}
        else:
            raise AppError('TARGET_IDENTITY_CHANGED', '任务的收件人 ID 无效。')
        if task.get('backend') != self.backend or task.get('account_id') != status['account_id']:
            raise AppError('TARGET_IDENTITY_CHANGED', '任务绑定的微信账号或发送方式已变化。')
        self._prepared = {'task_id': task['id'], 'request_id': task.get('dispatch_nonce') or task['id'], **recipient,
                          'account_id': status['account_id'], 'session_id': status['session_id'],
                          'message_hash': hashlib.sha256(task['message'].encode()).hexdigest()}
        return 'prepared'

    def abort(self, task):
        self._prepared = None

    def send(self, task):
        prepared, self._prepared = self._prepared, None
        if (not prepared or prepared['task_id'] != task['id']
                or prepared['message_hash'] != hashlib.sha256(task['message'].encode()).hexdigest()):
            raise AppError('NATIVE_PREPARATION_REQUIRED', '原生发送准备已失效，请核对任务。')
        payload = {k: v for k, v in prepared.items() if k not in ('message_hash', 'task_id')}
        reply = self.transport.request('POST', '/notifier/submit', {**payload, 'text': task['message']})
        if reply.get('receipt') != 'submitted' or reply.get('request_id') != prepared['request_id']:
            raise AppError('RESULT_UNKNOWN', '原生发送结果无法确认，请在对应会话核对通知编号。')
        return 'submitted'
