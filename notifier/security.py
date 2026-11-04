import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import stat


class Credentials:
    def __init__(self, directory):
        self.path = Path(directory) / 'credentials.json'
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'admin': secrets.token_urlsafe(36), 'ingest': secrets.token_urlsafe(36)}, stream)
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise RuntimeError('凭证文件必须归当前用户所有，且仅当前用户可读写。')
            data = json.load(stream)
        if set(data) != {'admin', 'ingest'} or any(not isinstance(v, str) or len(v) < 40 for v in data.values()):
            raise RuntimeError('凭证文件格式无效，未自动重置现有密钥。')
        self.admin, self.ingest = data['admin'], data['ingest']

    def role(self, token):
        if not isinstance(token, str) or len(token) > 256:
            return None
        if hmac.compare_digest(token, self.admin):
            return 'admin'
        if hmac.compare_digest(token, self.ingest):
            return 'ingest'
        return None


class InstanceLock:
    def __init__(self, directory):
        self.path, self.fd = Path(directory) / 'runtime.lock', None

    def acquire(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError('此数据目录已有服务运行，请勿重复启动。') from None
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
