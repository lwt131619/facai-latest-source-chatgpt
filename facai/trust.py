"""Versioned evidence, explicit human reviews and immutable proof snapshots.

This additive layer never changes legacy build_proof.verified, dispatches work,
fetches a URL, or treats a content hash as proof that an assertion is true.
"""
from contextlib import contextmanager
import io
import hashlib
import json
import os
from pathlib import Path
import stat
import zipfile
from uuid import uuid4

from .mission_spec import MissionSpecifications, canonical, digest
from .store import now

CONTEXT_KEYS = ('time', 'scope', 'environment', 'definition')
RUNTIME_TYPES = frozenset({'OPENCODE_COMPLETED', 'OPENCODE_FAILED', 'OPENCODE_TIMEOUT',
                          'GOOSE_COMPLETED', 'GOOSE_FAILED', 'BACKEND_ATTEMPT',
                          'EXECUTION_STARTED', 'STEP_COMPLETED', 'STEP_FAILED'})


class TrustError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def text(value, name, limit=2000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise TrustError(422, name + ' must be valid text')
    return value


def classify_conflict(left, right):
    """Only compare explicitly normalized subjects/predicates, never arbitrary prose."""
    if (left['subject'], left['predicate']) != (right['subject'], right['predicate']):
        return None
    if left['value'] == right['value']:
        return None
    a, b = left['context'], right['context']
    differences = [key for key in CONTEXT_KEYS if a.get(key) and b.get(key) and a[key] != b[key]]
    if differences:
        kind = {'time': 'TEMPORAL', 'scope': 'SCOPE', 'environment': 'SCOPE', 'definition': 'SEMANTIC'}[differences[0]]
        return {'classification': kind, 'different_dimensions': differences,
                'reason': 'Explicit contexts differ; neither value is automatically false'}
    if all(a.get(key) and a.get(key) == b.get(key) for key in CONTEXT_KEYS):
        return {'classification': 'DIRECT_CONFLICT', 'different_dimensions': [],
                'reason': 'Different values with the same explicitly supplied context'}
    return {'classification': 'REVIEW', 'different_dimensions': [],
            'reason': 'Context is incomplete; a direct contradiction has not been established'}


class TrustStore:
    def __init__(self, store):
        self.store = store
        MissionSpecifications(store)
        with store.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS trust_versions (
                mission_id TEXT PRIMARY KEY REFERENCES missions(id), revision INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS trust_evidence (
                id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
                document TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS trust_evidence_mission ON trust_evidence(mission_id);
              CREATE TABLE IF NOT EXISTS trust_claims (
                id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
                document TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS trust_claims_mission ON trust_claims(mission_id);
              CREATE TABLE IF NOT EXISTS trust_reviews (
                id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
                claim_id TEXT NOT NULL REFERENCES trust_claims(id),
                document TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS trust_reviews_mission ON trust_reviews(mission_id);
              CREATE TABLE IF NOT EXISTS trust_retractions (
                claim_id TEXT PRIMARY KEY REFERENCES trust_claims(id),
                mission_id TEXT NOT NULL REFERENCES missions(id), document TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS trust_proofs (
                id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
                document TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS trust_acceptances (
                proof_id TEXT PRIMARY KEY REFERENCES trust_proofs(id),
                document TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS knowledge_assets (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                mission_id TEXT NOT NULL REFERENCES missions(id),
                proof_id TEXT NOT NULL UNIQUE REFERENCES trust_proofs(id),
                document TEXT NOT NULL, sha256 TEXT NOT NULL,
                search_text TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS knowledge_by_owner ON knowledge_assets(owner_id,created_at);
            ''')

    def _owned(self, db, owner, mission_id):
        mission = db.execute('SELECT * FROM missions WHERE id=? AND owner_id=?', (mission_id, owner)).fetchone()
        if mission is None:
            raise TrustError(404, 'Mission not found')
        return dict(mission)

    def _revision(self, db, mission_id):
        row = db.execute('SELECT revision FROM trust_versions WHERE mission_id=?', (mission_id,)).fetchone()
        return row['revision'] if row else 0

    @contextmanager
    def _change(self, owner, mission_id, expected_revision):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._owned(db, owner, mission_id)
            revision = self._revision(db, mission_id)
            if expected_revision != revision:
                raise TrustError(409, 'Evidence changed; refresh before writing')
            yield db
            db.execute('INSERT INTO trust_versions VALUES (?,?) ON CONFLICT(mission_id) '
                       'DO UPDATE SET revision=excluded.revision', (mission_id, revision + 1))

    def _event(self, db, mission_id, kind, owner, **payload):
        self.store._event(db, mission_id, None, None, None, kind, {'actor': owner, **payload})

    def add_evidence(self, owner, mission_id, revision, *, content='', source='', event_id=None):
        """Owner notes are unverified submissions. Runtime imports record references only."""
        with self._change(owner, mission_id, revision) as db:
            self._capacity(db, 'trust_evidence', mission_id, 1000)
            kind = 'OWNER_NOTE'
            if event_id:
                event = db.execute('SELECT id,type,sequence,occurred_at,step_id,agent_id FROM events '
                                   'WHERE mission_id=? AND id=?', (mission_id, event_id)).fetchone()
                if event is None or event['type'] not in RUNTIME_TYPES:
                    raise TrustError(422, 'A supported runtime event from this mission is required')
                if db.execute("SELECT 1 FROM trust_evidence WHERE mission_id=? AND json_extract(document,'$.event_id')=?",
                              (mission_id, event_id)).fetchone():
                    raise TrustError(409, 'Runtime event already collected')
                kind, source, content = 'RUNTIME_EVENT', 'mission_event:' + event_id, canonical(dict(event))
            else:
                text(content, 'content', 20000)
                text(source, 'source')
            record = {'id': uuid4().hex, 'mission_id': mission_id, 'kind': kind,
                      'source': source, 'content': content, 'event_id': event_id,
                      'collected_by': owner, 'collected_at': now(),
                      'source_verification': 'PERSISTED_EVENT_REFERENCE' if event_id else 'NOT_INDEPENDENTLY_VERIFIED'}
            sha = digest(record)
            db.execute('INSERT INTO trust_evidence VALUES (?,?,?,?,?)',
                       (record['id'], mission_id, canonical(record), sha, now()))
            self._event(db, mission_id, 'TRUST_EVIDENCE_COLLECTED', owner, evidence_id=record['id'], sha256=sha)
        return {'evidence': {**record, 'sha256': sha}, 'revision': revision + 1}

    def add_claim(self, owner, mission_id, revision, *, statement, subject, predicate, value,
                  context=None, evidence_ids=()):
        for key, item in [('statement', statement), ('subject', subject), ('predicate', predicate), ('value', value)]:
            text(item, key)
        context = context or {}
        if not isinstance(context, dict) or set(context) - set(CONTEXT_KEYS):
            raise TrustError(422, 'Unsupported claim context')
        for key, item in context.items():
            text(item, key, required=False)
        with self._change(owner, mission_id, revision) as db:
            self._capacity(db, 'trust_claims', mission_id, 200)
            self._check_evidence(db, mission_id, evidence_ids)
            record = {'id': uuid4().hex, 'statement': statement, 'subject': subject,
                      'predicate': predicate, 'value': value, 'context': context,
                      'evidence_ids': list(dict.fromkeys(evidence_ids)), 'created_by': owner, 'created_at': now()}
            db.execute('INSERT INTO trust_claims VALUES (?,?,?,?)', (record['id'], mission_id, canonical(record), now()))
            self._event(db, mission_id, 'TRUST_CLAIM_CREATED', owner, claim_id=record['id'])
        return {'claim': record, 'revision': revision + 1}

    def _check_evidence(self, db, mission_id, ids):
        if not isinstance(ids, (list, tuple)) or len(ids) > 100 or any(not isinstance(x, str) for x in ids):
            raise TrustError(422, 'Evidence ids must be a bounded list')
        for id in ids:
            if db.execute('SELECT 1 FROM trust_evidence WHERE id=? AND mission_id=?', (id, mission_id)).fetchone() is None:
                raise TrustError(422, 'Evidence does not belong to this mission')

    def _capacity(self, db, table, mission_id, maximum):
        if db.execute('SELECT count(*) FROM '+table+' WHERE mission_id=?', (mission_id,)).fetchone()[0] >= maximum:
            raise TrustError(409, 'Mission evidence storage limit reached')

    def review(self, owner, mission_id, revision, claim_id, *, verdict, reason):
        if verdict not in {'FACT', 'INFERENCE', 'UNKNOWN'}:
            raise TrustError(422, 'Review verdict must be FACT, INFERENCE or UNKNOWN')
        text(reason, 'reason')
        with self._change(owner, mission_id, revision) as db:
            self._capacity(db, 'trust_reviews', mission_id, 2000)
            row = db.execute('SELECT document FROM trust_claims WHERE id=? AND mission_id=?', (claim_id, mission_id)).fetchone()
            if row is None:
                raise TrustError(404, 'Claim not found')
            claim = json.loads(row['document'])
            if db.execute('SELECT 1 FROM trust_retractions WHERE claim_id=?', (claim_id,)).fetchone():
                raise TrustError(409, 'Retracted claims cannot be reviewed; create a corrected claim')
            if verdict != 'UNKNOWN' and not claim['evidence_ids']:
                raise TrustError(409, 'A supported conclusion needs linked evidence')
            record = {'id': uuid4().hex, 'claim_id': claim_id, 'verdict': verdict,
                      'reason': reason, 'method': 'OWNER_REVIEW', 'actor': owner, 'reviewed_at': now()}
            db.execute('INSERT INTO trust_reviews VALUES (?,?,?,?,?)',
                       (record['id'], mission_id, claim_id, canonical(record), now()))
            self._event(db, mission_id, 'TRUST_CLAIM_REVIEWED', owner, claim_id=claim_id, verdict=verdict)
        return {'review': record, 'revision': revision + 1}

    def retract(self, owner, mission_id, revision, claim_id, reason):
        """Withdraw applicability, not history or a conflicting source's existence."""
        text(reason, 'reason')
        with self._change(owner, mission_id, revision) as db:
            if db.execute('SELECT 1 FROM trust_claims WHERE id=? AND mission_id=?', (claim_id, mission_id)).fetchone() is None:
                raise TrustError(404, 'Claim not found')
            if db.execute('SELECT 1 FROM trust_retractions WHERE claim_id=?', (claim_id,)).fetchone():
                raise TrustError(409, 'Claim already retracted')
            record = {'claim_id': claim_id, 'actor': owner, 'reason': reason, 'retracted_at': now()}
            db.execute('INSERT INTO trust_retractions VALUES (?,?,?)', (claim_id, mission_id, canonical(record)))
            self._event(db, mission_id, 'TRUST_CLAIM_RETRACTED', owner, claim_id=claim_id)
        return {'retraction': record, 'revision': revision + 1}

    def _artifacts(self, db, mission):
        """Hash bounded, regular, mission-local output files; never export paths or bytes."""
        output = []
        root = (self.store.root/'workspaces'/mission['id']).resolve()
        budget = 32 * 1024 * 1024
        for row in db.execute('SELECT * FROM artifacts WHERE mission_id=? ORDER BY id', (mission['id'],)):
            item = {key: row[key] for key in ('id', 'name', 'created_at')}
            item.update(integrity='UNAVAILABLE', sha256=None)
            path = Path(row['path'])
            path = path if path.is_absolute() else root/path
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root) or path.is_symlink():
                    item['integrity'] = 'OUTSIDE_MISSION_OR_SYMLINK'
                elif len(output) >= 256:
                    item['integrity'] = 'LIMIT_EXCEEDED'
                else:
                    fd = os.open(resolved, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                    with os.fdopen(fd, 'rb') as file:
                        before = os.fstat(file.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            item['integrity'] = 'NOT_REGULAR_FILE'
                        elif before.st_size > min(budget, 8 * 1024 * 1024):
                            item['integrity'] = 'LIMIT_EXCEEDED'
                        else:
                            content = file.read(min(budget, 8 * 1024 * 1024)+1)
                            budget -= len(content)
                            after = os.fstat(file.fileno())
                            if len(content) != before.st_size or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                                item['integrity'] = 'CHANGED_DURING_READ'
                            else:
                                item.update(integrity='HASHED', sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
            except (OSError, ValueError, RuntimeError):
                pass
            output.append(item)
        return output

    def _state(self, db, owner, mission_id):
        mission = self._owned(db, owner, mission_id)
        evidence = []
        for row in db.execute('SELECT * FROM trust_evidence WHERE mission_id=? ORDER BY rowid', (mission_id,)):
            document = json.loads(row['document'])
            if digest(document) != row['sha256']:
                raise TrustError(409, 'Stored evidence integrity check failed')
            evidence.append({**document, 'sha256': row['sha256']})
        claims = [json.loads(row['document']) for row in db.execute(
            'SELECT document FROM trust_claims WHERE mission_id=? ORDER BY rowid', (mission_id,))]
        reviews = [json.loads(row['document']) for row in db.execute(
            'SELECT document FROM trust_reviews WHERE mission_id=? ORDER BY rowid', (mission_id,))]
        last_review = {review['claim_id']: review for review in reviews}
        retractions = {row['claim_id']: json.loads(row['document']) for row in db.execute(
            'SELECT claim_id,document FROM trust_retractions WHERE mission_id=?', (mission_id,))}
        conflicts = []
        for i, left in enumerate(claims):
            for right in claims[i+1:]:
                conflict = classify_conflict(left, right)
                if conflict:
                    conflicts.append({'left': left['id'], 'right': right['id'],
                                      'active': left['id'] not in retractions and right['id'] not in retractions, **conflict})
        disputed = {id for c in conflicts if c['active'] and c['classification'] in {'DIRECT_CONFLICT', 'REVIEW'}
                    for id in (c['left'], c['right'])}
        for claim in claims:
            review = last_review.get(claim['id'])
            claim.update(status='DISPUTED' if claim['id'] in disputed else review['verdict'] if review else 'UNKNOWN',
                         review=review, lifecycle='RETRACTED' if claim['id'] in retractions else 'ACTIVE',
                         retraction=retractions.get(claim['id']),
                         confidence=None, confidence_basis='No calibrated probability model')
        active_claims = [claim for claim in claims if claim['lifecycle'] == 'ACTIVE']
        steps = [dict(row) for row in db.execute(
            'SELECT id,label,position,status,depends_on FROM steps WHERE mission_id=? ORDER BY position', (mission_id,))]
        trace = []
        for row in db.execute('SELECT id,sequence,type,step_id,agent_id,occurred_at,payload FROM events '
                              'WHERE mission_id=? ORDER BY sequence', (mission_id,)):
            event = dict(row)
            event['payload_sha256'] = hashlib.sha256(event.pop('payload').encode()).hexdigest()
            trace.append(event)
        # Execution fingerprint excludes our own audit events so creating or accepting
        # a proof does not invalidate itself. Any other new event invalidates acceptance.
        execution = {'mission': {key: mission[key] for key in ('id', 'status', 'agent_id', 'model_id', 'skill_id', 'runtime_id', 'professional_prompt')},
                     'steps': steps, 'events': [e for e in trace if not e['type'].startswith('TRUST_')],
                     'artifacts': self._artifacts(db, mission),
                     'costs': [dict(row) for row in db.execute('SELECT id,amount,tokens,model_id FROM costs WHERE mission_id=? ORDER BY id', (mission_id,))]}
        spec = db.execute('SELECT specification,sha256 FROM mission_specifications WHERE mission_id=?', (mission_id,)).fetchone()
        if spec and digest(json.loads(spec['specification'])) != spec['sha256']:
            raise TrustError(409, 'Mission specification integrity check failed')
        revision = self._revision(db, mission_id)
        token = digest({'revision': revision, 'execution': execution, 'evidence': evidence,
                        'claims': claims, 'reviews': reviews, 'specification': spec['sha256'] if spec else None})
        blockers = []
        if not spec:
            blockers.append('MISSING_CONFIRMED_MISSION_SPECIFICATION')
        if any(item['integrity'] != 'HASHED' for item in execution['artifacts']):
            blockers.append('ARTIFACT_INTEGRITY_UNAVAILABLE')
        if not steps or any(step['status'] != 'COMPLETED' for step in steps) or mission['status'] != 'COMPLETED':
            blockers.append('EXECUTION_NOT_COMPLETED')
        # Require completion evidence for every step, rather than a single cost row
        # or an Owner note claiming that execution happened.
        imported = {item['event_id'] for item in evidence if item['kind'] == 'RUNTIME_EVENT'}
        completed_steps = {event['step_id'] for event in trace if event['id'] in imported
                           and event['type'] in {'OPENCODE_COMPLETED', 'GOOSE_COMPLETED', 'STEP_COMPLETED'}}
        if not steps or any(step['id'] not in completed_steps for step in steps):
            blockers.append('MISSING_STEP_EXECUTION_EVIDENCE')
        if not active_claims or any(claim['status'] not in {'FACT', 'INFERENCE'} for claim in active_claims):
            blockers.append('CLAIMS_REQUIRE_REVIEW')
        return {'schema_version': 1, 'mission_id': mission_id, 'trace_id': mission_id,
                'revision': revision, 'snapshot_token': token, 'evidence': evidence,
                'claims': claims, 'reviews': reviews, 'conflicts': conflicts,
                'specification': json.loads(spec['specification']) if spec else None,
                'verification': {'integrity_verified': True, 'semantic_verification': 'OWNER_REVIEWED' if active_claims and all(c['review'] for c in active_claims) else 'PENDING',
                                 'execution_verification': 'PERSISTED_RECORDS_ONLY_NOT_EXTERNAL_ATTESTATION',
                                 'acceptance_blockers': blockers, 'confidence': None,
                                 'confidence_basis': 'Human review, not statistically calibrated; hashes verify bytes only'},
                'execution': {**execution, 'mission': {k: v for k, v in execution['mission'].items() if k != 'professional_prompt'}},
                'trace': trace}

    def get(self, owner, mission_id):
        with self.store.connect() as db:
            db.execute('BEGIN')
            state = self._state(db, owner, mission_id)
            proofs = []
            for row in db.execute('SELECT id,sha256,created_at,document FROM trust_proofs WHERE mission_id=? ORDER BY rowid DESC', (mission_id,)):
                document = json.loads(row['document'])
                if digest(document) != row['sha256']:
                    raise TrustError(409, 'Proof integrity check failed')
                accepted = db.execute('SELECT document FROM trust_acceptances WHERE proof_id=?', (row['id'],)).fetchone()
                current = document['snapshot_token'] == state['snapshot_token']
                proofs.append({'id': row['id'], 'sha256': row['sha256'], 'created_at': row['created_at'],
                               'current': current, 'acceptance': json.loads(accepted['document']) if accepted else None,
                               'acceptance_current': bool(accepted) and current})
            state['proofs'] = proofs
            return state

    def snapshot(self, owner, mission_id, revision):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._state(db, owner, mission_id)
            self._capacity(db, 'trust_proofs', mission_id, 100)
            if revision != state['revision']:
                raise TrustError(409, 'Evidence changed; refresh')
            id = uuid4().hex
            document = {**state, 'proof_id': id, 'created_by': owner, 'created_at': now(),
                        'artifact_scope': 'Proof report and recorded evidence only; runtime output files are not embedded'}
            sha = digest(document)
            db.execute('INSERT INTO trust_proofs VALUES (?,?,?,?,?)', (id, mission_id, canonical(document), sha, now()))
            self._event(db, mission_id, 'TRUST_PROOF_CREATED', owner, proof_id=id, sha256=sha)
        return {'id': id, 'sha256': sha, 'snapshot_token': state['snapshot_token'], 'verification': state['verification']}

    def _proof(self, db, owner, mission_id, proof_id):
        self._owned(db, owner, mission_id)
        row = db.execute('SELECT * FROM trust_proofs WHERE id=? AND mission_id=?', (proof_id, mission_id)).fetchone()
        if row is None:
            raise TrustError(404, 'Proof not found')
        document = json.loads(row['document'])
        if digest(document) != row['sha256']:
            raise TrustError(409, 'Proof integrity check failed')
        return document, row['sha256']

    def accept(self, owner, mission_id, proof_id, sha256, reason):
        text(reason, 'reason')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            proof, sha = self._proof(db, owner, mission_id, proof_id)
            if sha256 != sha:
                raise TrustError(409, 'Proof hash mismatch')
            state = self._state(db, owner, mission_id)
            if state['snapshot_token'] != proof['snapshot_token']:
                raise TrustError(409, 'Proof is stale; create and review a new snapshot')
            if state['verification']['acceptance_blockers']:
                raise TrustError(409, 'Acceptance blocked: ' + ', '.join(state['verification']['acceptance_blockers']))
            previous = db.execute('SELECT document FROM trust_acceptances WHERE proof_id=?', (proof_id,)).fetchone()
            if previous:
                return json.loads(previous['document'])
            record = {'proof_id': proof_id, 'proof_sha256': sha, 'actor': owner, 'reason': reason,
                      'accepted_at': now(), 'method': 'OWNER_ACCEPTANCE', 'snapshot_token': proof['snapshot_token']}
            db.execute('INSERT INTO trust_acceptances VALUES (?,?,?)', (proof_id, canonical(record), now()))
            self._event(db, mission_id, 'TRUST_PROOF_ACCEPTED', owner, proof_id=proof_id, sha256=sha)
        return record

    def export(self, owner, mission_id, proof_id):
        with self.store.connect() as db:
            proof, sha = self._proof(db, owner, mission_id, proof_id)
        report = ('# FACAI Proof Artifact\n\nMission: ' + mission_id + '\n\n'
                  'Snapshot: ' + proof_id + '\n\nThis report records evidence and explicit Owner reviews. '
                  'It does not certify external source truth. See verification.json for blockers.\n\n' +
                  '\n'.join('- ' + c.get('lifecycle', 'ACTIVE') + ' / ' + c['status'] + ': ' + c['statement'] for c in proof['claims']) + '\n')
        files = {'report.md': report.encode(),
                 **{name + '.json': (canonical(value)+'\n').encode() for name, value in {
                     'evidence': proof['evidence'], 'claims': proof['claims'], 'verification': proof['verification'],
                     'trace': proof['trace'], 'specification': proof['specification'],
                     'metadata': {'schema_version': 1, 'mission_id': mission_id, 'trace_id': mission_id,
                                  'proof_id': proof_id, 'proof_sha256': sha, 'snapshot_token': proof['snapshot_token'],
                                  'created_at': proof['created_at'], 'artifact_scope': proof['artifact_scope']},
                     'proof': proof}.items()}}
        # The externally supplied/accepted proof hash must also match the actual
        # exported proof.json bytes, not a subtly different newline variant.
        files['proof.json'] = canonical(proof).encode()
        files['manifest.json'] = (canonical({'algorithm': 'sha256', 'files': {
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()}})+'\n').encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return output.getvalue()

    def save_knowledge(self, owner, mission_id, proof_id, sha256, title, tags=()):
        """Explicitly curate an accepted proof; never promote inference to fact."""
        text(title, 'title', 200)
        if not isinstance(tags, (list, tuple)) or len(tags) > 20:
            raise TrustError(422, 'Tags must be a bounded list')
        for tag in tags:
            text(tag, 'tag', 50)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            proof, sha = self._proof(db, owner, mission_id, proof_id)
            if sha != sha256:
                raise TrustError(409, 'Proof hash mismatch')
            acceptance = db.execute('SELECT document FROM trust_acceptances WHERE proof_id=?', (proof_id,)).fetchone()
            state = self._state(db, owner, mission_id)
            if not acceptance or proof['snapshot_token'] != state['snapshot_token'] or state['verification']['acceptance_blockers']:
                raise TrustError(409, 'Knowledge requires a currently accepted proof snapshot')
            existing = db.execute('SELECT id FROM knowledge_assets WHERE proof_id=? AND owner_id=?', (proof_id, owner)).fetchone()
            if existing:
                return {'id': existing['id'], 'duplicate': True}
            id = uuid4().hex
            document = {'schema_version': 1, 'id': id, 'title': title, 'tags': list(dict.fromkeys(tags)),
                        'created_by': owner, 'created_at': now(), 'trace_id': mission_id,
                        'source': {'mission_id': mission_id, 'proof_id': proof_id, 'proof_sha256': sha,
                                   'snapshot_token': proof['snapshot_token'], 'acceptance': json.loads(acceptance['document'])},
                        'objective': proof['specification']['objective'],
                        'claims': proof['claims'], 'evidence_references': [
                            {'id': item['id'], 'sha256': item['sha256'], 'kind': item['kind'], 'source': item['source']}
                            for item in proof['evidence']],
                        'confidence': None, 'policy': 'Preserve source claim status and human-review basis; storage is not factual certification'}
            db.execute('INSERT INTO knowledge_assets VALUES (?,?,?,?,?,?,?,?)',
                       (id, owner, mission_id, proof_id, canonical(document), digest(document),
                        title+'\n'+'\n'.join(c['statement'] for c in proof['claims'])+'\n'+' '.join(tags), now()))
            self._event(db, mission_id, 'TRUST_KNOWLEDGE_SAVED', owner, knowledge_id=id, proof_id=proof_id)
        return {'id': id, 'duplicate': False}

    def knowledge(self, owner, query='', limit=50, offset=0):
        text(query, 'query', 200, required=False)
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise TrustError(422, 'Invalid pagination')
        # Treat wildcard characters as literal user text; SQL remains parameterized.
        pattern = '%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.store.connect() as db:
            rows = db.execute("SELECT document,sha256 FROM knowledge_assets WHERE owner_id=? AND search_text LIKE ? ESCAPE '\\' "
                              'ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (owner, pattern, limit, offset)).fetchall()
        result = []
        for row in rows:
            document = json.loads(row['document'])
            if digest(document) != row['sha256']:
                raise TrustError(409, 'Knowledge integrity check failed')
            result.append({'id': document['id'], 'title': document['title'], 'tags': document['tags'],
                           'created_at': document['created_at'], 'source': document['source'],
                           'claim_statuses': [claim['status'] for claim in document['claims']],
                           'sha256': row['sha256'], 'source_current': None})
        return {'items': result, 'limit': limit, 'offset': offset,
                'notice': 'Immutable accepted historical snapshots; open an item to check whether its source is still current'}

    def get_knowledge(self, owner, id):
        with self.store.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM knowledge_assets WHERE id=? AND owner_id=?', (id, owner)).fetchone()
            if row is None:
                raise TrustError(404, 'Knowledge asset not found')
            document = json.loads(row['document'])
            if digest(document) != row['sha256']:
                raise TrustError(409, 'Knowledge integrity check failed')
            proof, sha = self._proof(db, owner, row['mission_id'], row['proof_id'])
            if sha != document['source']['proof_sha256']:
                raise TrustError(409, 'Source proof hash mismatch')
            state = self._state(db, owner, row['mission_id'])
            current = proof['snapshot_token'] == state['snapshot_token']
            return {**document, 'sha256': row['sha256'], 'source_current': current,
                    'source_current_blockers': state['verification']['acceptance_blockers']}
