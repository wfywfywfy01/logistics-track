#!/usr/bin/env python
"""Transactional local state for logistics-track."""
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


PASSWORD_ITERATIONS = 310_000
ADMIN_ROLES = ("admin", "operator", "viewer")


def _password_hash(password, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256$%s$%s$%s" % (
        PASSWORD_ITERATIONS, salt.hex(), digest.hex())


def _password_matches(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


def utcnow():
    return datetime.now(UTC)


def iso(value=None):
    value = value or utcnow()
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def read_legacy_json(path):
    errors = []
    for candidate in (path, Path(str(path) + ".bak")):
        if not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{candidate.name}: {error}")
    if errors:
        raise ValueError("; ".join(errors))
    return None


class TaskConflict(RuntimeError):
    pass


class Storage:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.environ.get("LOGIBOT_DATA_DIR") or "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "shipments.db"
        self._initialize()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self):
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shipments (
                    order_no TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    name TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incoming_messages (
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','operator','viewer')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def ensure_admin_user(self, username, password):
        timestamp = iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM admin_users WHERE username=?", (username,)).fetchone()
            connection.execute(
                """INSERT INTO admin_users
                   (username,password_hash,role,active,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET
                   password_hash=excluded.password_hash,role='admin',active=1,
                   updated_at=excluded.updated_at""",
                (username, _password_hash(password), "admin", 1,
                 row["created_at"] if row else timestamp, timestamp),
            )

    def authenticate_admin(self, username, password, include_auth_version=False):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT username,password_hash,role,active,updated_at FROM admin_users WHERE username=?",
                (username,),
            ).fetchone()
        if not row:
            hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"\0" * 16,
                                PASSWORD_ITERATIONS)
            return None
        if not row["active"] or not _password_matches(password, row["password_hash"]):
            return None
        principal = {"username": row["username"], "role": row["role"]}
        if include_auth_version:
            principal["auth_version"] = row["updated_at"]
        return principal

    def get_admin_principal(self, username):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT username,role,active,updated_at FROM admin_users WHERE username=?",
                (username,),
            ).fetchone()
        if not row or not row["active"]:
            return None
        return {"username": row["username"], "role": row["role"],
                "auth_version": row["updated_at"]}

    def list_admin_users(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT username,role,active,created_at,updated_at FROM admin_users ORDER BY username"
            ).fetchall()
        return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def put_admin_user(self, username, password, role, active=None, operator=None, reason=None):
        username = str(username or "").strip()
        if not (3 <= len(username) <= 64) or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in username):
            raise ValueError("username must be 3-64 letters, digits, dot, underscore or hyphen")
        if role not in ADMIN_ROLES:
            raise ValueError("role must be admin, operator or viewer")
        if password and len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        if operator and not reason:
            raise ValueError("reason is required for audited user changes")
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT password_hash,role,active,created_at FROM admin_users WHERE username=?", (username,)
            ).fetchone()
            if username == "admin" and existing:
                raise ValueError("bootstrap admin is managed by ADMIN_TOKEN")
            if not existing and not password:
                raise ValueError("password is required for a new user")
            encoded = _password_hash(password) if password else existing["password_hash"]
            created_at = existing["created_at"] if existing else timestamp
            active_value = bool(existing["active"]) if active is None and existing else (
                True if active is None else bool(active))
            connection.execute(
                """INSERT INTO admin_users(username,password_hash,role,active,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET
                   password_hash=excluded.password_hash,role=excluded.role,
                   active=excluded.active,updated_at=excluded.updated_at""",
                (username, encoded, role, int(active_value), created_at, timestamp),
            )
            active_admins = connection.execute(
                "SELECT COUNT(*) FROM admin_users WHERE role='admin' AND active=1"
            ).fetchone()[0]
            if active_admins == 0:
                raise ValueError("at least one active admin is required")
            if operator:
                connection.execute(
                    """INSERT INTO audit_log
                       (order_no,entity_type,entity_id,action,operator,reason,created_at)
                       VALUES(NULL,'admin_user',?,'upsert',?,?,?)""",
                    (username, operator, reason, timestamp),
                )
        return {"username": username, "role": role, "active": active_value}

    def migrate_legacy_json(self):
        counts = {"shipments": 0, "documents": 0}
        now = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            shipment_path = self.data_dir / "shipments.json"
            shipments = read_legacy_json(shipment_path)
            if shipments is not None:
                if not isinstance(shipments, dict):
                    raise ValueError("shipments.json must contain an object")
                for order, payload in shipments.items():
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO shipments(order_no,payload,updated_at) VALUES(?,?,?)",
                        (order, json.dumps(payload, ensure_ascii=False), now),
                    )
                    counts["shipments"] += cursor.rowcount
            for filename in ("sales_map.json", "users_map.json", "org_people.json",
                             "ups_results.json", "watcher_state.json"):
                path = self.data_dir / filename
                payload = read_legacy_json(path)
                if payload is None:
                    continue
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO documents(name,payload,updated_at) VALUES(?,?,?)",
                    (path.stem, json.dumps(payload, ensure_ascii=False), now),
                )
                counts["documents"] += cursor.rowcount
        return counts

    def get_shipments(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT order_no,payload FROM shipments").fetchall()
        return {row["order_no"]: json.loads(row["payload"]) for row in rows}

    def get_shipment(self, order):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM shipments WHERE order_no=?", (order,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_shipment(self, order, payload):
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO shipments(order_no,payload,updated_at) VALUES(?,?,?)
                   ON CONFLICT(order_no) DO UPDATE SET payload=excluded.payload,
                   updated_at=excluded.updated_at""",
                (order, json.dumps(payload, ensure_ascii=False), iso()),
            )

    def put_shipments(self, shipments):
        """Legacy bulk import. Existing rows are never replaced by stale snapshots."""
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for order, payload in shipments.items():
                connection.execute(
                    "INSERT OR IGNORE INTO shipments(order_no,payload,updated_at) VALUES(?,?,?)",
                    (order, json.dumps(payload, ensure_ascii=False), timestamp),
                )

    def mutate_shipment(self, order, mutator, create=False):
        """Read and write one shipment under the same SQLite write transaction."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM shipments WHERE order_no=?", (order,)
            ).fetchone()
            if not row and not create:
                return None
            current = json.loads(row["payload"]) if row else None
            payload, tasks = mutator(current)
            if payload is None:
                return current
            timestamp = iso()
            connection.execute(
                """INSERT INTO shipments(order_no,payload,updated_at) VALUES(?,?,?)
                   ON CONFLICT(order_no) DO UPDATE SET payload=excluded.payload,
                   updated_at=excluded.updated_at""",
                (order, json.dumps(payload, ensure_ascii=False), timestamp),
            )
            for task in tasks:
                connection.execute(
                    """INSERT OR IGNORE INTO tasks
                       (kind,dedupe_key,payload,next_attempt_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (task["kind"], task["dedupe_key"],
                     json.dumps(task["payload"], ensure_ascii=False),
                     timestamp, timestamp, timestamp),
                )
        return payload

    def patch_shipment(self, order, updates, append=None):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM shipments WHERE order_no=?", (order,)
            ).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            payload.update(updates)
            for field, values in (append or {}).items():
                payload.setdefault(field, []).extend(values)
            connection.execute(
                "UPDATE shipments SET payload=?,updated_at=? WHERE order_no=?",
                (json.dumps(payload, ensure_ascii=False), iso(), order),
            )
        return payload

    def get_document(self, name, default=None):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM documents WHERE name=?", (name,)
            ).fetchone()
        return json.loads(row["payload"]) if row else default

    def put_document(self, name, payload):
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO documents(name,payload,updated_at) VALUES(?,?,?)
                   ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,
                   updated_at=excluded.updated_at""",
                (name, json.dumps(payload, ensure_ascii=False), iso()),
            )

    def mutate_document(self, name, updater, default=None):
        """Atomically update one JSON document and return its new value."""
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM documents WHERE name=?", (name,)
            ).fetchone()
            current = json.loads(row["payload"]) if row else default
            updated = updater(current)
            connection.execute(
                """INSERT INTO documents(name,payload,updated_at) VALUES(?,?,?)
                   ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,
                   updated_at=excluded.updated_at""",
                (name, json.dumps(updated, ensure_ascii=False), timestamp),
            )
        return updated

    def record_message(self, channel_id, message_id, created_at, payload):
        timestamp = iso()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO incoming_messages
                   (channel_id,message_id,created_at,payload,received_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (channel_id, message_id, created_at,
                 json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
            )
        return cursor.rowcount == 1

    def has_message(self, channel_id, message_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM incoming_messages WHERE channel_id=? AND message_id=?",
                (channel_id, message_id),
            ).fetchone()
        return row is not None

    def pending_messages(self, channel_id, limit=500):
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT message_id,created_at,payload,attempts FROM incoming_messages
                   WHERE channel_id=? AND status IN ('pending','retry')
                   ORDER BY created_at,message_id LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
        return [
            {"id": row["message_id"], "created_at": row["created_at"],
             "payload": json.loads(row["payload"]), "attempts": row["attempts"]}
            for row in rows
        ]

    def complete_message(self, channel_id, message_id):
        with self.connect() as connection:
            connection.execute(
                """UPDATE incoming_messages SET status='succeeded',updated_at=?
                   WHERE channel_id=? AND message_id=?""",
                (iso(), channel_id, message_id),
            )

    def complete_message_with_task(self, channel_id, message_id, kind, dedupe_key, payload):
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO tasks
                   (kind,dedupe_key,payload,next_attempt_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (kind, dedupe_key, json.dumps(payload, ensure_ascii=False),
                 timestamp, timestamp, timestamp),
            )
            connection.execute(
                """UPDATE incoming_messages SET status='succeeded',updated_at=?
                   WHERE channel_id=? AND message_id=?""",
                (timestamp, channel_id, message_id),
            )

    def fail_message(self, channel_id, message_id, error, max_attempts=10):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM incoming_messages WHERE channel_id=? AND message_id=?",
                (channel_id, message_id),
            ).fetchone()
            if not row:
                raise KeyError(message_id)
            status = "dead" if row["attempts"] + 1 >= max_attempts else "retry"
            connection.execute(
                """UPDATE incoming_messages SET status=?,attempts=attempts+1,
                   last_error=?,updated_at=? WHERE channel_id=? AND message_id=?""",
                (status, error[:500], iso(), channel_id, message_id),
            )
        return status

    def enqueue_inbox(self, item_id, payload, now=None):
        timestamp = iso(now)
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO inbox
                   (id,payload,next_attempt_at,updated_at) VALUES(?,?,?,?)""",
                (item_id, json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
            )
        return cursor.rowcount == 1

    def get_inbox(self, statuses=None):
        sql = "SELECT * FROM inbox"
        params = []
        if statuses:
            sql += " WHERE status IN (%s)" % ",".join("?" for _ in statuses)
            params.extend(statuses)
        sql += " ORDER BY next_attempt_at,id"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def claim_inbox(self, worker, now=None, lease_seconds=300):
        return self._claim("inbox", worker, now, lease_seconds)

    def complete_inbox(self, item_id):
        with self.connect() as connection:
            connection.execute(
                "UPDATE inbox SET status='succeeded',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (iso(), item_id),
            )

    def complete_inbox_with_task(self, item_id, kind, dedupe_key, payload, orders=None):
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO tasks
                   (kind,dedupe_key,payload,next_attempt_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (kind, dedupe_key, json.dumps(payload, ensure_ascii=False),
                 timestamp, timestamp, timestamp),
            )
            row = connection.execute("SELECT payload FROM inbox WHERE id=?", (item_id,)).fetchone()
            inbox_payload = json.loads(row["payload"]) if row else {}
            associated = sorted({str(order) for order in (orders or []) if order})
            if associated:
                inbox_payload["orders"] = associated
                if len(associated) == 1:
                    inbox_payload["order"] = associated[0]
            connection.execute(
                """UPDATE inbox SET payload=?,status='succeeded',lease_owner=NULL,
                   lease_until=NULL,updated_at=? WHERE id=?""",
                (json.dumps(inbox_payload, ensure_ascii=False), timestamp, item_id),
            )

    def fail_inbox(self, item_id, error, max_attempts=7, now=None):
        return self._fail("inbox", item_id, error, max_attempts, now)

    def enqueue_task(self, kind, dedupe_key, payload, now=None):
        timestamp = iso(now)
        with self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO tasks
                   (kind,dedupe_key,payload,next_attempt_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (kind, dedupe_key, json.dumps(payload, ensure_ascii=False),
                 timestamp, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT id FROM tasks WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
        return row["id"]

    def claim_task(self, worker, now=None, lease_seconds=300, kind=None):
        return self._claim("tasks", worker, now, lease_seconds, kind)

    def pending_task_count(self, kind=None):
        where = "status IN ('pending','retry','running')"
        params = []
        if kind is not None:
            where += " AND kind=?"
            params.append(kind)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM tasks WHERE {where}", params
            ).fetchone()
        return row["count"]

    def task_counts(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT kind,status,COUNT(*) AS count FROM tasks GROUP BY kind,status"
            ).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row["kind"], {})[row["status"]] = row["count"]
        return result

    def list_tasks(self, statuses=None, limit=500, kinds=None):
        sql = "SELECT * FROM tasks"
        params = []
        clauses = []
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(kinds)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC,id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def act_on_task(self, task_id, action, operator, reason):
        if action not in ("retry", "claim", "resolve"):
            raise ValueError("unknown task action")
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            if row["lease_until"] and row["lease_until"] > timestamp:
                raise TaskConflict("task is being processed")
            payload = json.loads(row["payload"])
            if action == "retry":
                connection.execute(
                    """UPDATE tasks SET status='pending',attempts=0,next_attempt_at=?,
                       lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?""",
                    (timestamp, timestamp, task_id),
                )
                status = "pending"
            elif action == "claim":
                payload["owner"] = operator
                connection.execute("UPDATE tasks SET payload=?,updated_at=? WHERE id=?",
                                   (json.dumps(payload, ensure_ascii=False), timestamp, task_id))
                status = row["status"]
            else:
                connection.execute(
                    """UPDATE tasks SET status='succeeded',lease_owner=NULL,lease_until=NULL,
                       last_error=NULL,updated_at=? WHERE id=?""", (timestamp, task_id))
                status = "succeeded"
            order = str(payload.get("order") or "")
            if action == "resolve" and row["kind"] in ("notify_group", "notify_dm") and order:
                shipment_row = connection.execute(
                    "SELECT payload FROM shipments WHERE order_no=?", (order,)).fetchone()
                if shipment_row:
                    shipment = json.loads(shipment_row["payload"])
                    history = shipment.get("history") or [{}]
                    current_key = (history[-1].get("at") or history[-1].get("observed_at") or
                                   f"legacy:{shipment.get('status', '-')}:{int(shipment.get('binding_version') or 0)}")
                    if current_key == payload.get("event_key"):
                        if row["kind"] == "notify_group":
                            shipment.update({"needs_notify": False,
                                             "notified_status": payload.get("status")})
                        else:
                            shipment.update({"dm_notified_status": payload.get("status"),
                                             "dm_notified_event": payload.get("event_key")})
                        connection.execute(
                            "UPDATE shipments SET payload=?,updated_at=? WHERE order_no=?",
                            (json.dumps(shipment, ensure_ascii=False), timestamp, order),
                        )
            connection.execute(
                """INSERT INTO audit_log(order_no,entity_type,entity_id,action,operator,reason,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (order or None, "task", str(task_id), action, operator, reason, timestamp),
            )
        return {"id": task_id, "status": status, "action": action}

    def resolve_tasks(self, kind, order, reason):
        timestamp = iso()
        resolved = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id,payload FROM tasks WHERE kind=? AND status IN ('pending','retry','running')",
                (kind,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                if str(payload.get("order") or "") != order:
                    continue
                connection.execute(
                    """UPDATE tasks SET status='succeeded',lease_owner=NULL,lease_until=NULL,
                       last_error=NULL,updated_at=? WHERE id=?""", (timestamp, row["id"]))
                connection.execute(
                    """INSERT INTO audit_log(order_no,entity_type,entity_id,action,operator,reason,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (order, "task", str(row["id"]), "auto_resolve", "system", reason, timestamp),
                )
                resolved += 1
        return resolved

    def list_audit(self, order=None, limit=200):
        sql = "SELECT * FROM audit_log"
        params = []
        if order:
            sql += " WHERE order_no=?"
            params.append(order)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def record_audit(self, order, entity_type, entity_id, action, operator, reason):
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO audit_log(order_no,entity_type,entity_id,action,operator,reason,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (order or None, entity_type, str(entity_id), action, operator, reason, iso()),
            )

    def retry_inbox(self, item_id, operator, reason):
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload,status,lease_until FROM inbox WHERE id=?", (item_id,)
            ).fetchone()
            if not row:
                return None
            if row["lease_until"] and row["lease_until"] > timestamp:
                raise TaskConflict("inbox item is being processed")
            payload = json.loads(row["payload"])
            connection.execute(
                """UPDATE inbox SET status='pending',attempts=0,next_attempt_at=?,lease_owner=NULL,
                   lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?""",
                (timestamp, timestamp, item_id),
            )
            connection.execute(
                """INSERT INTO audit_log(order_no,entity_type,entity_id,action,operator,reason,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (payload.get("order"), "inbox", item_id, "retry", operator, reason, timestamp),
            )
        return {"id": item_id, "status": "pending"}

    def assign_salesperson(self, order, name, user_id, operator, reason):
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM shipments WHERE order_no=?", (order,)).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            payload.update({"salesperson": name, "salesperson_id": user_id})
            connection.execute("UPDATE shipments SET payload=?,updated_at=? WHERE order_no=?",
                (json.dumps(payload, ensure_ascii=False), timestamp, order))
            connection.execute(
                """INSERT INTO audit_log(order_no,entity_type,entity_id,action,operator,reason,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (order, "shipment", order, "assign_salesperson", operator, reason, timestamp),
            )
        return {"order": order, "salesperson": name, "salesperson_id": user_id}

    def evidence_for_order(self, order, limit=100):
        with self.connect() as connection:
            messages = connection.execute(
                """SELECT channel_id,message_id,created_at,status,last_error,payload
                   FROM incoming_messages AS m WHERE json_extract(m.payload,'$.order')=?
                   OR EXISTS (SELECT 1 FROM json_each(m.payload,'$.orders') WHERE value=?)
                   ORDER BY created_at DESC LIMIT ?""", (order, order, limit)
            ).fetchall()
            inbox = connection.execute(
                """SELECT id,status,last_error,updated_at,payload FROM inbox AS i
                   WHERE json_extract(i.payload,'$.order')=?
                   OR EXISTS (SELECT 1 FROM json_each(i.payload,'$.orders') WHERE value=?)
                   ORDER BY updated_at DESC LIMIT ?""", (order, order, limit)
            ).fetchall()
        message_items = []
        for row in messages:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            message_items.append(item)
        inbox_items = []
        for row in inbox:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            inbox_items.append(item)
        return {"messages": message_items, "inbox": inbox_items}

    def list_issues(self, statuses=("pending", "retry", "dead", "unknown"), limit=500):
        issues = []
        for task in self.list_tasks(statuses, limit):
            task.update({"source": "task", "source_id": str(task["id"])})
            issues.append(task)
        for item in self.get_inbox(statuses):
            item.update({"source": "inbox", "source_id": item["id"], "kind": "ocr"})
            issues.append(item)
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT channel_id,message_id AS id,payload,status,attempts,last_error,updated_at
                    FROM incoming_messages WHERE status IN ({placeholders})
                    ORDER BY updated_at DESC LIMIT ?""", (*statuses, limit)
            ).fetchall()
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            item.update({"source": "incoming_message",
                         "source_id": item["channel_id"] + ":" + item["id"],
                         "kind": "incoming_message"})
            issues.append(item)
        return sorted(issues, key=lambda item: item.get("updated_at", ""), reverse=True)[:limit]

    def complete_task(self, task_id):
        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status='succeeded',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (iso(), task_id),
            )

    def complete_notification(self, task_id, order, event_key, updates, receipt=""):
        """Commit a delivery receipt without acknowledging a newer shipment event."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM shipments WHERE order_no=?", (order,)
            ).fetchone()
            if row:
                payload = json.loads(row["payload"])
                history = payload.get("history") or [{}]
                current_key = (history[-1].get("at") or history[-1].get("observed_at") or
                               f"legacy:{payload.get('status', '-')}:{int(payload.get('binding_version') or 0)}")
                if current_key == event_key:
                    payload.update(updates)
                connection.execute(
                    "UPDATE shipments SET payload=?,updated_at=? WHERE order_no=?",
                    (json.dumps(payload, ensure_ascii=False), iso(), order),
                )
            task = connection.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            task_payload = json.loads(task["payload"]) if task else {}
            task_payload["receipt"] = str(receipt or "")[:500]
            task_payload["completed_at"] = iso()
            connection.execute(
                """UPDATE tasks SET status='succeeded',payload=?,lease_owner=NULL,
                   lease_until=NULL,updated_at=? WHERE id=?""",
                (json.dumps(task_payload, ensure_ascii=False), iso(), task_id),
            )

    def fail_task(self, task_id, error, max_attempts=5, now=None):
        return self._fail("tasks", task_id, error, max_attempts, now)

    def mark_task_unknown(self, task_id, error):
        with self.connect() as connection:
            connection.execute(
                """UPDATE tasks SET status='unknown',lease_owner=NULL,lease_until=NULL,
                   last_error=?,updated_at=? WHERE id=?""",
                (error[:500], iso(), task_id),
            )

    def mark_delivery_inflight(self, task_id):
        with self.connect() as connection:
            connection.execute(
                """UPDATE tasks SET status='unknown',
                   last_error='delivery started; receipt pending',updated_at=? WHERE id=?""",
                (iso(), task_id),
            )

    def _claim(self, table, worker, now, lease_seconds, kind=None):
        timestamp = iso(now)
        lease_until = iso((now or utcnow()) + timedelta(seconds=lease_seconds))
        where = "((status IN ('pending','retry') AND next_attempt_at<=?) OR (status='running' AND lease_until<=?))"
        params = [timestamp, timestamp]
        if kind is not None:
            where += " AND kind=?"
            params.append(kind)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY next_attempt_at,id LIMIT 1",
                params,
            ).fetchone()
            if not row:
                return None
            connection.execute(
                f"UPDATE {table} SET status='running',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE id=?",
                (worker, lease_until, timestamp, row["id"]),
            )
            row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone()
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _fail(self, table, item_id, error, max_attempts, now):
        value = now or utcnow()
        timestamp = iso(value)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT attempts FROM {table} WHERE id=?", (item_id,)
            ).fetchone()
            if not row:
                raise KeyError(item_id)
            state = "dead" if row["attempts"] >= max_attempts else "retry"
            delay = min(3600, 60 * (2 ** max(0, row["attempts"] - 1)))
            connection.execute(
                f"""UPDATE {table} SET status=?,next_attempt_at=?,lease_owner=NULL,
                    lease_until=NULL,last_error=?,updated_at=? WHERE id=?""",
                (state, iso(value + timedelta(seconds=delay)), error[:500], timestamp, item_id),
            )
        return state
