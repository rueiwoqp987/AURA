import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _json_or_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _stable_id(prefix: str, *parts) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}_{digest[:32]}"


class SQLiteStorage:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._batch_depth = 0
        self._dirty = False
        self.default_run_id = self._infer_run_id()
        self._init_pragmas()
        self._init_schema()

    def _infer_run_id(self) -> str:
        parent_name = self.db_path.parent.name
        if parent_name.startswith("AURA_"):
            return parent_name
        return "RUN_CURRENT"

    def begin_batch(self):
        self._batch_depth += 1

    def end_batch(self):
        if self._batch_depth > 0:
            self._batch_depth -= 1
        if self._batch_depth == 0:
            self.flush()

    def _mark_dirty(self):
        self._dirty = True
        if self._batch_depth == 0:
            self.flush()

    def flush(self):
        if self._dirty:
            self.conn.commit()
            self._dirty = False

    def _init_pragmas(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.executescript(
            """
            DROP VIEW IF EXISTS messages_compact;
            DROP VIEW IF EXISTS attachment_artifacts;
            DROP VIEW IF EXISTS attachment_attempt_summary;
            DROP VIEW IF EXISTS message_attachment_summary;
            DROP VIEW IF EXISTS audit_review_events;

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                target_app TEXT,
                device_serial TEXT,
                aura_version TEXT,
                started_ts REAL,
                created_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collection_contexts (
                context_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                app TEXT NOT NULL,
                phase TEXT NOT NULL,
                account TEXT NOT NULL,
                created_ts REAL NOT NULL,
                UNIQUE (run_id, app, phase, account),
                FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS contacts (
                contact_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                contact_name TEXT NOT NULL,
                presence_text TEXT,
                created_ts REAL NOT NULL,
                UNIQUE (context_id, contact_name),
                FOREIGN KEY (context_id) REFERENCES collection_contexts (context_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chatrooms (
                chatroom_id TEXT PRIMARY KEY,
                app TEXT NOT NULL,
                logical_chatroom_id TEXT,
                chat_name TEXT,
                chat_type TEXT,
                peer_user_id TEXT,
                peer_mobile TEXT,
                ambiguous_deleted_account INTEGER NOT NULL DEFAULT 0,
                identity_status TEXT,
                dedup_applied INTEGER NOT NULL DEFAULT 1,
                artifacts_json TEXT,
                created_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chatroom_observations (
                chatroom_observation_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                chatroom_id TEXT NOT NULL,
                observed_chat_id TEXT NOT NULL,
                display_name TEXT,
                chat_type TEXT,
                raw_text TEXT,
                bounds TEXT,
                source_artifact_id TEXT,
                source_audit_seq INTEGER,
                created_ts REAL NOT NULL,
                UNIQUE (context_id, observed_chat_id),
                FOREIGN KEY (context_id) REFERENCES collection_contexts (context_id) ON DELETE CASCADE,
                FOREIGN KEY (chatroom_id) REFERENCES chatrooms (chatroom_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                message_pk TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                chatroom_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                message_type TEXT,
                sender TEXT,
                direction TEXT,
                status TEXT,
                timestamp TEXT,
                content TEXT,
                raw TEXT,
                created_ts REAL NOT NULL,
                UNIQUE (context_id, chatroom_id, record_id),
                FOREIGN KEY (context_id) REFERENCES collection_contexts (context_id) ON DELETE CASCADE,
                FOREIGN KEY (chatroom_id) REFERENCES chatrooms (chatroom_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS message_observations (
                message_observation_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL,
                chatroom_id TEXT NOT NULL,
                message_pk TEXT NOT NULL,
                observation_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                page_index INTEGER,
                page_row_index INTEGER,
                bounds TEXT,
                raw_text TEXT,
                raw_source TEXT,
                identity_status TEXT,
                dedup_policy TEXT,
                download_gate_key TEXT,
                policy_source TEXT,
                policy_version TEXT,
                screenshot_artifact_ids TEXT,
                uitree_artifact_ids TEXT,
                created_ts REAL NOT NULL,
                UNIQUE (context_id, chatroom_id, observation_id),
                FOREIGN KEY (context_id) REFERENCES collection_contexts (context_id) ON DELETE CASCADE,
                FOREIGN KEY (chatroom_id) REFERENCES chatrooms (chatroom_id) ON DELETE CASCADE,
                FOREIGN KEY (message_pk) REFERENCES messages (message_pk) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                context_id TEXT,
                app TEXT NOT NULL,
                phase TEXT,
                account TEXT,
                chatroom_id TEXT,
                observed_chat_id TEXT,
                message_pk TEXT,
                record_id TEXT,
                message_id TEXT,
                observation_id TEXT,
                artifact_path TEXT NOT NULL,
                artifact_kind TEXT,
                message_type TEXT,
                file_name TEXT,
                display_filename TEXT,
                device_path TEXT,
                device_basename TEXT,
                collected_path TEXT,
                download_detection_method TEXT,
                download_action_started_at REAL,
                device_file_size INTEGER,
                device_file_mtime INTEGER,
                sha256 TEXT,
                content_group_id TEXT,
                size_bytes INTEGER,
                identity_status TEXT,
                dedup_policy TEXT,
                download_gate_key TEXT,
                policy_source TEXT,
                policy_version TEXT,
                source_action TEXT,
                source_screen TEXT,
                created_ts REAL,
                UNIQUE (run_id, artifact_path),
                FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
                FOREIGN KEY (context_id) REFERENCES collection_contexts (context_id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ts REAL,
                app TEXT,
                phase TEXT,
                account TEXT,
                chat_id TEXT,
                source_class TEXT,
                source_func TEXT,
                action TEXT NOT NULL,
                selector TEXT,
                result TEXT,
                error TEXT,
                artifacts_json TEXT,
                side_effect_hint TEXT,
                PRIMARY KEY (run_id, seq)
            );

            CREATE TABLE IF NOT EXISTS artifact_action_context_links (
                link_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                artifact_id TEXT,
                audit_seq INTEGER,
                context_id TEXT,
                chatroom_id TEXT,
                observed_chat_id TEXT,
                message_pk TEXT,
                record_id TEXT,
                message_id TEXT,
                observation_id TEXT,
                link_type TEXT NOT NULL,
                source_action TEXT,
                source_screen TEXT,
                created_ts REAL NOT NULL,
                FOREIGN KEY (artifact_id) REFERENCES artifacts (artifact_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS derived_record_links (
                run_id TEXT NOT NULL,
                derived_table TEXT NOT NULL,
                derived_record_id TEXT NOT NULL,
                source_artifact_id TEXT,
                source_audit_seq INTEGER,
                derivation_type TEXT NOT NULL,
                confidence REAL,
                notes TEXT,
                created_ts REAL NOT NULL,
                PRIMARY KEY (run_id, derived_table, derived_record_id, derivation_type)
            );

            CREATE TABLE IF NOT EXISTS acquisition_attempts (
                run_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                app TEXT NOT NULL,
                context_id TEXT,
                phase TEXT,
                account TEXT,
                chatroom_id TEXT,
                observed_chat_id TEXT,
                record_id TEXT,
                message_id TEXT,
                observation_id TEXT,
                method TEXT,
                primitive TEXT,
                route_id TEXT,
                target_type TEXT,
                target_label TEXT,
                message_type TEXT,
                display_filename TEXT,
                status TEXT NOT NULL,
                failure_reason TEXT,
                error TEXT,
                ui_reached INTEGER,
                artifact_materialized INTEGER,
                started_audit_seq INTEGER,
                ended_audit_seq INTEGER,
                started_ts REAL,
                ended_ts REAL,
                duration_sec REAL,
                bounds TEXT,
                snapshot_dirs TEXT,
                download_detection_method TEXT,
                artifact_ids TEXT,
                artifact_paths TEXT,
                sha256_list TEXT,
                device_paths TEXT,
                screenshot_artifact_id TEXT,
                uitree_artifact_id TEXT,
                screenshot_path TEXT,
                uitree_path TEXT,
                identity_status TEXT,
                dedup_policy TEXT,
                download_gate_key TEXT,
                policy_source TEXT,
                policy_version TEXT,
                created_ts REAL,
                PRIMARY KEY (run_id, attempt_id)
            );
            """
        )
        self._ensure_run(self.default_run_id)
        self._init_views(cur)
        self._mark_dirty()
        cur.close()

    def _init_views(self, cur):
        cur.executescript(
            """
            CREATE VIEW messages_compact AS
            SELECT
                cc.phase,
                cc.account,
                cr.chat_name,
                cr.chat_type,
                co.observed_chat_id AS chat_id,
                m.record_id,
                m.message_id,
                m.message_type,
                m.sender,
                m.direction,
                m.status,
                m.timestamp,
                m.content,
                m.raw
            FROM messages m
            JOIN collection_contexts cc ON cc.context_id = m.context_id
            JOIN chatrooms cr ON cr.chatroom_id = m.chatroom_id
            LEFT JOIN chatroom_observations co
                ON co.context_id = m.context_id
                AND co.chatroom_id = m.chatroom_id;

            CREATE VIEW attachment_artifacts AS
            SELECT
                phase,
                account,
                observed_chat_id AS chat_id,
                record_id,
                message_id,
                observation_id,
                message_type,
                artifact_kind,
                file_name,
                display_filename,
                device_path,
                device_basename,
                collected_path,
                artifact_path,
                size_bytes,
                device_file_size,
                sha256,
                content_group_id,
                download_detection_method,
                identity_status,
                dedup_policy,
                download_gate_key,
                created_ts
            FROM artifacts
            WHERE artifact_kind LIKE 'attachment_%';

            CREATE VIEW attachment_attempt_summary AS
            SELECT
                aa.phase,
                aa.account,
                cr.chat_name,
                cr.chat_type,
                aa.observed_chat_id AS chat_id,
                aa.record_id,
                aa.message_id,
                aa.observation_id,
                aa.message_type,
                aa.display_filename,
                aa.status,
                aa.failure_reason,
                aa.duration_sec,
                aa.artifact_paths,
                aa.sha256_list,
                aa.device_paths,
                aa.screenshot_path,
                aa.uitree_path,
                aa.download_detection_method,
                aa.identity_status,
                aa.dedup_policy,
                aa.download_gate_key,
                aa.started_ts,
                aa.ended_ts
            FROM acquisition_attempts aa
            LEFT JOIN chatrooms cr ON cr.chatroom_id = aa.chatroom_id;

            CREATE VIEW message_attachment_summary AS
            SELECT
                cc.phase,
                cc.account,
                cr.chat_name,
                cr.chat_type,
                co.observed_chat_id AS chat_id,
                m.record_id,
                m.message_type,
                m.timestamp,
                m.content,
                COUNT(DISTINCT aa.attempt_id) AS attempt_count,
                COUNT(DISTINCT CASE WHEN aa.status = 'success' THEN aa.attempt_id END) AS success_count,
                COUNT(DISTINCT CASE WHEN aa.status != 'success' THEN aa.attempt_id END) AS failure_count,
                (
                    SELECT aa2.status
                    FROM acquisition_attempts aa2
                    WHERE aa2.context_id = m.context_id
                      AND aa2.chatroom_id = m.chatroom_id
                      AND aa2.record_id = m.record_id
                    ORDER BY COALESCE(aa2.ended_ts, aa2.started_ts, aa2.created_ts) DESC
                    LIMIT 1
                ) AS last_attempt_status,
                (
                    SELECT aa2.failure_reason
                    FROM acquisition_attempts aa2
                    WHERE aa2.context_id = m.context_id
                      AND aa2.chatroom_id = m.chatroom_id
                      AND aa2.record_id = m.record_id
                    ORDER BY COALESCE(aa2.ended_ts, aa2.started_ts, aa2.created_ts) DESC
                    LIMIT 1
                ) AS last_failure_reason,
                COUNT(DISTINCT a.artifact_id) AS artifact_count,
                CASE WHEN COUNT(DISTINCT a.artifact_id) > 0 THEN 1 ELSE 0 END AS has_collected_file,
                GROUP_CONCAT(DISTINCT a.file_name) AS file_names,
                GROUP_CONCAT(DISTINCT a.sha256) AS sha256_list,
                GROUP_CONCAT(DISTINCT a.collected_path) AS collected_paths,
                GROUP_CONCAT(DISTINCT a.device_path) AS device_paths
            FROM messages m
            JOIN collection_contexts cc ON cc.context_id = m.context_id
            JOIN chatrooms cr ON cr.chatroom_id = m.chatroom_id
            LEFT JOIN chatroom_observations co
                ON co.context_id = m.context_id
                AND co.chatroom_id = m.chatroom_id
            LEFT JOIN artifacts a
                ON a.context_id = m.context_id
                AND a.chatroom_id = m.chatroom_id
                AND a.record_id = m.record_id
                AND a.artifact_kind LIKE 'attachment_%'
            LEFT JOIN acquisition_attempts aa
                ON aa.context_id = m.context_id
                AND aa.chatroom_id = m.chatroom_id
                AND aa.record_id = m.record_id
            WHERE m.message_type IN ('Photo', 'Video', 'File')
            GROUP BY
                cc.phase,
                cc.account,
                cr.chat_name,
                cr.chat_type,
                co.observed_chat_id,
                m.record_id,
                m.message_type,
                m.timestamp,
                m.content;

            CREATE VIEW audit_review_events AS
            SELECT
                ae.run_id,
                ae.seq,
                ae.ts,
                ae.phase,
                ae.account,
                ae.chat_id,
                ae.action,
                ae.selector,
                ae.result,
                ae.error,
                ae.artifacts_json
            FROM audit_events ae
            ORDER BY ae.seq;
            """
        )

    def _ensure_run(self, run_id: str, **metadata):
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO runs (run_id, target_app, device_serial, aura_version, started_ts, created_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                target_app = COALESCE(excluded.target_app, runs.target_app),
                device_serial = COALESCE(excluded.device_serial, runs.device_serial),
                aura_version = COALESCE(excluded.aura_version, runs.aura_version),
                started_ts = COALESCE(excluded.started_ts, runs.started_ts)
            """,
            (
                run_id,
                metadata.get("target_app"),
                metadata.get("device_serial"),
                metadata.get("aura_version"),
                metadata.get("started_ts"),
                time.time(),
            ),
        )
        cur.close()

    def _context_id(self, app: str, phase: str | None, account: str | None, run_id: str | None = None) -> str:
        rid = run_id or self.default_run_id
        app_value = app or "unknown"
        phase_value = phase or ""
        account_value = account or ""
        context_id = _stable_id("ctx", rid, app_value, phase_value, account_value)
        self._ensure_run(rid)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO collection_contexts (context_id, run_id, app, phase, account, created_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, app, phase, account) DO UPDATE SET
                context_id = excluded.context_id
            """,
            (context_id, rid, app_value, phase_value, account_value, time.time()),
        )
        cur.close()
        return context_id

    def _chatroom_id(self, app: str, chat_id: str | None, logical_chatroom_id: str | None = None) -> str:
        logical_id = logical_chatroom_id or chat_id or "unknown_chat"
        return _stable_id("chat", app or "unknown", logical_id)

    def _message_pk(self, context_id: str, chatroom_id: str, record_id: str) -> str:
        return _stable_id("msg", context_id, chatroom_id, record_id)

    def _upsert_chatroom_entity(self, app: str, context_id: str, observed_chat_id: str, room: Mapping):
        logical_chatroom_id = room.get("logical_chatroom_id", observed_chat_id)
        chatroom_id = self._chatroom_id(app, observed_chat_id, logical_chatroom_id)
        chat_name = room.get("name") or room.get("chat_name")
        chat_type = room.get("type") or room.get("chat_type")
        artifacts = json.dumps(room.get("artifacts", []), ensure_ascii=False)
        ambiguous_deleted_account = 1 if room.get("ambiguous_deleted_account") else 0
        dedup_applied = 1 if room.get("dedup_applied", not room.get("ambiguous_deleted_account")) else 0
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO chatrooms (
                chatroom_id, app, logical_chatroom_id, chat_name, chat_type, peer_user_id, peer_mobile,
                ambiguous_deleted_account, identity_status, dedup_applied, artifacts_json, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chatroom_id) DO UPDATE SET
                logical_chatroom_id = excluded.logical_chatroom_id,
                chat_name = excluded.chat_name,
                chat_type = excluded.chat_type,
                peer_user_id = excluded.peer_user_id,
                peer_mobile = excluded.peer_mobile,
                ambiguous_deleted_account = excluded.ambiguous_deleted_account,
                identity_status = excluded.identity_status,
                dedup_applied = excluded.dedup_applied,
                artifacts_json = excluded.artifacts_json
            """,
            (
                chatroom_id,
                app,
                logical_chatroom_id,
                chat_name,
                chat_type,
                room.get("user_id") or room.get("peer_user_id"),
                room.get("mobile") or room.get("peer_mobile"),
                ambiguous_deleted_account,
                room.get("identity_status"),
                dedup_applied,
                artifacts,
                time.time(),
            ),
        )
        observation_id = _stable_id("chatobs", context_id, observed_chat_id)
        cur.execute(
            """
            INSERT INTO chatroom_observations (
                chatroom_observation_id, context_id, chatroom_id, observed_chat_id, display_name,
                chat_type, raw_text, bounds, source_artifact_id, source_audit_seq, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_id, observed_chat_id) DO UPDATE SET
                chatroom_id = excluded.chatroom_id,
                display_name = excluded.display_name,
                chat_type = excluded.chat_type,
                raw_text = excluded.raw_text,
                bounds = excluded.bounds,
                source_artifact_id = excluded.source_artifact_id,
                source_audit_seq = excluded.source_audit_seq
            """,
            (
                observation_id,
                context_id,
                chatroom_id,
                observed_chat_id,
                chat_name,
                chat_type,
                room.get("raw_text") or room.get("raw"),
                _json_or_value(room.get("bounds")),
                room.get("source_artifact_id"),
                room.get("source_audit_seq"),
                time.time(),
            ),
        )
        cur.close()
        return chatroom_id

    def _find_chatroom_id(self, app: str, context_id: str, observed_chat_id: str) -> str:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT chatroom_id
            FROM chatroom_observations
            WHERE context_id = ? AND observed_chat_id = ?
            """,
            (context_id, observed_chat_id),
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return row[0]
        return self._upsert_chatroom_entity(app, context_id, observed_chat_id, {"chat_id": observed_chat_id})

    def upsert_contacts(self, app: str, account: str, contacts: Sequence[str | Mapping[str, Any]], phase: str = ""):
        context_id = self._context_id(app, phase, account)
        rows = []
        for contact in contacts:
            if isinstance(contact, Mapping):
                name = contact.get("name")
                presence_text = contact.get("presence_text")
            else:
                name = contact
                presence_text = None
            if not name:
                continue
            rows.append((_stable_id("contact", context_id, name), context_id, name, presence_text, time.time()))
        if not rows:
            return
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT INTO contacts (contact_id, context_id, contact_name, presence_text, created_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(context_id, contact_name) DO UPDATE SET
                presence_text = excluded.presence_text
            """,
            rows,
        )
        self._mark_dirty()
        cur.close()

    def upsert_chatrooms(self, app: str, account: str, chatrooms: Iterable[Mapping], phase: str = ""):
        context_id = self._context_id(app, phase, account)
        for room in chatrooms:
            observed_chat_id = room.get("chat_id")
            if observed_chat_id is None:
                continue
            self._upsert_chatroom_entity(app, context_id, str(observed_chat_id), room)
        self._mark_dirty()

    def upsert_messages(
        self,
        app: str,
        account: str,
        chat_id: str,
        messages: Iterable[Mapping],
        phase: str = "",
        chat_name: str | None = None,
        chat_type: str | None = None,
        logical_chatroom_id: str | None = None,
        ambiguous_deleted_account: bool = False,
        identity_status: str | None = None,
        dedup_applied: bool = True,
    ):
        context_id = self._context_id(app, phase, account)
        chatroom_id = self._find_chatroom_id(app, context_id, chat_id)
        if chat_name or chat_type or logical_chatroom_id:
            chatroom_id = self._upsert_chatroom_entity(
                app,
                context_id,
                chat_id,
                {
                    "chat_id": chat_id,
                    "logical_chatroom_id": logical_chatroom_id or chat_id,
                    "name": chat_name,
                    "type": chat_type,
                    "ambiguous_deleted_account": ambiguous_deleted_account,
                    "identity_status": identity_status,
                    "dedup_applied": dedup_applied,
                },
            )

        message_rows = []
        observation_rows = []
        now = time.time()
        for msg in messages:
            message_id = msg.get("message_id")
            if not message_id:
                continue
            content = msg.get("content")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            record_id = msg.get("record_id", message_id)
            observation_id = msg.get("observation_id") or f"{record_id}:primary"
            bounds = _json_or_value(msg.get("bounds"))
            message_pk = self._message_pk(context_id, chatroom_id, record_id)
            message_rows.append(
                (
                    message_pk,
                    context_id,
                    chatroom_id,
                    record_id,
                    message_id,
                    msg.get("type") or msg.get("message_type"),
                    msg.get("sender"),
                    msg.get("direction"),
                    msg.get("status"),
                    msg.get("timestamp"),
                    content,
                    msg.get("raw"),
                    now,
                )
            )
            observation_rows.append(
                (
                    _stable_id("msgobs", context_id, chatroom_id, observation_id),
                    context_id,
                    chatroom_id,
                    message_pk,
                    observation_id,
                    record_id,
                    message_id,
                    msg.get("page_index"),
                    msg.get("page_row_index"),
                    bounds,
                    msg.get("raw"),
                    msg.get("raw_source", ""),
                    msg.get("identity_status", identity_status or "strong"),
                    msg.get("dedup_policy", "logical"),
                    msg.get("download_gate_key", message_id),
                    msg.get("policy_source", ""),
                    msg.get("policy_version", ""),
                    json.dumps(msg.get("screenshot_artifact_ids", msg.get("screenshot_paths", [])), ensure_ascii=False),
                    json.dumps(msg.get("uitree_artifact_ids", msg.get("uitree_paths", [])), ensure_ascii=False),
                    now,
                )
            )

        if not message_rows:
            return

        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT INTO messages (
                message_pk, context_id, chatroom_id, record_id, message_id, message_type,
                sender, direction, status, timestamp, content, raw, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_id, chatroom_id, record_id) DO UPDATE SET
                message_id = excluded.message_id,
                message_type = excluded.message_type,
                sender = excluded.sender,
                direction = excluded.direction,
                status = excluded.status,
                timestamp = excluded.timestamp,
                content = excluded.content,
                raw = excluded.raw
            """,
            message_rows,
        )
        cur.executemany(
            """
            INSERT INTO message_observations (
                message_observation_id, context_id, chatroom_id, message_pk, observation_id, record_id,
                message_id, page_index, page_row_index, bounds, raw_text, raw_source, identity_status,
                dedup_policy, download_gate_key, policy_source, policy_version, screenshot_artifact_ids,
                uitree_artifact_ids, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_id, chatroom_id, observation_id) DO UPDATE SET
                message_pk = excluded.message_pk,
                record_id = excluded.record_id,
                message_id = excluded.message_id,
                page_index = excluded.page_index,
                page_row_index = excluded.page_row_index,
                bounds = excluded.bounds,
                raw_text = excluded.raw_text,
                raw_source = excluded.raw_source,
                identity_status = excluded.identity_status,
                dedup_policy = excluded.dedup_policy,
                download_gate_key = excluded.download_gate_key,
                policy_source = excluded.policy_source,
                policy_version = excluded.policy_version,
                screenshot_artifact_ids = excluded.screenshot_artifact_ids,
                uitree_artifact_ids = excluded.uitree_artifact_ids
            """,
            observation_rows,
        )
        self._mark_dirty()
        cur.close()

    def upsert_file_artifact(self, payload: Mapping):
        run_id = payload.get("run_id") or self.default_run_id
        app = payload.get("app") or "unknown"
        phase = payload.get("phase") or ""
        account = payload.get("account") or ""
        context_id = self._context_id(app, phase, account, run_id=run_id)
        observed_chat_id = payload.get("chat_id")
        chatroom_id = None
        if observed_chat_id:
            chatroom_id = self._find_chatroom_id(app, context_id, str(observed_chat_id))
        record_id = payload.get("record_id")
        message_pk = None
        if chatroom_id and record_id:
            message_pk = self._message_pk(context_id, chatroom_id, record_id)
        artifact_path = payload.get("artifact_path")
        artifact_id = payload.get("artifact_id") or f"{run_id}:{artifact_path}"
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO artifacts (
                artifact_id, run_id, context_id, app, phase, account, chatroom_id, observed_chat_id,
                message_pk, record_id, message_id, observation_id, artifact_path, artifact_kind, message_type,
                file_name, display_filename, device_path, device_basename, collected_path,
                download_detection_method, download_action_started_at, device_file_size, device_file_mtime,
                sha256, content_group_id, size_bytes, identity_status, dedup_policy, download_gate_key,
                policy_source, policy_version, source_action, source_screen, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, artifact_path) DO UPDATE SET
                context_id = excluded.context_id,
                app = excluded.app,
                phase = excluded.phase,
                account = excluded.account,
                chatroom_id = excluded.chatroom_id,
                observed_chat_id = excluded.observed_chat_id,
                message_pk = excluded.message_pk,
                record_id = excluded.record_id,
                message_id = excluded.message_id,
                observation_id = excluded.observation_id,
                artifact_kind = excluded.artifact_kind,
                message_type = excluded.message_type,
                file_name = excluded.file_name,
                display_filename = excluded.display_filename,
                device_path = excluded.device_path,
                device_basename = excluded.device_basename,
                collected_path = excluded.collected_path,
                download_detection_method = excluded.download_detection_method,
                download_action_started_at = excluded.download_action_started_at,
                device_file_size = excluded.device_file_size,
                device_file_mtime = excluded.device_file_mtime,
                sha256 = COALESCE(excluded.sha256, artifacts.sha256),
                content_group_id = COALESCE(excluded.content_group_id, artifacts.content_group_id),
                size_bytes = excluded.size_bytes,
                identity_status = excluded.identity_status,
                dedup_policy = excluded.dedup_policy,
                download_gate_key = excluded.download_gate_key,
                policy_source = excluded.policy_source,
                policy_version = excluded.policy_version,
                source_action = excluded.source_action,
                source_screen = excluded.source_screen,
                created_ts = excluded.created_ts
            """,
            (
                artifact_id,
                run_id,
                context_id,
                app,
                phase,
                account,
                chatroom_id,
                observed_chat_id,
                message_pk,
                record_id,
                payload.get("message_id"),
                payload.get("observation_id"),
                artifact_path,
                payload.get("artifact_kind"),
                payload.get("message_type"),
                payload.get("file_name"),
                payload.get("display_filename"),
                payload.get("device_path"),
                payload.get("device_basename"),
                payload.get("collected_path"),
                payload.get("download_detection_method"),
                payload.get("download_action_started_at"),
                payload.get("device_file_size"),
                payload.get("device_file_mtime"),
                payload.get("sha256"),
                payload.get("content_group_id"),
                payload.get("size_bytes"),
                payload.get("identity_status"),
                payload.get("dedup_policy"),
                payload.get("download_gate_key"),
                payload.get("policy_source"),
                payload.get("policy_version"),
                payload.get("source_action"),
                payload.get("source_screen"),
                payload.get("created_ts") or time.time(),
            ),
        )
        source_action = payload.get("source_action")
        source_screen = payload.get("source_screen")
        if source_action or source_screen:
            link_id = _stable_id(
                "aac",
                run_id,
                artifact_id,
                context_id,
                chatroom_id,
                message_pk,
                payload.get("observation_id"),
                source_action,
                source_screen,
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO artifact_action_context_links (
                    link_id, run_id, artifact_id, audit_seq, context_id, chatroom_id, observed_chat_id,
                    message_pk, record_id, message_id, observation_id, link_type, source_action,
                    source_screen, created_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    run_id,
                    artifact_id,
                    payload.get("source_audit_seq"),
                    context_id,
                    chatroom_id,
                    observed_chat_id,
                    message_pk,
                    record_id,
                    payload.get("message_id"),
                    payload.get("observation_id"),
                    "artifact--action--context",
                    source_action,
                    source_screen,
                    time.time(),
                ),
            )
        self._mark_dirty()
        cur.close()

    def upsert_attachment_attempt(self, payload: Mapping):
        run_id = payload.get("run_id") or self.default_run_id
        app = payload.get("app") or "unknown"
        phase = payload.get("phase") or ""
        account = payload.get("account") or ""
        context_id = self._context_id(app, phase, account, run_id=run_id)
        observed_chat_id = payload.get("chat_id")
        chatroom_id = None
        if observed_chat_id:
            chatroom_id = self._find_chatroom_id(app, context_id, str(observed_chat_id))
        artifact_ids = payload.get("artifact_ids")
        if artifact_ids is None and payload.get("artifact_paths"):
            paths = payload.get("artifact_paths")
            if isinstance(paths, str):
                try:
                    paths = json.loads(paths)
                except Exception:
                    paths = [paths]
            artifact_ids = [f"{run_id}:{p}" for p in paths]
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO acquisition_attempts (
                run_id, attempt_id, app, context_id, phase, account, chatroom_id, observed_chat_id,
                record_id, message_id, observation_id, method, primitive, route_id, target_type, target_label,
                message_type, display_filename, status, failure_reason, error, ui_reached,
                artifact_materialized, started_audit_seq, ended_audit_seq, started_ts, ended_ts, duration_sec,
                bounds, snapshot_dirs, download_detection_method, artifact_ids, artifact_paths, sha256_list,
                device_paths, screenshot_artifact_id, uitree_artifact_id, screenshot_path, uitree_path,
                identity_status, dedup_policy, download_gate_key, policy_source, policy_version, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                payload.get("attempt_id"),
                app,
                context_id,
                phase,
                account,
                chatroom_id,
                observed_chat_id,
                payload.get("record_id"),
                payload.get("message_id"),
                payload.get("observation_id"),
                payload.get("method"),
                payload.get("primitive"),
                payload.get("route_id"),
                payload.get("target_type") or "attachment",
                payload.get("target_label") or payload.get("display_filename"),
                payload.get("message_type"),
                payload.get("display_filename"),
                payload.get("status"),
                payload.get("failure_reason"),
                str(payload.get("error")) if payload.get("error") is not None else None,
                payload.get("ui_reached"),
                payload.get("artifact_materialized"),
                payload.get("started_audit_seq"),
                payload.get("ended_audit_seq"),
                payload.get("started_ts"),
                payload.get("ended_ts"),
                payload.get("duration_sec"),
                _json_or_value(payload.get("bounds")),
                _json_or_value(payload.get("snapshot_dirs")),
                payload.get("download_detection_method"),
                _json_or_value(artifact_ids),
                _json_or_value(payload.get("artifact_paths")),
                _json_or_value(payload.get("sha256_list")),
                _json_or_value(payload.get("device_paths")),
                payload.get("screenshot_artifact_id"),
                payload.get("uitree_artifact_id"),
                payload.get("screenshot_path"),
                payload.get("uitree_path"),
                payload.get("identity_status"),
                payload.get("dedup_policy"),
                payload.get("download_gate_key"),
                payload.get("policy_source"),
                payload.get("policy_version"),
                payload.get("created_ts") or time.time(),
            ),
        )
        self._mark_dirty()
        cur.close()

    def update_file_artifact_hash(self, run_id: str, artifact_path: str, sha256: str, size_bytes: int | None = None):
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE artifacts
            SET sha256 = ?, content_group_id = ?, size_bytes = COALESCE(?, size_bytes)
            WHERE run_id = ? AND artifact_path = ?
            """,
            (sha256, sha256, size_bytes, run_id, artifact_path),
        )
        self._mark_dirty()
        cur.close()

    def close(self):
        try:
            self.flush()
            self.conn.close()
        except Exception:
            pass
