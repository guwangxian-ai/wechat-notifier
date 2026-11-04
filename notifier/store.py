"""Small SQLite outbox. GUI work is always outside database transactions."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .errors import AppError

ACTIVE = ('pending', 'preparing', 'dispatching')
STATUSES = ('pending', 'preparing', 'dispatching', 'observed', 'submitted', 'unknown', 'failed',
            'cancelled', 'expired', 'simulated', 'confirmed_manually')
DEFAULTS = {'dispatch_enabled': False, 'interval_seconds': 15, 'max_pending': 500}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'notifications.sqlite3'
        if self.path.is_symlink():
            raise RuntimeError('数据库路径不能是符号链接')
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS targets(
                  code TEXT PRIMARY KEY, label TEXT NOT NULL, group_name TEXT NOT NULL,
                  revision TEXT NOT NULL, mode TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                  verified_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                  id TEXT PRIMARY KEY, event_id TEXT NOT NULL, origin TEXT NOT NULL,
                  target_code TEXT NOT NULL, target_revision TEXT NOT NULL, group_name TEXT NOT NULL,
                  title TEXT NOT NULL, body TEXT NOT NULL, message TEXT NOT NULL, fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
                  next_due REAL NOT NULL, dispatch_at REAL, error_code TEXT, error_message TEXT,
                  UNIQUE(origin,event_id,target_code));
                CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status,next_due,created_at);
                CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at DESC,id);
                CREATE TABLE IF NOT EXISTS task_events(
                  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
                  at REAL NOT NULL, code TEXT);
                CREATE INDEX IF NOT EXISTS events_task ON task_events(task_id,id);
            ''')
            # Additive migration: keep existing queue/history and recipient snapshots.
            for table in ('targets', 'tasks'):
                columns = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
                for name, default in (('backend', 'visual'), ('group_id', ''), ('account_id', '')):
                    if name not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
                if table == 'tasks' and 'dispatch_nonce' not in columns:
                    db.execute("ALTER TABLE tasks ADD COLUMN dispatch_nonce TEXT NOT NULL DEFAULT ''")
            for key, value in DEFAULTS.items():
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(value)))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, task_id, status, code=None):
        db.execute('INSERT INTO task_events(task_id,status,at,code) VALUES (?,?,?,?)',
                   (task_id, status, time.time(), code))

    def settings(self, db=None):
        if db is not None:
            return {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}
        with self.connect() as db:
            return self.settings(db)

    def update_settings(self, values):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key, value in values.items():
                if key not in DEFAULTS:
                    raise ValueError(key)
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
            return self.settings(db)

    def recover(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id,status FROM tasks WHERE status IN ('preparing','dispatching')").fetchall()
            for row in rows:
                state = 'unknown' if row['status'] == 'dispatching' else 'failed'
                code = 'PROCESS_INTERRUPTED'
                db.execute('UPDATE tasks SET status=?,error_code=?,error_message=?,updated_at=? WHERE id=?',
                           (state, code, '进程中断，请核对任务与微信群后处理。', time.time(), row['id']))
                self.event(db, row['id'], state, code)
            if rows:
                db.execute("UPDATE settings SET value='false' WHERE key='dispatch_enabled'")

    def targets(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM targets ORDER BY active DESC,verified_at DESC')]

    def bind(self, code, label, group_name, mode, backend='visual', group_id='', account_id=''):
        with self.connect() as db:
            try:
                db.execute('''INSERT INTO targets(code,label,group_name,revision,mode,active,verified_at,
                              backend,group_id,account_id) VALUES (?,?,?,?,?,1,?,?,?,?)''',
                           (code, label, group_name, uuid.uuid4().hex, mode, time.time(), backend, group_id, account_id))
            except sqlite3.IntegrityError:
                raise AppError('TARGET_EXISTS', '群标识已存在，请使用新的标识，已有任务的目标不会被替换。') from None
        return next(item for item in self.targets() if item['code'] == code)

    def disable_target(self, code):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('UPDATE targets SET active=0 WHERE code=?', (code,)).rowcount:
                raise AppError('NOT_FOUND', '目标群不存在', 404)
            rows = db.execute("SELECT id FROM tasks WHERE target_code=? AND status='pending'", (code,)).fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET status='cancelled',updated_at=?,error_code='TARGET_DISABLED' WHERE id=?", (time.time(), row['id']))
                self.event(db, row['id'], 'cancelled', 'TARGET_DISABLED')
        return {'cancelled': len(rows)}

    def enqueue(self, request, origin):
        data = request.model_dump()
        digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM tasks WHERE origin=? AND event_id=? AND target_code=?',
                                  (origin, request.event_id, request.target_code)).fetchone()
            if previous:
                if previous['fingerprint'] != digest:
                    raise AppError('IDEMPOTENCY_CONFLICT', '此事件编号已用于不同内容，请核对调用方。')
                return dict(previous), True
            target = db.execute('SELECT * FROM targets WHERE code=? AND active=1', (request.target_code,)).fetchone()
            if not target:
                raise AppError('TARGET_UNAVAILABLE', '请先绑定可用的通知群。', 422)
            pending = db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pending','preparing','dispatching')").fetchone()[0]
            if pending >= self.settings(db)['max_pending']:
                raise AppError('QUEUE_FULL', '待发送队列已满，请稍后重试。', 429)
            task_id = uuid.uuid4().hex
            marker = 'WN-' + task_id[:12].upper()
            message = ((request.title + '\n\n') if request.title else '') + request.body + '\n\n通知编号：' + marker
            db.execute('''INSERT INTO tasks(id,event_id,origin,target_code,target_revision,group_name,
                title,body,message,fingerprint,status,created_at,updated_at,expires_at,next_due,backend,group_id,account_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)''',
                (task_id, request.event_id, origin, target['code'], target['revision'], target['group_name'],
                 request.title, request.body, message, digest, now, now, now + request.expires_in_seconds, now,
                 target['backend'], target['group_id'], target['account_id']))
            self.event(db, task_id, 'pending')
        return self.get(task_id), False

    def get(self, task_id, origin=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or (origin is not None and row['origin'] != origin):
                raise AppError('NOT_FOUND', '通知任务不存在', 404)
            result = dict(row)
            result['events'] = [dict(r) for r in db.execute('SELECT status,at,code FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 30', (task_id,))]
            return result

    def list(self, status='', page=1, limit=25, origin=None):
        clauses, args = [], []
        if status:
            if status not in STATUSES:
                raise AppError('BAD_STATUS', '任务状态无效', 422)
            clauses.append('status=?')
            args.append(status)
        if origin:
            clauses.append('origin=?')
            args.append(origin)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM tasks' + where, args).fetchone()[0]
            rows = db.execute('SELECT * FROM tasks' + where + ' ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                              (*args, limit, (page - 1) * limit)).fetchall()
            count_sql = 'SELECT status,COUNT(*) n FROM tasks' + (' WHERE origin=?' if origin else '') + ' GROUP BY status'
            counts = {r['status']: r['n'] for r in db.execute(count_sql, (origin,) if origin else ())}
        return {'items': [dict(r) for r in rows], 'total': total, 'counts': counts, 'page': page, 'limit': limit}

    def claim(self, mode, backend='visual'):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.settings(db)
            # Expiration is also called while paused, independent of GUI readiness.
            if not config['dispatch_enabled']:
                return None
            recent = db.execute('SELECT MAX(dispatch_at) FROM tasks').fetchone()[0] or 0
            if now - recent < config['interval_seconds']:
                return None
            row = db.execute("SELECT * FROM tasks WHERE status='pending' AND next_due<=? AND expires_at>? ORDER BY created_at LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            target = db.execute('SELECT * FROM targets WHERE code=?', (row['target_code'],)).fetchone()
            if (not target or not target['active'] or target['revision'] != row['target_revision'] or target['mode'] != mode
                    or target['backend'] != backend or row['backend'] != backend
                    or target['group_id'] != row['group_id'] or target['account_id'] != row['account_id']):
                db.execute("UPDATE tasks SET status='failed',error_code='TARGET_CHANGED',error_message='目标群已停用或运行模式变化，请重新绑定并提交。',updated_at=? WHERE id=?", (now, row['id']))
                self.event(db, row['id'], 'failed', 'TARGET_CHANGED')
                return None
            db.execute("UPDATE tasks SET status='preparing',attempts=attempts+1,updated_at=?,error_code=NULL,error_message=NULL WHERE id=?", (now, row['id']))
            self.event(db, row['id'], 'preparing')
            return dict(row)

    def expire(self):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id FROM tasks WHERE status='pending' AND expires_at<=? LIMIT 100", (now,)).fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET status='expired',updated_at=?,error_code='EXPIRED',error_message='超过任务有效期，未发送。' WHERE id=?", (now, row['id']))
                self.event(db, row['id'], 'expired', 'EXPIRED')

    def dispatch(self, task_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            target = db.execute('SELECT * FROM targets WHERE code=?', (row['target_code'],)).fetchone()
            if (not self.settings(db)['dispatch_enabled'] or not target['active']
                    or target['revision'] != row['target_revision'] or row['expires_at'] <= time.time()):
                return False
            stamp = time.time()
            db.execute("UPDATE tasks SET status='dispatching',dispatch_at=?,updated_at=? WHERE id=?", (stamp, stamp, task_id))
            self.event(db, task_id, 'dispatching')
            return True

    def finish(self, task_id, status, code=None, message=None, retry=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT attempts FROM tasks WHERE id=?', (task_id,)).fetchone()
            if retry and row['attempts'] < 3:
                status = 'pending'
            db.execute('UPDATE tasks SET status=?,updated_at=?,next_due=?,error_code=?,error_message=? WHERE id=?',
                       (status, time.time(), time.time() + min(300, 15 * 2 ** row['attempts']), code, message, task_id))
            self.event(db, task_id, status, code)
            if status == 'unknown':
                db.execute("UPDATE settings SET value='false' WHERE key='dispatch_enabled'")

    def task_action(self, task_id, action, checked=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise AppError('NOT_FOUND', '通知任务不存在', 404)
            if action == 'cancel':
                if row['status'] not in ('pending', 'failed', 'unknown'):
                    raise AppError('TASK_BUSY', '当前任务不能取消。')
                status = 'cancelled'
            elif action == 'confirm':
                if row['status'] not in ('unknown', 'submitted'):
                    raise AppError('INVALID_STATE', '只有已提交或结果未知的任务需要人工确认。')
                status = 'confirmed_manually'
            else:
                if row['status'] not in ('failed', 'unknown'):
                    raise AppError('INVALID_STATE', '只有失败或结果未知的任务可以重试。')
                if row['status'] == 'unknown' and not checked:
                    raise AppError('CHECK_REQUIRED', '先到群里核对通知编号，确认未发送后才能重试。')
                if row['expires_at'] <= time.time():
                    raise AppError('EXPIRED', '任务已过期，请使用新事件编号重新提交。')
                target = db.execute('SELECT * FROM targets WHERE code=?', (row['target_code'],)).fetchone()
                if not target or not target['active'] or target['revision'] != row['target_revision']:
                    raise AppError('TARGET_CHANGED', '原目标群不可用，请重新提交。')
                status = 'pending'
            db.execute('UPDATE tasks SET status=?,updated_at=?,next_due=?,attempts=0,error_code=NULL,error_message=NULL,dispatch_nonce=? WHERE id=?',
                       (status, time.time(), time.time(), uuid.uuid4().hex, task_id))
            self.event(db, task_id, status, 'MANUAL_' + action.upper())
        return self.get(task_id)
