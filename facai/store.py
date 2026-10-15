"""SQLite mission record. Every mutable item is keyed by mission ID."""

import json
import math
import sqlite3
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from .authority_gate import ensure_mission_grant


def now():
    return datetime.now(timezone.utc).isoformat()


# 协助模式 attribution. The API layer sets this (via require_mission's assist
# bypass) for the duration of a request that reaches another owner's resource
# through an active assist session: {"admin", "reason", "expires_at"}. Every
# event written inside such a request is stamped ``assist_by`` (see _event), so
# the audit trail never presents the operator's work as the customer's own.
# Background threads (scheduler, notify) run outside the request context and
# stay unstamped -- their events are the mission's own work, not the visit.
assist_context = ContextVar("facai_assist_context", default=None)


# Hosted-channel binding states (docs/CHANNEL_HOST_DESIGN.md §2). The order is
# the only legal forward path; UNBOUND is terminal and reopens by writing the
# same row back to BINDING (see transition_host_binding).
HOST_BINDING_STATES = ("BINDING", "ACTIVE", "UNBOUND")


# Extra lease time granted on top of a job's own execution budget. A claim
# lease must outlive everything the worker does for the job -- waiting up to
# ``jobs.timeout`` seconds for the agent, plus workspace/worktree setup and
# result handling -- because recovery requeues any RUNNING job whose lease has
# passed, and a requeued-but-alive job lets a second worker re-run the same step
# in the shared mission workspace.
JOB_LEASE_MARGIN_SECONDS = 300

# Absolute floor for a claim lease. Parallel branches of one mission take
# turns on the OpenCode CLI serial lock, so a branch's wall time can approach
# two timeouts (its own wait behind the lock plus its own run) even though its
# ``timeout`` budget covers only the run; the lease must outlive both or a
# peer scheduler's recovery requeues a live job for immediate re-execution.
JOB_LEASE_FLOOR_SECONDS = 600

# Head start given to a job that recovery requeues: a second worker may not
# re-claim it for this long after the lease expired, so the original worker --
# whose lease expiry is the only reason the requeue happened -- can still win
# the race and finalize its result instead of being double-executed.
JOB_REQUEUE_BUFFER_SECONDS = 60

# A recharge stays claimable past its payment window for this settlement
# grace (chain-confirmation lag), then closes as EXPIRED and releases its
# txid. Must stay in step with the chain-sync matching window, which also
# stops crediting a recharge at ``expires_at`` + this grace -- expiring
# earlier could strand a payment that was still going to auto-credit.
RECHARGE_EXPIRY_GRACE_SECONDS = 15 * 60

# 渠内审批（Grant×渠道相乘点）：一条审批请求在聊天里保持可回复的窗口。
# 客户的「批准」签发的一次性 grant 用同一个 TTL，所以提示文案里的
# 「15 分钟内有效」对审批行和 grant 同时成立。过期以 expires_at 与
# 读取时刻的比较为准（读时判定），行状态只是审计落点。
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60


