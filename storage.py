#!/usr/bin/env python
"""Transactional local state for logistics-track."""
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


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
                """
            )

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
        timestamp = iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for order, payload in shipments.items():
                connection.execute(
                    """INSERT INTO shipments(order_no,payload,updated_at) VALUES(?,?,?)
                       ON CONFLICT(order_no) DO UPDATE SET payload=excluded.payload,
                       updated_at=excluded.updated_at""",
                    (order, json.dumps(payload, ensure_ascii=False), timestamp),
                )

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

    def complete_task(self, task_id):
        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status='succeeded',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (iso(), task_id),
            )

    def fail_task(self, task_id, error, max_attempts=5, now=None):
        return self._fail("tasks", task_id, error, max_attempts, now)

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
