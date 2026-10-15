"""Persistent, owner-scoped intent preview and confirmation, separate from execution."""
import json
import re
import shutil
from uuid import uuid4

from .prompting import deterministic_plan
from .store import now

FIELDS = {'action', 'target', 'purpose', 'scope', 'constraints'}
ACTIONS = [('analyze', r'分析|analy[sz]e'), ('inspect', r'检查|inspect|check'),
           ('create', r'创建|生成|编写|create|build'), ('convert', r'转换|convert'),
           ('deploy', r'部署|deploy'), ('test', r'测试|test'),
           ('optimize', r'优化|optimi[sz]e'), ('compare', r'比较|对比|compare'),
           ('research', r'研究|research'), ('fix', r'修复|fix')]


class IntentError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def interpretation(raw, supplied=None):
    """Extract explicit phrases only. Unknown values remain empty and uncalibrated."""
    supplied = dict(supplied or {})
    if set(supplied) - FIELDS:
        raise IntentError(422, '理解字段不受支持；原始请求不可被覆盖')
    data = {'action': '', 'target': '', 'purpose': '', 'scope': '', 'constraints': []}
    for action, pattern in ACTIONS:
        match = re.search(pattern, raw, re.I)
        if match:
            data['action'] = action
            tail = raw[match.end():].strip(' ：:，,。.!！')
            purpose = re.search(r'(?:为了|用于)(.+)', tail)
            if purpose:
                data['purpose'] = purpose.group(1).strip()
                tail = tail[:purpose.start()].strip()
            data['target'] = re.sub(r'^(?:一下|这个|那个|该|全面|深入|快速)+', '', tail).strip()
            if re.search(r'这个|那个|上述|指定|该项目|该系统', tail) or tail in {'项目', '系统', 'project', 'system'}:
                data['target'] = ''
            break
    if '全面' in raw:
        data['scope'] = 'comprehensive'
    elif '快速' in raw:
        data['scope'] = 'quick'
    data.update(supplied)
    for field in ('action', 'target', 'purpose', 'scope'):
        if not isinstance(data[field], str) or len(data[field]) > 2000:
            raise IntentError(422, '理解字段必须是有效文本')
    if (not isinstance(data['constraints'], list) or len(data['constraints']) > 50
            or any(not isinstance(x, str) or len(x) > 2000 for x in data['constraints'])):
        raise IntentError(422, '约束必须是文本数组')
    missing = [field for field in ('action', 'target') if not data[field].strip()]
    review = bool(re.search(r'或者|还是|是否|可能', raw)) and not supplied
    return {**data, 'raw_request': raw, 'intent_preserved': True,
            'missing_context': missing, 'assumptions': [], 'confidence': None,
            'confidence_basis': '明确规则提取/用户修订，未校准概率；请核对后确认',
            'status': 'NEED_CONTEXT' if missing else ('REVIEW' if review else 'READY')}


