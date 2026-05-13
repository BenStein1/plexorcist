from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from backend.models import PlexAuthSession


class PlexAuthSessionStore:
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
                CREATE TABLE IF NOT EXISTS plex_auth_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    is_admin INTEGER NOT NULL,
                    plex_token TEXT,
                    auth_source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, session: PlexAuthSession) -> None:
        session.updated_at = datetime.utcnow()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plex_auth_sessions (
                    session_id, user_id, username, display_name,
                    is_admin, plex_token, auth_source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    username = excluded.username,
                    display_name = excluded.display_name,
                    is_admin = excluded.is_admin,
                    plex_token = excluded.plex_token,
                    auth_source = excluded.auth_source,
                    updated_at = excluded.updated_at
                """,
                (
                    session.session_id,
                    session.user_id,
                    session.username,
                    session.display_name,
                    1 if session.is_admin else 0,
                    session.plex_token,
                    session.auth_source,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )

    def get(self, session_id: str) -> PlexAuthSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plex_auth_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return PlexAuthSession(
            session_id=row[0],
            user_id=row[1],
            username=row[2],
            display_name=row[3],
            is_admin=bool(row[4]),
            plex_token=row[5],
            auth_source=row[6],
            created_at=datetime.fromisoformat(row[7]),
            updated_at=datetime.fromisoformat(row[8]),
        )

    def delete(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM plex_auth_sessions WHERE session_id = ?", (session_id,))