def mission_progress(completed, total, active=0):
    """Whole-percent mission progress in ``[0, 100]`` as an ``int``.

    Only real COMPLETED steps count (never a RUNNING or FAILED one), and the
    arithmetic stays in integers so the UI can never receive a float such as
    ``33.33333``. A partial mission is clamped to ``1..99``: the first completed
    step is visible as at least 1%, and the bar only reaches 100% once every
    step is complete. With ``total <= 100`` each completed step therefore moves
    the value by at least one percentage point.
    """
    if total <= 0:
        return 0
    if completed <= 0:
        return 1 if active else 0
    if completed >= total:
        return 100
    return max(1, min(99, (100 * completed) // total))


# SQLite caps host parameters per statement (999 before SQLite 3.32), so a
# mission list of any size must never be pasted into a single IN (...) clause.
_ID_CHUNK = 500


def _chunks(ids):
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start:start + _ID_CHUNK]


def _validate_step_graph(specs):
    """Reject cycles before a mission and its workspace are created."""
    dependencies = {spec["id"]: spec["depends_on"] for spec in specs}
    visiting, visited = set(), set()

    def visit(step_id):
        if step_id in visiting:
            raise ValueError("Step dependencies must form a DAG")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in dependencies[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for key in dependencies:
        visit(key)


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS missions (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, original_input TEXT NOT NULL,
 status TEXT NOT NULL, workspace TEXT NOT NULL, agent_id TEXT, model_id TEXT,
 created_at TEXT NOT NULL, professional_prompt TEXT,
 skill_id TEXT, runtime_id TEXT, owner_id TEXT NOT NULL DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS steps (
 id TEXT NOT NULL, mission_id TEXT NOT NULL REFERENCES missions(id),
 label TEXT NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'WAITING',
 depends_on TEXT NOT NULL DEFAULT '[]', model_id TEXT,
 PRIMARY KEY (mission_id, id)
);
CREATE TABLE IF NOT EXISTS events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE, mission_id TEXT NOT NULL REFERENCES missions(id),
 step_id TEXT, session_id TEXT, agent_id TEXT, type TEXT NOT NULL,
 payload TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_mission ON events(mission_id, sequence);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
 agent_id TEXT NOT NULL, model_id TEXT, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
 name TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registries (
 kind TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
 updated_at TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT 'admin',
 PRIMARY KEY (kind, id, owner_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
 id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
 commit_hash TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
);
-- input_tokens/output_tokens are appended last so a fresh database and one
-- upgraded by _migrate share the same column order; NULL means the upstream
-- never reported that side (historical rows included), never zero.
CREATE TABLE IF NOT EXISTS costs (
 id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
 provider TEXT, model_id TEXT, amount REAL NOT NULL, tokens INTEGER,
 currency TEXT NOT NULL, created_at TEXT NOT NULL,
 input_tokens INTEGER, output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS costs_by_mission ON costs(mission_id, created_at);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
 step_id TEXT, prompt TEXT NOT NULL, model_ref TEXT, status TEXT NOT NULL,
 priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 1, timeout INTEGER NOT NULL DEFAULT 300,
 lease_owner TEXT, lease_expires_at TEXT, error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, priority, created_at);
CREATE INDEX IF NOT EXISTS jobs_by_mission ON jobs(mission_id);
CREATE TABLE IF NOT EXISTS auth_sessions (
 token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, username TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS login_attempts (
 client_ip TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
 first_at TEXT NOT NULL, last_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_users (
 username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
-- Client API keys. Only the SHA-256 digest of a key's secret half is stored, so
-- the database cannot reproduce a key; ``prefix`` is the public label the
-- console shows, ``revoked_at`` non-NULL means the key no longer authenticates.
CREATE TABLE IF NOT EXISTS mission_grants (
    grant_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    action_key TEXT NOT NULL,
    issued_at TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, key_hash TEXT NOT NULL,
 prefix TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT,
 last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS api_keys_by_owner ON api_keys(owner_id, created_at);
CREATE TABLE IF NOT EXISTS user_budgets (
 username TEXT PRIMARY KEY, monthly_limit_usd REAL NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- 80% soft-threshold reminder dedupe (billing): one row per (owner, month)
-- once the "余额即将用尽，请及时充值" reminder has gone out, so the alert
-- fires exactly once per billing period no matter how many budget-gate calls
-- or dispatches cross the threshold. sent_at is the audit stamp.
CREATE TABLE IF NOT EXISTS user_alerts (
 owner_id TEXT NOT NULL, month TEXT NOT NULL, sent_at TEXT NOT NULL,
 PRIMARY KEY (owner_id, month)
);
CREATE TABLE IF NOT EXISTS recharges (
 id TEXT PRIMARY KEY, username TEXT NOT NULL, amount_usd REAL NOT NULL,
 txid TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 decided_at TEXT, decided_by TEXT
);
CREATE INDEX IF NOT EXISTS recharges_by_status ON recharges(status, created_at);
CREATE TABLE IF NOT EXISTS chain_transfers (
 txid TEXT PRIMARY KEY, from_address TEXT, to_address TEXT NOT NULL,
 amount_usd REAL NOT NULL, token TEXT NOT NULL DEFAULT 'USDT',
 block_ts INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'UNMATCHED',
 matched_recharge_id TEXT, raw TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chain_transfers_by_status ON chain_transfers(status);
CREATE TABLE IF NOT EXISTS user_models (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
 endpoint TEXT NOT NULL, model TEXT NOT NULL, api_key_encrypted TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner_id, name)
);
CREATE TABLE IF NOT EXISTS channel_notify_state (
 key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_messages (
 id TEXT PRIMARY KEY, channel TEXT NOT NULL, direction TEXT NOT NULL,
 external_id TEXT, sender TEXT, recipient TEXT, kind TEXT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
 mission_id TEXT, created_at TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT 'admin',
 conversion_owner TEXT, conversion_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS channel_messages_lookup ON channel_messages(channel, created_at);
CREATE INDEX IF NOT EXISTS channel_messages_by_mission ON channel_messages(mission_id);
CREATE TABLE IF NOT EXISTS channel_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE, channel TEXT NOT NULL, message_id TEXT NOT NULL,
 type TEXT NOT NULL, payload TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS channel_events_by_message ON channel_events(message_id, sequence);
CREATE TABLE IF NOT EXISTS n8n_deliveries (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 mission_id TEXT, event_id TEXT, event_type TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3, http_status INTEGER,
 error TEXT, retryable INTEGER NOT NULL DEFAULT 0,
 payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS n8n_deliveries_recent ON n8n_deliveries(created_at);
CREATE TABLE IF NOT EXISTS n8n_callbacks (
 request_id TEXT PRIMARY KEY, mission_id TEXT, status TEXT NOT NULL,
 detail TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS n8n_state (
 key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- A customer's own chat bot ("绑一次 token = 云端 7×24"): one row per
-- (owner, channel). The bot token is stored ONLY as a secretbox ciphertext --
-- never plaintext, never a hash that could be replayed. Read paths for the
-- API return chat_id/created_at alone; the token leaves the box only in
-- memory at the moment the NOTIFY loop sends that owner's progress.
CREATE TABLE IF NOT EXISTS channel_bindings (
 owner_id TEXT NOT NULL, channel TEXT NOT NULL,
 bot_token_encrypted TEXT NOT NULL, chat_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY (owner_id, channel)
);
-- Channel HOSTING bindings (docs/CHANNEL_HOST_DESIGN.md): the state machine
-- behind "每客户通道默认持久" for the hosted personal channels (wechat /
-- whatsapp). One row per (owner, channel) -- the per-customer/per-channel
-- quota of 1 is the primary key itself, not an application-layer promise.
-- No secret ever lands here: this wave stores lifecycle state and the
-- per-owner credential NAMESPACE (a relative path under
-- data/providers/hosting/<owner_id>/<channel>); the credential files
-- themselves belong to the per-owner monitor wave and are not fabricated
-- while absent. UNBOUND is terminal: reopening writes the SAME row back to
-- BINDING (fresh session_dir, refreshed updated_at, original created_at/id)
-- so the revocation history stays queryable.
CREATE TABLE IF NOT EXISTS channel_host_bindings (
 id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL, channel TEXT NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('BINDING','ACTIVE','UNBOUND')),
 session_dir TEXT, display_name TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY (owner_id, channel)
);
-- 协助模式（可见可审，绝不静默）: one row per operator assistance session on a
-- customer account. The row IS the audit record -- who assisted whom, why, and
-- until when; ended_at (NULL while live) keeps an early-ended session queryable
-- instead of erasing it. Expiry is judged on expires_at at read time, so an
-- out-of-window row can never admit the require_mission bypass even before
-- expire_assist_sessions housekeeping deletes it.
CREATE TABLE IF NOT EXISTS assist_sessions (
 id TEXT PRIMARY KEY, admin_username TEXT NOT NULL, owner_username TEXT NOT NULL,
 reason TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
 ended_at TEXT
);
CREATE INDEX IF NOT EXISTS assist_sessions_by_owner ON assist_sessions(owner_username, ended_at);
-- 渠内审批（in-channel approval）：一次渠道任务的授权请求，Grant×渠道的
-- 相乘点。Owner 核无法签发任务 grant 时（GRANT_REQUIRED），任务在此挂起，
-- 客户在原聊天里回复「批准」即可换发一次性 grant。owner_id 是隔离边界：
-- 只有行主人的回复能消费它。status: PENDING(待批) / CONSUMED(已批准消费) /
-- CANCELLED(客户取消) / EXPIRED(超时清扫)。是否过期以 expires_at 与读取
-- 时刻比较为准，状态只服务审计与防重发，不单独构成裁决。
CREATE TABLE IF NOT EXISTS pending_approvals (
 id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
 owner_id TEXT NOT NULL, channel TEXT NOT NULL, message_id TEXT,
 status TEXT NOT NULL DEFAULT 'PENDING',
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_approvals_by_owner
 ON pending_approvals(owner_id, status, expires_at);
CREATE INDEX IF NOT EXISTS pending_approvals_by_mission ON pending_approvals(mission_id);
"""


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "facai.sqlite3"
        with self.connect() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(missions)")}
        for name in ("professional_prompt", "skill_id", "runtime_id"):
            if name not in columns:
                db.execute(f"ALTER TABLE missions ADD COLUMN {name} TEXT")
        if "owner_id" not in columns:
            db.execute("ALTER TABLE missions ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
        # A registered account whose password is known-weak must rotate it
        # before normal use; the flag lives beside the hash (never the value).
        pu_columns = {row[1] for row in db.execute("PRAGMA table_info(platform_users)")}
        if "must_change_password" not in pu_columns:
            db.execute("ALTER TABLE platform_users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        # Preserve access to pre-tenant records for the configured legacy admin.
        import os
        legacy_owner = os.getenv("FACAI_ADMIN_USER", "admin")
        db.execute("UPDATE missions SET owner_id=? WHERE owner_id='admin'", (legacy_owner,))
        registry_columns = {row[1] for row in db.execute("PRAGMA table_info(registries)")}
        if "owner_id" not in registry_columns:
            # Recreate the table to update the PRIMARY KEY to (kind, id, owner_id)
            db.execute("ALTER TABLE registries RENAME TO registries_old")
            db.execute("""CREATE TABLE registries (
                kind TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
                updated_at TEXT NOT NULL, owner_id TEXT NOT NULL DEFAULT 'admin',
                PRIMARY KEY (kind, id, owner_id)
            )""")
            db.execute("INSERT INTO registries (kind, id, value, updated_at, owner_id) "
                       "SELECT kind, id, value, updated_at, 'admin' FROM registries_old")
            db.execute("DROP TABLE registries_old")
        db.execute("UPDATE registries SET owner_id=? WHERE owner_id='admin'", (legacy_owner,))
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(channel_messages)")}
        if "owner_id" not in message_columns:
            db.execute("ALTER TABLE channel_messages ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'admin'")
        db.execute("UPDATE channel_messages SET owner_id=? WHERE owner_id='admin'", (legacy_owner,))
        # The owner index lives here, never in SCHEMA: executescript(SCHEMA)
        # runs before this migration, so on a database whose channel_messages
        # predates the owner_id column the index statement would fail with
        # "no such column" and the store could never boot far enough to heal
        # itself. Created after the column is guaranteed to exist;
        # IF NOT EXISTS keeps every later boot (and a dropped index) covered.
        db.execute("CREATE INDEX IF NOT EXISTS channel_messages_by_owner"
                   " ON channel_messages(owner_id, created_at)")
        # Per-inbound-message conversion lease. These columns are added to
        # pre-existing databases so a duplicate webhook delivery races on an
        # atomic claim instead of both invoking the paid model.
        if "conversion_owner" not in message_columns:
            db.execute("ALTER TABLE channel_messages ADD COLUMN conversion_owner TEXT")
        if "conversion_expires_at" not in message_columns:
            db.execute("ALTER TABLE channel_messages ADD COLUMN conversion_expires_at TEXT")
        step_columns = {row[1] for row in db.execute("PRAGMA table_info(steps)")}
        if "depends_on" not in step_columns:
            db.execute("ALTER TABLE steps ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]'")
            missions = db.execute("SELECT id FROM missions").fetchall()
            for mission in missions:
                previous = None
                steps = db.execute("SELECT id FROM steps WHERE mission_id=? ORDER BY position",
                                   (mission["id"],)).fetchall()
                for row in steps:
                    deps = [previous] if previous else []
                    db.execute("UPDATE steps SET depends_on=? WHERE mission_id=? AND id=?",
                               (json.dumps(deps), mission["id"], row["id"]))
                    previous = row["id"]
        if "model_id" not in step_columns:
            # Optional per-step model binding; NULL means "use the mission model".
            db.execute("ALTER TABLE steps ADD COLUMN model_id TEXT")
        # Split token counts for the cost ledger. Pre-existing rows keep NULL:
        # the split was never captured, and NULL must stay distinguishable from
        # a reported zero.
        cost_columns = {row[1] for row in db.execute("PRAGMA table_info(costs)")}
        for name in ("input_tokens", "output_tokens"):
            if name not in cost_columns:
                db.execute(f"ALTER TABLE costs ADD COLUMN {name} INTEGER")
        # Self-healing retries: a transiently failed job requeues with a
        # not-before stamp so the next claim attempt waits out the backoff.
        job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "not_before" not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN not_before TEXT")
        # One on-chain txid backs at most one recharge: attaching it to a second
        # recharge is a double-claim attempt. Partial index (NULL/'' excluded)
        # so recharges without a txid never collide. Deliberately NOT in SCHEMA:
        # a legacy database that already holds duplicate non-empty txids could
        # not boot; here creation is best-effort and a failure (duplicates that
        # need a manual operator dedupe) is reported without breaking startup.
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS recharges_txid_unique"
                       " ON recharges(txid) WHERE txid IS NOT NULL AND txid <> ''")
        except sqlite3.IntegrityError as exc:
            print(f"store: recharges.txid unique index not created "
                  f"(duplicate txids need manual dedupe): {exc}", flush=True)

    def registry_list(self, kind, owner_id=None):
        with self.connect() as db:
            if owner_id is None:
                rows = db.execute("SELECT id, value FROM registries WHERE kind=? ORDER BY id", (kind,))
            else:
                rows = db.execute("SELECT id, value FROM registries WHERE kind=? AND owner_id=? ORDER BY id", (kind, owner_id))
            return {row["id"]: json.loads(row["value"]) for row in rows}

    def registry_register(self, kind, key, value, owner_id="admin"):
        with self.connect() as db:
            db.execute("""INSERT INTO registries (kind, id, value, updated_at, owner_id) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(kind, id, owner_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                       (kind, key, json.dumps(value, ensure_ascii=False), now(), owner_id))

    def registry_delete(self, kind, key, owner_id="admin"):
        """Remove one owner-scoped registry row; returns True when it existed."""
        with self.connect() as db:
            removed = db.execute("DELETE FROM registries WHERE kind=? AND id=? AND owner_id=?",
                                 (kind, key, owner_id)).rowcount
        return bool(removed)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        # WAL lets scheduler threads, the web layer and the digest job read and
        # write concurrently instead of serializing on the delete-mode write
        # lock (the root cause of the "database is locked" failures). The
        # setting is persistent per database file; synchronous=NORMAL is the
        # recommended pairing (safe against crashes, minor risk on power loss).
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def create(self, title, original_input, labels, owner_id="admin", *,
               connection=None, issue_grant=True, mission_id=None):
        """Create normally, or insert into a caller transaction without a grant.

        External transactions return only the id until committed. Their caller
        owns rollback/cleanup; IntentWorkspace handles its new workspace on failure.
        Existing callers retain full-record results and post-commit grants.
        """
        if connection is not None and issue_grant:
            raise ValueError("External mission transactions cannot issue grants")
        specs = []
        legacy = all(isinstance(item, str) for item in labels)
        for item in labels:
            if isinstance(item, str):
                specs.append({"id": uuid4().hex, "label": item, "depends_on": []})
            elif isinstance(item, dict) and isinstance(item.get("label"), str):
                spec = {"id": item.get("id") or uuid4().hex,
                        "label": item["label"], "depends_on": item.get("depends_on", [])}
                model_id = item.get("model_id")
                if model_id is not None:
                    if not isinstance(model_id, str) or not model_id.strip():
                        raise ValueError("Step model_id must be a non-empty string when provided")
                    spec["model_id"] = model_id
                specs.append(spec)
            else:
                raise ValueError("Each step must be a label or an object with a label")
        labels_seen = [spec["label"] for spec in specs]
        ids_seen = [spec["id"] for spec in specs]
        if any(not label.strip() for label in labels_seen):
            raise ValueError("Step labels cannot be empty")
        if (not legacy and len(set(labels_seen)) != len(labels_seen)) or len(set(ids_seen)) != len(ids_seen):
            raise ValueError("Step labels and ids must be unique")
        if any(not isinstance(step_id, str) or not step_id for step_id in ids_seen):
            raise ValueError("Step ids must be non-empty strings")
        references = {spec["id"]: spec["id"] for spec in specs}
        references.update({spec["label"]: spec["id"] for spec in specs})
        for position, spec in enumerate(specs):
            raw_deps = spec["depends_on"]
            if not isinstance(raw_deps, list) or any(not isinstance(ref, str) for ref in raw_deps):
                raise ValueError("depends_on must be a list of step ids or labels")
            if legacy and position:
                raw_deps = [specs[position - 1]["id"]]
            deps = []
            for ref in raw_deps:
                if ref not in references:
                    raise ValueError(f"Unknown step dependency: {ref}")
                dep_id = references[ref]
                if dep_id == spec["id"]:
                    raise ValueError("A step cannot depend on itself")
                if dep_id not in deps:
                    deps.append(dep_id)
            spec["depends_on"] = deps
        _validate_step_graph(specs)
        mission_id = mission_id or uuid4().hex
        if (not isinstance(mission_id, str) or len(mission_id) != 32
                or any(c not in "0123456789abcdef" for c in mission_id)):
            raise ValueError("Invalid mission id")
        workspace = (self.root / "workspaces" / mission_id).resolve()
        workspace.mkdir(parents=True, exist_ok=False)
        with (nullcontext(connection) if connection is not None else self.connect()) as db:
            db.execute("INSERT INTO missions (id, title, original_input, status, workspace, agent_id,"
                       " model_id, created_at, professional_prompt, owner_id)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (mission_id, title, original_input, "WAITING", str(workspace), None, None,
                        now(), None, owner_id))
            for position, spec in enumerate(specs):
                db.execute("INSERT INTO steps (id, mission_id, label, position, status, depends_on, model_id)"
                           " VALUES (?, ?, ?, ?, 'WAITING', ?, ?)",
                           (spec["id"], mission_id, spec["label"], position,
                            json.dumps(spec["depends_on"], ensure_ascii=False), spec.get("model_id")))
            self._event(db, mission_id, None, None, None, "MISSION_CREATED", {"title": title})
        # The mission grant is issued only after the row is committed: an
        # execution must never be authorized by a grant for a mission the
        # store does not yet serve.
        if connection is not None:
            return {"id": mission_id}
        if issue_grant:
            ensure_mission_grant(mission_id)
        return self.get(mission_id)

    def count_active_missions(self, owner_id):
        """在飞任务数：owner 名下状态仍为 WAITING/RUNNING 的 mission 行数。

        COMPLETED 与 FAILED（含步骤被取消/阻塞导致的终态）不计入——它们不占用
        调度容量，也不能把客户永久锁在限额之外：步骤置 CANCELLED 即释放名额。
        反代转售审计（2026-10-06）：转售者把平台当上游批发给下家时，在飞任务
        数是按账号计的硬容量，必须在 store 层一次 SQL 数出来，而不是靠进程内
        计数（多进程部署下会各数各的）。
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM missions WHERE owner_id=? AND status IN ('WAITING','RUNNING')",
                (owner_id,)).fetchone()
        return int(row[0])

    def list(self, owner_id=None):
        """Every mission as a full detail record, prefetched in one connection.

        The console renders this list on every load, so each child table is read
        with one grouped query per chunk of mission ids instead of one
        connection and seven queries per mission; cost then scales with the
        tables and chunks, not with missions times their child rows.
        """
        with self.connect() as db:
            if owner_id is None:
                rows = db.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
            else:
                rows = db.execute("SELECT * FROM missions WHERE owner_id=? ORDER BY created_at DESC",
                                  (owner_id,)).fetchall()
            steps, sessions, artifacts, checkpoints = {}, {}, {}, {}
            cost_totals, checkpoint_steps = {}, {}
            mission_ids = [row["id"] for row in rows]
            for chunk in _chunks(mission_ids):
                marks = ",".join("?" for _ in chunk)
                for row in db.execute(
                        "SELECT mission_id, id, label, position, status, depends_on, model_id FROM steps"
                        " WHERE mission_id IN (" + marks + ") ORDER BY mission_id, position", chunk):
                    # mission_id keys the group only; a step row keeps exactly
                    # the fields the per-mission query returned.
                    steps.setdefault(row["mission_id"], []).append(
                        {key: row[key] for key in
                         ("id", "label", "position", "status", "depends_on", "model_id")})
                for row in db.execute(
                        "SELECT * FROM sessions WHERE mission_id IN (" + marks + ")", chunk):
                    sessions.setdefault(row["mission_id"], []).append(row)
                for row in db.execute(
                        "SELECT * FROM artifacts WHERE mission_id IN (" + marks + ")", chunk):
                    artifacts.setdefault(row["mission_id"], []).append(row)
                for row in db.execute(
                        "SELECT * FROM checkpoints WHERE mission_id IN (" + marks + ")"
                        " ORDER BY mission_id, created_at", chunk):
                    checkpoints.setdefault(row["mission_id"], []).append(row)
                for row in db.execute(
                        "SELECT mission_id, COALESCE(SUM(amount), 0) FROM costs"
                        " WHERE mission_id IN (" + marks + ") GROUP BY mission_id", chunk):
                    cost_totals[row[0]] = row[1]
                for row in db.execute(
                        "SELECT DISTINCT mission_id, step_id FROM events"
                        " WHERE mission_id IN (" + marks + ")"
                        " AND type='GIT_CHECKPOINT' AND step_id IS NOT NULL", chunk):
                    checkpoint_steps.setdefault(row["mission_id"], set()).add(row["step_id"])
        return [self._mission_record(row, steps.get(row["id"], ()), sessions.get(row["id"], ()),
                                     artifacts.get(row["id"], ()), checkpoints.get(row["id"], ()),
                                     cost_totals.get(row["id"], 0), checkpoint_steps.get(row["id"], ()))
                for row in rows]

    @staticmethod
    def _mission_record(row, steps, sessions, artifacts, checkpoints, cost_total, checkpoint_steps):
        """Assemble one mission detail record from its child rows.

        Shared by ``get`` and ``list`` so both return exactly the same shape,
        whatever the caller prefetched: mission columns, then steps (with
        parsed dependencies and milestone progress), sessions, artifacts,
        checkpoints, cost_total and progress. Child rows are copied, so a
        caller-provided prefetch buffer is never mutated.
        """
        steps = [dict(s) for s in steps]
        for step in steps:
            step["depends_on"] = json.loads(step["depends_on"])
            # Milestone-based step progress from observable events only: a
            # RUNNING step is 1% once started and 90% once its Git checkpoint
            # is committed — never a timer-invented value.
            step["progress"] = 100 if step["status"] == "COMPLETED" else \
                (90 if step["status"] == "RUNNING" and step["id"] in checkpoint_steps
                 else 1 if step["status"] in {"RUNNING", "FAILED"} else 0)
        done = sum(step["status"] == "COMPLETED" for step in steps)
        active = sum(step["status"] == "RUNNING" for step in steps)
        return {**dict(row), "steps": steps, "sessions": [dict(s) for s in sessions],
                "artifacts": [dict(a) for a in artifacts],
                "checkpoints": [dict(c) for c in checkpoints],
                "cost_total": cost_total,
                "progress": mission_progress(done, len(steps), active)}

    def get(self, mission_id, owner_id=None):
        with self.connect() as db:
            if owner_id is None:
                row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM missions WHERE id=? AND owner_id=?",
                                 (mission_id, owner_id)).fetchone()
            if row is None:
                return None
            steps = db.execute(
                "SELECT id, label, position, status, depends_on, model_id FROM steps"
                " WHERE mission_id=? ORDER BY position", (mission_id,)).fetchall()
            sessions = db.execute("SELECT * FROM sessions WHERE mission_id=?", (mission_id,)).fetchall()
            artifacts = db.execute("SELECT * FROM artifacts WHERE mission_id=?", (mission_id,)).fetchall()
            checkpoints = db.execute(
                "SELECT * FROM checkpoints WHERE mission_id=? ORDER BY created_at", (mission_id,)).fetchall()
            cost_total = db.execute("SELECT COALESCE(SUM(amount), 0) FROM costs WHERE mission_id=?",
                                    (mission_id,)).fetchone()[0]
            checkpoint_steps = {row[0] for row in db.execute(
                "SELECT DISTINCT step_id FROM events WHERE mission_id=? AND type='GIT_CHECKPOINT'"
                " AND step_id IS NOT NULL", (mission_id,))}
        return self._mission_record(row, steps, sessions, artifacts, checkpoints,
                                    cost_total, checkpoint_steps)

    def _event(self, db, mission_id, step_id, session_id, agent_id, event_type, payload):
        event_id = uuid4().hex
        # 协助模式审计：during an assisted visit every event written for this
        # mission names the actual operator. Copied, never mutated: callers may
        # reuse their payload dicts.
        assist = assist_context.get()
        if assist:
            payload = {**payload, "assist_by": assist}
        db.execute("INSERT INTO events (id, mission_id, step_id, session_id, agent_id, type, payload, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (event_id, mission_id, step_id, session_id, agent_id, event_type,
                    json.dumps(payload, ensure_ascii=False), now()))
        return event_id

    def append(self, mission_id, event_type, payload, step_id=None, session_id=None, agent_id=None):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                raise KeyError(mission_id)
            if step_id and db.execute("SELECT 1 FROM steps WHERE mission_id=? AND id=?", (mission_id, step_id)).fetchone() is None:
                raise ValueError("Step belongs to another mission")
            if session_id and db.execute("SELECT 1 FROM sessions WHERE mission_id=? AND id=?", (mission_id, session_id)).fetchone() is None:
                raise ValueError("Session belongs to another mission")
            event_id = self._event(db, mission_id, step_id, session_id, agent_id, event_type, payload)
        return event_id

    def events(self, mission_id, after=0):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE mission_id=? AND sequence>? ORDER BY sequence", (mission_id, after))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def events_for_owner(self, owner_id, after=0):
        """The Event Bus spine: every event of every mission one owner owns,
        in one global sequence — multi-client replay and live subscription
        both hang off this single durable query."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT e.* FROM events e JOIN missions m ON e.mission_id=m.id "
                "WHERE m.owner_id=? AND e.sequence>? ORDER BY e.sequence",
                (owner_id, after))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def step_model_id(self, mission_id, step_id):
        """The per-step model binding, or None when the step uses the mission model."""
        with self.connect() as db:
            row = db.execute("SELECT model_id FROM steps WHERE mission_id=? AND id=?",
                             (mission_id, step_id)).fetchone()
        return row["model_id"] if row else None

    def step_status(self, mission_id, step_id, status):
        if status not in {"WAITING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}:
            raise ValueError("Invalid step status")
        with self.connect() as db:
            row = db.execute("SELECT status FROM steps WHERE mission_id=? AND id=?", (mission_id, step_id)).fetchone()
            if row is None:
                raise KeyError(step_id)
            allowed = {"WAITING": {"RUNNING", "BLOCKED", "CANCELLED"},
                       "RUNNING": {"COMPLETED", "FAILED"}, "FAILED": {"RUNNING"},
                       "BLOCKED": {"WAITING"}, "CANCELLED": {"WAITING"}, "COMPLETED": set()}
            if status == "RUNNING":
                dependencies = json.loads(db.execute(
                    "SELECT depends_on FROM steps WHERE mission_id=? AND id=?",
                    (mission_id, step_id)).fetchone()[0])
                if dependencies:
                    marks = ",".join("?" for _ in dependencies)
                    ready = db.execute(
                        f"SELECT COUNT(*) FROM steps WHERE mission_id=? AND id IN ({marks}) AND status='COMPLETED'",
                        (mission_id, *dependencies)).fetchone()[0]
                    if ready != len(dependencies):
                        raise ValueError("Step dependencies are not completed")
                if row["status"] == "FAILED":
                    self._unblock_descendants(db, mission_id, step_id)
            if status not in allowed[row["status"]]:
                raise ValueError(f"Invalid transition: {row['status']} -> {status}")
            db.execute("UPDATE steps SET status=? WHERE mission_id=? AND id=?", (status, mission_id, step_id))
            states = [r[0] for r in db.execute("SELECT status FROM steps WHERE mission_id=?", (mission_id,))]
            mission_status = "FAILED" if any(s in {"FAILED", "BLOCKED", "CANCELLED"} for s in states) else "COMPLETED" if all(s == "COMPLETED" for s in states) else "RUNNING"
            db.execute("UPDATE missions SET status=? WHERE id=?", (mission_status, mission_id))
            self._event(db, mission_id, step_id, None, None, f"STEP_{status}", {"status": status})
            if status in {"FAILED", "CANCELLED"}:
                self._refresh_blocked_jobs(db, mission_id)
        return self.get(mission_id)

    def _unblock_descendants(self, db, mission_id, step_id):
        """Re-open dependent steps when their failed prerequisite is retried."""
        rows = db.execute("SELECT id, status, depends_on FROM steps WHERE mission_id=?",
                          (mission_id,)).fetchall()
        dependencies = {row["id"]: json.loads(row["depends_on"]) for row in rows}
        blocked = {row["id"] for row in rows if row["status"] == "BLOCKED"}
        recovered = set()
        frontier = {step_id}
        while frontier:
            next_frontier = {candidate for candidate, deps in dependencies.items()
                             if candidate in blocked and candidate not in recovered
                             and any(dep in frontier for dep in deps)}
            if not next_frontier:
                break
            recovered.update(next_frontier)
            frontier = next_frontier
        for descendant in recovered:
            db.execute("UPDATE steps SET status='WAITING' WHERE mission_id=? AND id=?",
                       (mission_id, descendant))
            db.execute("UPDATE jobs SET status='QUEUED', error=NULL, updated_at=? "
                       "WHERE mission_id=? AND step_id=? AND status='BLOCKED'",
                       (now(), mission_id, descendant))
            self._event(db, mission_id, descendant, None, None, "STEP_UNBLOCKED",
                        {"status": "WAITING", "dependency_retry": step_id})

    def bind(self, mission_id, agent_id=None, model_id=None, skill_id=None, runtime_id=None,
             changed=None):
        """Bind any subset of the four capability registries to one mission.

        `changed` restricts which fields are written so agent, model, skill and
        runtime can be bound independently without clearing the others.
        """
        values = {"agent_id": agent_id, "model_id": model_id,
                  "skill_id": skill_id, "runtime_id": runtime_id}
        if changed is None:
            changed = set(values)
        updates = {key: values[key] for key in values if key in changed}
        with self.connect() as db:
            if db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                raise KeyError(mission_id)
            if updates:
                assignments = ", ".join(f"{key}=?" for key in updates)
                db.execute(f"UPDATE missions SET {assignments} WHERE id=?",
                           (*updates.values(), mission_id))
            row = db.execute("SELECT agent_id, model_id, skill_id, runtime_id FROM missions WHERE id=?",
                             (mission_id,)).fetchone()
            self._event(db, mission_id, None, None, row["agent_id"], "BINDING_CHANGED",
                        {"agent_id": row["agent_id"], "model_id": row["model_id"],
                         "skill_id": row["skill_id"], "runtime_id": row["runtime_id"],
                         "changed": sorted(updates)})
        return self.get(mission_id)

    def set_prompt(self, mission_id, professional_prompt):
        """Store the derived professional prompt. The original input is never modified.

        Each change records an audit event that keeps the exact original words
        next to the professional prompt, so the pairing can be reviewed later.
        """
        with self.connect() as db:
            row = db.execute("SELECT original_input FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise KeyError(mission_id)
            db.execute("UPDATE missions SET professional_prompt=? WHERE id=?", (professional_prompt, mission_id))
            self._event(db, mission_id, None, None, None, "PROMPT_UPDATED",
                        {"has_prompt": bool(professional_prompt),
                         "professional_prompt": professional_prompt,
                         "original_input": row["original_input"]})
        return self.get(mission_id)

    def start_session(self, mission_id, agent_id, model_id):
        session_id = uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (session_id, mission_id, agent_id, model_id, "PREPARED"))
            self._event(db, mission_id, None, session_id, agent_id, "SESSION_PREPARED", {"model_id": model_id})
        return session_id

    def session_status(self, mission_id, session_id, status):
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM sessions WHERE mission_id=? AND id=?", (mission_id, session_id)).fetchone()
            if row is None:
                raise KeyError(session_id)
            db.execute("UPDATE sessions SET status=? WHERE mission_id=? AND id=?", (status, mission_id, session_id))
            self._event(db, mission_id, None, session_id, None, f"SESSION_{status}", {"status": status})

    def add_artifact(self, mission_id, name, path):
        if (self.root / "workspaces").resolve() not in Path(path).resolve().parents:
            raise ValueError("Artifact must live inside the mission workspace tree")
        artifact_id = uuid4().hex
        with self.connect() as db:
            if db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                raise KeyError(mission_id)
            db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)",
                       (artifact_id, mission_id, name, str(path), now()))
            self._event(db, mission_id, None, None, None, "ARTIFACT_CREATED",
                        {"name": name, "artifact_id": artifact_id})
        return artifact_id

    def recent_artifacts(self, owner_id="admin", limit=100):
        """Newest artifacts across the owner's missions (the artifacts center)."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT a.id, a.mission_id, a.name, a.path, a.created_at, m.title,"
                " m.owner_id FROM artifacts a JOIN missions m ON m.id=a.mission_id"
                " WHERE m.owner_id=? ORDER BY a.created_at DESC, a.id LIMIT ?",
                (owner_id, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def get_artifact(self, mission_id, artifact_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE mission_id=? AND id=?",
                             (mission_id, artifact_id)).fetchone()
        return dict(row) if row else None

    def add_checkpoint(self, mission_id, commit_hash, message, stat=""):
        checkpoint_id = uuid4().hex
        with self.connect() as db:
            if db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                raise KeyError(mission_id)
            db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
                       (checkpoint_id, mission_id, commit_hash, message, now()))
            self._event(db, mission_id, None, None, None, "GIT_CHECKPOINT",
                        {"commit_hash": commit_hash, "message": message, "stat": stat})
        return checkpoint_id

    def add_cost(self, mission_id, provider, model_id, amount, tokens=None, currency="USD",
                 input_tokens=None, output_tokens=None):
        """Append one cost row; a side that was never reported stays NULL.

        ``input_tokens``/``output_tokens`` carry a real split only when the
        caller observed it (gateway usage); callers that hold a combined count
        alone (the OpenCode adapter) omit them, and readers must treat NULL as
        "not reported" rather than zero.
        """
        amount = float(amount)
        if not math.isfinite(amount) or amount < 0:
            # Agent-reported cost is untrusted input: a negative amount would
            # offset the month's recorded spend and defeat the budget gate, and
            # a non-finite NaN/Infinity would do worse — inf keeps the gate
            # closed forever and the JSON encoder rejects the whole page, NaN
            # violates the NOT NULL ledger column. Clamp any of them to zero;
            # the token count stays for the operator to inspect.
            amount = 0.0
        if amount <= 0 and not (tokens or input_tokens or output_tokens):
            return None
        cost_id = uuid4().hex
        with self.connect() as db:
            if db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                raise KeyError(mission_id)
            # Explicit column names: the table gained columns after the first
            # deployed databases, so positional VALUES would silently misalign.
            db.execute(
                "INSERT INTO costs (id, mission_id, provider, model_id, amount, tokens,"
                " currency, created_at, input_tokens, output_tokens)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cost_id, mission_id, provider, model_id, float(amount),
                 tokens, currency, now(), input_tokens, output_tokens))
            self._event(db, mission_id, None, None, None, "COST_RECORDED",
                        {"provider": provider, "model_id": model_id, "amount": float(amount),
                         "tokens": tokens, "input_tokens": input_tokens,
                         "output_tokens": output_tokens, "currency": currency})
        return cost_id

    def costs(self, mission_id):
        """Every cost row of one mission, oldest first (read-only).

        Evidence-chain read for facai.evidence: the per-mission detail carries
        only ``cost_total``, while the proof bundle must show each row. No
        writes here — the ledger's only writer stays ``add_cost``.
        """
        with self.connect() as db:
            rows = db.execute("SELECT * FROM costs WHERE mission_id=?"
                              " ORDER BY created_at, id", (mission_id,)).fetchall()
        return [dict(row) for row in rows]

    # --- durable scheduler jobs -------------------------------------------------

    def enqueue_job(self, mission_id, prompt, step_id=None, model_ref=None,
                    priority=0, max_attempts=1, timeout=300, dispatch=None):
        job_id = uuid4().hex
        with self.connect() as db:
            if db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                raise KeyError(mission_id)
            if step_id and db.execute("SELECT 1 FROM steps WHERE mission_id=? AND id=?",
                                      (mission_id, step_id)).fetchone() is None:
                raise ValueError("Step belongs to another mission")
            db.execute(
                "INSERT INTO jobs (id, mission_id, step_id, prompt, model_ref, status, priority,"
                " attempts, max_attempts, timeout, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, 0, ?, ?, ?, ?)",
                (job_id, mission_id, step_id, prompt, model_ref, int(priority),
                 int(max_attempts), int(timeout), now(), now()))
            self._event(db, mission_id, step_id, None, None, "JOB_QUEUED",
                        {"job_id": job_id, "priority": int(priority)})
            if dispatch is not None:
                self._event(db, mission_id, step_id, None, None, "DISPATCH_QUEUED",
                            {"job_id": job_id, **dispatch})
        return self.get_job(job_id)

    @staticmethod
    def _job_row(db, job_id, owner_id=None):
        """Fetch one job, optionally constrained to its owning tenant.

        Jobs are looked up by a globally unique id, so a bare id lookup would let
        one user read or mutate another user's job by guessing its id. When an
        owner is supplied the row must belong to a mission owned by that user.
        """
        if owner_id is None:
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return db.execute(
            "SELECT j.* FROM jobs AS j JOIN missions AS m ON m.id = j.mission_id"
            " WHERE j.id=? AND m.owner_id=?", (job_id, owner_id)).fetchone()

    def get_job(self, job_id, owner_id=None):
        with self.connect() as db:
            row = self._job_row(db, job_id, owner_id)
        return dict(row) if row else None

    def jobs(self, mission_id=None, owner_id=None):
        with self.connect() as db:
            if mission_id:
                if owner_id is None:
                    rows = db.execute(
                        "SELECT * FROM jobs WHERE mission_id=? ORDER BY priority DESC, created_at",
                        (mission_id,))
                else:
                    rows = db.execute(
                        "SELECT j.* FROM jobs AS j JOIN missions AS m ON m.id = j.mission_id"
                        " WHERE j.mission_id=? AND m.owner_id=?"
                        " ORDER BY j.priority DESC, j.created_at", (mission_id, owner_id))
            else:
                rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC")
            return [dict(row) for row in rows]

    def count_running_jobs(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE status='RUNNING'").fetchone()[0]

    def update_job_prompt(self, job_id, prompt, owner_id=None):
        """Rewrite one QUEUED job's prompt, keeping its identity.

        The conversion console's feedback resume uses this: the user's answer
        must ride the stage prompt, and keeping the same job row preserves
        dispatch attribution, priority and lease bookkeeping — superseding a
        not-yet-run stage never forks a second job for the same step.
        """
        with self.connect() as db:
            row = self._job_row(db, job_id, owner_id)
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "QUEUED":
                raise ValueError(f"Only QUEUED jobs can be reprompted: {row['status']}")
            db.execute("UPDATE jobs SET prompt=?, updated_at=? WHERE id=?",
                       (prompt, now(), job_id))
            self._event(db, row["mission_id"], row["step_id"], None, None,
                        "JOB_REPROMPTED", {"job_id": job_id})
        return self.get_job(job_id, owner_id=owner_id)

    def _refresh_blocked_jobs(self, db, mission_id):
        """Block queued work whose prerequisites have irrecoverably failed."""
        changed = True
        while changed:
            changed = False
            steps = {row["id"]: {**dict(row), "depends_on": json.loads(row["depends_on"])}
                     for row in db.execute("SELECT id, status, depends_on FROM steps WHERE mission_id=?",
                                           (mission_id,))}
            for step_id, step in steps.items():
                if step["status"] != "WAITING":
                    continue
                failed_dependency = next((steps[dep] for dep in step["depends_on"]
                                          if steps[dep]["status"] in {"FAILED", "CANCELLED", "BLOCKED"}), None)
                if failed_dependency is None:
                    continue
                if failed_dependency["status"] == "FAILED":
                    recoverable = db.execute(
                        "SELECT 1 FROM jobs WHERE mission_id=? AND step_id=? AND ("
                        "status IN ('QUEUED','RUNNING','PAUSED') OR "
                        "(status='FAILED' AND attempts < max_attempts)) LIMIT 1",
                        (mission_id, failed_dependency["id"])).fetchone()
                    if recoverable:
                        continue
                db.execute("UPDATE steps SET status='BLOCKED' WHERE mission_id=? AND id=?",
                           (mission_id, step_id))
                self._event(db, mission_id, step_id, None, None, "STEP_BLOCKED",
                            {"status": "BLOCKED", "dependency": failed_dependency["id"]})
                steps[step_id]["status"] = "BLOCKED"
                changed = True
            blocked_jobs = db.execute(
                "SELECT * FROM jobs WHERE mission_id=? AND status='QUEUED' AND step_id IN "
                "(SELECT id FROM steps WHERE mission_id=? AND status='BLOCKED')",
                (mission_id, mission_id)).fetchall()
            for job in blocked_jobs:
                db.execute("UPDATE jobs SET status='BLOCKED', error='dependency failed or cancelled', updated_at=? WHERE id=?",
                           (now(), job["id"]))
                self._event(db, mission_id, job["step_id"], None, None, "JOB_BLOCKED",
                            {"job_id": job["id"], "reason": "dependency failed or cancelled"})
                changed = True
        states = [row[0] for row in db.execute("SELECT status FROM steps WHERE mission_id=?", (mission_id,))]
        current_status = db.execute("SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()[0]
        mission_status = ("FAILED" if any(s in {"FAILED", "BLOCKED", "CANCELLED"} for s in states)
                          else "COMPLETED" if states and all(s == "COMPLETED" for s in states)
                          else "RUNNING" if "RUNNING" in states else current_status)
        db.execute("UPDATE missions SET status=? WHERE id=?", (mission_status, mission_id))

    def claim_job(self, worker_id, lease_seconds, parallel_mission_jobs=1):
        """Claim the next runnable job under a lease, atomically.

        ``lease_seconds`` is a floor for short jobs, never the whole story: the
        lease granted here always covers serial-lock queueing plus the job's
        own ``timeout`` (at least ``JOB_LEASE_FLOOR_SECONDS``, and at least
        twice the timeout plus ``JOB_LEASE_MARGIN_SECONDS``), so a job that is
        still executing -- or still waiting for the CLI lock -- is never
        requeued by ``requeue_expired`` (which runs every second) and never
        re-run concurrently by a second worker. A non-positive value is a
        caller's explicit request for an already-expired lease -- the way
        recovery paths are exercised with a simulated dead worker -- and is
        taken verbatim.

        By default one mission allows a single RUNNING job. With
        ``parallel_mission_jobs`` > 1 (bounded by the scheduler), dependency-ready
        jobs of DISTINCT steps may run concurrently in isolated worktrees; a
        mission-level job (``step_id IS NULL``) and any second claim of the SAME
        step still serialize the whole mission. ``BEGIN IMMEDIATE`` keeps the
        decision atomic across threads and processes.
        """
        limit = max(1, int(parallel_mission_jobs))
        with self.connect() as db:
            # Serialize claim decisions across scheduler threads/processes, and
            # never run two writers in the same mission workspace at once.
            db.execute("BEGIN IMMEDIATE")
            missions = [row[0] for row in db.execute(
                "SELECT DISTINCT mission_id FROM jobs WHERE status='QUEUED'")]
            for mission_id in missions:
                self._refresh_blocked_jobs(db, mission_id)
            candidates = db.execute(
                "SELECT j.* FROM jobs AS j WHERE j.status='QUEUED'"
                " AND (j.not_before IS NULL OR j.not_before <= ?) AND NOT EXISTS ("
                "SELECT 1 FROM jobs AS active WHERE active.mission_id=j.mission_id "
                "AND active.status='RUNNING' AND (active.step_id IS NULL OR j.step_id IS NULL "
                "OR active.step_id=j.step_id OR (SELECT COUNT(*) FROM jobs AS running "
                "WHERE running.mission_id=j.mission_id AND running.status='RUNNING') >= ?))"
                " ORDER BY j.priority DESC, j.created_at", (now(), limit)).fetchall()
            row = None
            for candidate in candidates:
                if candidate["step_id"] is None:
                    row = candidate
                    break
                step = db.execute("SELECT depends_on FROM steps WHERE mission_id=? AND id=?",
                                  (candidate["mission_id"], candidate["step_id"])).fetchone()
                dependencies = json.loads(step["depends_on"])
                if not dependencies:
                    row = candidate
                    break
                marks = ",".join("?" for _ in dependencies)
                ready = db.execute(
                    f"SELECT COUNT(*) FROM steps WHERE mission_id=? AND id IN ({marks}) AND status='COMPLETED'",
                    (candidate["mission_id"], *dependencies)).fetchone()[0]
                if ready == len(dependencies):
                    row = candidate
                    break
            if row is None:
                return None
            requested = int(lease_seconds)
            # Widen the lease beyond the job's own execution budget: a parallel
            # branch can spend up to its timeout waiting behind the OpenCode
            # serial lock before running for up to another timeout, so the
            # lease must cover roughly twice the timeout plus margin (see
            # JOB_LEASE_FLOOR_SECONDS); a bare caller-requested lease would
            # expire mid-run for any job whose timeout exceeds it.
            lease = requested if requested <= 0 else max(
                requested, JOB_LEASE_FLOOR_SECONDS,
                int(row["timeout"] or 0) * 2 + JOB_LEASE_MARGIN_SECONDS)
            expires = (datetime.now(timezone.utc) + timedelta(seconds=lease)).isoformat()
            cursor = db.execute(
                "UPDATE jobs SET status='RUNNING', lease_owner=?, lease_expires_at=?,"
                " attempts=attempts+1, updated_at=? WHERE id=? AND status='QUEUED'",
                (worker_id, expires, now(), row["id"]))
            if cursor.rowcount != 1:
                return None
            self._event(db, row["mission_id"], row["step_id"], None, None, "JOB_STARTED",
                        {"job_id": row["id"], "worker": worker_id, "attempt": row["attempts"] + 1})
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return dict(claimed)

    def touch_job_lease(self, job_id, timeout):
        """Extend a RUNNING job's lease once its worker actually holds execution.

        Called right before the agent run: a job can spend part of its claim
        lease queued (retry backoff, a busy batch, the OpenCode serial lock),
        so the worker re-arms the lease to ``now + timeout +
        JOB_LEASE_MARGIN_SECONDS`` here. The extension is monotonic -- a claim
        lease that already reaches further (long timeout, serial queueing
        covered at claim time) is never shortened -- and only a job still in
        RUNNING (i.e. not yet requeued by recovery) is touched.

        Returns True when the lease was extended, False otherwise.
        """
        expires = (datetime.now(timezone.utc)
                   + timedelta(seconds=int(timeout or 0) + JOB_LEASE_MARGIN_SECONDS)).isoformat()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET lease_expires_at=MAX(lease_expires_at, ?), updated_at=?"
                " WHERE id=? AND status='RUNNING'", (expires, now(), job_id))
            return cursor.rowcount == 1

    def finish_job(self, job_id, status, error=None):
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("Job can only finish as COMPLETED or FAILED")
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            db.execute("UPDATE jobs SET status=?, error=?, lease_owner=NULL, lease_expires_at=NULL,"
                       " updated_at=? WHERE id=?", (status, error, now(), job_id))
            if status == "FAILED" and row["step_id"]:
                step = db.execute("SELECT status FROM steps WHERE mission_id=? AND id=?",
                                  (row["mission_id"], row["step_id"])).fetchone()
                if step and step["status"] in {"WAITING", "RUNNING"}:
                    db.execute("UPDATE steps SET status='FAILED' WHERE mission_id=? AND id=?",
                               (row["mission_id"], row["step_id"]))
                    self._event(db, row["mission_id"], row["step_id"], None, None, "STEP_FAILED",
                                {"status": "FAILED", "error": error})
            self._event(db, row["mission_id"], row["step_id"], None, None,
                        "JOB_COMPLETED" if status == "COMPLETED" else "JOB_FAILED",
                        {"job_id": job_id, "error": error})
            if status == "FAILED" and row["attempts"] >= row["max_attempts"]:
                self._refresh_blocked_jobs(db, row["mission_id"])
        return self.get_job(job_id)

    def _set_job_status(self, job_id, allowed, new_status, event_type, error=None, owner_id=None):
        with self.connect() as db:
            row = self._job_row(db, job_id, owner_id)
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in allowed:
                raise ValueError(f"Invalid job transition: {row['status']} -> {new_status}")
            db.execute("UPDATE jobs SET status=?, error=?, lease_owner=NULL, lease_expires_at=NULL,"
                       " updated_at=? WHERE id=?", (new_status, error, now(), job_id))
            self._event(db, row["mission_id"], row["step_id"], None, None, event_type, {"job_id": job_id})
        return self.get_job(job_id, owner_id=owner_id)

    def pause_job(self, job_id, owner_id=None):
        return self._set_job_status(job_id, {"QUEUED"}, "PAUSED", "JOB_PAUSED", owner_id=owner_id)

    def resume_job(self, job_id, owner_id=None):
        return self._set_job_status(job_id, {"PAUSED"}, "QUEUED", "JOB_RESUMED", owner_id=owner_id)

    def cancel_job(self, job_id, owner_id=None):
        with self.connect() as db:
            row = self._job_row(db, job_id, owner_id)
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"QUEUED", "PAUSED"}:
                raise ValueError(f"Invalid job transition: {row['status']} -> CANCELLED")
            db.execute("UPDATE jobs SET status='CANCELLED', lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                       (now(), job_id))
            if row["step_id"]:
                step = db.execute("SELECT status FROM steps WHERE mission_id=? AND id=?",
                                  (row["mission_id"], row["step_id"])).fetchone()
                if step and step["status"] == "WAITING":
                    db.execute("UPDATE steps SET status='CANCELLED' WHERE mission_id=? AND id=?",
                               (row["mission_id"], row["step_id"]))
                    self._event(db, row["mission_id"], row["step_id"], None, None, "STEP_CANCELLED",
                                {"status": "CANCELLED"})
            self._event(db, row["mission_id"], row["step_id"], None, None, "JOB_CANCELLED", {"job_id": job_id})
            self._refresh_blocked_jobs(db, row["mission_id"])
        return self.get_job(job_id, owner_id=owner_id)

    def retry_job(self, job_id, owner_id=None):
        with self.connect() as db:
            row = self._job_row(db, job_id, owner_id)
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"FAILED", "CANCELLED"}:
                raise ValueError("Only failed or cancelled jobs can be retried")
            db.execute("UPDATE jobs SET status='QUEUED', attempts=0, error=NULL, lease_owner=NULL,"
                       " lease_expires_at=NULL, updated_at=? WHERE id=?", (now(), job_id))
            self._event(db, row["mission_id"], row["step_id"], None, None, "JOB_RETRIED", {"job_id": job_id})
            if row["step_id"]:
                db.execute("UPDATE steps SET status='WAITING' WHERE mission_id=? AND id=? AND status IN ('FAILED','CANCELLED')",
                           (row["mission_id"], row["step_id"]))
            # Downstream work may become ready again after this retry; retain the
            # dependency gate until its prerequisite actually completes.
            blocked_jobs = db.execute("SELECT id, step_id FROM jobs WHERE mission_id=? AND status='BLOCKED'",
                                      (row["mission_id"],)).fetchall()
            for blocked in blocked_jobs:
                db.execute("UPDATE jobs SET status='QUEUED', error=NULL, updated_at=? WHERE id=?",
                           (now(), blocked["id"]))
                if blocked["step_id"]:
                    db.execute("UPDATE steps SET status='WAITING' WHERE mission_id=? AND id=? AND status='BLOCKED'",
                               (row["mission_id"], blocked["step_id"]))
            db.execute("UPDATE missions SET status='RUNNING' WHERE id=?", (row["mission_id"],))
        return self.get_job(job_id, owner_id=owner_id)

    def requeue_job_backoff(self, job_id, delay_seconds, reason):
        """Requeue a just-failed job for automatic retry after a backoff.

        The self-healing path for TRANSIENT failures (upstream timeouts, rate
        limits, provider resource hiccups, transport errors): instead of
        finishing the job FAILED, it returns to QUEUED with a not-before stamp
        so the next claim waits out the exponential backoff. The attempts
        counter is NOT reset -- the remaining budget is what bounds retries.
        The step returns to WAITING so it can run again honestly.
        """
        delay = max(0, int(delay_seconds))
        not_before = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"RUNNING", "FAILED"}:
                raise ValueError(f"Cannot requeue from status {row['status']}")
            db.execute(
                "UPDATE jobs SET status='QUEUED', error=?, not_before=?, lease_owner=NULL,"
                " lease_expires_at=NULL, updated_at=? WHERE id=?",
                (reason, not_before, now(), job_id))
            if row["step_id"]:
                db.execute("UPDATE steps SET status='WAITING' WHERE mission_id=? AND id=?"
                           " AND status IN ('RUNNING','FAILED')", (row["mission_id"], row["step_id"]))
            self._event(db, row["mission_id"], row["step_id"], None, None, "JOB_REQUEUED",
                        {"job_id": job_id, "reason": reason, "retry_in_seconds": delay})
        return self.get_job(job_id)

    def reroute_job(self, job_id, model_ref, reason):
        """Requeue a just-failed job onto a different backend (P0-14 Router).

        The §15/§16 path for backend states (PROVIDER_REFUSAL,
        BACKEND_UNAVAILABLE, TIMEOUT): the same job returns to QUEUED with
        its ``model_ref`` repointed at the replacement backend, so the next
        claim executes there. The attempts counter is NOT touched -- a
        reroute is a change of execution plane, not a retry, and must not
        burn the transient-retry budget. ``reason`` is stored verbatim in
        the job error field; the router prefixes its machine marker so the
        next claim resolves the replacement model.
        """
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"RUNNING", "FAILED"}:
                raise ValueError(f"Cannot reroute from status {row['status']}")
            db.execute(
                "UPDATE jobs SET model_ref=?, status='QUEUED', error=?, not_before=NULL,"
                " lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (model_ref, reason, now(), job_id))
            if row["step_id"]:
                db.execute("UPDATE steps SET status='WAITING' WHERE mission_id=? AND id=?"
                           " AND status IN ('RUNNING','FAILED')", (row["mission_id"], row["step_id"]))
            self._event(db, row["mission_id"], row["step_id"], None, None, "JOB_REQUEUED",
                        {"job_id": job_id, "reason": reason,
                         "rerouted_to_model_ref": model_ref})
        return self.get_job(job_id)

    # --- admin authentication sessions -----------------------------------------

    def create_auth_session(self, token_hash, csrf_token, username, ttl_seconds):
        expires = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO auth_sessions (token_hash, csrf_token, username, created_at,"
                       " expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (token_hash, csrf_token, username, now(), expires, now()))
        return expires

    def get_auth_session(self, token_hash):
        with self.connect() as db:
            row = db.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def touch_auth_session(self, token_hash):
        with self.connect() as db:
            db.execute("UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?", (now(), token_hash))

    def delete_auth_session(self, token_hash):
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))

    def delete_user_auth_sessions(self, username):
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE username=?", (username,))

    def prune_auth_sessions(self):
        with self.connect() as db:
            cursor = db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now(),))
            return cursor.rowcount

    # --- visible assist sessions (协助模式) --------------------------------------

    def create_assist_session(self, admin_username, owner_username, reason, ttl_seconds):
        """Open one time-boxed assist session; the row itself is the audit record."""
        created = now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))).isoformat()
        session_id = uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO assist_sessions (id, admin_username, owner_username,"
                       " reason, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (session_id, admin_username, owner_username, reason, expires, created))
        return {"id": session_id, "admin_username": admin_username,
                "owner_username": owner_username, "reason": reason,
                "expires_at": expires, "created_at": created, "ended_at": None}

    def active_assist_for_owner(self, owner_username):
        """The live assist session on this account, or None.

        到期视为失效: the window is judged against UTC now at read time, so an
        expired row can never admit the require_mission bypass nor surface on
        /auth/session, even before expire_assist_sessions housekeeping runs.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM assist_sessions WHERE owner_username=? AND ended_at IS NULL"
                " AND expires_at > ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (owner_username, now())).fetchone()
        return dict(row) if row else None

    def end_assist_session(self, session_id):
        """Close a session early; ended_at keeps the audit trail queryable."""
        with self.connect() as db:
            db.execute("UPDATE assist_sessions SET ended_at=? WHERE id=? AND ended_at IS NULL",
                       (now(), session_id))

    def expire_assist_sessions(self):
        """Housekeeping: drop rows whose window has passed (they were already dead)."""
        with self.connect() as db:
            cursor = db.execute("DELETE FROM assist_sessions WHERE expires_at <= ?", (now(),))
            return cursor.rowcount

    def record_login_failure(self, client_ip, window_seconds):
        """Count a failed login inside a sliding window; reset the count when stale."""
        timestamp = now()
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        with self.connect() as db:
            # BEGIN IMMEDIATE serializes concurrent first failures: the old
            # check-then-insert lost updates under races and could even raise
            # UNIQUE on the insert, surfacing as a 500 on the login path.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM login_attempts WHERE client_ip=?", (client_ip,)).fetchone()
            if row is None:
                db.execute("INSERT INTO login_attempts VALUES (?, 1, ?, ?)", (client_ip, timestamp, timestamp))
                return 1
            stale = row["last_at"] < stale_before
            failures = 1 if stale else row["failures"] + 1
            first = timestamp if stale else row["first_at"]
            db.execute("UPDATE login_attempts SET failures=?, first_at=?, last_at=? WHERE client_ip=?",
                       (failures, first, timestamp, client_ip))
            return failures

    def login_failures(self, client_ip):
        with self.connect() as db:
            row = db.execute("SELECT failures FROM login_attempts WHERE client_ip=?", (client_ip,)).fetchone()
        return row["failures"] if row else 0

    def clear_login_failures(self, client_ip):
        with self.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE client_ip=?", (client_ip,))

    # --- self-registered platform accounts --------------------------------------

    def create_platform_user(self, username, password_hash):
        """Insert one registered account; a duplicate username is an error."""
        with self.connect() as db:
            try:
                db.execute("INSERT INTO platform_users (username, password_hash, created_at)"
                           " VALUES (?, ?, ?)", (username, password_hash, now()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("username already registered") from exc

    def platform_users(self):
        """Registered accounts as username -> password hash (never returned to clients)."""
        with self.connect() as db:
            rows = db.execute("SELECT username, password_hash FROM platform_users").fetchall()
        return {row["username"]: row["password_hash"] for row in rows}

    def update_platform_user_password(self, username, password_hash):
        """Rotate one registered account's password hash; False if it is absent."""
        with self.connect() as db:
            cur = db.execute("UPDATE platform_users SET password_hash=? WHERE username=?",
                             (password_hash, username))
            db.commit()
            return cur.rowcount > 0

    def set_platform_user_password(self, username, password_hash, must_change):
        """Set a registered account's password and its force-rotate flag."""
        with self.connect() as db:
            cur = db.execute(
                "UPDATE platform_users SET password_hash=?, must_change_password=? WHERE username=?",
                (password_hash, 1 if must_change else 0, username))
            db.commit()
            return cur.rowcount > 0

    def platform_user_must_change(self, username):
        with self.connect() as db:
            row = db.execute("SELECT must_change_password FROM platform_users WHERE username=?",
                             (username,)).fetchone()
        return bool(row and row[0])

    def count_platform_users(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM platform_users").fetchone()[0]

    def platform_user_exists(self, username):
        """Whether ``username`` is a self-registered platform account.

        The env-backed operator account has no row here by design, so admin
        member actions scoped to ``platform_users`` structurally cannot touch
        it (no operator self-freeze through the member console).
        """
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM platform_users WHERE username=?",
                             (username,)).fetchone()
        return row is not None

    def members_overview(self, year_month=None):
        """Admin-console roster: one read-only row per registered member.

        Joins, in a single SQL pass (no per-member queries): the explicit
        ``user_budgets`` row (``monthly_limit_usd`` is None when the account
        never had one — the balance-pool "未充值" state), live (never-revoked)
        api-key count, month-to-date USD spend across the member's OWN missions
        (same ``costs`` join discipline as :meth:`user_spend_month`: only
        ``currency='USD'`` sums; ISO-prefix month match), and bound-channel
        counts across both binding families — personal bots
        (``channel_bindings``) plus hosted channels (``channel_host_bindings``),
        where terminal UNBOUND rows no longer count as a bound channel.
        Ordered newest member first. Password hashes, bot tokens and key
        digests are never selected.
        """
        if year_month is None:
            year_month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.connect() as db:
            rows = db.execute(
                "SELECT pu.username, pu.created_at, pu.must_change_password,"
                " b.monthly_limit_usd,"
                " (SELECT COUNT(*) FROM api_keys k"
                "   WHERE k.owner_id=pu.username AND k.revoked_at IS NULL) AS api_keys,"
                " (SELECT COUNT(*) FROM channel_bindings cb"
                "   WHERE cb.owner_id=pu.username) AS channel_bindings,"
                " (SELECT COUNT(*) FROM channel_host_bindings hb"
                "   WHERE hb.owner_id=pu.username AND hb.state!='UNBOUND') AS host_bindings,"
                " (SELECT COALESCE(SUM(c.amount),0) FROM costs c"
                "   JOIN missions m ON m.id=c.mission_id"
                "   WHERE m.owner_id=pu.username AND c.currency='USD'"
                "   AND c.created_at LIKE ?) AS spend_usd"
                " FROM platform_users pu"
                " LEFT JOIN user_budgets b ON b.username=pu.username"
                " ORDER BY pu.created_at DESC, pu.username DESC",
                (f"{year_month}%",)).fetchall()
        return [{
            "username": r["username"],
            "created_at": r["created_at"],
            "must_change_password": bool(r["must_change_password"]),
            "monthly_limit_usd": (float(r["monthly_limit_usd"])
                                  if r["monthly_limit_usd"] is not None else None),
            "api_keys": int(r["api_keys"]),
            "channel_bindings": int(r["channel_bindings"]),
            "host_bindings": int(r["host_bindings"]),
            "channels": int(r["channel_bindings"]) + int(r["host_bindings"]),
            "spend_usd": round(float(r["spend_usd"] or 0.0), 10),
        } for r in rows]

    # --- client API keys ----------------------------------------------------------

    def create_api_key(self, key_id, owner_id, key_hash, prefix):
        """Persist one key's digest; the plaintext never reaches this method."""
        with self.connect() as db:
            db.execute("INSERT INTO api_keys (id, owner_id, key_hash, prefix, created_at)"
                       " VALUES (?, ?, ?, ?, ?)", (key_id, owner_id, key_hash, prefix, now()))
        return self.get_api_key(key_id)

    def get_api_key(self, key_id):
        """The verification row for one key id, or None.

        ``key_hash`` is for the verifier only and must never be serialized into
        an API response.
        """
        with self.connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        return dict(row) if row else None

    # --- mission grant persistence (gate restart survival) -------------------

    def save_grant(self, grant_id, subject, action_key, issued_at, expires_at):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO mission_grants"
                " (grant_id, subject, action_key, issued_at, expires_at) VALUES (?,?,?,?,?)",
                (grant_id, subject, action_key, issued_at, expires_at))

    def delete_grant(self, grant_id):
        with self.connect() as db:
            db.execute("DELETE FROM mission_grants WHERE grant_id=?", (grant_id,))

    def load_grants(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM mission_grants").fetchall()
        return [
            {
                "grant_id": r["grant_id"], "subject": r["subject"],
                "action_key": r["action_key"], "issued_at": r["issued_at"],
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]

    def api_keys_for(self, owner_id):
        """One owner's keys, newest first, without the digest column."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, prefix, created_at, revoked_at, last_used_at FROM api_keys"
                " WHERE owner_id=? ORDER BY created_at DESC, id", (owner_id,)).fetchall()
        return [dict(row) for row in rows]

    def touch_api_key(self, key_id):
        with self.connect() as db:
            db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now(), key_id))

    def revoke_api_key(self, owner_id, key_id):
        """Revoke one of the owner's live keys.

        Returns False when the id is unknown, belongs to another owner or is
        already revoked; a second revoke of the same key is therefore not
        distinguishable from a cross-owner attempt.
        """
        with self.connect() as db:
            cursor = db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND owner_id=?"
                                " AND revoked_at IS NULL", (now(), key_id, owner_id))
            return cursor.rowcount > 0

    def revoke_all_api_keys(self, owner_id):
        """Revoke every live key of one owner; returns how many were revoked.

        A password rotation or forced reset retires derived trust: keys minted
        under the old credential must not outlive it.
        """
        with self.connect() as db:
            cursor = db.execute("UPDATE api_keys SET revoked_at=?"
                                " WHERE owner_id=? AND revoked_at IS NULL", (now(), owner_id))
            return cursor.rowcount

    # --- per-user monthly spending budget ----------------------------------------

    def get_user_budget(self, username):
        """The user's explicit monthly limit row, or None when none was set.

        A missing row never means "blocked": the caller falls back to the
        instance-wide default (0 = unlimited).
        """
        with self.connect() as db:
            row = db.execute("SELECT username, monthly_limit_usd, created_at, updated_at"
                             " FROM user_budgets WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def set_user_budget(self, username, monthly_limit_usd):
        """Upsert one user's monthly USD limit; 0 means unlimited."""
        limit = float(monthly_limit_usd)
        if limit < 0:
            raise ValueError("monthly_limit_usd cannot be negative")
        timestamp = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO user_budgets (username, monthly_limit_usd, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(username) DO UPDATE SET monthly_limit_usd=excluded.monthly_limit_usd,"
                " updated_at=excluded.updated_at",
                (username, limit, timestamp, timestamp))
        return self.get_user_budget(username)

    def user_alert_sent(self, owner_id, month):
        """When this owner's billing-month reminder already went out, or None.

        Read-only: callers that only want to display state use this; the
        once-per-month decision itself lives in :mod:`facai.billing`.
        """
        with self.connect() as db:
            row = db.execute("SELECT sent_at FROM user_alerts WHERE owner_id=? AND month=?",
                             (owner_id, month)).fetchone()
        return row["sent_at"] if row else None

    def record_user_alert(self, owner_id, month):
        """Claim the once-per-billing-month alert slot; True when THIS call won it.

        The primary key is the dedupe: under concurrent gate calls exactly one
        INSERT survives and every loser reads rowcount 0, so a reminder can
        never double-send within a calendar month.
        """
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO user_alerts (owner_id, month, sent_at) VALUES (?, ?, ?)",
                (owner_id, month, now()))
        return cursor.rowcount > 0

    def create_recharge(self, username, amount_usd, window_seconds=300):
        """Open a USDT recharge request: PENDING for the payment window."""
        rid = uuid4().hex
        created = now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=int(window_seconds))).isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO recharges (id, username, amount_usd, status, created_at, expires_at)"
                " VALUES (?, ?, ?, 'PENDING', ?, ?)",
                (rid, username, float(amount_usd), created, expires))
        return self.get_recharge(rid)

    def get_recharge(self, recharge_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recharges WHERE id=?", (recharge_id,)).fetchone()
        return dict(row) if row else None

    def recharges_for(self, username, limit=20):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM recharges WHERE username=? ORDER BY created_at DESC LIMIT ?",
                (username, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def recharges_by_status(self, status="PENDING", limit=50):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM recharges WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def expire_stale_recharges(self, grace_seconds=RECHARGE_EXPIRY_GRACE_SECONDS):
        """Close PENDING recharges whose window plus settlement grace is over.

        An unpaid request left PENDING forever holds its txid hostage: the
        payer's real transfer can never be attached to a fresh request and
        chain-sync keeps waiting on a recharge whose acceptance window can
        never reopen. Closing is a guarded single transition on still-PENDING
        rows past ``expires_at + grace_seconds``; releasing ``txid`` frees the
        partial unique index so the same payment can back a new request.
        Never touches APPROVED/REJECTED/EXPIRED rows or their txids.
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=int(grace_seconds))).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            released = db.execute(
                "UPDATE recharges SET status='EXPIRED', txid=NULL"
                " WHERE status='PENDING' AND expires_at IS NOT NULL AND expires_at < ?",
                (cutoff,)).rowcount
        return released

    def attach_recharge_tx(self, recharge_id, username, txid):
        """The payer attaches their on-chain txid (owner-checked).

        Only a live request accepts a txid: PENDING status and inside the
        payment window. Otherwise an attacker could staple a well-known old
        public txid onto a fresh request and wait for the auto-credit.
        """
        # Reap dead requests first: their released txids must be claimable by
        # the payer's fresh request in the same call, not one sync pass later.
        self.expire_stale_recharges()
        with self.connect() as db:
            row = db.execute("SELECT * FROM recharges WHERE id=?", (recharge_id,)).fetchone()
            if row is None or row["username"] != username:
                raise KeyError(recharge_id)
            if row["status"] != "PENDING":
                raise ValueError(f"Recharge already {row['status']}")
            if row["expires_at"] and row["expires_at"] < now():
                raise ValueError("充值窗口已过期，请重新发起充值")
            try:
                claimed = db.execute(
                    "UPDATE recharges SET txid=? WHERE id=? AND status='PENDING'",
                    (txid, recharge_id))
            except sqlite3.IntegrityError:
                # recharges_txid_unique: one txid backs at most one recharge —
                # re-attaching it elsewhere is a double-claim attempt.
                raise ValueError("txid already attached to another recharge") from None
            if claimed.rowcount == 0:
                raise ValueError("Recharge is no longer pending")
        return self.get_recharge(recharge_id)

    def decide_recharge(self, recharge_id, approve, decided_by):
        """Approve or reject a PENDING recharge without touching the budget.

        Money-crediting approvals route through ``approve_recharge_and_credit``
        (the single source of truth shared with the chain-sync daemon); this
        path remains for rejects and legacy tooling and never credits by itself.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claimed = db.execute(
                "UPDATE recharges SET status=?, decided_at=?, decided_by=?"
                " WHERE id=? AND status='PENDING'",
                ("APPROVED" if approve else "REJECTED", now(), decided_by, recharge_id))
            if claimed.rowcount == 0:
                # Guarded single transition: the chain-sync daemon may have
                # credited this recharge between our read and the write; a
                # late reject must never overwrite an APPROVED (already
                # credited) recharge and desync the reconciliation.
                row = db.execute("SELECT status FROM recharges WHERE id=?",
                                 (recharge_id,)).fetchone()
                if row is None:
                    raise KeyError(recharge_id)
                raise ValueError(f"Recharge already {row['status']}")
        return self.get_recharge(recharge_id)

    def approve_recharge_and_credit(self, recharge_id, decided_by, txid=None):
        """Approve a PENDING recharge and credit the user's budget in one tx.

        The single source of truth for turning a recharge into money: the admin
        console and the TRON chain-sync daemon both call this, so an approval
        can never be split across two code paths and drift out of step. The
        ``status != PENDING`` guard makes it idempotent — a recharge is credited
        exactly once no matter how many times it is seen.

        When ``txid`` is given, the matching chain transfer is claimed through
        a guarded single transition (``UNMATCHED -> MATCHED``): a transfer
        already consumed by another recharge updates 0 rows, raises ValueError
        and nothing is credited. A txid that is not on chain yet still credits
        (admin judgment) — the transfer row may simply not be ingested at
        approval time.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM recharges WHERE id=?", (recharge_id,)).fetchone()
            if row is None:
                raise KeyError(recharge_id)
            # Atomic PENDING transition: the approve endpoint and the TRON
            # auto-credit daemon can race; only the writer that flips PENDING
            # here may credit, so a double-approval can never double-credit.
            claimed = db.execute(
                "UPDATE recharges SET status='APPROVED', decided_at=?, decided_by=?"
                " WHERE id=? AND status='PENDING'",
                (now(), decided_by, recharge_id))
            if claimed.rowcount == 0:
                raise ValueError(f"Recharge already {row['status']}")
            timestamp = now()
            if txid:
                transfer = db.execute(
                    "SELECT status, amount_usd FROM chain_transfers WHERE txid=?",
                    (txid,)).fetchone()
                if transfer is not None and transfer["status"] != "UNMATCHED":
                    # Any settled row for this txid — MATCHED to another
                    # recharge or IGNORED as off-platform history — must
                    # reject the claim, never credit past it.
                    raise ValueError(
                        f"Chain transfer {txid} already matched to another recharge")
                # Only bless the transfer as chain-proven when it genuinely
                # covers this recharge: a user-supplied txid worth less than
                # the requested amount stays UNMATCHED so reconciliation keeps
                # showing the delta instead of fake chain proof. The credit
                # itself remains an admin-judgment decision.
                if transfer is not None and (
                        float(transfer["amount_usd"]) + 0.01 >= float(row["amount_usd"])):
                    if db.execute(
                            "UPDATE chain_transfers SET status='MATCHED', matched_recharge_id=?"
                            " WHERE txid=? AND status='UNMATCHED'",
                            (recharge_id, txid)).rowcount == 0:
                        raise ValueError(
                            f"Chain transfer {txid} already matched to another recharge")
                # Writing the claimed txid onto this recharge must not collide
                # with the one-txid-one-recharge unique index (a racing approve
                # surfaces here as IntegrityError, not a 500).
                try:
                    db.execute(
                        "UPDATE recharges SET txid=? WHERE id=? AND status='APPROVED'",
                        (txid, recharge_id))
                except sqlite3.IntegrityError:
                    raise ValueError(
                        "txid already attached to another recharge") from None
            budget = db.execute("SELECT monthly_limit_usd FROM user_budgets WHERE username=?",
                                (row["username"],)).fetchone()
            current = float(budget["monthly_limit_usd"]) if budget else 0.0
            new_limit = current + float(row["amount_usd"])
            db.execute(
                "INSERT INTO user_budgets (username, monthly_limit_usd, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(username) DO UPDATE SET monthly_limit_usd=excluded.monthly_limit_usd,"
                " updated_at=excluded.updated_at",
                (row["username"], new_limit, timestamp, timestamp))
        return self.get_recharge(recharge_id)

    # --- on-chain transfers (TRC20 USDT) -------------------------------------

    def record_chain_transfer(self, txid, from_address, to_address, amount_usd,
                              token, block_ts, raw=None):
        """Record one incoming transfer once; True when it was newly inserted.

        Idempotent by txid so a re-polled block can never double-count money.
        """
        with self.connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO chain_transfers"
                " (txid, from_address, to_address, amount_usd, token, block_ts,"
                "  status, raw, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'UNMATCHED', ?, ?)",
                (str(txid), str(from_address or ""), str(to_address), float(amount_usd),
                 str(token or "USDT"), int(block_ts or 0),
                 json.dumps(raw or {}, ensure_ascii=False, default=str), now()))
            return cur.rowcount > 0

    def chain_transfer(self, txid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM chain_transfers WHERE txid=?", (txid,)).fetchone()
        return dict(row) if row else None

    def chain_transfers(self, status=None, limit=200):
        with self.connect() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM chain_transfers WHERE status=? ORDER BY block_ts DESC LIMIT ?",
                    (status, int(limit))).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM chain_transfers ORDER BY block_ts DESC LIMIT ?",
                    (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def matched_transfer_txids(self):
        """Txids already consumed by an approval (``chain_transfers.status='MATCHED'``).

        The chain-sync matching loop refreshes this set after every credit so a
        transfer cannot be applied to a second recharge off a stale snapshot.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT txid FROM chain_transfers WHERE status='MATCHED'").fetchall()
        return {row["txid"] for row in rows}

    def mark_transfer_ignored(self, txid, note=None):
        """Mark a chain transfer as platform-external (``UNMATCHED -> IGNORED``).

        Pre-platform wallet history (the operator's own prior business income)
        lands in the reconciliation queue because the lookback window sweeps
        it up. Ignoring is a guarded single transition: MATCHED transfers are
        never touched, and an ignored transfer can never be credited later.
        """
        with self.connect() as db:
            cur = db.execute(
                "UPDATE chain_transfers SET status='IGNORED',"
                " raw=json_set(COALESCE(raw,'{}'), '$.ignored_note', ?)"
                " WHERE txid=? AND status='UNMATCHED'",
                (note or "", txid))
            db.commit()
            return cur.rowcount > 0

    def set_sync_meta(self, key, value):
        """Persist a chain-sync freshness marker (reuses the kv table)."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO channel_notify_state (key, value, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, str(value), now()))
            db.commit()

    def get_sync_meta(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM channel_notify_state WHERE key=?",
                             (key,)).fetchone()
        return row[0] if row else None

    def billing_reconciliation(self):
        """One view that proves the money side adds up.

        ``approved_total`` is what the ledger says users paid; ``matched_total``
        is what the chain proves arrived and was tied to a recharge.
        ``approved_without_chain`` is every approved recharge with no verified
        on-chain match (empty txid OR a txid that never matched a real
        transfer), and ``unmatched_transfers`` is money that arrived but was
        never tied to a recharge — so no credit is ever silently unbacked.
        IGNORED transfers (operator-declared platform-external wallet history)
        are excluded from unmatched and reported only as counts. ``sync``
        carries the ingestion freshness markers the console shows so stale
        monitoring is visible at a glance.
        """
        with self.connect() as db:
            approved = db.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) AS s, COUNT(*) AS c"
                " FROM recharges WHERE status='APPROVED'").fetchone()
            matched = db.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) AS s, COUNT(*) AS c"
                " FROM chain_transfers WHERE status='MATCHED'").fetchone()
            without_chain = db.execute(
                "SELECT id, username, amount_usd, txid, decided_by, decided_at"
                " FROM recharges WHERE status='APPROVED'"
                " AND id NOT IN (SELECT matched_recharge_id FROM chain_transfers"
                "                WHERE status='MATCHED' AND matched_recharge_id IS NOT NULL)"
                " ORDER BY decided_at DESC LIMIT 50").fetchall()
            unmatched = db.execute(
                "SELECT txid, from_address, amount_usd, block_ts, created_at"
                " FROM chain_transfers WHERE status='UNMATCHED'"
                " ORDER BY block_ts DESC LIMIT 50").fetchall()
            ignored = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount_usd), 0) AS s"
                " FROM chain_transfers WHERE status='IGNORED'").fetchone()
        meta = {key: self.get_sync_meta(f"chain_sync:{key}")
                for key in ("last_round_at", "last_round_new", "wallet", "lookback_days")}
        sync = {
            "last_round_at": meta["last_round_at"],
            "last_round_new_transfers": int(meta["last_round_new"] or 0)
            if (meta["last_round_new"] or "").isdigit() else 0,
            "wallet_address": meta["wallet"],
            "lookback_days": int(meta["lookback_days"] or 0)
            if (meta["lookback_days"] or "").isdigit() else None,
        }
        return {
            "approved_total": round(float(approved["s"]), 6),
            "approved_count": int(approved["c"]),
            "matched_total": round(float(matched["s"]), 6),
            "matched_count": int(matched["c"]),
            "delta": round(float(approved["s"]) - float(matched["s"]), 6),
            "approved_without_chain": [dict(r) for r in without_chain],
            "unmatched_transfers": [dict(r) for r in unmatched],
            "ignored_count": int(ignored["c"]),
            "ignored_total_usd": round(float(ignored["s"]), 6),
            "sync": sync,
        }

    def user_spend_month(self, username, year_month=None):
        """Month-to-date usage for one user across the missions they own.

        ``costs`` rows are joined to ``missions`` on ``owner_id`` and restricted
        to the calendar month (UTC) via an ISO ``created_at LIKE 'YYYY-MM%'``
        prefix match, because ``created_at`` is stored as an ISO string. Only
        ``currency='USD'`` amounts sum into ``cost_usd`` — any other currency is
        excluded from that total and reported by code in ``other_currencies``
        (deduplicated, sorted) instead of being silently converted at an
        invented rate. Tokens are currency-agnostic and always summed:
        ``input_tokens``/``output_tokens`` carry only the rows whose split the
        upstream actually reported, and ``total_only_tokens`` carries the
        combined count of rows that never had a split, so a price estimate can
        flag exactly how much of the month rests on that weaker figure.
        ``total_tokens`` falls back to input+output for split-only rows.
        """
        if year_month is None:
            year_month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.connect() as db:
            rows = db.execute(
                "SELECT c.mission_id, c.amount, c.currency, c.tokens,"
                " c.input_tokens, c.output_tokens FROM costs AS c"
                " JOIN missions AS m ON m.id = c.mission_id"
                " WHERE m.owner_id=? AND c.created_at LIKE ?",
                (username, f"{year_month}%")).fetchall()
        cost_usd = 0.0
        other_currencies = set()
        total_tokens = 0
        input_tokens = 0
        output_tokens = 0
        total_only_tokens = 0
        missions = set()
        for row in rows:
            missions.add(row["mission_id"])
            split_input, split_output = row["input_tokens"], row["output_tokens"]
            split_seen = split_input is not None or split_output is not None
            if split_seen:
                input_tokens += int(split_input or 0)
                output_tokens += int(split_output or 0)
            if row["tokens"]:
                total_tokens += int(row["tokens"])
                if not split_seen:
                    total_only_tokens += int(row["tokens"])
            elif split_seen:
                total_tokens += int(split_input or 0) + int(split_output or 0)
            if row["currency"] == "USD":
                cost_usd += float(row["amount"])
            else:
                other_currencies.add(row["currency"])
        return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": total_tokens, "total_only_tokens": total_only_tokens,
                "cost_usd": round(cost_usd, 10), "other_currencies": sorted(other_currencies),
                "missions": len(missions)}


    # --- channel progress-notification cursor -----------------------------------

    def channel_notify_cursor(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM channel_notify_state WHERE key='event_cursor'").fetchone()
        try:
            return int(row["value"]) if row else None
        except (TypeError, ValueError):
            return None

    def set_channel_notify_cursor(self, sequence):
        with self.connect() as db:
            db.execute(
                "INSERT INTO channel_notify_state (key, value, updated_at) VALUES ('event_cursor', ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(int(sequence)), now()))

    def channel_linked_missions(self, channels):
        """Mission ids that originated from the given chat channels."""
        placeholders = ",".join("?" * len(channels))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT DISTINCT mission_id FROM channel_messages WHERE direction='INBOUND'"
                f" AND mission_id IS NOT NULL AND channel IN ({placeholders})", channels).fetchall()
        return {row["mission_id"] for row in rows}

    def channel_sender_for_mission(self, mission_id, channel):
        """The chat sender to reply to for a channel-originated mission."""
        with self.connect() as db:
            row = db.execute(
                "SELECT sender FROM channel_messages WHERE mission_id=? AND channel=?"
                " AND direction='INBOUND' ORDER BY created_at LIMIT 1",
                (mission_id, channel)).fetchone()
        return row["sender"] if row and row["sender"] else None

    # --- message channel audit ------------------------------------------------

    def record_channel_message(self, channel, direction, status, *, external_id=None,
                               sender=None, recipient=None, kind="text", text="",
                               payload=None, mission_id=None, owner_id="admin"):
        """Audit one inbound or outbound channel message (never a credential)."""
        if direction not in {"INBOUND", "OUTBOUND"}:
            raise ValueError("Direction must be INBOUND or OUTBOUND")
        message_id = uuid4().hex
        with self.connect() as db:
            if direction == "INBOUND" and external_id:
                # Serialize check/insert across concurrent webhook deliveries.
                # Preserve legacy audit rows instead of deleting duplicates.
                # Scoped to the owning account: upstream ids are only unique per
                # bot account, so the station's WeChat and a hosted customer's
                # WeChat can both deliver external_id "101" -- dropping the second
                # as a "duplicate" would silently lose a customer's message.
                db.execute("BEGIN IMMEDIATE")
                duplicate = db.execute(
                    "SELECT id FROM channel_messages WHERE channel=? AND direction='INBOUND'"
                    " AND external_id=? AND owner_id=? LIMIT 1",
                    (channel, external_id, owner_id)).fetchone()
                if duplicate:
                    return None
            db.execute(
                "INSERT INTO channel_messages (id, channel, direction, external_id, sender,"
                " recipient, kind, text, status, payload, mission_id, created_at, owner_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, channel, direction, external_id, sender, recipient, kind,
                 text, status, json.dumps(payload or {}, ensure_ascii=False),
                 mission_id, now(), owner_id))
        return message_id

    def channel_messages(self, channel=None, limit=100, owner_id=None):
        limit = max(1, min(int(limit), 500))
        with self.connect() as db:
            if channel and owner_id is not None:
                rows = db.execute(
                    "SELECT * FROM channel_messages WHERE channel=? AND owner_id=? ORDER BY created_at DESC LIMIT ?",
                    (channel, owner_id, limit))
            elif channel:
                rows = db.execute(
                    "SELECT * FROM channel_messages WHERE channel=? ORDER BY created_at DESC LIMIT ?",
                    (channel, limit))
            elif owner_id is not None:
                rows = db.execute(
                    "SELECT * FROM channel_messages WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                    (owner_id, limit))
            else:
                rows = db.execute(
                    "SELECT * FROM channel_messages ORDER BY created_at DESC LIMIT ?", (limit,))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def channel_message(self, message_id, owner_id=None):
        with self.connect() as db:
            if owner_id is None:
                row = db.execute("SELECT * FROM channel_messages WHERE id=?", (message_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM channel_messages WHERE id=? AND owner_id=?",
                                 (message_id, owner_id)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def channel_message_by_external_id(self, channel, external_id, owner_id=None):
        """Find an inbound delivery after an idempotent webhook insert conflict.

        ``owner_id`` narrows the lookup to one account's rows (per-owner hosted
        channels share the channel name with the station's own account); None
        keeps the historic unscoped behaviour.
        """
        with self.connect() as db:
            if owner_id is None:
                row = db.execute(
                    "SELECT * FROM channel_messages WHERE channel=? AND direction='INBOUND'"
                    " AND external_id=? ORDER BY created_at LIMIT 1",
                    (channel, external_id)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM channel_messages WHERE channel=? AND direction='INBOUND'"
                    " AND external_id=? AND owner_id=? ORDER BY created_at LIMIT 1",
                    (channel, external_id, owner_id)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def pending_inbound_messages(self, channel, limit=100, owner_id=None):
        """Inbound messages of one channel still awaiting conversion/dispatch.

        Only ``RECEIVED``/``RETRY`` messages with no linked mission are returned,
        oldest first. ``CONVERSION_FAILED`` rows are intentionally excluded so a
        blank or malformed message is never retried forever, and a message that
        already owns a mission is never replayed into a second one.
        ``owner_id`` restricts the replay to one account's rows: the station
        monitor must never replay a hosted customer's message under the admin
        owner, and a customer's monitor must never replay the station's.
        """
        limit = max(1, min(int(limit), 500))
        with self.connect() as db:
            if owner_id is None:
                rows = db.execute(
                    "SELECT * FROM channel_messages WHERE channel=? AND direction='INBOUND'"
                    " AND status IN ('RECEIVED', 'RETRY') AND mission_id IS NULL"
                    " ORDER BY created_at, id LIMIT ?", (channel, limit)).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM channel_messages WHERE channel=? AND direction='INBOUND'"
                    " AND status IN ('RECEIVED', 'RETRY') AND mission_id IS NULL"
                    " AND owner_id=?"
                    " ORDER BY created_at, id LIMIT ?", (channel, owner_id, limit)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
    def deferred_channel_messages(self, channels, limit=500):
        """Inbound messages whose linked mission has no queued/running jobs.

        These messages were persisted and converted to a mission, but their
        dispatch was deferred because no valid agent/model/runtime binding
        existed at the time.  The LEFT JOIN excludes missions that already
        have at least one job row, so a message is returned at most once
        per call and `ensure_channel_dispatch` will never duplicate a job.
        """
        limit = max(1, min(int(limit), 500))
        placeholders = ",".join("?" * len(channels))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT cm.* FROM channel_messages cm "
                f"LEFT JOIN jobs j ON j.mission_id = cm.mission_id "
                f"WHERE cm.direction='INBOUND' AND cm.mission_id IS NOT NULL "
                f"AND cm.channel IN ({placeholders}) "
                f"AND j.id IS NULL "
                f"ORDER BY cm.created_at, cm.id LIMIT ?",
                (*channels, limit)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


    def messages_for_mission(self, mission_id):
        """Return the channel messages linked to a mission, oldest first."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM channel_messages WHERE mission_id=? ORDER BY created_at",
                (mission_id,))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def set_channel_message_status(self, message_id, status):
        with self.connect() as db:
            updated = db.execute("UPDATE channel_messages SET status=? WHERE id=?",
                                 (status, message_id)).rowcount
        if not updated:
            raise KeyError(message_id)
        return status

    def register_user_model(self, owner_id, name, endpoint, model, api_key_encrypted):
        """Store one client-supplied model credential (already encrypted).

        The plaintext key never reaches the store; callers encrypt with
        ``facai.secretbox`` and only the token is persisted. Names are unique
        per owner so re-registering replaces the credential in place.
        """
        model_id = uuid4().hex
        timestamp = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM user_models WHERE owner_id=? AND name=?",
                (owner_id, name)).fetchone()
            if existing:
                db.execute(
                    "UPDATE user_models SET endpoint=?, model=?, api_key_encrypted=?,"
                    " updated_at=? WHERE id=?",
                    (endpoint, model, api_key_encrypted, timestamp, existing["id"]))
                model_id = existing["id"]
            else:
                db.execute(
                    "INSERT INTO user_models (id, owner_id, name, endpoint, model,"
                    " api_key_encrypted, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (model_id, owner_id, name, endpoint, model, api_key_encrypted,
                     timestamp, timestamp))
        return model_id

    def user_models(self, owner_id):
        """The owner's registered models, keys masked (never plaintext, never tokens)."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM user_models WHERE owner_id=? ORDER BY created_at",
                (owner_id,)).fetchall()
        return [dict(row) for row in rows]

    def user_model(self, owner_id, model_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM user_models WHERE owner_id=? AND id=?",
                             (owner_id, model_id)).fetchone()
        return dict(row) if row else None

    def user_model_encrypted_key(self, owner_id, model_id):
        with self.connect() as db:
            row = db.execute("SELECT api_key_encrypted FROM user_models WHERE owner_id=? AND id=?",
                             (owner_id, model_id)).fetchone()
        return row["api_key_encrypted"] if row else None

    def delete_user_model(self, owner_id, model_id):
        with self.connect() as db:
            removed = db.execute("DELETE FROM user_models WHERE owner_id=? AND id=?",
                                 (owner_id, model_id)).rowcount
        return bool(removed)

    # --- customer channel bindings (per-tenant persistent channels) ------------

    def upsert_channel_binding(self, owner_id, channel, bot_token_encrypted, chat_id):
        """Bind one owner's own chat bot; re-binding rotates the credential in place.

        ``bot_token_encrypted`` arrives already sealed (callers use
        ``facai.secretbox``), so the store never sees the plaintext token. The
        row is keyed (owner_id, channel) -- one owner holds exactly one bot per
        channel, and the first-bind ``created_at`` survives a re-bind.
        """
        with self.connect() as db:
            db.execute(
                "INSERT INTO channel_bindings (owner_id, channel, bot_token_encrypted,"
                " chat_id, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(owner_id, channel) DO UPDATE SET"
                " bot_token_encrypted=excluded.bot_token_encrypted,"
                " chat_id=excluded.chat_id",
                (owner_id, channel, bot_token_encrypted, chat_id, now()))
        return self.channel_binding(owner_id, channel)

    def channel_binding(self, owner_id, channel):
        """The owner's binding metadata. The token column is never selected."""
        with self.connect() as db:
            row = db.execute(
                "SELECT owner_id, channel, chat_id, created_at FROM channel_bindings"
                " WHERE owner_id=? AND channel=?", (owner_id, channel)).fetchone()
        return dict(row) if row else None

    def channel_binding_token(self, owner_id, channel):
        """The stored token ciphertext, for send-time decryption only."""
        with self.connect() as db:
            row = db.execute(
                "SELECT bot_token_encrypted FROM channel_bindings"
                " WHERE owner_id=? AND channel=?", (owner_id, channel)).fetchone()
        return row["bot_token_encrypted"] if row else None

    def delete_channel_binding(self, owner_id, channel):
        """Unbind one owner's channel bot; returns True when a binding existed."""
        with self.connect() as db:
            removed = db.execute("DELETE FROM channel_bindings WHERE owner_id=? AND channel=?",
                                 (owner_id, channel)).rowcount
        return bool(removed)

    # --- channel hosting bindings (per-owner hosted channels) -------------------
    # State machine behind docs/CHANNEL_HOST_DESIGN.md: BINDING -> ACTIVE ->
    # UNBOUND, with UNBOUND reopening as a SAME-ROW write back to BINDING.
    # Illegal transitions raise ValueError here so the API layer can turn them
    # into honest 409/422 responses instead of silently inventing state.

    def transition_host_binding(self, owner_id, channel, target_state,
                                session_dir=None, display_name=None):
        """Move one (owner, channel) hosting binding to ``target_state``.

        Legal transitions (docs/CHANNEL_HOST_DESIGN.md §2):

        * no row   -> BINDING   (first bind; ``created_at`` and ``id`` are born)
        * BINDING  -> ACTIVE    (credentials confirmed -- next wave)
        * BINDING  -> UNBOUND   (abandoned mid-binding)
        * ACTIVE   -> UNBOUND   (stop / revoke)
        * UNBOUND  -> BINDING   (reopen: same row, fresh session_dir)

        A same-state request is a no-op that returns the row unchanged --
        "start while already BINDING" is idempotent, never a fake second QR
        lifecycle. Anything else raises ValueError. ``session_dir`` is stored
        verbatim as the RELATIVE namespace path; the store never touches the
        filesystem -- files under that namespace are the monitor wave's job.
        """
        if target_state not in HOST_BINDING_STATES:
            raise ValueError(f"unknown host binding state: {target_state!r}")
        timestamp = now()
        with self.connect() as db:
            row = db.execute(
                "SELECT id, state FROM channel_host_bindings WHERE owner_id=? AND channel=?",
                (owner_id, channel)).fetchone()
            if row is None:
                if target_state != "BINDING":
                    raise ValueError(
                        f"cannot move a nonexistent {channel} host binding to {target_state}")
                binding_id = uuid4().hex
                db.execute(
                    "INSERT INTO channel_host_bindings"
                    " (id, owner_id, channel, state, session_dir, display_name,"
                    "  created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (binding_id, owner_id, channel, target_state,
                     session_dir, display_name, timestamp, timestamp))
            else:
                current = row["state"]
                if current == target_state:
                    pass  # idempotent no-op
                elif (current, target_state) in {("BINDING", "ACTIVE"), ("BINDING", "UNBOUND"),
                                                 ("ACTIVE", "UNBOUND"), ("UNBOUND", "BINDING")}:
                    db.execute(
                        "UPDATE channel_host_bindings SET state=?,"
                        " session_dir=COALESCE(?, session_dir),"
                        " display_name=COALESCE(?, display_name), updated_at=?"
                        " WHERE owner_id=? AND channel=?",
                        (target_state, session_dir, display_name, timestamp,
                         owner_id, channel))
                else:
                    raise ValueError(
                        f"illegal host binding transition {current} -> {target_state}")
        return self.host_binding(owner_id, channel)

    def host_binding(self, owner_id, channel):
        """One owner's hosting binding, or None. No secret column exists."""
        with self.connect() as db:
            row = db.execute(
                "SELECT id, owner_id, channel, state, session_dir, display_name,"
                " created_at, updated_at FROM channel_host_bindings"
                " WHERE owner_id=? AND channel=?", (owner_id, channel)).fetchone()
        return dict(row) if row else None

    def host_bindings_for(self, owner_id):
        """All hosting bindings of one owner, channel order stable for the API."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, owner_id, channel, state, session_dir, display_name,"
                " created_at, updated_at FROM channel_host_bindings"
                " WHERE owner_id=? ORDER BY channel", (owner_id,)).fetchall()
        return [dict(row) for row in rows]

    def active_host_bindings(self, channel):
        """Every ACTIVE hosting binding of one channel, across all owners.

        The process-startup scan for the per-owner monitors
        (docs/CHANNEL_HOST_DESIGN.md wave 2): each row here gets its monitor
        task back after a restart, driven by the credential file in the row's
        own ``providers/hosting/<owner_id>`` namespace.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, owner_id, channel, state, session_dir, display_name,"
                " created_at, updated_at FROM channel_host_bindings"
                " WHERE channel=? AND state='ACTIVE' ORDER BY owner_id", (channel,)).fetchall()
        return [dict(row) for row in rows]

    def ops_brief(self, hours=24):
        """The 24-hour station report: what ran, what broke, what self-healed.

        This is the machine-generated morning standup for a one-operator
        deployment: throughput, success rate, self-healing retries, spend and
        anything currently stuck -- real aggregates from the ledger, never
        invented. Times are ISO strings compared as text.
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).isoformat()
        out = {"window_hours": int(hours), "since": since}
        with self.connect() as db:
            out["missions_created"] = db.execute(
                "SELECT COUNT(*) FROM missions WHERE created_at >= ?", (since,)).fetchone()[0]
            # The missions table has no updated_at; job ledger activity in the
            # window is the honest throughput signal.
            out["missions_completed"] = db.execute(
                "SELECT COUNT(DISTINCT mission_id) FROM jobs WHERE status='COMPLETED' AND updated_at >= ?",
                (since,)).fetchone()[0]
            out["missions_failed"] = db.execute(
                "SELECT COUNT(DISTINCT mission_id) FROM jobs WHERE status='FAILED' AND updated_at >= ?",
                (since,)).fetchone()[0]
            out["jobs_completed"] = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='COMPLETED' AND updated_at >= ?",
                (since,)).fetchone()[0]
            out["jobs_failed"] = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='FAILED' AND updated_at >= ?",
                (since,)).fetchone()[0]
            out["retries_selfhealed"] = db.execute(
                "SELECT COUNT(*) FROM events WHERE type='JOB_REQUEUED' AND occurred_at >= ?",
                (since,)).fetchone()[0]
            usage = db.execute(
                "SELECT COALESCE(SUM(tokens),0), COALESCE(SUM(amount),0) FROM costs"
                " WHERE created_at >= ?", (since,)).fetchone()
            out["tokens"] = int(usage[0] or 0)
            out["cost_usd"] = round(float(usage[1] or 0), 6)
            out["channel_inbound"] = db.execute(
                "SELECT COUNT(*) FROM channel_messages WHERE direction='INBOUND' AND created_at >= ?",
                (since,)).fetchone()[0]
            out["replies_outbound"] = db.execute(
                "SELECT COUNT(*) FROM channel_messages WHERE direction='OUTBOUND' AND created_at >= ?",
                (since,)).fetchone()[0]
            out["stuck_running"] = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='RUNNING' AND lease_expires_at < ?",
                (now(),)).fetchone()[0]
            out["by_owner"] = [dict(row) for row in db.execute(
                # DISTINCT missions: the LEFT JOIN to costs multiplies rows, so
                # a bare COUNT(*) reports one mission once per cost row.
                "SELECT m.owner_id, COUNT(DISTINCT m.id) AS missions,"
                " COALESCE(SUM(c.amount),0) AS cost_usd"
                " FROM missions m LEFT JOIN costs c ON c.mission_id=m.id AND c.created_at >= ?"
                " WHERE m.created_at >= ? GROUP BY m.owner_id ORDER BY missions DESC",
                (since, since)).fetchall()]
            out["recent_failures"] = [dict(row) for row in db.execute(
                "SELECT mission_id, step_id, error, updated_at FROM jobs WHERE status='FAILED'"
                " AND updated_at >= ? ORDER BY updated_at DESC LIMIT 5", (since,)).fetchall()]
        return out

    def channel_outbound_pending(self, channel, limit=100):
        """Queued outbound rows of one channel, oldest first (relay outbox).

        The bridge client fetches these, delivers them upstream, and
        acknowledges the ids; an acked row leaves this list for good.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, recipient, text, payload, created_at FROM channel_messages"
                " WHERE channel=? AND direction='OUTBOUND' AND status='QUEUED'"
                " ORDER BY created_at, id LIMIT ?", (channel, int(limit))).fetchall()
        pending = []
        for row in rows:
            try:
                reply_to = (json.loads(row["payload"] or "{}") or {}).get("reply_to")
            except ValueError:
                reply_to = None
            pending.append({"id": row["id"], "recipient": row["recipient"], "text": row["text"],
                            "reply_to": reply_to, "created_at": row["created_at"]})
        return pending

    def mark_channel_outbound_sent(self, channel, message_ids):
        """Mark one channel's queued outbound rows SENT (delivered upstream).

        Only rows that are still QUEUED flip to SENT, so a duplicate ack can
        never resurrect a row and ids of other channels are ignored.
        """
        ids = [mid for mid in (message_ids or []) if isinstance(mid, str) and mid]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self.connect() as db:
            updated = db.execute(
                "UPDATE channel_messages SET status='SENT' WHERE channel=?"
                " AND direction='OUTBOUND' AND status='QUEUED' AND id IN (" + marks + ")",
                (channel, *ids)).rowcount
        return updated

    def claim_channel_conversion(self, message_id, lease_seconds=300, owner=None):
        """Atomically claim the one-time prompt conversion of an inbound message.

        Duplicate webhook deliveries of the same message must not each invoke the
        paid conversion model. The claim is a durable lease stored on the audit
        row: ``BEGIN IMMEDIATE`` serializes contenders so exactly one caller gets
        ``claimed=True`` and may convert, while every duplicate receives the live
        row with ``reason='IN_PROGRESS'``. A claim whose owner crashed is taken
        over once ``lease_seconds`` elapse (``reason=None``), so a message can
        never be stranded by a dead worker. Rows that already own a mission are
        never claimable.
        """
        owner = owner or uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM channel_messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["direction"] != "INBOUND":
                return {"claimed": False, "reason": "NOT_FOUND", "owner": None, "row": None}
            if row["mission_id"]:
                return {"claimed": False, "reason": "ALREADY_LINKED", "owner": None,
                        "row": {**dict(row), "payload": json.loads(row["payload"])}}
            if row["conversion_owner"] and row["conversion_expires_at"] \
                    and row["conversion_expires_at"] >= now():
                return {"claimed": False, "reason": "IN_PROGRESS", "owner": None,
                        "row": {**dict(row), "payload": json.loads(row["payload"])}}
            expires = (datetime.now(timezone.utc)
                       + timedelta(seconds=lease_seconds)).isoformat()
            cursor = db.execute(
                "UPDATE channel_messages SET status='CONVERTING', conversion_owner=?,"
                " conversion_expires_at=? WHERE id=? AND mission_id IS NULL"
                " AND (conversion_expires_at IS NULL OR conversion_expires_at < ?)",
                (owner, expires, message_id, now()))
            current = db.execute("SELECT * FROM channel_messages WHERE id=?",
                                 (message_id,)).fetchone()
            if cursor.rowcount != 1:
                return {"claimed": False, "reason": "IN_PROGRESS", "owner": None,
                        "row": {**dict(current), "payload": json.loads(current["payload"])}}
            return {"claimed": True, "reason": None, "owner": owner,
                    "row": {**dict(current), "payload": json.loads(current["payload"])}}

    def release_channel_conversion(self, message_id, owner, status="RETRY"):
        """Release a conversion lease without touching a completed mission link."""
        with self.connect() as db:
            updated = db.execute(
                "UPDATE channel_messages SET status=?, conversion_owner=NULL,"
                " conversion_expires_at=NULL WHERE id=? AND conversion_owner=?",
                (status, message_id, owner)).rowcount
        return bool(updated)

    def recover_stale_channel_conversions(self):
        """Return crashed conversion claims to the replayable RETRY pool.

        Bounded by the claim lease: only rows whose lease has already expired are
        reset. A message that linked a mission in the meantime is left alone, and
        a live claim is never disturbed, so recovery cannot duplicate a mission.
        """
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE channel_messages SET status='RETRY', conversion_owner=NULL,"
                " conversion_expires_at=NULL WHERE direction='INBOUND'"
                " AND mission_id IS NULL AND conversion_owner IS NOT NULL"
                " AND conversion_expires_at IS NOT NULL AND conversion_expires_at < ?",
                (now(),))
            return cursor.rowcount

    def create_mission_from_message(self, message_id, channel, *, title, original_input,
                                    labels, professional_prompt, conversion_payload, owner_id=None):
        """Atomically convert one inbound message into a linked mission.

        The check and the insert run inside a single ``BEGIN IMMEDIATE``
        transaction, so two concurrent dispatches of the same message can never
        create two missions: the loser rolls back, removes its unused workspace
        directory and receives the winner's mission id. The message row is never
        deleted or overwritten - only its ``mission_id``/``status`` change.
        """
        mission_id = uuid4().hex
        workspace = (self.root / "workspaces" / mission_id).resolve()
        workspace.mkdir(parents=True, exist_ok=False)
        claimed = False
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM channel_messages WHERE id=?",
                                 (message_id,)).fetchone()
                if row is None or row["channel"] != channel or row["direction"] != "INBOUND":
                    db.execute("ROLLBACK")
                    return None, False
                if row["mission_id"]:
                    db.execute("ROLLBACK")
                    return row["mission_id"], False
                target_owner = owner_id or row["owner_id"]
                db.execute(
                    "INSERT INTO missions (id, title, original_input, status, workspace, agent_id,"
                    " model_id, created_at, professional_prompt, owner_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (mission_id, title, original_input, "WAITING", str(workspace), None, None,
                     now(), professional_prompt, target_owner))
                for position, label in enumerate(labels):
                    db.execute("INSERT INTO steps (id, mission_id, label, position, status) VALUES (?, ?, ?, ?, ?)",
                               (uuid4().hex, mission_id, label, position, "WAITING"))
                self._event(db, mission_id, None, None, None, "MISSION_CREATED",
                            {"title": title, "channel": channel, "message_id": message_id})
                self._event(db, mission_id, None, None, None, "PROMPT_CONVERTED",
                            conversion_payload)
                db.execute(
                    "UPDATE channel_messages SET mission_id=?, status='DISPATCHED',"
                    " conversion_owner=NULL, conversion_expires_at=NULL WHERE id=?",
                    (mission_id, message_id))
                claimed = True
            # Only the dispatch that created the row signs the grant; a losing
            # concurrent dispatch returns the winner's mission, which already
            # carries one.
            if claimed:
                ensure_mission_grant(mission_id)
            return mission_id, True
        finally:
            if not claimed:
                try:
                    workspace.rmdir()
                except OSError:
                    pass

    def attach_channel_mission(self, message_id, mission_id):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                raise KeyError(mission_id)
            updated = db.execute(
                "UPDATE channel_messages SET mission_id=?, status='DISPATCHED'"
                " WHERE id=? AND direction='INBOUND'", (mission_id, message_id)).rowcount
        if not updated:
            raise KeyError(message_id)
        return self.channel_messages(limit=500)

    # --- message-scoped audit events ------------------------------------------

    def record_channel_event(self, channel, message_id, event_type, payload=None):
        """Append one message-level event, even when no mission exists yet."""
        event_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO channel_events (id, channel, message_id, type, payload, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, channel, message_id, event_type,
                 json.dumps(payload or {}, ensure_ascii=False), now()))
        return event_id

    def channel_events(self, message_id=None, channel=None, after=0, limit=500):
        limit = max(1, min(int(limit), 500))
        clauses, params = ["sequence > ?"], [int(after)]
        if message_id:
            clauses.append("message_id = ?")
            params.append(message_id)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM channel_events WHERE {' AND '.join(clauses)}"
                " ORDER BY sequence LIMIT ?", (*params, limit))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    # --- 渠内审批（pending approvals）------------------------------------------
    # Grant×渠道相乘点的持久侧：任务被授权门挡下时落一行审批请求，客户的
    # 聊天回复在 api 层消费它。这里只做owner 隔离的行管理，不做任何签发——
    # grant 的签发权永远在 Owner 核（facai.authority）。

    def create_pending_approval(self, mission_id, owner_id, channel, message_id=None,
                                ttl_seconds=DEFAULT_APPROVAL_TTL_SECONDS):
        """Record one approval request for a channel-originated mission."""
        approval_id = uuid4().hex
        created = now()
        expires = (datetime.now(timezone.utc)
                   + timedelta(seconds=ttl_seconds)).isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO pending_approvals (id, mission_id, owner_id, channel,"
                " message_id, status, expires_at, created_at) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)",
                (approval_id, mission_id, owner_id, channel, message_id,
                 expires, created))
        return {"id": approval_id, "mission_id": mission_id, "owner_id": owner_id,
                "channel": channel, "message_id": message_id, "status": "PENDING",
                "expires_at": expires, "created_at": created}

    def pending_approval(self, approval_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM pending_approvals WHERE id=?",
                             (approval_id,)).fetchone()
        return dict(row) if row else None

    def pending_approvals_for_owner(self, owner_id, limit=100):
        """The owner's LIVE approval rows (PENDING and unexpired), oldest first.

        Owner-scoped by construction: this is the isolation boundary that
        keeps one customer's 「批准」 from signing another customer's mission.
        Expiry is judged at read time against ``expires_at``, so an unswept
        lapsed row can never be consumed.
        """
        limit = max(1, min(int(limit), 500))
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM pending_approvals WHERE owner_id=? AND status='PENDING'"
                " AND expires_at > ? ORDER BY created_at, id LIMIT ?",
                (owner_id, now(), limit)).fetchall()
        return [dict(row) for row in rows]

    def latest_approval_for_mission(self, mission_id):
        """The mission's most recent approval row of any status (or None).

        The dispatch path uses this to stay quiet while a request is already
        outstanding (PENDING, live or lapsed): the monitor replays jobless
        missions every round, and a re-prompt each round would flood the chat.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM pending_approvals WHERE mission_id=?"
                " ORDER BY created_at DESC, id DESC LIMIT 1", (mission_id,)).fetchone()
        return dict(row) if row else None

    def consume_pending_approval(self, approval_id, owner_id):
        """Atomically consume one live approval row (PENDING -> CONSUMED).

        The whole decision rides one BEGIN IMMEDIATE transaction, so two
        concurrent 「批准」 replies can never both spend the same request:
        the loser sees the row already consumed and receives None. Refuses
        rows that are not the owner's, already terminal, or past expiry.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM pending_approvals WHERE id=? AND owner_id=?",
                (approval_id, owner_id)).fetchone()
            if row is None or row["status"] != "PENDING" or row["expires_at"] <= now():
                return None
            db.execute("UPDATE pending_approvals SET status='CONSUMED' WHERE id=?",
                       (approval_id,))
            return dict(row)

    def cancel_pending_approval(self, approval_id, owner_id):
        """Void one pending approval row (PENDING -> CANCELLED); owner-scoped.

        An already-terminal row is refused (None): cancelling twice must not
        rewrite history. Expiry does not block a cancel -- an explicit 「取消」
        after the window still records the customer's refusal honestly.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM pending_approvals WHERE id=? AND owner_id=?",
                (approval_id, owner_id)).fetchone()
            if row is None or row["status"] != "PENDING":
                return None
            db.execute("UPDATE pending_approvals SET status='CANCELLED' WHERE id=?",
                       (approval_id,))
            return dict(row)

    def expire_pending_approvals(self):
        """Sweep lapsed PENDING rows to EXPIRED; returns how many.

        Housekeeping only -- consumption already refuses lapsed rows at read
        time. The sweep keeps the audit truthful (a query sees EXPIRED, not a
        stale PENDING) and is safe to run on every approval lookup.
        """
        with self.connect() as db:
            return db.execute(
                "UPDATE pending_approvals SET status='EXPIRED'"
                " WHERE status='PENDING' AND expires_at <= ?", (now(),)).rowcount

    # --- n8n integration audit -------------------------------------------------

    def last_event_sequence(self):
        with self.connect() as db:
            return db.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]

    def n8n_events_after(self, sequence, limit=50):
        limit = max(1, min(int(limit), 500))
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                (int(sequence), limit))
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def n8n_get_state(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM n8n_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def n8n_set_state(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO n8n_state (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), now()))

    def n8n_cursor(self):
        value = self.n8n_get_state("event_cursor")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def n8n_set_cursor(self, sequence):
        self.n8n_set_state("event_cursor", int(sequence))

    @staticmethod
    def _n8n_delivery_row(row):
        if row is None:
            return None
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        data["retryable"] = bool(data["retryable"])
        return data

    def create_n8n_delivery(self, request_id, mission_id, event_id, event_type,
                            payload, max_attempts=3):
        """Insert one delivery row, or return the existing row for that request.

        The unique ``request_id`` makes the delivery idempotent across a retry or
        a process restart: the same event is never recorded twice.
        """
        delivery_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO n8n_deliveries (id, request_id, mission_id, event_id,"
                " event_type, status, attempts, max_attempts, retryable, payload, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, 1, ?, ?, ?)",
                (delivery_id, request_id, mission_id, event_id, event_type,
                 int(max_attempts), json.dumps(payload, ensure_ascii=False), now(), now()))
            row = db.execute("SELECT * FROM n8n_deliveries WHERE request_id=?",
                             (request_id,)).fetchone()
        return self._n8n_delivery_row(row)

    def get_n8n_delivery(self, delivery_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM n8n_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return self._n8n_delivery_row(row)

    def record_n8n_delivery_result(self, delivery_id, status, http_status=None,
                                   error=None, retryable=False):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM n8n_deliveries WHERE id=?", (delivery_id,)).fetchone() is None:
                raise KeyError(delivery_id)
            db.execute(
                "UPDATE n8n_deliveries SET status=?, attempts=attempts+1, http_status=?,"
                " error=?, retryable=?, updated_at=? WHERE id=?",
                (status, http_status, error, 1 if retryable else 0, now(), delivery_id))
            row = db.execute("SELECT * FROM n8n_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return self._n8n_delivery_row(row)

    def n8n_retryable_deliveries(self, limit=50):
        limit = max(1, min(int(limit), 500))
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM n8n_deliveries WHERE status IN ('PENDING', 'FAILED')"
                " AND retryable=1 AND attempts < max_attempts ORDER BY created_at, id LIMIT ?",
                (limit,)).fetchall()
        return [self._n8n_delivery_row(row) for row in rows]

    def n8n_deliveries(self, limit=20):
        limit = max(1, min(int(limit), 200))
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM n8n_deliveries ORDER BY created_at DESC, id LIMIT ?",
                (limit,)).fetchall()
        return [self._n8n_delivery_row(row) for row in rows]

    def n8n_delivery_summary(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS n FROM n8n_deliveries GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def claim_n8n_callback(self, request_id, mission_id, status, detail):
        """Claim an inbound callback exactly once. Returns (claimed, existing)."""
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO n8n_callbacks (request_id, mission_id, status, detail,"
                " received_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, mission_id, status, detail, now()))
            if cursor.rowcount == 0:
                row = db.execute("SELECT * FROM n8n_callbacks WHERE request_id=?",
                                 (request_id,)).fetchone()
                return False, dict(row)
            return True, None

    def finish_n8n_callback(self, request_id, status, detail):
        with self.connect() as db:
            db.execute("UPDATE n8n_callbacks SET status=?, detail=? WHERE request_id=?",
                       (status, detail, request_id))

    def n8n_callbacks(self, limit=20):
        limit = max(1, min(int(limit), 200))
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM n8n_callbacks ORDER BY received_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def requeue_expired(self):
        recovered = []
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status='RUNNING' AND lease_expires_at IS NOT NULL"
                " AND lease_expires_at < ?", (now(),)).fetchall()
            for row in rows:
                if row["attempts"] >= row["max_attempts"]:
                    db.execute("UPDATE jobs SET status='FAILED', error='lease expired', lease_owner=NULL,"
                               " lease_expires_at=NULL, updated_at=? WHERE id=?", (now(), row["id"]))
                    if row["step_id"]:
                        db.execute("UPDATE steps SET status='FAILED' WHERE mission_id=? AND id=? AND status='RUNNING'",
                                   (row["mission_id"], row["step_id"]))
                        self._event(db, row["mission_id"], row["step_id"], None, None, "STEP_FAILED",
                                    {"status": "FAILED", "error": "lease expired"})
                    self._event(db, row["mission_id"], row["step_id"], None, None, "JOB_FAILED",
                                {"job_id": row["id"], "error": "lease expired"})
                    self._refresh_blocked_jobs(db, row["mission_id"])
                else:
                    # The lease expiring is not proof the worker died: it may
                    # be between expiry and its finalize write (or still
                    # queued behind the serial lock on a stale lease). The
                    # not-before buffer stops a second worker from claiming
                    # and re-running the step in that window (see
                    # JOB_REQUEUE_BUFFER_SECONDS).
                    buffer = (datetime.now(timezone.utc)
                              + timedelta(seconds=JOB_REQUEUE_BUFFER_SECONDS)).isoformat()
                    db.execute("UPDATE jobs SET status='QUEUED', not_before=?, lease_owner=NULL,"
                               " lease_expires_at=NULL, updated_at=? WHERE id=?",
                               (buffer, now(), row["id"]))
                    self._event(db, row["mission_id"], row["step_id"], None, None, "JOB_REQUEUED",
                                {"job_id": row["id"]})
                recovered.append(row["id"])
        return recovered
