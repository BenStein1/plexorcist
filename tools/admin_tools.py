from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import json
import re
import unicodedata
from typing import Any

import httpx

from backend.auth_context import FriendlyNameDirectory
from backend.shabbos.flags import is_shabbos_user, set_shabbos_mode
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
        audit_path: str = "plexorcist.log",
    ) -> None:
        self.transmission = transmission
        self.store = store
        self.friendly_names = friendly_names
        self.verify_wait_seconds = max(0, int(verify_wait_seconds))
        self.audit_path = audit_path

    # -- Shabbos Mode ------------------------------------------------------

    async def set_shabbos_mode(self, user_query: str, enabled: bool) -> dict[str, Any]:
        """Admin-only: put an account into (or out of) the AI-free command interface."""
        if self.store is None:
            return {
                "ok": False,
                "action": "set_shabbos_mode",
                "reason": "store_unavailable",
                "user_summary": "Shabbos Mode can't be changed because the store is not configured.",
            }
        resolved = self._resolve_user_query(user_query)
        if not resolved.get("ok"):
            return {**resolved, "action": "set_shabbos_mode"}

        user = resolved["user"]
        user_id = str(user.get("user_id") or "").strip()
        label = str(user.get("label") or user.get("username") or user_id)

        set_shabbos_mode(self.store, user_id, bool(enabled))
        state = "ON" if enabled else "OFF"
        return {
            "ok": True,
            "action": "set_shabbos_mode",
            "user_id": user_id,
            "enabled": bool(enabled),
            "user_summary": (
                f"Shabbos Mode is now {state} for {label}. "
                + (
                    "They get the deterministic command interface — no language model touches their account."
                    if enabled
                    else "They're back on the normal conversational interface."
                )
            ),
        }

    async def get_shabbos_diagnostics(self, user_query: str | None = None) -> dict[str, Any]:
        """Admin-only: prove the deterministic route is what actually ran.

        This reads the real audit log rather than reporting a hardcoded zero -- a
        constant would merely assert the guarantee; counting real traffic
        demonstrates it.
        """
        if self.store is None:
            return {"ok": False, "action": "shabbos_diagnostics", "reason": "store_unavailable"}

        target_id: str | None = None
        label = "all users"
        if user_query:
            resolved = self._resolve_user_query(user_query)
            if not resolved.get("ok"):
                return {**resolved, "action": "shabbos_diagnostics"}
            target_id = str(resolved["user"].get("user_id") or "")
            label = str(resolved["user"].get("label") or target_id)

        commands = 0
        ai_invoked = 0
        by_command: dict[str, int] = {}
        try:
            with open(self.audit_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("event_type") != "shabbos_command":
                        continue
                    if target_id and str(record.get("user_id")) != target_id:
                        continue
                    commands += 1
                    if record.get("ai_invoked"):
                        ai_invoked += 1
                    name = str(record.get("command") or "?")
                    by_command[name] = by_command.get(name, 0) + 1
        except FileNotFoundError:
            pass

        enabled = is_shabbos_user(self.store, target_id) if target_id else None
        return {
            "ok": True,
            "action": "shabbos_diagnostics",
            "user": label,
            "shabbos_mode": enabled,
            "router": "deterministic",
            "commands_run": commands,
            "by_command": by_command,
            "llm_calls": ai_invoked,
            "embedding_calls": 0,  # the app has no embedding client at all
            "user_summary": (
                f"{label}: {commands} Shabbos command(s) recorded, {ai_invoked} of which invoked a language model. "
                + ("Clean." if ai_invoked == 0 else "*** AI INVOCATION DETECTED — INVESTIGATE ***")
            ),
        }

    async def get_admin_task_summary(
        self,
        user_query: str | None = None,
        scope: str = "all_users",
        days: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if self.store is None:
            return {
                "ok": False,
                "action": "admin_task_summary_unavailable",
                "reason": "store_unavailable",
                "user_summary": "Admin task summary is unavailable because the memory store is not configured.",
            }

        days = max(1, min(int(days), 365)) if days is not None else None
        limit = max(1, min(int(limit or 100), 100))
        scope_value = getattr(scope, "value", scope)
        scope = str(scope_value or "all_users").strip().lower()
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
                    "note_id": row["note_id"],
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
            window = f" in the last {days} day(s)" if days is not None else ""
            summary = f"No open user tasks found{target}{window}."
        else:
            lines = [f"Found {len(tasks)} live open user task(s), verbatim:"]
            for task in tasks:
                lines.append(f"- [note_id={task['note_id']}] {task['user_label']}: {task['content']}")
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

    async def resolve_admin_task(
        self,
        note_id: int | None = None,
        task_query: str | None = None,
        resolve_all_matches: bool = False,
    ) -> dict[str, Any]:
        if self.store is None:
            return {
                "ok": False,
                "action": "admin_task_resolve_unavailable",
                "reason": "store_unavailable",
                "user_summary": "Task resolution is unavailable because the memory store is not configured.",
            }

        if note_id is not None:
            task = self._get_open_task(int(note_id))
            if task is None:
                return {
                    "ok": False,
                    "action": "admin_task_resolve",
                    "reason": "task_not_found",
                    "note_id": note_id,
                    "verified_closed": False,
                    "user_summary": f"I couldn't find an open task with id {note_id} to resolve.",
                }
            resolved = self.store.resolve_user_memory_note(int(note_id))
            verified_closed = resolved and self._get_open_task(int(note_id)) is None
            if not verified_closed:
                return {
                    "ok": False,
                    "action": "admin_task_resolve",
                    "reason": "task_update_failed",
                    "note_id": note_id,
                    "verified_closed": False,
                    "user_summary": f"Task {note_id} was found but is still open after the update attempt.",
                }
            return {
                "ok": True,
                "action": "admin_task_resolve",
                "note_id": note_id,
                "resolved_task": task,
                "resolved_note_ids": [int(note_id)],
                "resolved_count": 1,
                "verified_closed": True,
                "user_summary": f"Closed and verified task {note_id}: {task['content']}",
            }

        query = str(task_query or "").strip()
        if not query:
            return {
                "ok": False,
                "action": "admin_task_resolve",
                "reason": "target_required",
                "user_summary": "I need either a note_id from a recent task summary or a description of the task to resolve.",
            }

        rows = self._query_open_tasks(days=None, limit=1000)
        users = self._load_user_labels()
        scored_matches: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            user_id = str(row["user_id"])
            user_label = self._format_user_label(user_id, users.get(user_id))
            metadata = self._parse_json(row.get("metadata_json"), default={})
            score = self._task_match_score(
                query,
                user_id=user_id,
                user_label=user_label,
                note_type=str(row.get("note_type") or ""),
                content=str(row.get("content") or ""),
                task_id=str(row.get("task_id") or ""),
                metadata=metadata,
            )
            if score < 0.58:
                continue
            scored_matches.append(
                (
                    score,
                    {
                        "note_id": row["note_id"],
                        "user_id": user_id,
                        "user_label": user_label,
                        "content": row["content"],
                        "task_id": row["task_id"],
                        "status": row["status"],
                        "updated_at": row["updated_at"],
                        "metadata": metadata,
                        "match_score": round(score, 3),
                    },
                )
            )

        if not scored_matches:
            return {
                "ok": False,
                "action": "admin_task_resolve",
                "reason": "task_not_found",
                "task_query": task_query,
                "user_summary": f"I could not find an open task matching {task_query}.",
            }

        top_score = max(item[0] for item in scored_matches)
        selected = [item for item in scored_matches if item[0] >= max(0.58, top_score - 0.08)]
        matches = [item[1] for item in selected]
        unique_user_ids = {str(match["user_id"]) for match in matches}
        same_user_group = len(unique_user_ids) == 1 and self._query_has_task_terms_for_user(
            query,
            matches[0]["user_label"],
        )
        if len(matches) > 1 and not (resolve_all_matches or same_user_group):
            return {
                "ok": False,
                "action": "admin_task_resolve",
                "reason": "task_ambiguous",
                "task_query": task_query,
                "candidates": matches[:10],
                "user_summary": "That matches open tasks for multiple people. Choose note_id(s), or retry with resolve_all_matches=true: "
                + "; ".join(f"{m['user_label']}: {m['content']}" for m in matches[:5]),
            }

        targets = matches if (resolve_all_matches or same_user_group) else matches[:1]
        resolved_tasks: list[dict[str, Any]] = []
        failed_note_ids: list[int] = []
        for match in targets:
            target_id = int(match["note_id"])
            if self.store.resolve_user_memory_note(target_id) and self._get_open_task(target_id) is None:
                resolved_tasks.append(match)
            else:
                failed_note_ids.append(target_id)
        resolved_note_ids = [int(match["note_id"]) for match in resolved_tasks]
        verified_closed = bool(resolved_tasks) and not failed_note_ids
        return {
            "ok": verified_closed,
            "action": "admin_task_resolve",
            "note_id": resolved_note_ids[0] if len(resolved_note_ids) == 1 else None,
            "resolved_note_ids": resolved_note_ids,
            "resolved_count": len(resolved_tasks),
            "resolved_tasks": resolved_tasks,
            "failed_note_ids": failed_note_ids,
            "verified_closed": verified_closed,
            "match_scope": "explicit_all" if resolve_all_matches else ("same_user_group" if same_user_group else "single"),
            "user_summary": (
                f"Closed and verified {len(resolved_tasks)} task(s): "
                + "; ".join(f"[{m['note_id']}] {m['user_label']}: {m['content']}" for m in resolved_tasks)
                if verified_closed
                else f"Some matching tasks are still open after the update attempt: {failed_note_ids}."
            ),
        }

    async def find_users(self, query: str | None = None, limit: int = 25) -> dict[str, Any]:
        """Admin-only: browse the people on file.

        There was previously no way to LOOK at the roster at all -- only to
        resolve a name to exactly one person and fail otherwise. So "take a look
        at the friendly names for Mike" had nothing to call, and came back empty
        even though two Mikes were sitting in the ledger.
        """
        if self.store is None:
            return {
                "ok": False,
                "action": "find_users_unavailable",
                "reason": "store_unavailable",
                "user_summary": "I can't look up users because the memory store is not configured.",
            }
        limit = max(1, min(int(limit or 25), 200))
        needle = str(query or "").strip().lower()
        tokens = [token for token in re.split(r"\W+", needle) if token]

        people: list[dict[str, Any]] = []
        for label in self._load_user_labels().values():
            if tokens:
                haystack = " ".join(value.lower() for value in self._searchable_values(label) if value)
                if not any(token in haystack for token in tokens):
                    continue
            people.append(dict(label))
        # Registered accounts first -- those are the ones Ben can act on.
        people.sort(key=lambda item: (not item.get("has_account"), str(item.get("friendly_name") or "").lower()))
        registered = sum(1 for item in people if item.get("has_account"))

        if not people:
            return {
                "ok": True,
                "action": "find_users",
                "query": query,
                "users": [],
                "match_count": 0,
                "user_summary": (
                    f"Nobody on file matches {query}." if needle else "There's nobody on file at all."
                ),
            }
        shown = people[:limit]
        listing = ", ".join(self._candidate_summary(item) for item in shown)
        more = f" (+{len(people) - len(shown)} more)" if len(people) > len(shown) else ""
        scope = f"matching {query}" if needle else "on file"
        return {
            "ok": True,
            "action": "find_users",
            "query": query,
            "users": shown,
            "match_count": len(people),
            "registered_count": registered,
            "user_summary": (
                f"{len(people)} {scope} ({registered} with a Plexorcist account): {listing}{more}. "
                "Anyone marked [no account yet] is in the friendly-names list but has never logged in, "
                "so there's no account to attach a message to."
            ),
        }

    async def set_user_friendly_name(self, user_query: str, friendly_name: str) -> dict[str, Any]:
        if self.friendly_names is None:
            return {
                "ok": False,
                "action": "set_user_friendly_name_unavailable",
                "reason": "friendly_names_unavailable",
                "user_summary": "Friendly-name storage is not configured.",
            }
        # Friendly names are keyed by username, not user_id, so renaming someone
        # who has never logged in is perfectly serviceable -- and is exactly how
        # a ledger-only entry gets a better name in the first place.
        resolved = self._resolve_user_query(user_query, require_account=False)
        if not resolved.get("ok"):
            return {**resolved, "action": "set_user_friendly_name"}
        username = str(resolved["user"].get("username") or "").strip()
        if not username:
            return {
                "ok": False,
                "action": "set_user_friendly_name",
                "reason": "user_has_no_username",
                "user_summary": "That user doesn't have a Plex username on file to key the friendly name by.",
            }
        try:
            self.friendly_names.set_friendly_name(username, friendly_name)
        except ValueError as exc:
            return {
                "ok": False,
                "action": "set_user_friendly_name",
                "reason": "invalid_input",
                "user_summary": str(exc),
            }
        return {
            "ok": True,
            "action": "set_user_friendly_name",
            "username": username,
            "friendly_name": friendly_name.strip(),
            "user_summary": f"Updated {username}'s friendly name to {friendly_name.strip()}.",
        }

    async def send_admin_message(
        self,
        user_query: str | None = None,
        message: str = "",
        sender_user_id: str = "",
        sender_name: str = "Ben",
        task_query: str | None = None,
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
        resolved = self._resolve_message_recipient(
            user_query=user_query,
            task_query=task_query,
            sender_user_id=sender_user_id,
        )
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
            "sent_message": message,
            "delivery_confirmation": {
                "direction": "admin_to_user",
                "status": "queued",
                "recipient_label": recipient["label"],
                "message": message,
            },
            "target_basis": resolved.get("target_basis"),
            "matched_task": resolved.get("matched_task"),
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

    def _get_open_task(self, note_id: int) -> dict[str, Any] | None:
        with self.store._connect() as conn:  # type: ignore[union-attr, protected-access]
            row = conn.execute(
                """
                SELECT id, user_id, note_type, content, task_id, status, tier, metadata_json, created_at, updated_at
                FROM user_memory_notes
                WHERE id = ? AND status IN ('open', 'unresolved')
                """,
                (int(note_id),),
            ).fetchone()
        if row is None:
            return None
        columns = ["note_id", "user_id", "note_type", "content", "task_id", "status", "tier", "metadata_json", "created_at", "updated_at"]
        task = dict(zip(columns, row))
        task["metadata"] = self._parse_json(task.pop("metadata_json", None), default={})
        return task

    @staticmethod
    def _normalize_task_text(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return " ".join("".join(char if char.isalnum() else " " for char in normalized).split())

    @classmethod
    def _task_match_score(
        cls,
        query: str,
        *,
        user_id: str,
        user_label: str,
        note_type: str,
        content: str,
        task_id: str,
        metadata: dict[str, Any],
    ) -> float:
        normalized_query = cls._normalize_task_text(query)
        if not normalized_query:
            return 0.0
        chunks = [
            content,
            f"{user_label} {content}",
            user_label,
            user_id,
            note_type,
            task_id,
            json.dumps(metadata, ensure_ascii=False) if metadata else "",
        ]
        normalized_chunks = [cls._normalize_task_text(chunk) for chunk in chunks if chunk]
        searchable = " ".join(normalized_chunks)
        if normalized_query in searchable:
            return 1.0

        query_tokens = set(normalized_query.split())
        searchable_tokens = set(searchable.split())
        if not query_tokens:
            return 0.0
        overlap = len(query_tokens & searchable_tokens) / len(query_tokens)
        if query_tokens <= searchable_tokens:
            return 0.98
        sequence = max(
            (SequenceMatcher(None, normalized_query, chunk).ratio() for chunk in normalized_chunks),
            default=0.0,
        )
        return (overlap * 0.72) + (sequence * 0.28)

    @classmethod
    def _query_has_task_terms_for_user(cls, query: str, user_label: str) -> bool:
        query_tokens = set(cls._normalize_task_text(query).split())
        label_tokens = set(cls._normalize_task_text(user_label).split())
        task_tokens = {token for token in query_tokens - label_tokens if len(token) >= 2}
        return bool(task_tokens)

    def _query_open_tasks(self, *, days: int | None, limit: int) -> list[dict[str, Any]]:
        with self.store._connect() as conn:  # type: ignore[union-attr, protected-access]
            if days is None:
                rows = conn.execute(
                    """
                    SELECT id, user_id, note_type, content, task_id, status, tier, metadata_json, created_at, updated_at
                    FROM user_memory_notes
                    WHERE status IN ('open', 'unresolved')
                    ORDER BY datetime(updated_at) DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, user_id, note_type, content, task_id, status, tier, metadata_json, created_at, updated_at
                    FROM user_memory_notes
                    WHERE status IN ('open', 'unresolved')
                      AND datetime(updated_at) >= datetime('now', ?)
                    ORDER BY datetime(updated_at) DESC
                    LIMIT ?
                    """,
                    (f"-{int(days)} days", int(limit)),
                ).fetchall()
        columns = ["note_id", "user_id", "note_type", "content", "task_id", "status", "tier", "metadata_json", "created_at", "updated_at"]
        return [dict(zip(columns, row)) for row in rows]

    def _load_user_labels(self) -> dict[str, dict[str, Any]]:
        labels: dict[str, dict[str, Any]] = {}
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
                "has_account": True,
                "label": self._format_user_label(user_id, {
                    "username": username,
                    "display_name": display_name,
                    "friendly_name": friendly_name,
                }),
            }

        # plex_auth_sessions is the LOGIN table, not the roster. It only holds
        # people who have actually signed in; the friendly-names ledger holds
        # everyone Ben knows (in prod: 15 rows vs 64 names). Resolving a name
        # against the login table alone answers "I could not find a user
        # matching Mike" for most of the people he'd ever ask about, which reads
        # as "I don't know them" when the truth is "they've never logged in".
        # Ledger-only people carry has_account=False and no user_id; callers
        # that key durable state decide what to do with that.
        if self.friendly_names is not None:
            known = {
                str(entry.get("username") or "").lower()
                for entry in labels.values()
                if entry.get("username")
            }
            for raw_username, raw_friendly in self.friendly_names.all_names().items():
                username = str(raw_username or "").strip()
                if not username or username.lower() in known:
                    continue
                known.add(username.lower())
                friendly_name = str(raw_friendly or "").strip()
                labels[self._LEDGER_KEY_PREFIX + username.lower()] = {
                    "user_id": "",
                    "username": username,
                    "display_name": "",
                    "friendly_name": friendly_name,
                    "has_account": False,
                    "label": self._format_user_label("", {
                        "username": username,
                        "display_name": "",
                        "friendly_name": friendly_name,
                    }),
                }
        return labels

    _LEDGER_KEY_PREFIX = "ledger:"

    @staticmethod
    def _match_key(item: dict[str, Any]) -> str:
        """Dedupe key. Every ledger-only person has user_id "", so keying on
        user_id alone would silently collapse all of them into one match."""
        user_id = str(item.get("user_id") or "")
        if user_id:
            return user_id
        return AdminTools._LEDGER_KEY_PREFIX + str(item.get("username") or "").lower()

    @staticmethod
    def _searchable_values(label: dict[str, Any]) -> list[str]:
        """Deliberately built from label["user_id"], never from the dict key --
        the key for a ledger-only row is a synthetic "ledger:<username>" string
        and must never be matchable text or leak into a result."""
        return [
            str(label.get(field) or "")
            for field in ("user_id", "username", "display_name", "friendly_name", "label")
        ]

    def _candidate_summary(self, item: dict[str, Any]) -> str:
        label = str(item.get("label") or item.get("user_id") or item.get("username") or "unknown")
        return label if item.get("has_account") else f"{label} [no account yet]"

    @staticmethod
    def _pick_or_dead_end(shown: list[dict[str, Any]], require_account: bool) -> str:
        """Closing sentence for a candidate list: a real question, or the truth.

        Ben's "Mike Young" landed on two people who only exist in the
        friendly-names ledger. Ending that with "Which one?" invites him to pick
        a recipient that refuses him on the very next turn, and he has to ask
        twice to learn the actual state. When nobody in the list can receive
        anything, say so instead of asking a question with no good answer.
        """
        if not require_account or any(item.get("has_account") for item in shown):
            return "Which one?"
        # "Neither" and "none" carry the negation themselves; the singular does not.
        subject = {1: "They have never", 2: "Neither has ever", }.get(len(shown), "None of them have ever")
        return f"{subject} logged into Plexorcist, so there's no account to send this to."

    def _resolve_user_query(self, user_query: str, *, require_account: bool = True) -> dict[str, Any]:
        """Resolve a loose human reference to one person.

        require_account defaults to True (fail closed): most callers write
        durable state keyed by user_id, and a ledger-only person has none --
        writing one anyway would key a row to "" that matches nobody, forever.
        set_user_friendly_name keys off username instead, so it passes False.
        """
        query = str(user_query or "").strip().lower()
        if not query:
            return {
                "ok": False,
                "reason": "user_query_required",
                "user_summary": "I need a friendly name, username, display name, or user ID.",
            }
        users = self._load_user_labels()
        tokens = [token for token in re.split(r"\W+", query) if token]

        # Four passes, strongest first. The old code had only exact + substring,
        # which is why "Mike Young" could never reach a person on file as "Mike":
        # the whole query had to appear inside one value. Tokens fix that, but an
        # any-token hit is a guess ("Young" alone would match a Young Nicole), so
        # those are offered as suggestions and never auto-selected.
        exact: list[dict[str, Any]] = []
        substring: list[dict[str, Any]] = []
        all_tokens: list[dict[str, Any]] = []
        any_token: list[dict[str, Any]] = []
        for label in users.values():
            lowered = [value.lower() for value in self._searchable_values(label) if value]
            if any(query == value for value in lowered):
                exact.append(dict(label))
                continue
            if any(query in value for value in lowered):
                substring.append(dict(label))
                continue
            if not tokens:
                continue
            haystack = " ".join(lowered)
            hits = sum(1 for token in tokens if token in haystack)
            if hits == len(tokens):
                all_tokens.append(dict(label))
            elif hits:
                any_token.append(dict(label))

        matches = exact or substring or all_tokens
        if not matches:
            suggestions = list({self._match_key(item): item for item in any_token}.values())
            if suggestions:
                shown = suggestions[:5]
                lead = f"No match for {user_query}. Closest I have: " + ", ".join(
                    self._candidate_summary(item) for item in shown
                )
                return {
                    "ok": False,
                    "reason": "user_not_found",
                    "user_query": user_query,
                    "candidates": suggestions[:10],
                    "user_summary": f"{lead}. {self._pick_or_dead_end(shown, require_account)}",
                }
            return {
                "ok": False,
                "reason": "user_not_found",
                "user_query": user_query,
                "user_summary": f"I could not find a user matching {user_query}.",
            }

        matches = list({self._match_key(item): item for item in matches}.values())
        if require_account:
            # Don't offer a ledger-only candidate to a caller that would only
            # refuse it on the next turn -- but keep them if they're all we have,
            # so the "never logged in" answer below can still be given.
            with_account = [item for item in matches if item.get("has_account")]
            if with_account:
                matches = with_account
        if len(matches) > 1:
            shown = matches[:5]
            lead = "That user match is ambiguous: " + ", ".join(
                self._candidate_summary(item) for item in shown
            )
            return {
                "ok": False,
                "reason": "user_ambiguous",
                "user_query": user_query,
                "candidates": matches[:10],
                "user_summary": f"{lead}. {self._pick_or_dead_end(shown, require_account)}",
            }
        match = matches[0]
        if require_account and not match.get("has_account"):
            return {
                "ok": False,
                "reason": "user_not_registered",
                "user_query": user_query,
                "user": match,
                "user_summary": (
                    f"{match.get('label')} is in your friendly-names list but has never "
                    "logged into Plexorcist, so there's no account to attach this to."
                ),
            }
        return {"ok": True, "user": match}

    def _resolve_message_recipient(
        self,
        *,
        user_query: str | None,
        task_query: str | None,
        sender_user_id: str | None,
    ) -> dict[str, Any]:
        query = str(user_query or "").strip()
        task = str(task_query or "").strip()
        if query:
            resolved = self._resolve_user_query(query)
            if resolved.get("ok"):
                resolved["target_basis"] = "user_query"
            return resolved
        if task:
            return self._resolve_task_recipient(task, sender_user_id=sender_user_id)
        return {
            "ok": False,
            "reason": "recipient_required",
            "user_summary": "I need either a user/friendly name or a task/title to identify who should receive the admin message.",
        }

    def _resolve_task_recipient(self, task_query: str, *, sender_user_id: str | None) -> dict[str, Any]:
        query = task_query.strip().lower()
        if not query:
            return {
                "ok": False,
                "reason": "task_query_required",
                "user_summary": "I need a title or task description to find the affected user.",
            }
        rows = self._query_open_tasks(days=365, limit=200)
        users = self._load_user_labels()
        matches: list[dict[str, Any]] = []
        for row in rows:
            user_id = str(row["user_id"])
            # The admin's own memory can contain copied task summaries about other users.
            # For "whoever requested X", prefer the affected user's task row, not Ben's dashboard note.
            if sender_user_id and user_id == str(sender_user_id):
                continue
            metadata = self._parse_json(row.get("metadata_json"), default={})
            searchable = " ".join(
                str(item or "")
                for item in (
                    row.get("content"),
                    row.get("task_id"),
                    json.dumps(metadata, ensure_ascii=False) if metadata else "",
                )
            ).lower()
            if query not in searchable:
                continue
            matches.append(
                {
                    "user_id": user_id,
                    "user_label": self._format_user_label(user_id, users.get(user_id)),
                    "content": row["content"],
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "updated_at": row["updated_at"],
                    "metadata": metadata,
                }
            )
        if not matches:
            return {
                "ok": False,
                "reason": "task_not_found",
                "task_query": task_query,
                "user_summary": f"I could not find an open task matching {task_query}.",
            }
        unique_user_ids = {str(match["user_id"]) for match in matches}
        if len(unique_user_ids) > 1:
            return {
                "ok": False,
                "reason": "task_recipient_ambiguous",
                "task_query": task_query,
                "candidates": matches[:10],
                "user_summary": "That task/title matches multiple users. Pick one: "
                + ", ".join(str(match["user_label"]) for match in matches[:5]),
            }
        match = matches[0]
        user_id = str(match["user_id"])
        user = users.get(user_id) or {"user_id": user_id, "label": user_id}
        return {
            "ok": True,
            "user": {**user, "user_id": user_id, "label": self._format_user_label(user_id, user)},
            "target_basis": "task_query",
            "matched_task": match,
        }

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

    def _format_user_label(self, user_id: str, label: dict[str, Any] | None) -> str:
        if not label:
            return user_id
        friendly = (label.get("friendly_name") or "").strip()
        username = (label.get("username") or "").strip()
        display_name = (label.get("display_name") or "").strip()
        # Ledger-only people have no user_id at all; the old format rendered
        # that as "Mike (mwco8, )", and this string is what lands in every
        # user_summary and candidate list Ben reads.
        suffix = f", {user_id}" if user_id else ""
        if friendly and username and friendly.lower() != username.lower():
            return f"{friendly} ({username}{suffix})"
        if display_name and username and display_name.lower() != username.lower():
            return f"{display_name} ({username}{suffix})"
        base = username or display_name or user_id
        if not user_id:
            return base or "unknown"
        return f"{base} ({user_id})"

    def _parse_json(self, raw: Any, *, default: Any) -> Any:
        try:
            return json.loads(raw or "")
        except Exception:
            return default
