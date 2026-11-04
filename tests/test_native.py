import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from notifier.app import create_app
from notifier.driver import Controller
from notifier.errors import AppError
from notifier.models import Notification
from notifier.native import BridgeHTTP, NativeDriver, PROTOCOL, UPSTREAM_COMMIT, bridge_url, load_config, save_config
from notifier.store import Store
from notifier.worker import Worker


class BridgeFixture:
    def __init__(self):
        self.state = {'protocol': PROTOCOL, 'upstream_commit': UPSTREAM_COMMIT,
                      'version': '4.1.15.55', 'build': '270135', 'account_id': 'wxid_test',
                      'session_id': 'test-session', 'ready': True}
        self.groups = [{'group_id': '100@chatroom', 'group_name': '测试通知群✅'},
                       {'group_id': '200@chatroom', 'group_name': '测试通知群✅'}]
        self.calls, self.failure, self.bad_receipt = [], False, False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.failure:
            raise AppError('BRIDGE_UNAVAILABLE', '连接异常', 503)
        if path == '/notifier/status':
            return {'status': 'ok', 'data': copy.deepcopy(self.state)}
        if path == '/notifier/groups':
            return {'status': 'ok', 'data': {**self.state, 'groups': copy.deepcopy(self.groups)}}
        if path == '/notifier/submit':
            return {'status': 'ok', 'receipt': 'observed' if self.bad_receipt else 'submitted',
                    'request_id': payload['request_id']}
        raise AssertionError(path)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = BridgeFixture()
        self.driver = NativeDriver(self.temp.name, self.bridge, lambda: ('4.1.15.55', '270135'))
        self.store = Store(self.temp.name)
        self.store.bind('leads', '通知群', '测试通知群✅', 'live', backend='onebot',
                        group_id='200@chatroom', account_id='wxid_test')
        self.controller = Controller(self.driver)
        self.worker = Worker(self.store, self.controller)
        self.store.update_settings({'dispatch_enabled': True})

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self):
        return self.store.enqueue(Notification(event_id='native:1', target_code='leads',
                                               body='两行正文\n中文、emoji✅与 https://example.com'), 'admin')[0]

    def test_native_flow_preserves_exact_recipient_and_reports_submission(self):
        task = self.enqueue()
        self.assertTrue(self.worker.tick())
        self.assertEqual(self.store.get(task['id'])['status'], 'submitted')
        sends = [c for c in self.bridge.calls if c[1] == '/notifier/submit']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][2]['group_id'], '200@chatroom')
        self.assertEqual(sends[0][2]['text'], task['message'])
        self.assertFalse(self.worker.tick())
        self.store.task_action(task['id'], 'confirm')
        self.assertEqual(self.store.get(task['id'])['status'], 'confirmed_manually')

    def test_same_named_groups_have_distinct_candidate_ids(self):
        found = self.controller.search('测试通知群✅')
        self.assertEqual(len(found), 2)
        self.assertNotEqual(found[0]['candidate_id'], found[1]['candidate_id'])
        self.assertNotEqual(found[0]['group_id'], found[1]['group_id'])
        self.assertNotIn('_account_id', found[0])

    def test_binding_requires_confirmation_and_rechecks_id(self):
        rows = self.driver.search('测试通知群')
        with self.assertRaises(AppError):
            self.driver.verify_candidate(rows[0], False)
        self.bridge.groups[0]['group_name'] = '已更名'
        with self.assertRaises(AppError):
            self.driver.verify_candidate(rows[0], True)

    def test_account_change_prevents_native_call(self):
        task = self.enqueue()
        self.bridge.state['account_id'] = 'wxid_other'
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'failed')
        self.assertFalse(any(c[1] == '/notifier/submit' for c in self.bridge.calls))

    def test_old_visual_target_cannot_be_sent_through_native_backend(self):
        self.store.bind('legacy', '旧群', '测试通知群✅', 'live')
        task = self.store.enqueue(Notification(event_id='old:1', target_code='legacy', body='旧任务'), 'admin')[0]
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['error_code'], 'TARGET_CHANGED')
        self.assertFalse(any(c[1] == '/notifier/submit' for c in self.bridge.calls))

    def test_unknown_send_pauses_and_never_automatically_retries(self):
        task = self.enqueue()
        self.bridge.bad_receipt = True
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'unknown')
        self.assertFalse(self.store.settings()['dispatch_enabled'])
        self.worker.tick()
        self.assertEqual(len([c for c in self.bridge.calls if c[1] == '/notifier/submit']), 1)

    def test_bridge_disconnection_after_prepare_is_unknown(self):
        task = self.enqueue()
        original = self.driver.send
        def disconnect(value):
            self.bridge.failure = True
            return original(value)
        self.driver.send = disconnect
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'unknown')

    def test_manual_retry_uses_new_bridge_attempt_id(self):
        task = self.enqueue()
        self.bridge.bad_receipt = True
        self.worker.tick()
        self.store.task_action(task['id'], 'retry', checked=True)
        self.bridge.bad_receipt = False
        self.store.update_settings({'dispatch_enabled': True})
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET dispatch_at=NULL WHERE id=?', (task['id'],))
        self.assertTrue(self.worker.tick())
        requests = [c[2]['request_id'] for c in self.bridge.calls if c[1] == '/notifier/submit']
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(*requests)
        self.assertEqual(self.store.get(task['id'])['status'], 'submitted')

    def test_version_gate_refuses_app_store_or_different_build(self):
        self.driver.version_reader = lambda: ('4.1.15.35', '270115')
        self.assertFalse(self.driver.health()['ready'])
        self.assertEqual(self.bridge.calls, [])
        self.driver.version_reader = lambda: ('4.1.15.55', 'unexpected')
        self.assertFalse(self.driver.health(fresh=True)['ready'])

    def test_wrong_bridge_protocol_or_source_is_rejected(self):
        for key in ('protocol', 'upstream_commit', 'build'):
            value = self.bridge.state[key]
            self.bridge.state[key] = 'unexpected'
            self.assertFalse(self.driver.health(fresh=True)['ready'])
            self.bridge.state[key] = value

    def test_malformed_status_fails_as_app_error(self):
        self.bridge.state = []
        self.assertFalse(self.driver.health()['ready'])

    def test_unseeded_connection_keeps_tasks_pending(self):
        task = self.enqueue()
        self.bridge.state['ready'] = False
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'pending')

    def test_native_prepare_and_abort_have_no_chat_mutations(self):
        task = self.enqueue()
        self.driver.prepare(task)
        self.driver.abort(task)
        with self.assertRaises(AppError):
            self.driver.send(task)
        self.assertTrue(all(c[0] == 'GET' for c in self.bridge.calls))

    def test_invalid_or_duplicate_group_ids_fail_closed(self):
        self.bridge.groups.append(copy.deepcopy(self.bridge.groups[0]))
        with self.assertRaises(AppError):
            self.driver.search('测试通知群')
        self.bridge.groups = [{'group_id': 'wxid_private', 'group_name': '私人聊天'}]
        with self.assertRaises(AppError):
            self.driver.search('私人聊天')

    def test_health_uses_bounded_cache(self):
        self.driver.health()
        self.driver.health()
        self.assertEqual(len(self.bridge.calls), 1)
        self.driver.health(fresh=True)
        self.assertEqual(len(self.bridge.calls), 2)

    def test_api_binds_id_and_never_exposes_bridge_token(self):
        save_config(self.temp.name, 'http://127.0.0.1:58080', 'private-bridge-secret-for-tests')
        app = create_app(self.temp.name, self.driver, start_worker=False)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            admin = {'Authorization': 'Bearer ' + app.state.credentials.admin}
            rows = client.post('/api/v1/targets/search', json={'query':'测试通知群'}, headers=admin).json()['items']
            result = client.post('/api/v1/targets/bind', headers=admin, json={
                'candidate_id': rows[1]['candidate_id'], 'code': 'native-new', 'label': '新绑定', 'confirmed_visual': True})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['group_id'], '200@chatroom')
            config = client.get('/api/v1/native/connection', headers=admin)
            self.assertTrue(config.json()['has_token'])
            self.assertNotIn('private-bridge-secret', config.text)
            self.assertEqual(client.get('/api/v1/native/connection').status_code, 401)