class IntentWorkspace:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS intent_requests (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, raw_request TEXT NOT NULL,
                current_revision INTEGER NOT NULL, state TEXT NOT NULL,
                mission_id TEXT UNIQUE REFERENCES missions(id), created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS intent_revisions (
                intent_id TEXT NOT NULL REFERENCES intent_requests(id), revision INTEGER NOT NULL,
                interpretation TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(intent_id, revision));
              CREATE INDEX IF NOT EXISTS intent_by_owner ON intent_requests(owner_id, created_at);
            ''')

    def _row(self, db, owner, id):
        row = db.execute('SELECT * FROM intent_requests WHERE id=? AND owner_id=?', (id, owner)).fetchone()
        if row is None:
            raise IntentError(404, 'Intent不存在')
        return row

    def _view(self, db, row):
        revisions = db.execute('SELECT revision,interpretation,created_at FROM intent_revisions WHERE intent_id=? ORDER BY revision', (row['id'],)).fetchall()
        latest = json.loads(revisions[-1]['interpretation'])
        execution = 'NOT_STARTED'
        if row['mission_id']:
            job = db.execute('SELECT status FROM jobs WHERE mission_id=? ORDER BY created_at DESC LIMIT 1', (row['mission_id'],)).fetchone()
            if job:
                execution = job['status']
        return {**latest, 'id': row['id'], 'revision': row['current_revision'],
                'state': row['state'], 'mission_id': row['mission_id'],
                'created_at': row['created_at'],
                'revisions': [{'revision': x['revision'], 'interpretation': json.loads(x['interpretation']),
                               'created_at': x['created_at']} for x in revisions],
                'authorization': 'NOT_EVALUATED', 'execution': execution}

    def get(self, owner, id):
        with self.store.connect() as db:
            return self._view(db, self._row(db, owner, id))

    def list(self, owner):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM intent_requests WHERE owner_id=? ORDER BY created_at DESC LIMIT 100', (owner,)).fetchall()
            return [self._view(db, row) for row in rows]

    def compile(self, owner, raw, supplied=None):
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 20000:
            raise IntentError(422, '请输入1至20000字符的原始请求')
        value = interpretation(raw, supplied)
        id = uuid4().hex
        with self.store.connect() as db:
            db.execute('INSERT INTO intent_requests VALUES (?,?,?,1,?,NULL,?)', (id, owner, raw, 'DRAFT', now()))
            db.execute('INSERT INTO intent_revisions VALUES (?,1,?,?)', (id, json.dumps(value, ensure_ascii=False), now()))
        return self.get(owner, id)

    def revise(self, owner, id, revision, supplied):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, owner, id)
            if row['state'] != 'DRAFT' or row['current_revision'] != revision:
                raise IntentError(409, 'Intent状态或版本已变化，请刷新')
            previous = self._view(db, row)
            fields = {key: previous[key] for key in FIELDS}
            fields.update(supplied)
            value = interpretation(row['raw_request'], fields)
            db.execute('INSERT INTO intent_revisions VALUES (?,?,?,?)', (id, revision+1, json.dumps(value, ensure_ascii=False), now()))
            db.execute('UPDATE intent_requests SET current_revision=? WHERE id=?', (revision+1, id))
        return self.get(owner, id)

    def cancel(self, owner, id, revision):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, owner, id)
            if row['current_revision'] != revision or row['state'] == 'CONFIRMED':
                raise IntentError(409, '已确认或版本已变化')
            db.execute("UPDATE intent_requests SET state='CANCELLED' WHERE id=?", (id,))
        return self.get(owner, id)

    def confirm(self, owner, id, revision, before_create=None):
        preview = self.get(owner, id)
        if preview['revision'] != revision or preview['state'] == 'CANCELLED':
            raise IntentError(409, 'Intent状态或版本已变化')
        if not preview['mission_id'] and preview['status'] != 'READY':
            raise IntentError(409, '理解仍需补充或复核')
        if before_create and not preview['mission_id']:
            before_create()
        created = None
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = self._row(db, owner, id)
                if row['current_revision'] != revision or row['state'] == 'CANCELLED':
                    raise IntentError(409, 'Intent状态或版本已变化')
                if row['mission_id']:
                    return {'intent': self._view(db, row), 'mission_id': row['mission_id'], 'duplicate': True}
                view = self._view(db, row)
                if view['status'] != 'READY':
                    raise IntentError(409, '理解仍需补充或复核')
                plan = deterministic_plan(row['raw_request'])
                created = uuid4().hex
                self.store.create(plan.title, row['raw_request'], list(plan.steps), owner_id=owner,
                                  connection=db, issue_grant=False, mission_id=created)
                prompt = ('用户原话：'+row['raw_request']+'\n已确认理解：'+json.dumps({k: view[k] for k in FIELDS}, ensure_ascii=False)
                          +'\n仅执行已确认目标；未明确的目标不自行添加。验证要求：以实际执行证据对照已确认目标和约束。')
                db.execute('UPDATE missions SET professional_prompt=? WHERE id=?', (prompt, created))
                db.execute("UPDATE intent_requests SET state='CONFIRMED',mission_id=? WHERE id=?", (created, id))
                self.store._event(db, created, None, None, None, 'INTENT_CONFIRMED',
                                  {'intent_id': id, 'revision': revision, 'authorization': 'NOT_GRANTED', 'execution': 'NOT_STARTED'})
        except Exception:
            if created:
                shutil.rmtree(self.store.root/'workspaces'/created, ignore_errors=True)
            raise
        return {'intent': self.get(owner, id), 'mission_id': created, 'duplicate': False}
