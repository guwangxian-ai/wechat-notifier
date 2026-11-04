import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from notifier.app import create_app
from notifier.driver import Controller, MacDriver, DryRunDriver
from notifier.errors import AppError
from notifier.models import Notification
from notifier.store import Store
from notifier.worker import Worker


NATIVE = {'wechat_running': True, 'accessibility': True, 'screen_recording': True,
          'screen_locked': False, 'wechat_version': '4.1.15'}
UNSUPPORTED = {'supported': False, 'editor_count': 0, 'search_count': 0, 'nodes': 5}


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.driver = MacDriver(self.temp.name)
        self.driver._native = Mock(return_value=NATIVE)
        self.driver._bridge = Mock(return_value=UNSUPPORTED)
        self.platform = patch('notifier.driver.sys.platform', 'darwin')
        self.platform.start()

    def tearDown(self):
        self.platform.stop()
        self.temp.cleanup()

    def test_permissions_alone_do_not_report_ready(self):
        result = self.driver.health(fresh=True)
        self.assertEqual(result['execution_mode'], 'background')
        self.assertTrue(all(check['ok'] for check in result['checks'][:-1]))
        self.assertFalse(result['ready'])
        self.assertFalse(result['checks'][-1]['ok'])

    def test_supported_controls_pass_without_running_input_actions(self):
        self.driver._bridge.return_value = {'supported': True, 'search_count': 1, 'editor_count': 1}
        result = self.driver.health(fresh=True)
        self.assertTrue(result['ready'])
        self.driver._bridge.assert_called_once_with('capabilities')

    def test_unsupported_background_keeps_task_unclaimed_across_restart(self):
        store = Store(self.temp.name)
        store.bind('leads', '测试通知群', '测试通知群', 'live')
        task, _ = store.enqueue(Notification(event_id='test:background', target_code='leads', body='测试'), 'admin')
        store.update_settings({'dispatch_enabled': True})
        for _ in range(2):
            worker = Worker(store, Controller(self.driver))
            self.assertFalse(worker.tick())
        record = store.get(task['id'])
        self.assertEqual(record['status'], 'pending')
        self.assertEqual(record['attempts'], 0)
        self.driver._bridge.assert_called_once_with('capabilities')

    def test_search_stops_before_any_input_when_unsupported(self):
        with self.assertRaises(AppError):
            self.driver.search('测试通知群')
        self.driver._bridge.assert_called_once_with('capabilities')

    def test_user_using_wechat_is_not_a_permissions_failure(self):
        self.driver._bridge.side_effect = AppError('WECHAT_IN_USE', '微信正在前台使用。')
        status = self.driver.health(fresh=True)
        self.assertFalse(status['ready'])
        self.assertEqual(status['background']['error'], 'WECHAT_IN_USE')
        self.assertTrue(status['checks'][1]['ok'])

    def test_capture_diagnostic_does_not_return_chat_text(self):
        self.driver._capture = Mock(return_value={'items': [{'text': 'private text'}],
                                                  'foreground_unchanged': True})
        result = self.driver.background_check()
        self.assertNotIn('private text', json.dumps(result))
        self.assertEqual(result['window_capture']['text_count'], 1)
        self.assertTrue(result['window_capture']['foreground_unchanged'])
        self.assertFalse(result['ready'])
        self.driver._bridge.assert_called_once_with('capabilities')

    def test_foreground_composer_diagnostic_is_read_only_and_redacts_draft(self):
        self.driver.health = Mock(return_value={'ready': False, 'checks': [
            {'name': '辅助功能', 'ok': True}, {'name': '后台控件', 'ok': False}],
            'background': {'error': 'WECHAT_IN_USE'}})
        fixture = json.loads((Path(__file__).parent / 'fixtures/editor-layout-4.1.15.json').read_text())
        capture = {**fixture, 'items': [],
                   'regions': {'header': [{'text': '测试通知群（13）', 'x': .3, 'y': .03, 'w': .2, 'h': .03}],
                               'sidebar': [], 'lower': [{'text': '发送', 'x': .8, 'y': .94, 'w': .04, 'h': .03}]}}
        probe = {'snapshots': {'composer': {'blank': False, 'ink_pixels': 120}},
                 'regions': {'composer': [{'text': 'private personal draft', 'x': .3, 'y': .85, 'w': .2, 'h': .03}]}}
        scan = {**fixture, 'snapshots': {'composer': {'row_profile': fixture['rows']}}}
        self.driver._bridge = Mock(side_effect=[capture, scan, probe])
        result = self.driver.background_check('测试通知群')
        self.assertNotIn('private personal draft', json.dumps(result))
        self.assertFalse(result['composer_probe']['pixels']['blank'])
        self.assertEqual(result['composer_probe']['text_boxes'][0]['characters'], 22)
        self.assertEqual([call.args for call in self.driver._bridge.call_args_list], [('capture',)] * 3)
        self.assertTrue(all(call.kwargs['diagnostic'] for call in self.driver._bridge.call_args_list))
        self.assertFalse(result['ready'])

    def test_existing_draft_is_never_replaced(self):
        self.driver.open_group = Mock(return_value={'items': []})
        self.driver._bridge.return_value = {'text': '已有草稿'}
        with self.assertRaises(AppError) as caught:
            self.driver.prepare({'id': 'a' * 32, 'group_name': '测试通知群', 'message': '测试'})
        self.assertEqual(caught.exception.code, 'EXISTING_DRAFT')
        self.driver._bridge.assert_called_once_with('read', message='')

    def test_layout_diagnostic_baseline_is_unconfirmed_and_never_a_saved_binding(self):
        self.driver.health = Mock(return_value={'ready': False, 'checks': []})
        frame = {'items': [], 'bounds': {'Width': 1000, 'Height': 800},
                 'regions': {'header': [{'text': '测试群（3）', 'x': .3, 'y': .03, 'w': .2, 'h': .03}],
                             'sidebar': [{'text': '搜索', 'x': .1, 'y': .03, 'w': .04, 'h': .03}], 'lower': []}}
        proof = {'snapshots': {'composer': {'blank': False}},
                 'regions': {'composer': [{'text': 'private original draft'}]}}
        profile = {'size': [1000, 800], 'composer_rect': {'x': .3}, 'header_hash': 'immutable'}
        resized = {**profile, 'size': [900, 700]}
        self.driver._bridge = Mock(return_value=frame)
        self.driver.visual.describe = Mock(return_value=(profile, proof))
        self.driver.visual.relocate = Mock(return_value=(resized, proof))
        first = self.driver.background_check('测试群', True)
        second = self.driver.background_check('测试群', True)
        self.assertTrue(first['layout_verification']['baseline_created'])
        self.assertFalse(second['layout_verification']['baseline_created'])
        self.assertEqual(second['layout_verification']['previous_size'], [1000, 800])
        self.assertEqual(second['layout_verification']['current_size'], [900, 700])
        self.assertEqual(second['layout_verification']['draft_guard'], 'EXISTING_DRAFT')
        self.assertNotIn('private original draft', json.dumps(second))
        self.assertFalse(self.driver.visual.path.exists())
        self.assertFalse(second['ready'])
        self.driver.visual.relocate.assert_called_once_with('测试群', profile, diagnostic=True)

    def test_layout_diagnostic_failure_keeps_original_identity_baseline(self):
        self.driver.health = Mock(return_value={'ready': False, 'checks': []})
        frame = {'items': [], 'bounds': {'Width': 1000, 'Height': 800},
                 'regions': {'header': [{'text': '测试群（3）', 'x': .3, 'y': .03, 'w': .2, 'h': .03}],
                             'sidebar': [], 'lower': []}}
        self.driver._bridge = Mock(return_value=frame)
        self.driver._layout_probe = ('测试群', {'header_hash': 'original'})
        self.driver.visual.relocate = Mock(side_effect=AppError('TITLE_CHANGED', '标题变化'))
        report = self.driver.background_check('测试群', True)
        self.assertFalse(report['layout_verification']['ok'])
        self.assertEqual(self.driver._layout_probe, ('测试群', {'header_hash': 'original'}))
        self.assertFalse(self.driver.visual.path.exists())

    def test_focus_probe_requires_a_unique_empty_search_field(self):
        field = {'text': '搜索', 'x': .15, 'y': .08, 'w': .04, 'h': .02, 'confidence': .99}
        for items in ([], [field, field], [{**field, 'text': '已有搜索内容'}]):
            self.driver._capture = Mock(return_value={'items': items})
            with self.assertRaises(AppError) as caught:
                self.driver.focus_search_check()
            self.assertEqual(caught.exception.code, 'EMPTY_SEARCH_NOT_FOUND')
        self.driver._bridge.assert_not_called()

    def test_focus_probe_only_clicks_search_and_redacts_chat_text(self):
        capture = {'items': [
            {'text': '搜索', 'x': .1, 'y': .08, 'w': .04, 'h': .02, 'confidence': .99},
            {'text': 'private chat', 'x': .6, 'y': .3, 'w': .1, 'h': .02, 'confidence': .99}],
            'bounds': {'X': 100, 'Y': 50, 'Width': 1000, 'Height': 800}}
        self.driver._capture = Mock(return_value=capture)
        self.driver._bridge.return_value = {'foreground_unchanged': True}
        report = self.driver.focus_search_check()
        self.assertEqual(self.driver._bridge.call_count, 1)
        self.assertEqual(self.driver._bridge.call_args.args, ('focus_search',))
        self.assertAlmostEqual(self.driver._bridge.call_args.kwargs['x'], 220)
        self.assertAlmostEqual(self.driver._bridge.call_args.kwargs['y'], 122)
        self.assertNotIn('private chat', json.dumps(report))
        self.assertFalse(report['sent'])

    def test_native_body_goes_only_to_stdin(self):
        driver = MacDriver(self.temp.name)
        with patch.object(Path, 'is_file', return_value=True), patch('notifier.driver.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok":true}', '')
            driver._composer('write', '通知\n特殊字符 $(`x`)')
        command = run.call_args.args[0]
        self.assertEqual(len(command), 1)
        self.assertEqual(Path(command[0]).name, 'background-bridge')
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['message'], '通知\n特殊字符 $(`x`)')
        self.assertEqual(list(Path(self.temp.name).glob('composer-*')), [])

    def test_native_error_is_not_retried_with_foreground_tool(self):
        driver = MacDriver(self.temp.name)
        with patch.object(Path, 'is_file', return_value=True), patch.object(driver, '_run') as run:
            run.return_value = '{"error":"BACKGROUND_UNSUPPORTED","message":"不兼容"}'
            with self.assertRaises(AppError) as error:
                driver._composer('write', '测试')
            self.assertEqual(error.exception.code, 'BACKGROUND_UNSUPPORTED')
            self.assertEqual(run.call_count, 1)

    def test_diagnostic_route_is_admin_only_and_cannot_enable_dispatch(self):
        app = create_app(self.temp.name, self.driver, start_worker=False)
        self.driver._capture = Mock(return_value={'items': []})
        with TestClient(app, base_url='http://127.0.0.1') as client:
            key = app.state.credentials
            self.assertEqual(client.post('/api/v1/background/check', json={}).status_code, 401)
            self.assertEqual(client.post('/api/v1/background/check', json={}, headers={
                'Authorization': 'Bearer ' + key.ingest}).status_code, 403)
            response = client.post('/api/v1/background/check', json={}, headers={
                'Authorization': 'Bearer ' + key.admin})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['ready'])
            self.assertFalse(app.state.store.settings()['dispatch_enabled'])
            self.assertEqual(app.state.store.list()['total'], 0)

    def test_dry_run_cannot_masquerade_as_background_compatibility(self):
        app = create_app(self.temp.name, DryRunDriver(), start_worker=False)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            response = client.post('/api/v1/background/check', json={}, headers={
                'Authorization': 'Bearer ' + app.state.credentials.admin})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['error']['code'], 'DRY_RUN')


if __name__ == '__main__':
    unittest.main()
