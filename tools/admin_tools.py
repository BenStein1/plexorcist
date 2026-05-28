from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from backend.auth_context import FriendlyNameDirectory
from backend.state import ConversationStore
from clients.transmission_client import TransmissionClient


class AdminTools:
    def __init__(
        self,
        transmission: TransmissionClient,
        *,
        store: ConversationStore | None = None,
        friendly_names: FriendlyNameDirectory | None = None,
        verify_wait_seconds: int = 30,
    ) -> None:
        self.transmission = transmission
        self.store = store
        self.friendly_names = friendly_names
        self.verify_wait_seconds = max(0, int(verify_wait_seconds))

    async def get_admin_task_summary(
        self,
        user_query: str | None = None,
        scope: str = "all_users",
        days: int = 30,
        limit: int = 20,
    ) -> dict[str, Any]:
        if self.store is None:
            return {
                "ok": False,
                "action": "admin_task_summary_unavailable",
                "reason": "store_unavailable",
                "user_summary": "Admin task summary is unavailable because the memory store is not configured.",
            }

        days = max(1, min(int(days or 30), 365))
        limit = max(1, min(int(limit or 20), 100))
        scope = str(scope or "all_users").strip().lower()
        if scope not in {"all_users", "specific_user"}:
            scope = "all_users"
        query = (user_query or "").strip()
        if scope == "all_users":
            query = ""
        elif not query:
            return {
                "ok": False,
                "action": "admin_task_summary",
                "reason": "user_query_required",
                "scope": scope,
                "days": days,
                "limit": limit,
                "user_summary": "I need a friendly name, username, or user ID to summarize tasks for a specific user.",
            }
        rows = self._query_open_tasks(days=days, limit=limit * 5 if query else limit)
        users = self._load_user_labels()
        snapshots = self._load_latest_tier2_snapshots({str(row["user_id"]) for row in rows})

        tasks: list[dict[str, Any]] = []
        for row in rows:
            user_id = str(row["user_id"])
            user_label = self._format_user_label(user_id, users.get(user_id))
            searchable = " ".join(
                str(item or "")
                for item in (
                    user_id,
                    user_label,
                    users.get(user_id, {}).get("username") if users.get(user_id) else "",
                    users.get(user_id, {}).get("display_name") if users.get(user_id) else "",
                    users.get(user_id, {}).get("friendly_name") if users.get(user_id) else "",
                )
            ).lower()
            if query and query.lower() not in searchable:
                continue
            metadata = self._parse_json(row.get("metadata_json"), default={})
            tasks.append(
                {
                    "user_id": user_id,
                    "user_label": user_label,
                    "note_type": row["note_type"],
                    "status": row["status"],
                    "content": row["content"],
                    "task_id": row["task_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "metadata": metadata,
                    "tier2_context": snapshots.get(user_id),
                }
            )
            if len(tasks) >= limit:
                break

        if not tasks:
            target = f" for {query}" if query else ""
            summary = f"No open user tasks found{target} in the last {days} day(s)."
        else:
            lines = [f"Found {len(tasks)} open user task(s):"]
            for task in tasks[:10]:
                lines.append(f"- {task['user_label']}: {task['content']}")
            if len(tasks) > 10:
                lines.append(f"- plus {len(tasks) - 10} more.")
            summary = "\n".join(lines)

        return {
            "ok": True,
            "action": "admin_task_summary",
            "days": days,
            "limit": limit,
            "scope": scope,
            "user_query": query or None,
            "task_count": len(tasks),
            "tasks": tasks,
            "user_summary": summary,
        }

    async def send_admin_message(
        self,
        user_query: str,
        message: str,
        sender_user_id: str,
        sender_name: str = "Ben",
    ) -> dict[str, Any]:
        if self.store is None:
            return {
                "ok": False,
                "action": "admin_message_unavailable",
                "reason": "store_unavailable",
                "user_summary": "Admin message delivery is unavailable because the memory store is not configured.",
            }
        message = str(message or "").strip()
        if not message:
            return {
                "ok": False,
                "action": "admin_message_not_queued",
                "reason": "message_required",
                "user_summary": "I need a message to send.",
            }
        resolved = self._resolve_user_query(user_query)
        if not resolved.get("ok"):
            return {
                **resolved,
                "action": "admin_message_not_queued",
            }
        recipient = resolved["user"]
        self.store.add_user_memory_note(
            user_id=recipient["user_id"],
            note_type="admin_message",
            content=message,
            status="unread",
            tier=1,
            metadata={
                "from_admin_user_id": sender_user_id,
                "from_admin_name": sender_name,
                "recipient_label": recipient["label"],
                "created_by_tool": "send_admin_message",
            },
        )
        return {
            "ok": True,
            "action": "admin_message_queued",
            "recipient": recipient,
            "recipient_label": recipient["label"],
            "message": message,
            "user_summary": f"Queued admin message for {recipient['label']}: {message}",
        }

    async def set_admin_motd(self, message: str, sender_user_id: str, sender_name: str = "Ben") -> dict[str, Any]:
        if self.store is None:
            return {
                "ok": False,
                "action": "admin_motd_unavailable",
                "reason": "store_unavailable",
                "user_summary": "MOTD is unavailable because the memory store is not configured.",
            }
        message = str(message or "").strip()
        if not message:
            return {
                "ok": False,
                "action": "admin_motd_not_set",
                "reason": "message_required",
                "user_summary": "I need MOTD text to set.",
            }
        self.store.set_user_flag(
            "__global__",
            "admin_motd",
            json.dumps(
                {
                    "message": message,
                    "from_admin_user_id": sender_user_id,
                    "from_admin_name": sender_name,
                }
            ),
        )
        return {
            "ok": True,
            "action": "admin_motd_set",
            "message": message,
            "user_summary": f"MOTD set: {message}",
        }

    async def clear_admin_motd(self) -> dict[str, Any]:
        if self.store is None:
            return {
                "ok": False,
                "action": "admin_motd_unavailable",
                "reason": "store_unavailable",
                "user_summary": "MOTD is unavailable because the memory store is not configured.",
            }
        deleted = self.store.clear_user_flag("__global__", "admin_motd")
        return {
            "ok": True,
            "action": "admin_motd_cleared",
            "deleted": deleted,
            "user_summary": "MOTD cleared." if deleted else "There was no active MOTD to clear.",
        }

    async def run_transmission_maintenance(self) -> dict[str, Any]:
        try:
            torrents = await self.transmission.get_torrents()
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "action": "transmission_maintenance_failed",
                "reason": "transmission_unreachable",
                "error": str(exc),
                "user_summary": "Transmission maintenance could not start because Transmission was unreachable.",
            }

        completed = [torrent for torrent in torrents if bool(torrent.get("isFinished"))]
        initial_errors = [torrent for torrent in torrents if int(torrent.get("error") or 0) != 0]

        verified: list[dict[str, Any]] = []
        verify_failures: list[dict[str, Any]] = []
        for torrent in completed:
            torrent_id = self._torrent_id(torrent)
            if torrent_id is None:
                continue
            try:
                await self.transmission.verify_torrent(torrent_id)
                verified.append(self._torrent_summary(torrent))
            except httpx.HTTPError as exc:
                verify_failures.append({**self._torrent_summary(torrent), "error": str(exc)})

        if completed and self.verify_wait_seconds:
            await asyncio.sleep(self.verify_wait_seconds)

        try:
            refreshed = await self.transmission.get_torrents()
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "action": "transmission_maintenance_partial",
                "reason": "transmission_unreachable_after_verify",
                "error": str(exc),
                "completed_count": len(completed),
                "verified_count": len(verified),
                "verify_failure_count": len(verify_failures),
                "user_summary": "Transmission maintenance verified completed torrents, but could not recheck the queue afterward.",
            }

        error_torrents = [torrent for torrent in refreshed if int(torrent.get("error") or 0) != 0]
        removed: list[dict[str, Any]] = []
        remove_failures: list[dict[str, Any]] = []
        for torrent in error_torrents:
            torrent_id = self._torrent_id(torrent)
            if torrent_id is None:
                continue
            try:
                await self.transmission.remove_torrent(torrent_id, delete_local_data=True)
                removed.append(self._torrent_summary(torrent))
            except httpx.HTTPError as exc:
                remove_failures.append({**self._torrent_summary(torrent), "error": str(exc)})

        try:
            refreshed = await self.transmission.get_torrents()
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "action": "transmission_maintenance_partial",
                "reason": "transmission_unreachable_after_remove",
                "error": str(exc),
                "completed_count": len(completed),
                "verified_count": len(verified),
                "removed_error_count": len(removed),
                "user_summary": "Transmission maintenance removed errored torrents, but could not recheck the queue for peer refreshes afterward.",
            }

        stalled = [
            torrent
            for torrent in refreshed
            if float(torrent.get("percentDone") or 0.0) == 0.0 and int(torrent.get("status") or 0) == 4
        ]
        reannounced: list[dict[str, Any]] = []
        reannounce_failures: list[dict[str, Any]] = []
        for torrent in stalled:
            torrent_id = self._torrent_id(torrent)
            if torrent_id is None:
                continue
            try:
                await self.transmission.reannounce_torrent(torrent_id)
                reannounced.append(self._torrent_summary(torrent))
            except httpx.HTTPError as exc:
                reannounce_failures.append({**self._torrent_summary(torrent), "error": str(exc)})

        failed_count = len(verify_failures) + len(remove_failures) + len(reannounce_failures)
        return {
            "ok": failed_count == 0,
            "action": "transmission_maintenance_completed",
            "completed_count": len(completed),
            "initial_error_count": len(initial_errors),
            "verified_count": len(verified),
            "removed_error_count": len(removed),
            "reannounced_stalled_count": len(reannounced),
            "verify_failure_count": len(verify_failures),
            "remove_failure_count": len(remove_failures),
            "reannounce_failure_count": len(reannounce_failures),
            "verified": verified[:20],
            "removed_errors": removed[:20],
            "reannounced_stalled": reannounced[:20],
            "failures": {
                "verify": verify_failures[:20],
                "remove": remove_failures[:20],
                "reannounce": reannounce_failures[:20],
            },
            "user_summary": (
                "Transmission maintenance ran: "
                f"verified {len(verified)} completed torrent(s), "
                f"removed {len(removed)} errored torrent(s), "
                f"and asked trackers for more peers on {len(reannounced)} stalled torrent(s)."
            ),
        }

    def _torrent_id(self, torrent: dict[str, Any]) -> int | None:
        try:
            return int(torrent.get("id"))
        except (TypeError, ValueError):
            return None

    def _torrent_summary(self, torrent: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": self._torrent_id(torrent),
            "name": str(torrent.get("name") or ""),
            "status": torrent.get("status"),
            "error": torrent.get("error"),
            "error_string": torrent.get("errorString"),
            "percent_done": torrent.get("percentDone"),
        }

    def _query_open_tasks(self, *, days: int, limit: int) -> list[dict[str, Any]]:
        with self.store._connect() as conn:  # type: ignore[union-attr, protected-access]
            rows = conn.execute(
                """
                SELECT user_id, note_type, content, task_id, status, tier, metadata_json, created_at, updated_at
                FROM user_memory_notes
                WHERE status IN ('open', 'unresolved')
                  AND datetime(updated_at) >= datetime('now', ?)
                ORDER BY datetime(updated_at) DESC
                LIMIT ?
                """,
                (f"-{int(days)} days", int(limit)),
            ).fetchall()
        columns = ["user_id", "note_type", "content", "task_id", "status", "tier", "metadata_json", "created_at", "updated_at"]
        return [dict(zip(columns, row)) for row in rows]

    def _load_user_labels(self) -> dict[str, dict[str, str]]:
        labels: dict[str, dict[str, str]] = {}
        with self.store._connect() as conn:  # type: ignore[union-attr, protected-access]
            rows = conn.execute(
                """
                SELECT user_id, username, display_name, MAX(updated_at) AS updated_at
                FROM plex_auth_sessions
                GROUP BY user_id, username, display_name
                ORDER BY datetime(updated_at) DESC
                """
            ).fetchall()
        for user_id, username, display_name, _updated_at in rows:
            user_id = str(user_id)
            if user_id in labels:
                continue
            username = str(username or "")
            display_name = str(display_name or "")
            friendly_name = (
                self.friendly_names.resolve(username, display_name)
                if self.friendly_names is not None
                else (display_name or username)
            )
            labels[user_id] = {
                "user_id": user_id,
                "username": username,
                "display_name": display_name,
                "friendly_name": friendly_name,
                "label": self._format_user_label(user_id, {
                    "username": username,
                    "display_name": display_name,
                    "friendly_name": friendly_name,
                }),
            }
        return labels

    def _resolve_user_query(self, user_query: str) -> dict[str, Any]:
        query = str(user_query or "").strip().lower()
        if not query:
            return {
                "ok": False,
                "reason": "user_query_required",
                "user_summary": "I need a friendly name, username, display name, or user ID.",
            }
        users = self._load_user_labels()
        matches = []
        for user_id, label in users.items():
            values = {
                user_id,
                str(label.get("username") or ""),
                str(label.get("display_name") or ""),
                str(label.get("friendly_name") or ""),
                str(label.get("label") or ""),
            }
            if any(query == value.lower() for value in values if value):
                matches.append({**label, "user_id": user_id})
        if not matches:
            for user_id, label in users.items():
                values = [
                    user_id,
                    str(label.get("username") or ""),
                    str(label.get("display_name") or ""),
                    str(label.get("friendly_name") or ""),
                    str(label.get("label") or ""),
                ]
                if any(query in value.lower() for value in values if value):
                    matches.append({**label, "user_id": user_id})
        if not matches:
            return {
                "ok": False,
                "reason": "user_not_found",
                "user_query": user_query,
                "user_summary": f"I could not find a user matching {user_query}.",
            }
        unique: dict[str, dict[str, str]] = {str(item["user_id"]): item for item in matches}
        matches = list(unique.values())
        if len(matches) > 1:
            return {
                "ok": False,
                "reason": "user_ambiguous",
                "user_query": user_query,
                "candidates": matches[:10],
                "user_summary": "That user match is ambiguous. Pick one: "
                + ", ".join(str(item.get("label") or item.get("user_id")) for item in matches[:5]),
            }
        return {"ok": True, "user": matches[0]}

    def _load_latest_tier2_snapshots(self, user_ids: set[str]) -> dict[str, str]:
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        with self.store._connect() as conn:  # type: ignore[union-attr, protected-access]
            rows = conn.execute(
                f"""
                SELECT user_id, summary
                FROM user_memory_snapshots
                WHERE tier = 2
                  AND user_id IN ({placeholders})
                ORDER BY datetime(created_at) DESC
                """,
                tuple(user_ids),
            ).fetchall()
        snapshots: dict[str, str] = {}
        for user_id, summary in rows:
            user_id = str(user_id)
            if user_id not in snapshots:
                snapshots[user_id] = str(summary or "")
        return snapshots

    def _format_user_label(self, user_id: str, label: dict[str, str] | None) -> str:
        if not label:
            return user_id
        friendly = (label.get("friendly_name") or "").strip()
        username = (label.get("username") or "").strip()
        display_name = (label.get("display_name") or "").strip()
        if friendly and username and friendly.lower() != username.lower():
            return f"{friendly} ({username}, {user_id})"
        if display_name and username and display_name.lower() != username.lower():
            return f"{display_name} ({username}, {user_id})"
        return f"{username or display_name or user_id} ({user_id})"

    def _parse_json(self, raw: Any, *, default: Any) -> Any:
        try:
            return json.loads(raw or "")
        except Exception:
            return default
