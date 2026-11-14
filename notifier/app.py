from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .driver import Controller
from .errors import AppError
from .models import BackgroundCheck, Bind, NativeConnection, Notification, Retry, Search, Settings
from .native import NativeDriver, load_config, save_config
from .security import Credentials, InstanceLock
from .store import Store
from .worker import Worker

STATIC = Path(__file__).parent / 'static'


def create_app(directory, driver=None, start_worker=True):
    store = Store(directory)
    credentials = Credentials(store.directory)
    controller = Controller(driver or NativeDriver(store.directory))
    worker = Worker(store, controller)
    lock = InstanceLock(store.directory)

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        for pattern in ('capture-*.png', 'composer-*.txt'):
            for temporary in store.directory.glob(pattern):
                temporary.unlink(missing_ok=True)
        if start_worker:
            worker.start()
        else:
            store.recover()
        try:
            yield
        finally:
            worker.close()
            lock.release()

    app = FastAPI(title='个人微信通知', version='1.2.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.credentials = store, credentials
    app.state.worker, app.state.controller = worker, controller
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]'])

    @app.middleware('http')
    async def security_headers(request, call_next):
        length = request.headers.get('content-length', '')
        if length and (not length.isdecimal() or int(length) > 32768):
            return JSONResponse({'error': {'code': 'BODY_TOO_LARGE', 'message': '请求内容过大。'}}, status_code=413)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not length or request.headers.get('transfer-encoding'):
                return JSONResponse({'error': {'code': 'LENGTH_REQUIRED', 'message': '请提供有界的 JSON 请求体和 Content-Length。'}}, status_code=411)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'error': {'code': 'ORIGIN_REJECTED', 'message': '跨站请求已拒绝。'}}, status_code=403)
            if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                return JSONResponse({'error': {'code': 'JSON_REQUIRED', 'message': '请使用 application/json。'}}, status_code=415)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = ['.'.join(map(str, error['loc'][1:])) for error in exc.errors()]
        names = {'body': '通知内容', 'title': '标题', 'event_id': '事件编号', 'target_code': '目标标识',
                 'expires_in_seconds': '有效期', 'query': '搜索内容', 'label': '显示名称', 'code': '目标标识',
                 'recipient_type': '目标类型', 'allow_contact_lookup': '联系人查询授权',
                 'interval_seconds': '发送间隔', 'max_pending': '队列上限'}
        message = '请检查：' + '、'.join(dict.fromkeys(names.get(field, field) for field in fields)) + '。'
        return JSONResponse({'error': {'code': 'VALIDATION_ERROR', 'message': message, 'fields': fields}}, status_code=422)

    bearer = HTTPBearer(auto_error=False)

    def authenticated(auth: HTTPAuthorizationCredentials | None = Depends(bearer)):
        role = credentials.role(auth.credentials) if auth and auth.scheme.lower() == 'bearer' else None
        if not role:
            raise AppError('UNAUTHORIZED', '访问密钥无效，请重新连接。', 401)
        return role

    def admin(role=Depends(authenticated)):
        if role != 'admin':
            raise AppError('FORBIDDEN', '此操作需要管理密钥。', 403)
        return role

    @app.get('/healthz')
    def healthz():
        return {'ok': True, 'service': 'wechat-notifier'}

    @app.get('/api/v1/status', dependencies=[Depends(admin)])
    def status(fresh: bool = False):
        return {'settings': store.settings(), 'connection': controller.driver.health(fresh=fresh),
                'worker': {'active': worker.active, 'last_error': worker.last_error}}

    @app.post('/api/v1/background/check', dependencies=[Depends(admin)])
    def background_check(payload: BackgroundCheck):
        if controller.driver.mode != 'live':
            raise AppError('DRY_RUN', '预演模式不能验证真实微信后台兼容性。')
        with controller.exclusive():
            return controller.driver.background_check(payload.query, payload.verify_layout)

    @app.post('/api/v1/background/focus-search', dependencies=[Depends(admin)])
    def background_focus_search():
        if getattr(controller.driver, 'backend', '') == 'onebot':
            raise AppError('NATIVE_NO_WINDOW_ACTION', '原生发送不需要搜索或操作微信窗口。')
        if controller.driver.mode != 'live':
            raise AppError('DRY_RUN', '预演模式不能验证真实微信后台兼容性。')
        with controller.exclusive():
            return controller.driver.focus_search_check()

    @app.get('/api/v1/settings', dependencies=[Depends(admin)])
    def settings():
        return store.settings()

    @app.get('/api/v1/native/connection', dependencies=[Depends(admin)])
    def native_connection():
        config = load_config(store.directory)
        return {'base_url': config['base_url'], 'has_token': bool(config['token'])}

    @app.put('/api/v1/native/connection', dependencies=[Depends(admin)])
    def native_connection_update(payload: NativeConnection):
        with controller.exclusive():
            if controller.driver.mode != 'live':
                raise AppError('DRY_RUN', '预演环境不能修改真实发送桥。')
            config = save_config(store.directory, payload.base_url, payload.token)
            store.update_settings({'dispatch_enabled': False})
            controller.driver = NativeDriver(store.directory)
            controller.candidates.clear()
        return {'base_url': config['base_url'], 'has_token': True, 'dispatch_enabled': False}

    @app.patch('/api/v1/settings', dependencies=[Depends(admin)])
    def settings_update(payload: Settings):
        values = payload.model_dump(exclude_none=True)
        if values.get('dispatch_enabled'):
            active = [t for t in store.targets() if t['active'] and t['mode'] == controller.driver.mode
                      and t['backend'] == getattr(controller.driver, 'backend', 'visual')]
            if not active:
                raise AppError('NO_TARGET', '请先绑定当前运行模式的通知目标。')
            if not controller.driver.health(fresh=True)['ready']:
                raise AppError('WECHAT_NOT_READY', '微信未就绪，请先查看连接设置。', 503)
        result = store.update_settings(values)
        worker.wake.set()
        return result

    @app.get('/api/v1/targets')
    def targets(role=Depends(authenticated)):
        rows = store.targets()
        if role != 'admin':
            rows = [{k: row[k] for k in ('code', 'label', 'active')} for row in rows if row['active']]
        return {'items': rows}

    @app.post('/api/v1/targets/search', dependencies=[Depends(admin)])
    def target_search(payload: Search):
        return {'items': controller.search(payload.query, payload.recipient_type, payload.allow_contact_lookup)}

    @app.post('/api/v1/targets/bind', dependencies=[Depends(admin)])
    def target_bind(payload: Bind):
        return controller.bind(store, payload)

    @app.post('/api/v1/targets/{code}/disable', dependencies=[Depends(admin)])
    def target_disable(code: str):
        with controller.exclusive():
            result = store.disable_target(code)
        return result

    @app.get('/api/v1/tasks')
    def tasks(status: str = '', page: int = Query(1, ge=1, le=100000),
              limit: int = Query(25, ge=1, le=100), role=Depends(authenticated)):
        return store.list(status, page, limit, None if role == 'admin' else role)

    @app.post('/api/v1/notifications')
    def notify(payload: Notification, role=Depends(authenticated)):
        task, duplicate = store.enqueue(payload, role)
        worker.wake.set()
        return JSONResponse({'task_id': task['id'], 'status': task['status'], 'duplicate': duplicate,
                             'status_url': '/api/v1/tasks/' + task['id']}, status_code=200 if duplicate else 202)

    @app.get('/api/v1/tasks/{task_id}')
    def task(task_id: str, role=Depends(authenticated)):
        return store.get(task_id, None if role == 'admin' else role)

    @app.post('/api/v1/tasks/{task_id}/retry', dependencies=[Depends(admin)])
    def retry(task_id: str, payload: Retry):
        result = store.task_action(task_id, 'retry', payload.checked_not_sent)
        worker.wake.set()
        return result

    @app.post('/api/v1/tasks/{task_id}/cancel', dependencies=[Depends(admin)])
    def cancel(task_id: str):
        return store.task_action(task_id, 'cancel')

    @app.post('/api/v1/tasks/{task_id}/confirm', dependencies=[Depends(admin)])
    def confirm(task_id: str):
        return store.task_action(task_id, 'confirm')

    @app.post('/api/v1/integration-key', dependencies=[Depends(admin)])
    def integration_key():
        return {'token': credentials.ingest}

    @app.post('/api/v1/permissions/{kind}', dependencies=[Depends(admin)])
    def privacy_settings(kind: str):
        if getattr(controller.driver, 'backend', '') == 'onebot':
            raise AppError('NATIVE_NO_UI_PERMISSION', '原生发送桥不依赖辅助功能或屏幕录制权限。')
        if controller.driver.mode != 'live':
            raise AppError('DRY_RUN', '预演模式不打开系统权限设置。')
        controller.driver.open_privacy(kind)
        return {'ok': True, 'message': '已在运行服务的 Mac 打开系统设置，请在该机器上确认授权。'}

    @app.get('/api/v1/openapi.json', dependencies=[Depends(admin)])
    def openapi():
        return app.openapi()

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app
