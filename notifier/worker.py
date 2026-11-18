import logging
import threading

from .errors import AppError

LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, controller):
        self.store, self.controller = store, controller
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.active = False
        self.last_error = None

    def start(self):
        self.store.recover()
        self.thread = threading.Thread(target=self.run, name='wechat-notifications', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=45)
            if self.thread.is_alive():
                raise RuntimeError('微信操作尚未退出，保留进程锁等待进程结束。')

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.tick()
            except Exception:
                LOG.error('Notification worker failure; no message or credentials logged')
                self.last_error = '后台任务异常，请检查服务运行状态。'
            self.wake.wait(5 if self.store.settings()['dispatch_enabled'] else 30)

    def tick(self):
        self.store.expire()
        if self.stop.is_set() or not self.store.settings()['dispatch_enabled']:
            return False
        if not self.controller.driver.health()['ready']:
            self.last_error = '微信尚未就绪，任务已保留。'
            return False
        task, dispatched = None, False
        try:
            with self.controller.exclusive():
                task = self.store.claim(self.controller.driver.mode, getattr(self.controller.driver, 'backend', 'visual'))
                if not task:
                    return False
                self.active, self.last_error = True, None
                prepared = self.controller.driver.prepare(task)
                if prepared == 'already_observed':
                    self.store.finish(task['id'], 'observed', 'EXISTING_MARKER')
                    return True
                if self.stop.is_set() or not self.store.dispatch(task['id']):
                    self.controller.driver.abort(task)
                    self.store.finish(task['id'], 'pending', 'PAUSED', '发送前已暂停，任务保留。')
                    return False
                dispatched = True
                status = self.controller.driver.send(task)
                if status not in ('observed', 'simulated', 'submitted'):
                    raise AppError('RESULT_UNKNOWN', '微信发送结果无法确认，请在对应会话核对通知编号。')
                self.store.finish(task['id'], status)
                return True
        except AppError as exc:
            self.last_error = exc.message
            if task:
                self.store.finish(task['id'], 'unknown' if dispatched else 'failed', exc.code, exc.message,
                                  retry=not dispatched and exc.retryable)
            return False
        except Exception:
            if task:
                self.store.finish(task['id'], 'unknown' if dispatched else 'failed',
                                  'AUTOMATION_ERROR', '微信操作异常，请核对桌面与任务记录。')
            raise
        finally:
            self.active = False
