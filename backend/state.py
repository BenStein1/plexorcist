from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.models import ChatMessage, ConversationState, Intent


class ConversationStore:
    def __init__(self, database_url: str) -> None:
        self.db_path = self._resolve_path(database_url)
        self._init_db()

    def _resolve_path(self, database_url: str) -> Path:
        if database_url.startswith("sqlite:///"):
            return Path(database_url.removeprefix("sqlite:///"))
        return Path("plexorcist.db")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    pending_confirmation TEXT,
                    candidate_media_json TEXT NOT NULL,
                    last_issue_key TEXT,
                    support_context_json TEXT NOT NULL,
                    last_tool_actions_json TEXT NOT NULL,
                    escalation_history_json TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    memory_compacted_at TEXT,
                    memory_compaction_status TEXT,
                    memory_compaction_attempts INTEGER NOT NULL DEFAULT 0,
                    memory_compaction_last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory_profile (
                    user_id TEXT PRIMARY KEY,
                    rolling_summary TEXT NOT NULL,
                    preferences_json TEXT NOT NULL,
                    familiarity_notes_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_conversation_compaction_columns(conn)

    def _ensure_conversation_compaction_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(conversations)").fetchall()
        columns = {str(row[1]) for row in rows}
        if "memory_compacted_at" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN memory_compacted_at TEXT")
        if "memory_compaction_status" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN memory_compaction_status TEXT")
        if "memory_compaction_attempts" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN memory_compaction_attempts INTEGER NOT NULL DEFAULT 0")
        if "memory_compaction_last_error" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN memory_compaction_last_error TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    note_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memory_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    source_span_start TEXT,
                    source_span_end TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    def get_or_create(self, user_id: str, conversation_id: str | None = None) -> ConversationState:
        if conversation_id:
            state = self.get(conversation_id)
            if state is not None:
                return state

        state = ConversationState(user_id=user_id)
        if conversation_id:
            state.conversation_id = conversation_id
        self.save(state)
        return state

    def get(self, conversation_id: str) -> ConversationState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()

        if row is None:
            return None

        import json

        return ConversationState(
            conversation_id=row[0],
            user_id=row[1],
            intent=Intent(row[2]),
            pending_confirmation=row[3],
            candidate_media=json.loads(row[4]),
            last_issue_key=row[5],
            support_context=json.loads(row[6]),
            last_tool_actions=json.loads(row[7]),
            escalation_history=json.loads(row[8]),
            messages=[ChatMessage.model_validate(item) for item in json.loads(row[9])],
            updated_at=datetime.fromisoformat(row[10]),
        )

    def list_stale_conversations(
        self,
        user_id: str,
        *,
        older_than: datetime,
        exclude_conversation_id: str | None = None,
        limit: int = 10,
    ) -> list[ConversationState]:
        sql = (
            "SELECT conversation_id FROM conversations "
            "WHERE user_id = ? AND updated_at < ? AND messages_json != '[]' "
            "AND memory_compacted_at IS NULL "
        )
        params: list[Any] = [user_id, older_than.isoformat()]
        if exclude_conversation_id:
            sql += "AND conversation_id != ? "
            params.append(exclude_conversation_id)
        # Only consider the immediate prior conversation for this user.
        sql += (
            "AND conversation_id = ("
            "  SELECT c2.conversation_id FROM conversations c2 "
            "  WHERE c2.user_id = conversations.user_id "
            "  ORDER BY c2.updated_at DESC LIMIT 1 OFFSET 1"
            ") "
        )
        sql += "ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        states: list[ConversationState] = []
        for row in rows:
            conversation_id = str(row[0] or "")
            if not conversation_id:
                continue
            state = self.get(conversation_id)
            if state is None or not state.messages:
                continue
            states.append(state)
        return states

    def list_global_stale_uncompacted_conversations(
        self,
        *,
        older_than: datetime,
        limit: int,
    ) -> list[ConversationState]:
        sql = (
            "SELECT conversation_id FROM conversations "
            "WHERE updated_at < ? AND messages_json != '[]' "
            "AND memory_compacted_at IS NULL "
            "AND conversation_id != ("
            "  SELECT c2.conversation_id FROM conversations c2 "
            "  WHERE c2.user_id = conversations.user_id "
            "  ORDER BY c2.updated_at DESC LIMIT 1"
            ") "
            "AND conversation_id = ("
            "  SELECT c3.conversation_id FROM conversations c3 "
            "  WHERE c3.user_id = conversations.user_id "
            "  ORDER BY c3.updated_at DESC LIMIT 1 OFFSET 1"
            ") "
            "ORDER BY updated_at ASC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (older_than.isoformat(), max(1, int(limit)))).fetchall()
        states: list[ConversationState] = []
        for row in rows:
            conversation_id = str(row[0] or "")
            if not conversation_id:
                continue
            state = self.get(conversation_id)
            if state is None or not state.messages:
                continue
            states.append(state)
        return states

    def prune_user_conversations(self, user_id: str, *, keep: int = 2) -> int:
        keep_n = max(1, int(keep))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT conversation_id
                FROM conversations
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT -1 OFFSET ?
                """,
                (user_id, keep_n),
            ).fetchall()
            if not rows:
                return 0
            ids = [str(r[0]) for r in rows if r and r[0]]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM conversations WHERE user_id = ? AND conversation_id IN ({placeholders})",
                (user_id, *ids),
            )
            return len(ids)

    def claim_conversation_for_compaction(self, conversation_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE conversations
                SET memory_compaction_status = 'processing',
                    memory_compaction_attempts = COALESCE(memory_compaction_attempts, 0) + 1,
                    memory_compaction_last_error = NULL
                WHERE conversation_id = ?
                  AND memory_compacted_at IS NULL
                  AND (memory_compaction_status IS NULL OR memory_compaction_status != 'processing')
                """,
                (conversation_id,),
            )
            return cur.rowcount > 0

    def mark_conversation_compaction(
        self,
        conversation_id: str,
        *,
        status: str,
        error: str | None = None,
        compacted: bool = True,
    ) -> None:
        compacted_at = datetime.utcnow().isoformat() if compacted else None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE conversations
                SET memory_compacted_at = ?,
                    memory_compaction_status = ?,
                    memory_compaction_last_error = ?
                WHERE conversation_id = ?
                """,
                (compacted_at, status, error, conversation_id),
            )


    def save(self, state: ConversationState) -> None:
        import json

        state.updated_at = datetime.utcnow()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, user_id, intent, pending_confirmation,
                    candidate_media_json, last_issue_key, support_context_json, last_tool_actions_json,
                    escalation_history_json, messages_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    intent = excluded.intent,
                    pending_confirmation = excluded.pending_confirmation,
                    candidate_media_json = excluded.candidate_media_json,
                    last_issue_key = excluded.last_issue_key,
                    support_context_json = excluded.support_context_json,
                    last_tool_actions_json = excluded.last_tool_actions_json,
                    escalation_history_json = excluded.escalation_history_json,
                    messages_json = excluded.messages_json,
                    updated_at = excluded.updated_at
                """,
                (
                    state.conversation_id,
                    state.user_id,
                    state.intent.value,
                    state.pending_confirmation,
                    json.dumps(state.candidate_media),
                    state.last_issue_key,
                    json.dumps(state.support_context, default=str),
                    json.dumps(state.last_tool_actions, default=str),
                    json.dumps(state.escalation_history, default=str),
                    json.dumps([message.model_dump(mode="json") for message in state.messages]),
                    state.updated_at.isoformat(),
                ),
            )

    def get_user_memory_context(self, user_id: str, recent_notes_limit: int = 12) -> dict[str, Any]:
        import json

        with self._connect() as conn:
            profile_row = conn.execute(
                """
                SELECT rolling_summary, preferences_json, familiarity_notes_json, updated_at
                FROM user_memory_profile
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            notes_rows = conn.execute(
                """
                SELECT note_type, content, task_id, status, tier, metadata_json, created_at
                FROM user_memory_notes
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, max(1, int(recent_notes_limit))),
            ).fetchall()
            open_task_rows = conn.execute(
                """
                SELECT note_type, content, task_id, status, tier, metadata_json, created_at
                FROM user_memory_notes
                WHERE user_id = ? AND task_id IS NOT NULL AND status = 'open'
                ORDER BY updated_at DESC
                LIMIT 20
                """,
                (user_id,),
            ).fetchall()

        rolling_summary = ""
        preferences: list[Any] = []
        familiarity_notes: list[Any] = []
        updated_at: str | None = None
        if profile_row:
            rolling_summary = str(profile_row[0] or "")
            preferences = json.loads(profile_row[1] or "[]")
            familiarity_notes = json.loads(profile_row[2] or "[]")
            updated_at = profile_row[3]

        def _row_to_note(row: tuple[Any, ...]) -> dict[str, Any]:
            metadata = {}
            try:
                metadata = json.loads(row[5] or "{}")
            except Exception:
                metadata = {}
            return {
                "note_type": row[0],
                "content": row[1],
                "task_id": row[2],
                "status": row[3],
                "tier": row[4],
                "metadata": metadata,
                "created_at": row[6],
            }

        recent_notes = [_row_to_note(row) for row in notes_rows]
        open_tasks = [_row_to_note(row) for row in open_task_rows]
        return {
            "rolling_summary": rolling_summary,
            "preferences": preferences,
            "familiarity_notes": familiarity_notes,
            "recent_notes": recent_notes,
            "open_tasks": open_tasks,
            "updated_at": updated_at,
        }

    def upsert_user_memory_profile(
        self,
        user_id: str,
        rolling_summary: str,
        preferences: list[Any] | None = None,
        familiarity_notes: list[Any] | None = None,
    ) -> None:
        import json

        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_memory_profile (
                    user_id, rolling_summary, preferences_json, familiarity_notes_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    rolling_summary = excluded.rolling_summary,
                    preferences_json = excluded.preferences_json,
                    familiarity_notes_json = excluded.familiarity_notes_json,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    rolling_summary.strip(),
                    json.dumps(preferences or []),
                    json.dumps(familiarity_notes or []),
                    now,
                ),
            )

    def add_user_memory_note(
        self,
        user_id: str,
        note_type: str,
        content: str,
        *,
        task_id: str | None = None,
        status: str = "logged",
        tier: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        import json

        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_memory_notes (
                    user_id, note_type, content, task_id, status, tier, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    note_type,
                    content.strip(),
                    task_id,
                    status,
                    int(tier),
                    json.dumps(metadata or {}),
                    now,
                    now,
                ),
            )

    def add_user_memory_snapshot(
        self,
        user_id: str,
        summary: str,
        *,
        tier: int = 2,
        source_span_start: str | None = None,
        source_span_end: str | None = None,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_memory_snapshots (
                    user_id, tier, summary, source_span_start, source_span_end, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    int(tier),
                    summary.strip(),
                    source_span_start,
                    source_span_end,
                    now,
                ),
            )

    def compact_user_memory(
        self,
        user_id: str,
        *,
        tier1_keep: int,
        tier2_to_tier3_threshold: int,
    ) -> None:
        """Simple decay: trim old tier-1 notes and collapse older tier-2 snapshots into one tier-3 snapshot."""
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM user_memory_notes
                WHERE id IN (
                    SELECT id FROM user_memory_notes
                    WHERE user_id = ? AND tier = 1
                    ORDER BY updated_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (user_id, max(1, int(tier1_keep))),
            )

            tier2_rows = conn.execute(
                """
                SELECT id, summary FROM user_memory_snapshots
                WHERE user_id = ? AND tier = 2
                ORDER BY created_at ASC
                """,
                (user_id,),
            ).fetchall()
            if len(tier2_rows) < max(2, int(tier2_to_tier3_threshold)):
                return
            collapse_rows = tier2_rows[:-3]
            if not collapse_rows:
                return
            collapsed_text = " ".join(str(row[1] or "").strip() for row in collapse_rows if str(row[1] or "").strip())
            if collapsed_text:
                now = datetime.utcnow().isoformat()
                conn.execute(
                    """
                    INSERT INTO user_memory_snapshots (
                        user_id, tier, summary, source_span_start, source_span_end, created_at
                    ) VALUES (?, 3, ?, NULL, NULL, ?)
                    """,
                    (user_id, collapsed_text[:8000], now),
                )
            ids = [row[0] for row in collapse_rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM user_memory_snapshots WHERE id IN ({placeholders})",
                ids,
            )