class ConfigAndTransportTests(unittest.TestCase):
    def test_only_literal_loopback_without_credentials_or_paths(self):
        for url in ('http://localhost:58080', 'https://127.0.0.1:58080', 'http://127.0.0.1:58080/path',
                    'http://127.0.0.1:58080?token=bad', 'http://u:p@127.0.0.1:58080',
                    'http://127.0.0.1:58080#bad', 'http://example.com:58080', 'http://127.0.0.1:80'):
            with self.subTest(url=url), self.assertRaises(AppError):
                bridge_url(url)
        self.assertEqual(bridge_url('http://127.0.0.1:58080/'), 'http://127.0.0.1:58080')

    def test_config_permissions_and_blank_token_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = save_config(directory, 'http://127.0.0.1:58080')
            self.assertGreaterEqual(len(saved['token']), 24)
            self.assertEqual(save_config(directory, 'http://127.0.0.1:58081')['token'], saved['token'])
            path = Path(directory) / 'native-bridge.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            with self.assertRaises(AppError):
                load_config(directory)

    def test_user_client_path_is_preserved_when_connection_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            app=str(Path.home()/'Applications/WeChat.app')
            save_config(directory,'http://127.0.0.1:58080',wechat_app=app)
            updated=save_config(directory,'http://127.0.0.1:58081')
            self.assertEqual(updated['wechat_app'],app)
            with self.assertRaises(AppError):
                save_config(directory,'http://127.0.0.1:58080',wechat_app='/tmp/unknown.app')

    def test_real_http_client_authentication_json_and_redirect_refusal(self):
        recorded = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                recorded.append(self.headers.get('Authorization'))
                self.send_response(302 if self.path == '/redirect' else 200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok","data":{}}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = BridgeHTTP({'base_url':f'http://127.0.0.1:{server.server_port}', 'token':'test-http-secret'})
            self.assertEqual(client.request('GET', '/notifier/status')['data'], {})
            with self.assertRaises(AppError):
                client.request('GET', '/redirect')
            self.assertEqual(recorded, ['Bearer test-http-secret']*2)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_additive_migration_preserves_old_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.bind('old', '旧群', '原有群', 'live')
            task = store.enqueue(Notification(event_id='old:1', target_code='old', body='原内容'), 'admin')[0]
            with store.connect() as db:
                for table in ('tasks','targets'):
                    for column in ('backend','group_id','account_id'):
                        db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
            reopened = Store(directory)
            self.assertEqual(reopened.get(task['id'])['message'], task['message'])
            self.assertEqual(reopened.get(task['id'])['backend'], 'visual')

    def test_directory_failure_uses_known_reason_without_exposing_raw_errors(self):
        reason = ['key_missing']
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(503)
                self.end_headers()
                self.wfile.write(json.dumps({'status':'failed', 'reason':reason[0],
                                             'last_error':'private-path-and-key'}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = BridgeHTTP({'base_url':f'http://127.0.0.1:{server.server_port}', 'token':'test-http-secret'})
            for known, expected in [('key_missing','GROUP_KEY_MISSING'), ('unsupported','GROUP_DIRECTORY_UNSUPPORTED'),
                                    ('decrypt_failed','GROUP_DIRECTORY_DECRYPT_FAILED'), ('db_missing','GROUP_DATABASE_UNAVAILABLE'),
                                    ('private-path-and-key','BRIDGE_RESPONSE_INVALID'), ([], 'BRIDGE_RESPONSE_INVALID')]:
                reason[0] = known
                with self.subTest(reason=known), self.assertRaises(AppError) as caught:
                    client.request('GET', '/notifier/groups')
                self.assertEqual(caught.exception.code,expected)
                self.assertNotIn('private-path-and-key',caught.exception.message)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
