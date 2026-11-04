"""Verified window-only input for WeChat renderers without Accessibility fields."""
import json
import math
import os
from collections import Counter
from pathlib import Path
import re
import time
import unicodedata
import uuid

from .errors import AppError

PROTOCOL = 'window-unicode-v1'


def compact(text):
    return re.sub(r'\s+', '', text)


def draft_text(text):
    return compact(unicodedata.normalize('NFKC', text))


def name_match(actual, expected):
    actual = re.sub(r'[（(]\d+[)）]$', '', compact(actual))
    expected = compact(expected)
    if actual == expected:
        return True
    # This is a candidate for HUMAN visual confirmation, never a runtime identity
    # check. Runtime identity uses the confirmed title pixels, including the emoji.
    # Vision reads the same emoji as different single glyphs at different
    # widths. Do not keep an ever-growing alias list: only propose a candidate
    # when the entire textual prefix and the one-glyph suffix length agree.
    return expected.endswith('✅') and len(actual) == len(expected) and actual[:-1] == expected[:-1]


def reading_order(items):
    rows = []
    for item in sorted(items, key=lambda row: row['y'] + row['h'] / 2):
        center = item['y'] + item['h'] / 2
        if rows and abs(center - rows[-1][0]) < min(item['h'], rows[-1][1][0]['h']) * .55:
            rows[-1][1].append(item)
        else:
            rows.append((center, [item]))
    return '\n'.join(' '.join(i['text'] for i in sorted(row, key=lambda i: i['x'])) for _, row in rows)


def candidates(items, query):
    groups, result = False, []
    boundaries = {'联系人', '聊天记录', '公众号', '小程序', 'Contacts', 'ChatHistory', 'OfficialAccounts'}
    for item in sorted(items, key=lambda i: i['y']):
        text = compact(item['text'])
        if re.sub(r'[（(]\d+[)）]$', '', text) in {'群聊', 'GroupChats', 'Groups'}:
            groups = True
        elif groups and text in boundaries:
            break
        elif groups and name_match(text, query):
            result.append(item)
    return result


