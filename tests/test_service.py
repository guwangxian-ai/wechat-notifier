from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from notifier.app import create_app
from notifier.driver import Controller, DryRunDriver, canonical, group_candidates, marker_visible, title_matches
from notifier.errors import AppError
from notifier.models import Notification
from notifier.security import InstanceLock
from notifier.store import Store
from notifier.worker import Worker


class FakeDriver(DryRunDriver):
    def __init__(self):
        self.sent = []
        self.abort_count = 0
        self.on_prepare = None
        self.failure = None
        self.available = True

    def health(self, fresh=False):
        return {'ready': self.available, 'mode': self.mode, 'checks': []}

    def prepare(self, task):
        if self.on_prepare:
            return self.on_prepare(task) or 'prepared'
        return 'prepared'

    def abort(self, task):
        self.abort_count += 1

    def send(self, task):
        self.sent.append(task['id'])
        if self.failure:
            raise self.failure
        return 'simulated'


def payload(event='lead:1', body='需求原话\nhttps://example.com/source'):
    return {'event_id': event, 'target_code': 'leads', 'title': '测试通知', 'body': body}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.driver = FakeDriver()
        self.app = create_app(self.temp.name, driver=self.driver, start_worker=False)
        self.client = TestClient(self.app, base_url='http://127.0.0.1')
        self.client.__enter__()
        self.admin = {'Authorization': 'Bearer ' + self.app.state.credentials.admin}
        self.ingest = {'Authorization': 'Bearer ' + self.app.state.credentials.ingest}
        self.app.state.store.bind('leads', '测试群', '线索测试群', 'dry_run')

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def submit(self, data=None, headers=None):
        return self.client.post('/api/v1/notifications', json=data or payload(), headers=headers or self.admin)

    def test_authentication_and_role_boundary(self):
        self.assertEqual(self.client.get('/api/v1/tasks').status_code, 401)
        self.assertEqual(self.client.get('/api/v1/status', headers=self.ingest).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/integration-key', json={}, headers=self.ingest).status_code, 403)
        self.assertEqual(self.client.patch('/api/v1/settings', json={'dispatch_enabled': True}, headers=self.ingest).status_code, 403)

    def test_root_and_security_headers(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('通知任务', response.text)
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(self.client.get('/api/v1/openapi.json').status_code, 401)

    def test_idempotency_and_payload_conflict(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['task_id'], second.json()['task_id'])
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(self.submit(payload(body='不同内容')).status_code, 409)

    def test_submission_is_not_delivery(self):
        response = self.submit()
        self.assertEqual(response.json()['status'], 'pending')
        self.assertEqual(self.driver.sent, [])

    def test_ingest_cannot_read_admin_tasks_or_counts(self):
        admin_id = self.submit().json()['task_id']
        ingest_id = self.submit(payload('ingest:1'), self.ingest).json()['task_id']
        listing = self.client.get('/api/v1/tasks', headers=self.ingest).json()
        self.assertEqual(listing['total'], 1)
        self.assertEqual(listing['counts']['pending'], 1)
        self.assertEqual(listing['items'][0]['id'], ingest_id)
        self.assertEqual(self.client.get('/api/v1/tasks/' + admin_id, headers=self.ingest).status_code, 404)

    def test_origin_and_host_protection(self):
        response = self.submit(headers={**self.admin, 'Origin': 'https://other.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/healthz', headers={'Host': 'evil.example'}).status_code, 400)

    def test_size_and_content_type(self):
        self.assertEqual(self.client.post('/api/v1/notifications', content='x' * 40000, headers=self.admin).status_code, 413)
        self.assertEqual(self.client.post('/api/v1/notifications', content='text', headers=self.admin).status_code, 415)

    def test_validation_boundaries(self):
        for data in [payload(body='   '), payload(body='x\x00y'), {**payload(), 'target_code': '../bad'},
                     {**payload(), 'expires_in_seconds': True}, {**payload(), 'extra': 'unknown'}]:
            with self.subTest(data=data):
                self.assertEqual(self.submit(data).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/targets/search', json={'query': '  '}, headers=self.admin).status_code, 422)

    def test_target_binding_requires_ephemeral_candidate(self):
        data = {'candidate_id': '0' * 32, 'code': 'new-group', 'label': '新群'}
        self.assertEqual(self.client.post('/api/v1/targets/bind', json=data, headers=self.admin).status_code, 409)
        found = self.client.post('/api/v1/targets/search', json={'query': '新的通知群'}, headers=self.admin).json()
        data['candidate_id'] = found['items'][0]['candidate_id']
        result = self.client.post('/api/v1/targets/bind', json=data, headers=self.admin)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['group_name'], '新的通知群')
        self.assertEqual(self.client.post('/api/v1/targets/bind', json=data, headers=self.admin).status_code, 409)

    def test_disabled_target_cancels_only_unsent_tasks(self):
        task_id = self.submit().json()['task_id']
        self.assertEqual(self.client.post('/api/v1/targets/leads/disable', json={}, headers=self.admin).json()['cancelled'], 1)
        self.assertEqual(self.app.state.store.get(task_id)['status'], 'cancelled')
        self.assertEqual(self.submit(payload('new')).status_code, 422)

    def test_queue_backpressure_and_duplicate_still_readable(self):
        self.client.patch('/api/v1/settings', json={'max_pending': 1}, headers=self.admin)
        self.assertEqual(self.submit().status_code, 202)
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit(payload('lead:2')).status_code, 429)

    def test_unknown_requires_manual_check_and_expired_cannot_retry(self):
        task_id = self.submit().json()['task_id']
        self.app.state.store.finish(task_id, 'unknown')
        self.assertEqual(self.client.post(f'/api/v1/tasks/{task_id}/retry', json={}, headers=self.admin).status_code, 409)
        self.assertEqual(self.client.post(f'/api/v1/tasks/{task_id}/retry', json={'checked_not_sent': True}, headers=self.admin).status_code, 200)
        self.app.state.store.finish(task_id, 'failed')
        with self.app.state.store.connect() as db:
            db.execute('UPDATE tasks SET expires_at=0 WHERE id=?', (task_id,))
        self.assertEqual(self.client.post(f'/api/v1/tasks/{task_id}/retry', json={}, headers=self.admin).status_code, 409)

    def test_pagination_and_bad_filter(self):
        for i in range(4):
            self.submit(payload('pagination:' + str(i)))
        listing = self.client.get('/api/v1/tasks?page=2&limit=2', headers=self.admin).json()
        self.assertEqual(len(listing['items']), 2)
        self.assertEqual(listing['total'], 4)
        self.assertEqual(self.client.get('/api/v1/tasks?limit=1000', headers=self.admin).status_code, 422)
        self.assertEqual(self.client.get('/api/v1/tasks?status=madeup', headers=self.admin).status_code, 422)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.bind('leads', '测试群', '线索测试群', 'dry_run')
        self.driver = FakeDriver()
        self.controller = Controller(self.driver)
        self.worker = Worker(self.store, self.controller)
        self.store.update_settings({'dispatch_enabled': True})

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, event='worker:1'):
        return self.store.enqueue(Notification(**payload(event)), 'admin')[0]

    def test_rehearsal_never_reports_real_delivery(self):
        task = self.enqueue()
        self.assertTrue(self.worker.tick())
        self.assertEqual(self.store.get(task['id'])['status'], 'simulated')
        self.assertFalse(self.worker.tick())
        self.assertEqual(len(self.driver.sent), 1)

    def test_pause_during_preparation_prevents_send(self):
        task = self.enqueue()
        self.driver.on_prepare = lambda _: self.store.update_settings({'dispatch_enabled': False}) and 'prepared'
        self.worker.tick()
        self.assertEqual(self.driver.sent, [])
        self.assertEqual(self.driver.abort_count, 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'pending')

    def test_uncertain_send_pauses_globally_and_is_not_retried(self):
        task = self.enqueue()
        self.driver.failure = AppError('RESULT_UNKNOWN', '未确认')
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'unknown')
        self.assertFalse(self.store.settings()['dispatch_enabled'])
        self.worker.tick()
        self.assertEqual(len(self.driver.sent), 1)

    def test_preparation_failure_never_calls_send(self):
        task = self.enqueue()
        def fail(_):
            raise AppError('TITLE_MISMATCH', '目标错误')
        self.driver.on_prepare = fail
        self.worker.tick()
        self.assertEqual(self.driver.sent, [])
        self.assertEqual(self.store.get(task['id'])['status'], 'failed')

    def test_retryable_preparation_error_has_three_attempt_limit(self):
        task = self.enqueue()
        def fail(_):
            raise AppError('TEMPORARY', '暂时不可用', retryable=True)
        self.driver.on_prepare = fail
        for _ in range(3):
            with self.store.connect() as db:
                db.execute('UPDATE tasks SET next_due=0 WHERE id=?', (task['id'],))
            self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'failed')
        self.assertEqual(self.store.get(task['id'])['attempts'], 3)
        self.assertEqual(self.driver.sent, [])

    def test_busy_gui_does_not_claim_task(self):
        task = self.enqueue()
        with self.controller.exclusive():
            self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'pending')

    def test_rate_limit_survives_worker_recreation(self):
        first, second = self.enqueue('one'), self.enqueue('two')
        self.worker.tick()
        worker = Worker(self.store, self.controller)
        self.assertFalse(worker.tick())
        self.assertEqual(self.store.get(second['id'])['status'], 'pending')

    def test_expired_task_is_not_dispatched_even_while_paused(self):
        task = self.enqueue()
        self.store.update_settings({'dispatch_enabled': False})
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET expires_at=0 WHERE id=?', (task['id'],))
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'expired')
        self.assertEqual(self.driver.sent, [])

    def test_restart_marks_uncertain_and_unstarted_separately(self):
        a, b = self.enqueue('a'), self.enqueue('b')
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET status='dispatching' WHERE id=?", (a['id'],))
            db.execute("UPDATE tasks SET status='preparing' WHERE id=?", (b['id'],))
        self.store.recover()
        self.assertEqual(self.store.get(a['id'])['status'], 'unknown')
        self.assertEqual(self.store.get(b['id'])['status'], 'failed')
        self.assertFalse(self.store.settings()['dispatch_enabled'])

    def test_existing_marker_avoids_repeated_send(self):
        task = self.enqueue()
        self.driver.on_prepare = lambda _: 'already_observed'
        self.worker.tick()
        self.assertEqual(self.store.get(task['id'])['status'], 'observed')
        self.assertEqual(self.driver.sent, [])

    def test_parallel_idempotent_submissions(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            ids = list(executor.map(lambda _: self.enqueue('same')['id'], range(18)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.store.list()['total'], 1)

    def test_single_instance_lock(self):
        a, b = InstanceLock(self.temp.name), InstanceLock(self.temp.name)
        a.acquire()
        try:
            with self.assertRaises(RuntimeError):
                b.acquire()
        finally:
            a.release()
        b.acquire()
        b.release()


class RecognitionTests(unittest.TestCase):
    def row(self, text, x=.08, y=.2, confidence=.99):
        return {'text': text, 'x': x, 'y': y, 'w': .15, 'h': .02, 'confidence': confidence}

    def test_only_group_section_is_a_candidate(self):
        items = [self.row('群聊', y=.15), self.row('销售通知', y=.20), self.row('联系人', y=.35), self.row('销售通知', y=.40)]
        self.assertEqual(len(group_candidates(items, '销售')), 1)
        self.assertEqual(group_candidates([self.row('销售通知')], '销售'), [])

    def test_titles_require_exact_match_and_header_location(self):
        self.assertFalse(title_matches([self.row('销售通知二群', x=.4, y=.05)], '销售通知'))
        self.assertTrue(title_matches([self.row('销售通知 (12)', x=.4, y=.05)], '销售通知'))
        self.assertFalse(title_matches([self.row('销售通知', x=.4, y=.5)], '销售通知'))

    def test_marker_must_be_in_message_pane(self):
        marker = 'WN-ABC123'
        self.assertFalse(marker_visible([self.row(marker, x=.6, y=.85)], marker))
        self.assertFalse(marker_visible([self.row(marker, x=.1, y=.3)], marker))
        self.assertTrue(marker_visible([self.row(marker, x=.6, y=.5)], marker))

    def test_normalization_is_not_substring_matching(self):
        self.assertEqual(canonical('通知群（12）'), '通知群（12）')
        self.assertNotEqual(canonical('通知群二群'), canonical('通知群'))


if __name__ == '__main__':
    unittest.main()
