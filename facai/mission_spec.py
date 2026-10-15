"""An immutable task contract, compiled from a confirmed interpretation only."""
import hashlib
import json

from .store import now


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class MissionSpecifications:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS mission_specifications (
                mission_id TEXT PRIMARY KEY REFERENCES missions(id),
                specification TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL)''')

    def create(self, db, mission_id, intent, steps):
        specification = {
            'schema_version': 1, 'mission_id': mission_id,
            'intent_id': intent['id'], 'intent_revision': intent['revision'],
            'trace_id': mission_id, 'raw_request': intent['raw_request'],
            'objective': {key: intent[key] for key in ('action', 'target', 'purpose', 'scope', 'constraints')},
            'required_inputs': [{'name': 'target', 'value': intent['target'], 'basis': 'confirmed_intent'}],
            'expected_outputs': [{'name': 'result', 'format': None, 'basis': 'unspecified_in_intent'}],
            'execution_steps': steps, 'plan_basis': 'deterministic_process_scaffold_not_executed',
            'verification_method': 'Owner compares actual execution evidence with the confirmed objective and constraints',
            'evidence_required': ['execution_record', 'claim_sources', 'verification_review'],
            'missing_context': intent['missing_context'], 'assumptions': intent['assumptions'],
            'authorization': 'NOT_GRANTED', 'execution': 'NOT_STARTED',
        }
        db.execute('INSERT INTO mission_specifications VALUES (?,?,?,?)',
                   (mission_id, canonical(specification), digest(specification), now()))
        return specification

    def get(self, owner, mission_id):
        with self.store.connect() as db:
            row = db.execute('SELECT s.* FROM mission_specifications s JOIN missions m ON m.id=s.mission_id '
                             'WHERE m.id=? AND m.owner_id=?', (mission_id, owner)).fetchone()
        if row is None:
            return None
        result = json.loads(row['specification'])
        if digest(result) != row['sha256']:
            raise ValueError('Mission specification integrity check failed')
        return {'specification': result, 'sha256': row['sha256'], 'created_at': row['created_at']}
