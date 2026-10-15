"""Local-appliance Owner initialization; never an application-wide auth bypass."""
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit

from .auth import hash_password, hash_token, is_weak_password


class SetupError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def write_private(path, value):
    """Replace private local credential metadata atomically, without printing it."""
    path = Path(path)
    temporary = path.with_name('.owner-' + secrets.token_hex(8))
    try:
        with open(temporary, 'x', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LocalOwnerSetup:
    def __init__(self, store, auth):
        self.store, self.auth = store, auth
        self.lock = threading.RLock()

    def _local_request(self, request, write=False):
        peer = request.client.host if request.client else ''
        if peer not in {'127.0.0.1', '::1'} or request.url.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise SetupError(403, 'Owner bootstrap is available only on this computer')
        if any(header in request.headers for header in ('forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto')):
            raise SetupError(403, 'Forwarded bootstrap requests are not accepted')
        if write:
            origin = urlsplit(request.headers.get('origin', ''))
            if origin.scheme != request.url.scheme or origin.netloc != request.headers.get('host'):
                raise SetupError(403, 'Same-origin bootstrap request required')

    def _read(self):
        configured = os.getenv('FACAI_LOCAL_OWNER_FILE', '')
        path = self.store.root/'local-owner.json'
        if not configured or Path(configured).resolve() != path.resolve() or path.is_symlink():
            raise SetupError(409, 'This installation uses externally managed Owner credentials')
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise SetupError(409, 'Local Owner configuration must be private')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise SetupError(409, 'Invalid local Owner configuration')
        return path, value

    def status(self, request):
        self._local_request(request)
        try:
            _, value = self._read()
        except (SetupError, OSError, ValueError):
            return {'local_appliance': False, 'bootstrap_pending': False, 'recovery_available': False}
        return {'local_appliance': True, 'bootstrap_pending': value.get('setup_complete') is False,
                'recovery_available': bool(value.get('recovery_hash'))}

    def initialize(self, request, token, username, password):
        self._local_request(request, write=True)
        if not re.fullmatch(r'[A-Za-z0-9_-]{3,32}', username):
            raise SetupError(422, 'Owner username must be 3-32 letters, digits, underscores or hyphens')
        self._password(password)
        with self.lock:
            path, value = self._read()
            if value.get('setup_complete') is not False:
                raise SetupError(409, 'Owner initialization already completed')
            expected = os.getenv('FACAI_BOOTSTRAP_TOKEN', '')
            if not expected or not token or not hmac.compare_digest(token, expected):
                raise SetupError(403, 'Invalid or expired bootstrap token; relaunch FACAI')
            if self.auth.is_locked('local-bootstrap'):
                raise SetupError(429, 'Too many recovery attempts; try later')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT count(*) FROM missions').fetchone()[0] or db.execute('SELECT count(*) FROM platform_users').fetchone()[0]:
                    raise SetupError(409, 'Existing accounts or missions require explicit migration, not bootstrap')
                db.execute('DELETE FROM auth_sessions')
            recovery = secrets.token_urlsafe(32)
            password_hash = hash_password(password)
            write_private(path, {'version': 2, 'setup_complete': True, 'username': username,
                                 'password_hash': password_hash, 'recovery_hash': hash_token(recovery)})
            os.environ['FACAI_ADMIN_USER'] = username
            os.environ['FACAI_ADMIN_PASSWORD'] = password_hash
            os.environ.pop('FACAI_BOOTSTRAP_TOKEN', None)
            return {'initialized': True, 'username': username, 'recovery_key': recovery,
                    'recovery_scope': 'Local Owner login reset only; not a Runtime execution grant',
                    'next': '/login?next=%2Fsetup'}

    def _password(self, password):
        if not isinstance(password, str) or len(password) > 512 or is_weak_password(password):
            raise SetupError(422, 'Choose a strong Owner password of at least 8 characters')

    def recover(self, request, recovery_key, password):
        self._local_request(request, write=True)
        self._password(password)
        with self.lock:
            path, value = self._read()
            if not value.get('setup_complete') or not value.get('recovery_hash'):
                raise SetupError(409, 'No local Owner recovery is configured')
            if self.auth.is_locked('local-bootstrap'):
                raise SetupError(429, 'Too many recovery attempts; try later')
            if not hmac.compare_digest(hash_token(recovery_key), value['recovery_hash']):
                self.store.record_login_failure('local-bootstrap', self.auth.failure_window)
                raise SetupError(403, 'Invalid recovery key')
            password_hash, recovery = hash_password(password), secrets.token_urlsafe(32)
            # Revocation is committed before the private file changes. A file-write
            # failure leaves old credentials usable but old sessions revoked.
            self.store.delete_user_auth_sessions(value['username'])
            write_private(path, {**value, 'password_hash': password_hash, 'recovery_hash': hash_token(recovery)})
            os.environ['FACAI_ADMIN_USER'] = value['username']
            os.environ['FACAI_ADMIN_PASSWORD'] = password_hash
            self.store.clear_login_failures('local-bootstrap')
            return {'recovered': True, 'username': value['username'], 'recovery_key': recovery,
                    'recovery_scope': 'Login password reset; existing scoped API keys and Runtime grants are unchanged'}