class VisualSession:
    def __init__(self, driver):
        self.driver = driver
        self.path = Path(driver.directory) / 'visual-profiles.json'
        self.prepared = {}

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > 1_000_000:
                raise ValueError
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or len(data) > 100:
                raise ValueError
            return data
        except (ValueError, OSError):
            raise AppError('PROFILE_INVALID', '后台群配置无法读取，请重新绑定目标群。', 503) from None

    def ready(self, capability, version):
        bounds = capability.get('bounds', {})
        return any(p.get('protocol') == PROTOCOL and p.get('wechat_version') == version
                   and (p.get('title_anchor') or p.get('size') == [bounds.get('Width'), bounds.get('Height')])
                   for p in self._load().values())

    def _save(self, name, profile):
        values = self._load()
        if name not in values and len(values) >= 100:
            raise AppError('PROFILE_LIMIT', '最多保存 100 个后台群配置。')
        values[name] = {k: v for k, v in profile.items() if k != 'preview'}
        temporary = self.path.with_name('.visual-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as file:
                os.chmod(temporary, 0o600)
                json.dump(values, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def capture(self, profile=None, *, preview=False, marker=None, diagnostic=False):
        args = {'visual': True}
        if diagnostic:
            args['diagnostic'] = True
        if profile:
            args.update(header_rect=profile['header_rect'], composer_rect=profile['composer_rect'])
        if preview:
            args['preview'] = True
        if marker:
            args['expected_marker'] = marker
        result = self.driver._bridge('capture', **args)
        if profile and profile['size'] != [result['bounds']['Width'], result['bounds']['Height']]:
            raise AppError('WINDOW_LAYOUT_CHANGED', '微信窗口在本次操作过程中发生变化，已停止输入或发送。窗口稳定后可重新操作。')
        return result

    @staticmethod
    def _same_window(before, after):
        if before['bounds'] != after['bounds'] or before['window_id'] != after['window_id']:
            raise AppError('WINDOW_CHANGED', '微信窗口仍在变化，未执行输入。请在调整结束后重试。')

    def relocate(self, name, profile, *, diagnostic=False):
        """Refresh geometry before a transaction, never after writing a draft.

        OCR proposes the new title location; an exact match against the previously
        confirmed title pixels is required before accepting any fresh coordinates.
        The confirmed hash is immutable. No search, input, or clearing occurs here.
        """
        anchor = profile.get('title_anchor')
        if not anchor:
            current = self.capture(profile, diagnostic=diagnostic)
            self._title(current, profile)
            return dict(profile), current
        current = self.capture(diagnostic=diagnostic)
        titles = self._matched_titles(current, name)
        if len(titles) != 1:
            raise AppError('TITLE_CHANGED', '当前群标题无法完整核对，未执行输入。请打开已绑定的目标群。')
        title = titles[0]
        box = title.get('ink_box') if anchor.get('origin_type') == 'ink' else title.get('name_box') or title
        if not box:
            raise AppError('TITLE_CHANGED', '无法定位已确认标题的字形像素，未执行输入。')
        image = current['image_size']
        scale = [image['width'] / current['bounds']['Width'], image['height'] / current['bounds']['Height']]
        if any(abs(a - b) > .001 for a, b in zip(scale, anchor['scale'])):
            raise AppError('TITLE_SCALE_CHANGED', '窗口捕获清晰度发生变化，无法核对原群标题，请重新查看标题预览。')
        identity = {'hash': profile['header_hash'], 'pixel_size': anchor['pixel_size'],
                    'origin': [box['x'] * image['width'] + anchor['offset'][0],
                               box['y'] * image['height'] + anchor['offset'][1]]}
        proof = self.driver._bridge('capture', diagnostic=diagnostic, header_identity=identity)
        self._same_window(current, proof)
        matched = proof.get('header_match')
        if not matched or not matched.get('matched'):
            raise AppError('TITLE_CHANGED', '重新定位后的群标题与已确认图像不一致，未执行输入。请核对群标题预览。')
        editor, _, _ = self.measure_composer(current, title, self._send_button(current), diagnostic=diagnostic)
        updated = {**profile, 'size': [current['bounds']['Width'], current['bounds']['Height']],
                   'header_rect': matched['rect'], 'composer_rect': editor,
                   'search_point': self._search_point(current),
                   'send_point': self._center(self._send_button(current))}
        final = self.capture(updated, diagnostic=diagnostic)
        self._same_window(current, final)
        self._title(final, updated)
        return updated, final

    def _act(self, action, capture, point=None, **values):
        args = {'window_id': capture['window_id'], 'bounds': capture['bounds'], **values}
        if point:
            args['x'], args['y'] = self._position(capture, point)
        return self.driver._bridge('visual_' + action, **args)

    @staticmethod
    def _position(capture, point):
        b = capture['bounds']
        return b['X'] + point[0] * b['Width'], b['Y'] + point[1] * b['Height']

    @staticmethod
    def _center(item):
        return [item['x'] + item['w'] / 2, item['y'] + item['h'] / 2]

    @staticmethod
    def _search_point(capture):
        b = capture['bounds']
        fields = [i for i in capture['regions']['sidebar']
                  if 55 < i['x'] * b['Width'] < 270 and 10 < i['y'] * b['Height'] < 65]
        if len(fields) > 1:
            text_fields = [i for i in fields if not re.fullmatch(r'[QqOo0〇○⌕🔍,，·。]+', compact(i['text']))]
            if len(text_fields) == 1:
                text = text_fields[0]
                icons = [i for i in fields if i is not text]
                if all(abs((i['y'] + i['h']/2 - text['y'] - text['h']/2) * b['Height']) < 8
                       and -8 < (text['x'] - i['x'] - i['w']) * b['Width'] < 24 for i in icons):
                    fields = text_fields
        if len(fields) != 1:
            raise AppError('SEARCH_LAYOUT_UNKNOWN', '未能唯一识别微信搜索框，请保持主窗口打开且搜索栏没有弹出菜单。')
        return VisualSession._center(fields[0])

    @staticmethod
    def _send_button(capture):
        buttons = [i for i in capture['regions']['lower'] if i['x'] > .30 and i['y'] > .72
                   and re.fullmatch(r'发送(?:[（(][sS][)）])?|Send(?:\(S\))?', compact(i['text']))]
        if len(buttons) != 1:
            raise AppError('SEND_BUTTON_UNKNOWN', '未能唯一识别当前群的发送按钮，未执行发送。')
        return buttons[0]

    def _search_open(self, query, search_point=None):
        before = self.capture()
        point = search_point or self._search_point(before)
        # A visible group title is stronger evidence than a truncated sidebar
        # result. It also permits binding a group the user has already opened.
        titles = self._matched_titles(before, query)
        if len(titles) == 1:
            return before, titles[0], point
        self._act('search', before, point, query=query)
        found = []
        for _ in range(3):
            time.sleep(.25)
            current = self.capture()
            found = candidates(current['regions']['sidebar'], query)
            if found:
                break
        if len(found) != 1:
            raise AppError('AMBIGUOUS_GROUP', '没有找到唯一、完整匹配的本地群聊。请输入完整群名，避免重名或名称被截断。')
        self._act('click', current, self._center(found[0]))
        for _ in range(3):
            time.sleep(.2)
            opened = self.capture()
            titles = self._matched_titles(opened, query)
            if len(titles) == 1:
                return opened, titles[0], point
        raise AppError('TITLE_MISMATCH', '打开后的群标题无法核对，已停止操作。')

    @staticmethod
    def _matched_titles(capture, query):
        return [i for i in capture['regions']['header']
                if i['x'] > .15 and name_match(i['text'], query)
                and re.search(r'[（(]\d+[)）]$', compact(i['text']))]

    @staticmethod
    def _layout_region(capture, title, send):
        b = capture['bounds']
        # Stay inside the editor border: even its antialiasing can otherwise
        # hide a horizontal separator in the per-row uniform-color scan.
        left = title.get('ink_box', title)['x']
        return {'x': left, 'y': .45,
                'w': min(.98, send['x'] + send['w'] + 12/b['Width']) - left,
                'h': send['y'] + .03 - .45}

    @staticmethod
    def _composer_from_rows(capture, title, send, rows):
        b = capture['bounds']
        colors = [round(row[3]) for row in rows if row[3] is not None
                  and send['y'] - .20 < row[0] < send['y'] - .04]
        if len(colors) < 5:
            raise AppError('COMPOSER_LAYOUT_UNKNOWN', '无法确认完整聊天输入区，已保留内容，未写入校验草稿。')
        background, count = Counter(colors).most_common(1)[0]
        if count < 5:
            raise AppError('COMPOSER_LAYOUT_UNKNOWN', '聊天输入区背景不稳定，未写入校验草稿。')
        rules = [row for row in rows if row[3] is not None
                 and .50 < row[0] < send['y'] - 45/b['Height']
                 and 8 <= abs(row[3] - background) <= 70]
        if not rules:
            raise AppError('COMPOSER_LAYOUT_UNKNOWN', '未能定位聊天输入区上边界，已保留内容，未写入校验草稿。')
        image_height = capture.get('image_size', {}).get('height', b['Height'] * 2)
        top = rules[-1][0] + 8/image_height
        bottom = send['y'] - 16/b['Height']
        if not 35 <= (bottom - top) * b['Height'] <= b['Height'] * .38:
            raise AppError('COMPOSER_LAYOUT_UNKNOWN', '聊天输入区高度无法核对，未写入校验草稿。')
        area = VisualSession._layout_region(capture, title, send)
        return {**area, 'y': top, 'h': bottom - top}

    def measure_composer(self, capture, title, send, *, diagnostic=False):
        area = self._layout_region(capture, title, send)
        scan = self.driver._bridge('capture', diagnostic=diagnostic, composer_rect=area, layout_profile=True)
        if scan['bounds'] != capture['bounds'] or scan['window_id'] != capture['window_id']:
            raise AppError('WINDOW_CHANGED', '窗口在区域校验时发生变化，未执行输入。')
        rows = scan['snapshots']['composer']['row_profile']
        try:
            return self._composer_from_rows(capture, title, send, rows), area, rows
        except AppError as exc:
            # Numeric evidence is useful when a separator cannot be detected;
            # never attach OCR text or an image to an error/diagnostic report.
            exc.layout_probe = {'layout_region': area, 'layout_rows': rows,
                                'image_size': scan.get('image_size'),
                                'pixel_size': scan['snapshots']['composer'].get('pixel_size'),
                                'reference_rgb': scan['snapshots']['composer'].get('reference_rgb')}
            raise

    @staticmethod
    def require_empty(capture):
        text = capture.get('regions', {}).get('composer', [])
        if text:
            raise AppError('EXISTING_DRAFT', '聊天输入框已有文字，已保留原有草稿，未写入测试内容。')
        if not capture['snapshots']['composer']['blank']:
            raise AppError('COMPOSER_REGION_UNCERTAIN', '输入区含非空像素，尚不能确认是草稿还是界面边缘。已保留内容，未执行输入。')

    def describe(self, current, title, point, query, *, preview=False, diagnostic=False):
        """Build an unconfirmed, read-only layout description from a current frame."""
        b = current['bounds']
        box = title.get('name_box') or {k: title[k] for k in ('x', 'y', 'w', 'h')}
        if not box['w'] or not box['h']:
            raise AppError('TITLE_MISMATCH', '群标题没有完整显示，请使用较短且唯一的群备注。')
        header = {'x': max(0, box['x'] - 3/b['Width']), 'y': max(0, box['y'] - 3/b['Height']),
                  'w': min(1-box['x'], box['w'] + 6/b['Width']), 'h': box['h'] + 6/b['Height']}
        send = self._send_button(current)
        # Detect the full text area above the Send row. A lower strip alone can
        # miss the first draft line and include the scrollbar on the right.
        editor, _, _ = self.measure_composer(current, title, send, diagnostic=diagnostic)
        profile = {'protocol': PROTOCOL, 'wechat_version': self.driver._native('health')['wechat_version'],
                   'size': [b['Width'], b['Height']], 'header_rect': header,
                   'composer_rect': editor, 'search_point': point, 'send_point': self._center(send)}
        proof = self.capture(profile, preview=preview, diagnostic=diagnostic)
        self._same_window(current, proof)
        profile['header_hash'] = proof['snapshots']['header']['hash']
        image = proof['image_size']
        pixels = proof['snapshots']['header']['pixel_size']
        anchor_box = title.get('ink_box', box)
        profile['title_anchor'] = {
            'origin_type': 'ink' if title.get('ink_box') else 'ocr',
            'offset': [math.floor(header['x'] * image['width']) - anchor_box['x'] * image['width'],
                       math.floor(header['y'] * image['height']) - anchor_box['y'] * image['height']],
            'pixel_size': pixels,
            'scale': [image['width'] / b['Width'], image['height'] / b['Height']],
        }
        return profile, proof

    def search(self, query):
        current, title, point = self._search_open(query)
        profile, proof = self.describe(current, title, point, query, preview=True)
        return [{'group_name': query, 'preview': proof['snapshots']['header']['preview'],
                 'selection_source': 'window_title',
                 'requires_visual_confirmation': True,
                 '_visual_profile': profile}]

    @staticmethod
    def _title(capture, profile):
        if capture['snapshots']['header']['hash'] != profile['header_hash']:
            raise AppError('TITLE_CHANGED', '当前群标题图像与绑定时不一致，请重新核对或绑定；未发送。')

    def _view(self, capture, profile, page):
        rect = profile['composer_rect']
        point = [rect['x'] + 12/capture['bounds']['Width'], rect['y'] + 10/capture['bounds']['Height']]
        sx, sy = self._position(capture, profile['search_point'])
        self._act('view', capture, point, page=page, search_x=sx, search_y=sy)
        result = self.capture(profile)
        self._title(result, profile)
        return result

    def _clear_owned(self, capture, profile):
        self._title(capture, profile)
        latest = self.capture(profile)
        self._title(latest, profile)
        if latest['snapshots']['composer']['hash'] != capture['snapshots']['composer']['hash']:
            raise AppError('DRAFT_CHANGED', '输入框在核对后发生变化，已保留内容，请手动检查。')
        rect = profile['composer_rect']
        point = [rect['x'] + 12/latest['bounds']['Width'], rect['y'] + 10/latest['bounds']['Height']]
        sx, sy = self._position(latest, profile['search_point'])
        self._act('clear', latest, point, search_x=sx, search_y=sy)
        cleared = self.capture(profile)
        self._title(cleared, profile)
        if not cleared['snapshots']['composer']['blank']:
            raise AppError('CLEAR_FAILED', '本次校验草稿未能清空，请在微信中核对；未发送。')
        return cleared

    def bind(self, name, profile, confirmed):
        if not confirmed:
            raise AppError('VISUAL_CONFIRMATION_REQUIRED', '请核对实际群标题预览，并确认该群没有需要保留的草稿。')
        profile, current = self.relocate(name, profile)
        self._title(current, profile)
        self.require_empty(current)
        prefix, suffix = 'CALIBRATIONSTARTZXK', 'CALIBRATIONENDMNP'
        sample = prefix + ' ' + ' '.join(f'sample{i:04d}' for i in range(220)) + ' ' + suffix
        rect = profile['composer_rect']
        point = [rect['x'] + 12/current['bounds']['Width'], rect['y'] + 10/current['bounds']['Height']]
        self._act('write', current, point, message=sample)
        owned = None
        try:
            owned = self._view(self.capture(profile), profile, 0)
            starts = [i for i in owned['regions']['lower'] if prefix in draft_text(i['text'])]
            if len(starts) != 1:
                raise AppError('EDITOR_CALIBRATION_FAILED', '输入框开头定位未通过，暂不启用后台发送。')
            send = self._send_button(owned)
            top = starts[0]['y'] - 3/owned['bounds']['Height']
            left = starts[0]['x'] - 3/owned['bounds']['Width']
            if top < .5 or send['y'] - top < .025:
                raise AppError('EDITOR_CALIBRATION_FAILED', '输入框区域无法核对。')
            own_rows = [i for i in owned['regions']['lower']
                        if re.search(r'sample\d{4}', i['text']) and i['y'] >= top]
            right = max([send['x'] + send['w'] + 12/owned['bounds']['Width']]
                        + [i['x'] + i['w'] + 5/owned['bounds']['Width'] for i in own_rows])
            if right > rect['x'] + rect['w']:
                raise AppError('EDITOR_CALIBRATION_FAILED', '输入框右边界无法核对。')
            profile['composer_rect'] = {'x': left, 'y': top, 'w': rect['x'] + rect['w'] - left,
                                        'h': send['y'] - 10/owned['bounds']['Height'] - top}
            owned = self.capture(profile)
            owned = self._view(owned, profile, 'end')
            if suffix not in draft_text(reading_order(owned['regions']['composer'])):
                raise AppError('EDITOR_CALIBRATION_FAILED', '输入框末尾定位未通过，暂不启用后台发送。')
            page = self._view(owned, profile, 1)
            owned = page
            body = draft_text(reading_order(page['regions']['composer']))
            if prefix in body or not body:
                raise AppError('EDITOR_CALIBRATION_FAILED', '输入框翻页未通过，暂不启用后台发送。')
        finally:
            if owned is not None:
                self._clear_owned(owned, profile)
        self._save(name, profile)
        self.driver._health_at = 0
        return name

    def profile(self, name):
        profile = self._load().get(name)
        if not profile or profile.get('protocol') != PROTOCOL:
            raise AppError('PROFILE_REQUIRED', '目标群尚未完成后台校验，请重新绑定。')
        if profile['wechat_version'] != self.driver._native('health')['wechat_version']:
            raise AppError('CLIENT_CHANGED', '微信版本已变化，请重新绑定并校验后台输入。')
        return profile

    def open_group(self, name, marker=None):
        profile = self.profile(name)
        if profile.get('title_anchor'):
            # Refresh even at the same outer size: the sidebar/editor split may
            # have changed. Never reuse its old search or send coordinates.
            self._search_open(name)
            profile, current = self.relocate(name, profile)
            if marker:
                current = self.capture(profile, marker=marker)
            return profile, current
        current = self.capture(profile, marker=marker)
        if current['snapshots']['header']['hash'] != profile['header_hash']:
            self._search_open(name, profile['search_point'])
            current = self.capture(profile, marker=marker)
        self._title(current, profile)
        return profile, current

    def _read_expected(self, capture, profile, expected):
        expected = draft_text(expected)
        covered, previous = 0, ''
        for page in range(25):
            capture = self._view(capture, profile, page)
            fragment = draft_text(reading_order(capture['regions']['composer']))
            if not fragment or fragment == previous:
                break
            positions = [match.start() for match in re.finditer('(?=' + re.escape(fragment) + ')', expected)]
            possible = [p for p in positions if p <= covered < p + len(fragment)]
            if len(possible) != 1 or (page == 0 and possible[0] != 0):
                break
            covered = possible[0] + len(fragment)
            if covered == len(expected):
                return capture
            previous = fragment
        raise AppError('DRAFT_MISMATCH', '通知内容未能逐页完整核对，未发送。请缩短内容或检查输入框中的草稿。')

    def prepare(self, task):
        marker = 'WN-' + task['id'][:12].upper()
        profile, current = self.open_group(task['group_name'], marker)
        if current['outgoing_marker']:
            return 'already_observed'
        for page in (0, 'end'):
            current = self._view(current, profile, page)
            self.require_empty(current)
        rect = profile['composer_rect']
        point = [rect['x'] + 12/current['bounds']['Width'], rect['y'] + 10/current['bounds']['Height']]
        self._act('write', current, point, message=task['message'])
        checked = self._read_expected(self.capture(profile), profile, task['message'])
        self.prepared = {task['id']: (profile, checked['snapshots']['composer']['hash'])}
        return 'prepared'

    def abort(self, task):
        prepared = self.prepared.pop(task['id'], None)
        if prepared:
            profile, expected = prepared
            current = self.capture(profile)
            if current['snapshots']['composer']['hash'] == expected:
                self._clear_owned(current, profile)

    def send(self, task):
        prepared = self.prepared.pop(task['id'], None)
        if not prepared:
            raise AppError('DRAFT_MISMATCH', '缺少已核对的本次通知草稿，未发送。')
        profile, expected = prepared
        current = self.capture(profile)
        self._title(current, profile)
        if current['snapshots']['composer']['hash'] != expected:
            raise AppError('DRAFT_CHANGED', '发送前草稿发生变化，未点击发送。')
        button = self._send_button(current)
        self._act('click', current, self._center(button))
        marker = 'WN-' + task['id'][:12].upper()
        for _ in range(4):
            time.sleep(.3)
            proof = self.capture(profile, marker=marker)
            self._title(proof, profile)
            if proof['outgoing_marker'] and proof['snapshots']['composer']['blank']:
                return 'observed'
        raise AppError('RESULT_UNKNOWN', '已点击发送，但未同时确认本地消息与空输入框；请核对通知编号，队列已暂停。')
