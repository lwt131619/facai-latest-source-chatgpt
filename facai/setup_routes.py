"""First-run UI and owner-only checks; public bootstrap is narrow and loopback-only."""
from pathlib import Path
import json
import platform
import shutil
import tempfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .mission_spec import canonical
from .onboarding import LocalOwnerSetup, SetupError
from .store import now


class BootstrapBody(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=512)


class RecoverBody(BaseModel):
    recovery_key: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=512)


class FinishBody(BaseModel):
    acknowledge_unready: bool = False


def router_for(store, auth, owner, require_admin, registries, probe_model, model_fingerprint):
    local = LocalOwnerSetup(store, auth)
    router = APIRouter()
    with store.connect() as db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS setup_progress (
            owner_id TEXT PRIMARY KEY, document TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS setup_model_probes (
            owner_id TEXT NOT NULL, model_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            document TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(owner_id,model_id));
        ''')

    def call(method, *args):
        try:
            return method(*args)
        except SetupError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except (OSError, ValueError):
            raise HTTPException(409, 'Local Owner configuration could not be read or saved') from None

    def diagnostics():
        checks = {'python': {'status': 'AVAILABLE', 'version': platform.python_version()},
                  'rust': {'status': 'COMPILER_INSTALLED_NOT_BUILD_TESTED' if shutil.which('rustc') else 'NOT_INSTALLED', 'required': False},
                  'docker': {'status': 'CLI_INSTALLED_NOT_DAEMON_TESTED' if shutil.which('docker') else 'NOT_INSTALLED', 'required': False},
                  'database': {'status': 'UNAVAILABLE'}, 'storage': {'status': 'UNAVAILABLE'},
                  'authentication': {'status': 'ENABLED' if auth.enabled else 'NOT_CONFIGURED'},
                  'runtime': {name: 'CLI_INSTALLED_NOT_EXECUTION_TESTED' if shutil.which(name) else 'NOT_INSTALLED'
                              for name in ('opencode', 'goose')}}
        try:
            with store.connect() as db:
                checks['database']['status'] = 'AVAILABLE' if db.execute('SELECT 1').fetchone()[0] == 1 else 'UNAVAILABLE'
            with tempfile.TemporaryFile(dir=store.root) as file:
                file.write(b'facai-storage-check'); file.flush(); file.seek(0)
                if file.read() == b'facai-storage-check':
                    checks['storage']['status'] = 'READ_WRITE'
        except OSError:
            pass
        models = []
        for id, config in registries.list('models', owner_id=owner()).items():
            with store.connect() as db:
                row = db.execute('SELECT * FROM setup_model_probes WHERE owner_id=? AND model_id=?', (owner(), id)).fetchone()
            current = bool(row) and row['fingerprint'] == model_fingerprint(id)
            result = json.loads(row['document']) if row and current else None
            models.append({'id': id, 'name': config.get('name', id), 'model': config.get('model'),
                           'last_probe': result, 'checked_at': row['checked_at'] if current else None,
                           'status': 'LAST_PROBE_SUCCEEDED' if result and result['status'] == 'MODEL_COMPLETED' else
                                     'LAST_PROBE_FAILED' if result else 'NOT_TESTED'})
        with store.connect() as db:
            row = db.execute('SELECT document FROM setup_progress WHERE owner_id=?', (owner(),)).fetchone()
        blockers = []
        for key in ('database', 'storage', 'authentication'):
            if checks[key]['status'] not in {'AVAILABLE', 'READ_WRITE', 'ENABLED'}:
                blockers.append(key.upper()+'_NOT_READY')
        if not any(m['status'] == 'LAST_PROBE_SUCCEEDED' for m in models):
            blockers.append('MODEL_NOT_VERIFIED')
        if all(value == 'NOT_INSTALLED' for value in checks['runtime'].values()):
            blockers.append('EXECUTION_CLI_NOT_INSTALLED')
        return {'checked_at': now(), 'checks': checks, 'models': models, 'blocking_checks': blockers,
                'setup': json.loads(row['document']) if row else None,
                'notice': 'CLI presence and past model probes do not establish end-to-end execution or production readiness'}

    @router.get('/setup')
    def page():
        return FileResponse(Path(__file__).parent/'static/setup.html')

    @router.get('/api/v1/setup/bootstrap')
    def bootstrap_status(request: Request):
        return call(local.status, request)

    @router.post('/api/v1/setup/bootstrap')
    def bootstrap(request: Request, body: BootstrapBody):
        return call(local.initialize, request, body.token, body.username, body.password)

    @router.post('/api/v1/setup/recover')
    def recover(request: Request, body: RecoverBody):
        return call(local.recover, request, body.recovery_key, body.password)

    @router.get('/api/v1/setup/status')
    @router.get('/status')
    def status():
        require_admin()
        return diagnostics()

    @router.post('/api/v1/setup/models/{model_id}/probe')
    async def probe(model_id: str):
        require_admin()
        fingerprint = model_fingerprint(model_id)
        if fingerprint is None:
            raise HTTPException(404, 'Model not found')
        result = await probe_model(model_id)
        record = {'status': result.get('status'), 'http_status': result.get('http_status'),
                  'method': 'EXPLICIT_CONNECTION_PROBE', 'model_id': model_id}
        with store.connect() as db:
            db.execute('INSERT INTO setup_model_probes VALUES (?,?,?,?,?) ON CONFLICT(owner_id,model_id) '
                       'DO UPDATE SET fingerprint=excluded.fingerprint,document=excluded.document,checked_at=excluded.checked_at',
                       (owner(), model_id, fingerprint, canonical(record), now()))
        return record

    @router.post('/api/v1/setup/finish')
    def finish(body: FinishBody):
        require_admin()
        state = diagnostics()
        if state['blocking_checks'] and not body.acknowledge_unready:
            raise HTTPException(409, 'Unready checks must be explicitly acknowledged: '+', '.join(state['blocking_checks']))
        record = {'acknowledged_at': now(), 'actor': owner(), 'unready_acknowledged': body.acknowledge_unready,
                  'blocking_checks_at_acknowledgment': state['blocking_checks'], 'production_ready': False}
        with store.connect() as db:
            db.execute('INSERT INTO setup_progress VALUES (?,?,?) ON CONFLICT(owner_id) '
                       'DO UPDATE SET document=excluded.document,updated_at=excluded.updated_at',
                       (owner(), canonical(record), now()))
        return record

    @router.get('/metrics')
    def metrics():
        require_admin()
        with store.connect() as db:
            missions = db.execute('SELECT count(*) FROM missions WHERE owner_id=?', (owner(),)).fetchone()[0]
            events = db.execute('SELECT count(*) FROM events e JOIN missions m ON m.id=e.mission_id WHERE m.owner_id=?', (owner(),)).fetchone()[0]
        return PlainTextResponse('# TYPE facai_missions gauge\nfacai_missions '+str(missions)+
                                 '\n# TYPE facai_events gauge\nfacai_events '+str(events)+'\n')

    return router
