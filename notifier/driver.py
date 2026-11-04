"""Background-only macOS adapter; unsupported clients never activate the desktop."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from .errors import AppError
from .visual import PROTOCOL, VisualSession

SCRIPTS = Path(__file__).parent / 'macos'


def canonical(text):
    return re.sub(r'\s+', '', str(text))


def group_candidates(items, query):
    """Accept only OCR rows under the explicit local Group Chats section."""
    left = sorted((i for i in items if i['x'] < .37 and .07 < i['y'] < .93), key=lambda i: i['y'])
    in_groups, results = False, []
    boundaries = {'联系人', '聊天记录', '公众号', '服务号', '小程序', '搜索建议', '网络结果',
                  'Contacts', 'ChatHistory', 'OfficialAccounts', 'MiniPrograms'}
    for item in left:
        text = re.sub(r'\s+', '', item['text'])
        if text in ('群聊', 'GroupChats', 'Groups'):
            in_groups = True
            continue
        if in_groups and text in boundaries:
            break
        if (in_groups and canonical(query) in canonical(text) and item.get('confidence', 0) >= .75
                and text not in ('查看全部', '更多群聊')):
            results.append({**item, 'group_name': item['text'].strip()})
    return results


def title_matches(items, group_name):
    return any(.27 < i['x'] < .94 and .015 < i['y'] < .14
               and i.get('confidence', 0) >= .8
               and (canonical(i['text']) == canonical(group_name)
                    or re.fullmatch(re.escape(canonical(group_name)) + r'[（(]\d+[)）]', canonical(i['text'])))
               for i in items)


def marker_visible(items, marker):
    # Exclude sidebar, title, composer, and left-aligned incoming message bubbles.
    return any(i['x'] > .45 and .14 < i['y'] < .76 and i.get('confidence', 0) >= .8
               and marker in re.sub(r'\s+', '', i['text']) for i in items)


class MacDriver:
    mode = 'live'
    execution_mode = 'background'

    def __init__(self, directory):
        self.directory = Path(directory)
        self._health, self._health_at = None, 0
        self._cache_lock = threading.Lock()
        self.visual = VisualSession(self)
        self._visual_enabled = False
        self._layout_probe = None

    def _run(self, command, args=(), timeout=12, payload=None):
        try:
            result = subprocess.run(command + list(args), text=True, capture_output=True, timeout=timeout,
                                    input=payload,
                                    env={**os.environ, 'LANG': 'en_US.UTF-8'})
        except subprocess.TimeoutExpired:
            raise AppError('AUTOMATION_TIMEOUT', '微信界面操作超时，请核对桌面后重试。') from None
        except OSError:
            raise AppError('AUTOMATION_UNAVAILABLE', '当前系统无法运行 macOS 微信自动化。', 503) from None
        if result.returncode:
            # osascript stderr may contain UI text; never return or persist it.
            raise AppError('AUTOMATION_BLOCKED', '后台执行器未能运行，请检查系统授权与客户端兼容性。', 503)
        return result.stdout[:-1] if result.stdout.endswith('\n') else result.stdout

    def _native(self, action, *args):
        return self._bridge(action)

    def _bridge(self, action, **values):
        executable = SCRIPTS.parents[1] / '.state/bin/background-bridge'
        if not executable.is_file():
            raise AppError('BACKGROUND_NOT_BUILT', '后台执行器尚未构建，请运行 scripts/build_background_bridge.py。', 503)
        # Message bodies go through stdin, never arguments, pasteboard, or a text file.
        timeout = 42 if action == 'capture' else 12
        if action == 'visual_write':
            timeout = min(180, 20 + len(values.get('message', '')) * .03)
        raw = self._run([str(executable)], timeout=timeout,
                        payload=json.dumps({'action': action, **values}, ensure_ascii=False))
        return self._decode(raw)

    @staticmethod
    def _decode(raw):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise AppError('INVALID_NATIVE_RESULT', '系统自动化返回了无法识别的结果。', 503) from None
        if data.get('error'):
            raise AppError(data['error'], data['message'], 503)
        return data

    def health(self, fresh=False):
        with self._cache_lock:
            if not fresh and self._health and time.monotonic() - self._health_at < 15:
                return self._health
            result = {'mode': self.mode, 'execution_mode': self.execution_mode,
                      'ready': False, 'checks': [], 'checked_at': time.time()}
            if sys.platform != 'darwin':
                result['checks'] = [{'name': 'macOS', 'ok': False, 'detail': '真实发送需要 macOS 桌面会话。'}]
            else:
                try:
                    native = self._native('health')
                    result['wechat_version'] = native.get('wechat_version', '')
                    result['checks'] = [
                        {'name': '个人微信', 'ok': native['wechat_running'], 'detail': '微信需在本机桌面运行并登录。'},
                        {'name': '辅助功能', 'ok': native['accessibility'], 'detail': '允许实际运行程序操作微信控件。'},
                        {'name': '屏幕录制', 'ok': native['screen_recording'], 'detail': '用于局部 OCR 核对目标群与发送结果。'},
                        {'name': '桌面可交互', 'ok': not native['screen_locked'], 'detail': '锁屏时保留队列，暂停发送。'},
                    ]
                    if all(i['ok'] for i in result['checks']):
                        try:
                            capability = self._bridge('capabilities')
                            native_controls = capability.get('supported') is True
                            self._visual_enabled = not native_controls and capability.get('visual_protocol') == PROTOCOL
                            supported = native_controls or (self._visual_enabled and self.visual.ready(capability, result['wechat_version']))
                            result['background'] = {**capability, 'supported': supported,
                                                    'backend': 'visual' if self._visual_enabled else 'accessibility'}
                            if self._visual_enabled:
                                detail = ('后台窗口输入已校验；每条通知会再次核对群标题和完整正文。' if supported else
                                          '请先在“目标群”搜索完整群名并完成绑定校验；操作前会重新定位窗口布局并核对群标题。')
                            else:
                                detail = ('后台控件可用；群绑定和发送结果仍会逐次核对。' if supported else
                                          '当前微信未提供可读写控件，且窗口输入接口不可用。任务保留，不切换前台。')
                            result['checks'].append({'name': '后台控件', 'ok': supported, 'detail': detail})
                        except AppError as exc:
                            result['background'] = {'supported': False, 'error': exc.code}
                            result['checks'].append({'name': '后台控件', 'ok': False, 'detail': exc.message})
                    result['ready'] = all(i['ok'] for i in result['checks'])
                    result['note'] = '仅后台操作，不激活微信，不使用全局按键、鼠标或剪贴板。微信主窗口需保持打开，可以被其他应用遮挡。'
                    if os.environ.get('WECHAT_NOTIFIER_APP'):
                        result['permission_app'] = '微信群通知.app'
                        result['note'] += ' 在 Mac mini 的隐私与安全中，将桌面的“微信群通知.app”加入辅助功能和屏幕录制授权。'
                except AppError as exc:
                    result['checks'] = [{'name': '系统检查', 'ok': False, 'detail': exc.message}]
            self._health, self._health_at = result, time.monotonic()
            return result

    def background_check(self, query='', verify_layout=False):
        """Read-only diagnostic; no search, draft mutation, or message send."""
        status = self.health(fresh=True)
        report = {'execution_mode': self.execution_mode, 'ready': status['ready'],
                  'capabilities': status.get('background', {}), 'checks': status['checks']}
        if all(check['ok'] for check in status['checks'] if check['name'] != '后台控件'):
            try:
                capture = self._bridge('capture', visual=True, diagnostic=True) if query else self._capture()
                report['window_capture'] = {'ok': bool(capture['items']), 'text_count': len(capture['items']),
                    **{key: capture[key] for key in ('foreground_unchanged', 'mouse_unchanged', 'clipboard_unchanged', 'capture_seconds', 'bounds', 'image_size', 'source_size') if key in capture}}
                if query:
                    core = canonical(query).rstrip('✅\ufe0f')
                    sidebar = capture.get('regions', {}).get('sidebar', [])
                    report['search_diagnostic'] = {
                        'header_layout': [i for i in capture.get('regions', {}).get('header', [])
                                          if .18 < i['x'] < .60 and i['y'] * capture['bounds']['Height'] < 50],
                        'send_buttons': [i for i in capture.get('regions', {}).get('lower', [])
                                         if re.fullmatch(r'发送(?:[（(][sS][)）])?|Send(?:\(S\))?', canonical(i['text']))],
                        'search_field': [i for i in sidebar
                                         if 55 < i['x'] * capture['bounds']['Width'] < 270
                                         and 10 < i['y'] * capture['bounds']['Height'] < 65],
                        'sections': [i for i in sidebar if re.fullmatch(r'(群聊|联系人|聊天记录|公众号|搜索建议|搜索|取消)(?:[（(]\d+[)）])?', canonical(i['text']))],
                        'query_hits': [i for i in sidebar if core and core in canonical(i['text'])],
                        'sidebar_text_count': len(sidebar),
                    }
                    titles = self.visual._matched_titles(capture, query)
                    report['search_diagnostic']['current_group_matches_query'] = len(titles) == 1
                    if verify_layout:
                        try:
                            if len(titles) != 1:
                                raise AppError('TITLE_CHANGED', '请打开指定群聊天页，当前标题无法完整核对。')
                            previous = self._layout_probe
                            if previous and previous[0] == query:
                                profile, checked = self.visual.relocate(query, previous[1], diagnostic=True)
                            else:
                                profile, checked = self.visual.describe(
                                    capture, titles[0], self.visual._search_point(capture), query, diagnostic=True)
                            guard = None
                            try:
                                self.visual.require_empty(checked)
                            except AppError as exc:
                                guard = exc.code
                            report['layout_verification'] = {
                                'ok': True, 'read_only': True,
                                'baseline_created': not previous or previous[0] != query,
                                'previous_size': previous[1]['size'] if previous and previous[0] == query else None,
                                'current_size': profile['size'], 'composer_region': profile['composer_rect'],
                                'draft_guard': guard,
                                **{key: checked[key] for key in ('foreground_unchanged', 'mouse_unchanged', 'clipboard_unchanged') if key in checked},
                            }
                            # A single unconfirmed diagnostic baseline, in memory
                            # only. It is never saved as a bound/sending profile.
                            self._layout_probe = (query, profile)
                        except AppError as exc:
                            report['layout_verification'] = {'ok': False, 'read_only': True,
                                                             'error': exc.code, 'detail': exc.message}
                        return report
                    if not titles:
                        # Read-only layout diagnosis may inspect the current
                        # group, but never treats it as the requested recipient.
                        titles = [i for i in report['search_diagnostic']['header_layout']
                                  if re.search(r'[（(]\d+[)）]$', canonical(i['text']))]
                    if len(titles) == 1:
                        try:
                            rect, layout, rows = self.visual.measure_composer(
                                capture, titles[0], self.visual._send_button(capture), diagnostic=True)
                            probe = self._bridge('capture', diagnostic=True, composer_rect=rect)
                            report['composer_probe'] = {
                                'region': rect, 'pixels': probe['snapshots']['composer'],
                                'layout_region': layout, 'layout_rows': rows,
                                'text_boxes': [{**{k: item[k] for k in ('x', 'y', 'w', 'h')},
                                                'characters': len(item['text']),
                                                'owned_calibration': 'CALIBRATION' in item['text']}
                                               for item in probe['regions']['composer']],
                            }
                        except AppError as exc:
                            report['composer_probe'] = {'error': exc.code, 'detail': exc.message}
                            report['composer_probe'].update(getattr(exc, 'layout_probe', {}))
            except AppError as exc:
                report['window_capture'] = {'ok': False, 'error': exc.code, 'detail': exc.message}
        return report

    def preflight(self):
        status = self.health(fresh=True)
        if not status['ready']:
            detail = '；'.join(i['name'] for i in status['checks'] if not i['ok'])
            raise AppError('WECHAT_NOT_READY', '微信暂不可用：' + detail + '。请在连接设置查看。', 503, retryable=True)

    def focus_search_check(self):
        """Focus only a uniquely visible empty search field; never type or send."""
        before = self._capture()
        bounds = before.get('bounds', {})
        fields = [i for i in before['items'] if 40 < i['x'] * bounds.get('Width', 1) < 400
                  and 10 < i['y'] * bounds.get('Height', 1) < 90
                  and i.get('confidence', 0) >= .3
                  and re.fullmatch(r'[QqOo0〇○⌕🔍]*搜索[×x]*|Search', canonical(i['text']))]
        if len(fields) != 1:
            raise AppError('EMPTY_SEARCH_NOT_FOUND', '未找到唯一的空搜索框，未执行点击。请先清空微信搜索框。')
        item, bounds = fields[0], before['bounds']
        result = self._bridge('focus_search',
                              x=bounds['X'] + (item['x'] + item['w'] / 2) * bounds['Width'],
                              y=bounds['Y'] + (item['y'] + item['h'] / 2) * bounds['Height'])
        after = self._capture()
        labels = {'搜索', 'Search', '取消', 'Cancel', '最近搜索', '搜索历史', '群聊', '聊天记录', '联系人'}
        def visible_labels(capture):
            return [canonical(i['text']) for i in capture['items']
                    if i['x'] < .37 and canonical(i['text']) in labels]
        return {**result, 'before_labels': visible_labels(before), 'after_labels': visible_labels(after),
                'capture_seconds': after.get('capture_seconds'), 'sent': False}

    def open_privacy(self, kind):
        panels = {'accessibility': 'Privacy_Accessibility', 'screen': 'Privacy_ScreenCapture', 'automation': 'Privacy_Automation'}
        if kind not in panels:
            raise AppError('BAD_PANEL', '不支持的设置页面。', 422)
        self._run(['/usr/bin/open'], ['x-apple.systempreferences:com.apple.preference.security?' + panels[kind]])

    def _capture(self):
        return self._native('capture')

    def search(self, query):
        status = self.health(fresh=True)
        if self._visual_enabled and status.get('background', {}).get('window_id'):
            return self.visual.search(query)
        if not status['ready']:
            detail = '；'.join(i['name'] for i in status['checks'] if not i['ok'])
            raise AppError('WECHAT_NOT_READY', '微信暂不可用：' + detail + '。请在连接设置查看。', 503, retryable=True)
        self._bridge('search', query=query)
        last = []
        for _ in range(3):
            capture = self._capture()
            last = group_candidates(capture['items'], query)
            if last:
                return [{'group_name': item['group_name']} for item in last]
            time.sleep(.35)
        raise AppError('NO_GROUP_RESULTS', '没有识别到本地群聊结果。请确认微信已登录、群已同步，输入完整且唯一的群名；不要在操作期间切换微信窗口。', 422)

    def open_group(self, group_name):
        self.preflight()
        self._bridge('search', query=group_name)
        capture, candidates = None, []
        for _ in range(3):
            capture = self._capture()
            candidates = [i for i in group_candidates(capture['items'], group_name)
                          if canonical(i['group_name']) == canonical(group_name)]
            if candidates:
                break
            time.sleep(.35)
        if len(candidates) != 1:
            raise AppError('AMBIGUOUS_GROUP', '目标群无法唯一确认，已停止操作。请使用不重名且未截断的群名或群备注。')
        item, bounds = candidates[0], capture['bounds']
        x = bounds['X'] + max(.10, item['x'] + item['w'] / 2) * bounds['Width']
        y = bounds['Y'] + (item['y'] + item['h'] / 2) * bounds['Height']
        self._bridge('click', x=x, y=y)
        for _ in range(3):
            current = self._capture()
            if title_matches(current['items'], group_name):
                return current
            time.sleep(.3)
        raise AppError('TITLE_MISMATCH', '当前会话标题与绑定群不一致，已停止操作。')

    def verify_group(self, group_name):
        if self._visual_enabled:
            self.visual.open_group(group_name)
            return group_name
        self.open_group(group_name)
        return group_name

    def _composer(self, action, message=''):
        result = self._bridge(action, message=message)
        return result.get('text', '') if action == 'read' else result

    def prepare(self, task):
        if self._visual_enabled:
            return self.visual.prepare(task)
        capture = self.open_group(task['group_name'])
        marker = 'WN-' + task['id'][:12].upper()
        if marker_visible(capture['items'], marker):
            return 'already_observed'
        if self._composer('read'):
            raise AppError('EXISTING_DRAFT', '目标群已有草稿，已保留原文。请核对并清空草稿后手动重试。')
        self._composer('write', task['message'])
        if self._composer('read') != task['message']:
            raise AppError('DRAFT_MISMATCH', '输入框内容未能准确写入，未发送。请核对微信输入框。')
        return 'prepared'

    def abort(self, task):
        if self._visual_enabled:
            return self.visual.abort(task)
        capture = self._capture()
        if title_matches(capture['items'], task['group_name']) and self._composer('read') == task['message']:
            self._composer('clear', task['message'])

    def send(self, task):
        if self._visual_enabled:
            return self.visual.send(task)
        if not title_matches(self._capture()['items'], task['group_name']):
            raise AppError('TITLE_MISMATCH', '发送前目标群校验失败，请核对后处理。')
        self._composer('send', task['message'])
        marker = 'WN-' + task['id'][:12].upper()
        for _ in range(4):
            capture = self._capture()
            if (title_matches(capture['items'], task['group_name']) and marker_visible(capture['items'], marker)
                    and not self._composer('read')):
                return 'observed'
            time.sleep(.5)
        raise AppError('RESULT_UNKNOWN', '已执行发送操作，但未能确认本地消息。请在群内核对通知编号，系统已暂停。')


class DryRunDriver:
    """Explicit local rehearsal; never reports an actual WeChat delivery."""
    mode = 'dry_run'

    def health(self, fresh=False):
        return {'mode': self.mode, 'ready': True, 'checked_at': time.time(),
                'checks': [{'name': '预演模式', 'ok': True, 'detail': '只验证提交和排队，不操作个人微信。'}]}

    def search(self, query):
        return [{'group_name': query.strip()}]

    def verify_group(self, name):
        return name

    def prepare(self, task):
        return 'prepared'

    def abort(self, task):
        pass

    def send(self, task):
        return 'simulated'


class Controller:
    def __init__(self, driver):
        self.driver, self.lock = driver, threading.Lock()
        self.candidates = {}

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise AppError('GUI_BUSY', '正在操作微信，请稍后再试。', 409, retryable=True)
        try:
            yield
        finally:
            self.lock.release()

    def search(self, query):
        with self.exclusive():
            self.candidates.clear()
            rows = self.driver.search(query.strip())
            response = []
            for row in rows[:20]:
                key = uuid.uuid4().hex
                self.candidates[key] = (row['group_name'], time.monotonic() + (300 if '_visual_profile' in row else 120), row)
                response.append({'candidate_id': key, **{k: v for k, v in row.items() if not k.startswith('_')}})
            return response

    def bind(self, store, request):
        with self.exclusive():
            entry = self.candidates.get(request.candidate_id)
            if not entry or time.monotonic() > entry[1]:
                raise AppError('CANDIDATE_EXPIRED', '群候选已过期，请重新搜索。')
            if entry[2].get('selection_source') == 'native_directory':
                binding = self.driver.verify_candidate(entry[2], request.confirmed_visual)
                target = store.bind(request.code, request.label.strip(), mode=self.driver.mode, **binding)
                del self.candidates[request.candidate_id]
                return target
            if entry[2].get('_visual_profile'):
                name = self.driver.visual.bind(entry[0], entry[2]['_visual_profile'], request.confirmed_visual)
            else:
                name = self.driver.verify_group(entry[0])
            target = store.bind(request.code, request.label.strip(), name, self.driver.mode)
            del self.candidates[request.candidate_id]
            return target
