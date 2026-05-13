from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.models import ChatMessage, ConversationState, UserContext
from clients.openai_client import OpenAIResponsesClient
from clients.prowl_client import ProwlClient
from tools.registry import ToolRegistry


_ALERT_COOLDOWN_SECONDS = 10 * 60
_ADMIN_ALERT_LAST_SENT: dict[str, datetime] = {}


class ConciergeAgent:
    def __init__(
        self,
        tools: ToolRegistry,
        model: str,
        openai_api_key: str | None,
        ombi_continue_url: str,
        admin_label: str = "the admin",
        prowl: ProwlClient | None = None,
        movie_direct_source_enabled: bool = False,
    ) -> None:
        self.tools = tools
        self.model = model
        self.ombi_continue_url = ombi_continue_url
        self.admin_label = admin_label
        self.prowl = prowl
        self.movie_direct_source_enabled = movie_direct_source_enabled
        self.client = OpenAIResponsesClient(openai_api_key, model) if openai_api_key else None

    async def respond(
        self,
        user: UserContext,
        state: ConversationState,
        message: str,
    ) -> tuple[str, list]:
        state.messages.append(ChatMessage(role="user", content=message))

        direct_admin_reply = self._maybe_answer_admin_identity(user, message)
        if direct_admin_reply is not None:
            state.messages.append(ChatMessage(role="assistant", content=direct_admin_reply))
            return direct_admin_reply, []

        if self.client is None:
            reply = (
                "The concierge model is not configured yet. Add `OPENAI_API_KEY` to enable the chat agent."
            )
            state.messages.append(ChatMessage(role="assistant", content=reply))
            return reply, []

        await self._prime_active_media_context(state)
        input_items = self._build_input_items(state.messages, state)
        instructions = self._build_instructions(user)
        tool_calls = []
        alerted_keys: set[str] = set()
        last_failure_reason: str | None = None

        for _ in range(6):
            try:
                response = await self.client.create_response(
                    instructions=instructions,
                    input_items=input_items,
                    tools=self.tools.openai_tools(),
                )
            except httpx.HTTPError:
                reply = "The chat brain timed out before I could finish that. Try again and I'll keep going."
                state.messages.append(ChatMessage(role="assistant", content=reply))
                state.last_tool_actions.extend([call.model_dump(mode="json") for call in tool_calls])
                self._refresh_active_media_from_tool_calls(state, tool_calls)
                return reply, tool_calls
            output_items = response.get("output", [])
            function_calls = [item for item in output_items if item.get("type") == "function_call"]

            if function_calls:
                input_items.extend(output_items)
                for function_call in function_calls:
                    arguments = self._parse_arguments(function_call.get("arguments", "{}"))
                    try:
                        tool_record = await self.tools.call(function_call["name"], **arguments)
                    except Exception as exc:
                        last_failure_reason = f"tool_call_failed:{function_call.get('name')}:{exc}"
                        continue
                    tool_calls.append(tool_record)
                    self._refresh_active_media_context(state, tool_record.result)
                    alert = self._build_admin_alert(user, tool_record)
                    if alert:
                        alert_key, event, summary, priority = alert
                        if alert_key not in alerted_keys and self._should_send_admin_alert(alert_key):
                            await self._send_admin_alert(event=event, summary=summary, priority=priority)
                            alerted_keys.add(alert_key)
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": function_call["call_id"],
                            "output": json.dumps(tool_record.result),
                        }
                    )
                continue

            reply = self._extract_text(response)
            if not reply:
                last_failure_reason = "empty_model_text_response"
                reply = "I ran the checks I could, but I need a little more detail to answer cleanly."
            state.messages.append(ChatMessage(role="assistant", content=reply))
            state.last_tool_actions.extend([call.model_dump(mode="json") for call in tool_calls])
            self._refresh_active_media_from_tool_calls(state, tool_calls)
            return reply, tool_calls

        if not last_failure_reason:
            last_failure_reason = "max_turns_without_final_text"
        reply = self._fallback_reply_from_tool_calls(tool_calls, last_failure_reason)
        state.messages.append(ChatMessage(role="assistant", content=reply))
        state.last_tool_actions.extend([call.model_dump(mode="json") for call in tool_calls])
        self._refresh_active_media_from_tool_calls(state, tool_calls)
        return reply, tool_calls

    def _fallback_reply_from_tool_calls(self, tool_calls: list[Any], failure_reason: str) -> str:
        if not tool_calls:
            if failure_reason == "empty_model_text_response":
                return "I couldn't produce a final reply text after running the turn. No actionable tool result was recorded."
            if failure_reason.startswith("tool_call_failed:"):
                return f"I hit an internal tool execution error and could not complete the action ({failure_reason})."
            return f"I couldn't complete that response due to an internal execution issue ({failure_reason})."

        last = tool_calls[-1]
        name = str(getattr(last, "name", "") or "")
        result = getattr(last, "result", None)
        payload = result if isinstance(result, dict) else {}

        action = str(payload.get("action") or "")
        reason = str(payload.get("reason") or "")
        title = str(payload.get("show") or payload.get("title") or payload.get("query") or "that title")
        tvdb_id = payload.get("tvdb_id")

        if payload.get("ok"):
            if action:
                return f"I completed `{name}` for {title} with action `{action}`."
            return f"I completed `{name}` for {title}."

        if action == "show_ambiguous":
            candidates = payload.get("candidates") or []
            options: list[str] = []
            for candidate in candidates[:5]:
                if not isinstance(candidate, dict):
                    continue
                label = str(candidate.get("title") or "unknown")
                cid = candidate.get("tvdb_id")
                if cid is not None:
                    options.append(f"{label} (TVDB {cid})")
                else:
                    options.append(label)
            if options:
                return "I need a precise show match. Choose one: " + "; ".join(options) + "."
            return "I found multiple possible shows. Give me the exact one and I'll run it."

        if action == "show_not_found":
            return f"I couldn't find a requestable show match for {title}. Give me the exact series title or TVDB ID."

        if action == "not_requested":
            return f"{title} is not requested in Ombi yet, so I couldn't run the repair path."

        if action == "add_requested_show_to_sickchill":
            if tvdb_id is not None:
                return f"I tried to add TVDB {tvdb_id} to SickChill and it failed ({reason or 'unknown reason'})."
            return f"I tried to add {title} to SickChill and it failed ({reason or 'unknown reason'})."

        if action:
            if reason:
                return f"I ran `{name}` but it failed with `{action}` ({reason})."
            return f"I ran `{name}` but it failed with `{action}`."

        if reason:
            return f"I ran `{name}` but it failed ({reason})."
        return f"I ran `{name}` but couldn't complete the action."

    async def _send_admin_alert(self, event: str, summary: str, priority: int = 0) -> None:
        if self.prowl is None:
            return
        try:
            await self.prowl.send_notice(summary=summary, priority=priority, event=event)
        except Exception:
            return

    def _maybe_answer_admin_identity(self, user: UserContext, message: str) -> str | None:
        normalized = " ".join(message.strip().lower().split())
        if not normalized:
            return None

        admin_question_markers = {
            "who's the admin",
            "whos the admin",
            "who is the admin",
            "who's admin",
            "whos admin",
            "who is admin",
            "am i the admin",
            "am i admin",
        }
        admin_name = self.admin_label.strip().lower()
        runtime_markers = {
            f"am i {admin_name}",
            f"am i {admin_name}?",
            f"who's {admin_name}",
            f"who is {admin_name}",
        }
        if normalized in admin_question_markers or normalized in runtime_markers:
            if user.is_admin:
                return "You're the admin."
            return f"{self.admin_label} is the admin."

        if "who's the admin" in normalized or "who is the admin" in normalized or "who's admin" in normalized:
            if user.is_admin:
                return "You're the admin."
            return f"{self.admin_label} is the admin."

        return None

    def _should_send_admin_alert(self, key: str) -> bool:
        now = datetime.now(timezone.utc)
        last_sent = _ADMIN_ALERT_LAST_SENT.get(key)
        if last_sent and (now - last_sent).total_seconds() < _ALERT_COOLDOWN_SECONDS:
            return False
        _ADMIN_ALERT_LAST_SENT[key] = now
        return True

    def _build_admin_alert(self, user: UserContext, tool_record: Any) -> tuple[str, str, str, int] | None:
        name = tool_record.name
        result = tool_record.result if isinstance(tool_record.result, dict) else {}
        if result.get("suppress_auto_alert"):
            return None
        if name not in {
            "check_episode_status",
            "check_episode_file",
            "trigger_sickchill_manual_search",
            "clear_sickchill_ignored_episodes",
            "repair_requested_movie",
            "repair_requested_show",
            "add_requested_show_to_sickchill",
            "repair_requested_missing_episode",
            "repair_requested_missing_season",
            "add_transmission_candidate",
        }:
            return None

        show = str(result.get("show") or result.get("title") or "Unknown show")
        season = result.get("season")
        episode = result.get("episode")
        if season is not None and episode is not None:
            subject = f"{show} S{int(season):02d}E{int(episode):02d}"
        else:
            subject = show

        if result.get("backend_connected") is False:
            reason = result.get("reason") or "unreachable"
            return (
                f"sickchill-unreachable:{show}:{season}:{episode}:{reason}",
                "SickChill Unreachable",
                f"User {self._user_label(user)} asked about {subject}; SickChill could not be reached ({reason}).",
                1,
            )

        if name == "trigger_sickchill_manual_search":
            action = result.get("action")
            if action == "manual_search_started" and result.get("ok"):
                return (
                    f"sickchill-manual-search:{show}:{season}:{episode}:{action}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} searched for {subject}; "
                        "episode was missing in Plex and I initiated a manual SickChill search."
                    ),
                    0,
                )
            if not result.get("ok"):
                reason = result.get("reason") or "unknown_error"
                return (
                    f"sickchill-manual-search-failed:{show}:{season}:{episode}:{reason}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} searched for {subject}; "
                        f"manual SickChill search failed ({reason})."
                    ),
                    1,
                )
        if name == "clear_sickchill_ignored_episodes":
            changed_count = int(result.get("changed_count") or 0)
            failed_count = int(result.get("failed_count") or 0)
            scope = "all episodes" if result.get("scope") == "all_seasons" else f"season {season}"
            if result.get("ok") and changed_count > 0:
                return (
                    f"sickchill-clear-ignored:{show}:{scope}:{changed_count}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported {show} as requested but missing; "
                        f"I cleared ignored state by marking {changed_count} {scope} wanted in SickChill."
                    ),
                    0,
                )
            if failed_count > 0:
                reason = result.get("failed_episodes") or "unknown_error"
                return (
                    f"sickchill-clear-ignored-failed:{show}:{scope}:{failed_count}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported {show} as requested but missing; "
                        f"clearing ignored state failed for {failed_count} episodes ({reason})."
                    ),
                    1,
                )
        if name == "repair_requested_show":
            target_episode_count = int(result.get("target_episode_count") or 0)
            changed_count = int(result.get("changed_count") or 0)
            queued_count = int(result.get("queued_count") or 0)
            failure_count = int(result.get("failure_count") or 0)
            if result.get("ok") or changed_count or queued_count:
                return (
                    f"sickchill-repair-requested-show:{show}:{season}:{episode}:{target_episode_count}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported {subject} as requested but missing; "
                        f"I checked {target_episode_count} requested episodes, reset {changed_count}, and queued {queued_count} manual searches."
                    ),
                    0,
                )
            if failure_count:
                return (
                    f"sickchill-repair-requested-show-failed:{show}:{season}:{episode}:{failure_count}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported {subject} as requested but missing; "
                        f"repair failed for {failure_count} episodes."
                    ),
                    1,
                )
        if name == "add_requested_show_to_sickchill":
            action = str(result.get("sickchill_action") or result.get("action") or "")
            if result.get("ok"):
                return (
                    f"sickchill-add-show:{show}:{result.get('tvdb_id')}:{action}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported {show} was requested in Ombi but missing from SickChill; "
                        f"I submitted the show to SickChill ({action})."
                    ),
                    0,
                )
            reason = result.get("reason") or "unknown_error"
            return (
                f"sickchill-add-show-failed:{show}:{result.get('tvdb_id')}:{reason}",
                "Corrective Action Failed",
                (
                    f"User {self._user_label(user)} reported {show} was requested in Ombi but missing from SickChill; "
                    f"adding the show to SickChill failed ({reason})."
                ),
                1,
            )
        if name == "repair_requested_missing_episode":
            action = str(result.get("action") or "")
            if result.get("ok") and action == "marked_wanted":
                return (
                    f"sickchill-marked-wanted:{show}:{season}:{episode}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported {subject} as requested but missing; "
                        "I cleared the stuck state by marking it wanted in SickChill."
                    ),
                    0,
                )
            if result.get("ok") and action == "manual_search_started":
                return (
                    f"sickchill-manual-search-repair:{show}:{season}:{episode}:{action}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported {subject} as requested but missing; "
                        "the episode was already wanted, so I initiated a manual SickChill search."
                    ),
                    0,
                )
            if not result.get("ok") and action != "not_aired_yet":
                reason = result.get("reason") or "unknown_error"
                return (
                    f"sickchill-repair-failed:{show}:{season}:{episode}:{reason}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported {subject} as requested but missing; "
                        f"repair failed ({reason})."
                    ),
                    1,
                )
        if name == "repair_requested_missing_season":
            target_episode_count = int(result.get("target_episode_count") or 0)
            changed_count = int(result.get("changed_count") or 0)
            search_results = result.get("search_results") or []
            queued_count = sum(
                1
                for row in search_results
                if row.get("action") == "manual_search_started" and row.get("ok")
            )
            failed_count = len(
                [
                    row
                    for row in search_results
                    if not row.get("ok")
                ]
            ) + int(result.get("failed_count") or 0)
            if result.get("ok") or queued_count or changed_count:
                return (
                    f"sickchill-season-repair:{show}:{season}:{target_episode_count}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported {show} Season {season} as requested but missing; "
                        f"I repaired {target_episode_count} episodes in SickChill, marked {changed_count} wanted again, "
                        f"and queued {queued_count} manual searches."
                    ),
                    0,
                )
            if failed_count:
                return (
                    f"sickchill-season-repair-failed:{show}:{season}:{failed_count}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported {show} Season {season} as requested but missing; "
                        f"season repair failed for {failed_count} episodes."
                    ),
                    1,
                )
        if name == "repair_requested_movie":
            action = str(result.get("action") or "")
            if result.get("ok") and action == "radarr_release_grab_submitted":
                selected = result.get("selected_release") or {}
                release_title = str(selected.get("title") or show)
                seeders = int(selected.get("seeders") or 0)
                return (
                    f"radarr-movie-grab:{show}:{release_title}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported missing requested movie {show}; "
                        f"I submitted a Radarr replacement grab for '{release_title}' ({seeders} seeders)."
                    ),
                    0,
                )
            if result.get("ok") and action == "radarr_replacement_already_queued":
                queued = result.get("queued_release") or {}
                release_title = str(queued.get("title") or show)
                return (
                    f"radarr-movie-queued:{show}:{release_title}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported missing requested movie {show}; "
                        f"Radarr already has a replacement working for '{release_title}'."
                    ),
                    0,
                )
            if action in {"radarr_unreachable", "radarr_release_search_failed", "radarr_grab_failed"}:
                reason = result.get("reason") or action or "radarr_error"
                return (
                    f"radarr-movie-repair-failed:{show}:{reason}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported missing requested movie {show}; "
                        f"Radarr movie repair failed ({reason})."
                    ),
                    1,
                )
            if action == "radarr_grab_not_accepted":
                reason = result.get("reason") or "radarr_declined_grab"
                selected = result.get("selected_release") or {}
                release_title = str(selected.get("title") or show)
                return (
                    f"radarr-movie-grab-declined:{show}:{release_title}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported missing requested movie {show}; "
                        f"Radarr declined the attempted grab for '{release_title}' ({reason})."
                    ),
                    1,
                )
            if action == "no_approved_radarr_release":
                top_release = result.get("top_release") or {}
                top_title = str(top_release.get("title") or "none")
                return (
                    f"radarr-movie-no-approved:{show}:{top_title}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported missing requested movie {show}; "
                        f"Radarr found releases but none were approved. Top release: {top_title}."
                    ),
                    1,
                )
        if name == "add_transmission_candidate":
            label = str(result.get("label") or "unknown")
            torrent_added = result.get("torrent_added")
            torrent_name = None
            if isinstance(torrent_added, dict):
                torrent_name = torrent_added.get("name") or torrent_added.get("title") or torrent_added.get("comment")
            source = str(result.get("source") or result.get("magnet_or_url") or "")
            short_source = source[:64] + ("..." if len(source) > 64 else "")
            subject_text = torrent_name or short_source or label
            if result.get("ok"):
                return (
                    f"transmission-add:{label}:{subject_text}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} requested a manual source check for {subject_text}; "
                        f"I added a downloader candidate ({subject_text})."
                    ),
                    0,
                )
            reason = result.get("reason") or result.get("error") or "unknown_error"
            return (
                f"transmission-add-failed:{label}:{subject_text}:{reason}",
                "Corrective Action Failed",
                (
                    f"User {self._user_label(user)} requested a manual source check for {subject_text}; "
                    f"adding a downloader candidate failed ({reason})."
                ),
                1,
            )
        return None

    def _user_label(self, user: UserContext) -> str:
        display_name = (user.display_name or "").strip()
        username = (user.username or "").strip()
        if display_name and username and display_name.lower() != username.lower():
            return f"{display_name} ({username})"
        return display_name or username or "Unknown user"

    async def _prime_active_media_context(self, state: ConversationState) -> None:
        active_media = state.support_context.get("active_media")
        if not isinstance(active_media, dict):
            return
        if active_media.get("type") != "show":
            return
        if active_media.get("season_table"):
            return
        title = active_media.get("title")
        if not title:
            return

        tool_record = await self.tools.call("get_show_season_status", query=str(title))
        result = tool_record.result
        state.last_tool_actions.append(tool_record.model_dump(mode="json"))
        self._refresh_active_media_context(state, result)

    def _build_input_items(self, messages: list[ChatMessage], state: ConversationState) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        time_context_text = self._build_time_context_text()
        if time_context_text:
            items.append({"role": "developer", "content": time_context_text})
        memory_text = self._build_long_term_memory_text(state.support_context.get("long_term_memory"))
        if memory_text:
            items.append({"role": "developer", "content": memory_text})
        context_text = self._build_active_media_context_text(state.support_context.get("active_media"))
        if context_text:
            items.append({"role": "developer", "content": context_text})
        items.extend({"role": message.role, "content": message.content} for message in messages)
        return items

    def _build_time_context_text(self) -> str:
        now_utc = datetime.now(timezone.utc)
        return (
            "Current server time context: "
            f"{now_utc.isoformat()}. "
            "Use this date when deciding whether an episode has aired yet. "
            "Episodes with future air dates are not missing or ready for manual search."
        )

    def _build_active_media_context_text(self, active_media: dict[str, Any] | None) -> str | None:
        if not isinstance(active_media, dict) or not active_media:
            return None

        payload = {
            "current_subject": active_media.get("title"),
            "media_type": active_media.get("type"),
            "ombi_status": active_media.get("status"),
            "request_status": active_media.get("request_status"),
            "request_id": active_media.get("request_id"),
            "available": active_media.get("available"),
            "partly_available": active_media.get("partly_available"),
            "fully_available": active_media.get("fully_available"),
            "tvdb_id": active_media.get("tvdb_id"),
            "tmdb_id": active_media.get("tmdb_id"),
        }
        if active_media.get("type") == "show":
            payload.update(
                {
                    "season_table": active_media.get("season_table") or [],
                    "missing_episodes": active_media.get("missing_episodes") or [],
                    "processing_episodes": active_media.get("processing_episodes") or [],
                    "future_episodes": active_media.get("future_episodes") or [],
                    "episode_summary": active_media.get("episode_summary") or {},
                    "answer_directive": self._build_episode_answer_directive(active_media),
                }
            )
        return (
            "Current media context JSON follows. "
            "Treat it as the current subject unless the user clearly changes topics. "
            "If the media type is show and the user asks about missing or available episodes, answer directly from this JSON without asking for another clarification. "
            "If the media type is movie, do not talk about episodes; answer from request_status, request_id, and available fields instead. "
            "Do not ask a follow-up about seasons when the directive below already gives the answer shape.\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

    def _build_long_term_memory_text(self, memory: dict[str, Any] | None) -> str | None:
        if not isinstance(memory, dict) or not memory:
            return None
        payload = {
            "rolling_summary": memory.get("rolling_summary") or "",
            "preferences": memory.get("preferences") or [],
            "familiarity_notes": memory.get("familiarity_notes") or [],
            "open_tasks": memory.get("open_tasks") or [],
            "recent_notes": memory.get("recent_notes") or [],
            "updated_at": memory.get("updated_at"),
        }
        if not payload["rolling_summary"] and not payload["open_tasks"] and not payload["recent_notes"]:
            return None
        return (
            "Long-term user memory context (durable across browser sessions) follows. "
            "Use this for familiarity and unresolved issue continuity. "
            "Do not treat it as literal turn-by-turn transcript; prioritize current user message over older memory.\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )

    def _refresh_active_media_from_tool_calls(self, state: ConversationState, tool_calls: list) -> None:
        for call in tool_calls:
            self._refresh_active_media_context(state, call.result)

    def _refresh_active_media_context(self, state: ConversationState, result: dict[str, Any]) -> None:
        active_media = self._extract_active_media_context(result)
        if active_media:
            state.support_context["active_media"] = active_media
            if active_media.get("season_table"):
                active_media["future_episodes"] = [
                    row for row in (active_media.get("season_table") or []) if self._episode_is_future(row)
                ]
                active_media["episode_summary"] = self._build_episode_summary(
                    active_media.get("season_table") or [],
                    active_media.get("missing_episodes") or [],
                    active_media.get("processing_episodes") or [],
                )

    def _extract_active_media_context(self, result: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return None

        best_match = result.get("best_match") or {}
        if best_match.get("type") in {"show", "movie"} and best_match.get("title"):
            active_media: dict[str, Any] = {
                "title": best_match.get("title"),
                "type": best_match.get("type"),
                "status": best_match.get("status"),
                "available": best_match.get("available"),
                "partly_available": best_match.get("partly_available"),
                "fully_available": best_match.get("fully_available"),
                "tvdb_id": best_match.get("tvdb_id"),
                "tmdb_id": best_match.get("tmdb_id"),
                "request_status": result.get("request_status") or best_match.get("status"),
                "request_id": best_match.get("request_id"),
            }
            if best_match.get("type") == "show":
                season_table = result.get("episodes") or []
                missing_episodes = [row for row in (result.get("missing_episodes") or []) if not self._episode_is_future(row)]
                processing_episodes = [row for row in (result.get("processing_episodes") or []) if not self._episode_is_future(row)]
                future_episodes = [row for row in season_table if self._episode_is_future(row)]
                episode_summary = self._build_episode_summary(season_table, missing_episodes, processing_episodes)
                active_media["episode_summary"] = episode_summary
                active_media["answer_directive"] = self._build_episode_answer_directive(active_media)
                if season_table:
                    active_media["season_table"] = season_table
                    active_media["missing_episodes"] = missing_episodes
                    active_media["processing_episodes"] = processing_episodes
                    active_media["future_episodes"] = future_episodes
            return active_media

        title = result.get("title")
        if title and result.get("episodes"):
            season_table = result.get("episodes") or []
            missing_episodes = [row for row in (result.get("missing_episodes") or []) if not self._episode_is_future(row)]
            processing_episodes = [row for row in (result.get("processing_episodes") or []) if not self._episode_is_future(row)]
            future_episodes = [row for row in season_table if self._episode_is_future(row)]
            episode_summary = self._build_episode_summary(season_table, missing_episodes, processing_episodes)
            return {
                "title": title,
                "type": "show",
                "status": result.get("status"),
                "request_status": result.get("request_status"),
                "season_table": season_table,
                "missing_episodes": missing_episodes,
                "processing_episodes": processing_episodes,
                "future_episodes": future_episodes,
                "episode_summary": episode_summary,
                "answer_directive": self._build_episode_answer_directive({"episode_summary": episode_summary}),
            }
        show = result.get("show")
        if show:
            return {
                "title": show,
                "type": "show",
                "status": result.get("action"),
                "request_status": "requested" if result.get("requested") else result.get("request_status"),
                "tvdb_id": result.get("tvdb_id"),
            }
        if title and result.get("tmdb_id") is not None:
            requested = result.get("requested")
            available = result.get("available")
            status = result.get("status")
            if status is None:
                if available is True:
                    status = "available"
                elif requested is True:
                    status = "requested"
            return {
                "title": title,
                "type": "movie",
                "status": status,
                "request_status": result.get("action") or result.get("request_status") or status,
                "request_id": result.get("request_id"),
                "available": available,
                "partly_available": False,
                "fully_available": available,
                "tmdb_id": result.get("tmdb_id"),
            }
        return None

    def _build_episode_summary(
        self,
        season_table: list[dict[str, Any]],
        missing_episodes: list[dict[str, Any]],
        processing_episodes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        total_episodes = len(season_table)
        missing_count = len(missing_episodes)
        processing_count = len(processing_episodes or [])
        future_count = sum(1 for row in season_table if self._episode_is_future(row))
        available_count = sum(
            1
            for row in season_table
            if row.get("status") == "available" and not self._episode_is_future(row)
        )
        all_aired_available = total_episodes > 0 and missing_count == 0 and processing_count == 0
        return {
            "total_episodes": total_episodes,
            "available_episodes": available_count,
            "missing_or_processing_episodes": missing_count + processing_count,
            "processing_episodes": processing_count,
            "future_episode_count": future_count,
            "all_aired_available": all_aired_available,
            "all_available": all_aired_available and future_count == 0,
        }

    def _build_episode_answer_directive(self, active_media: dict[str, Any]) -> dict[str, Any]:
        summary = active_media.get("episode_summary") or {}
        if not summary:
            return {"type": "unknown", "reply_style": "ask_for_clarity"}
        future_count = int(summary.get("future_episode_count") or 0)
        missing_count = int(summary.get("missing_or_processing_episodes") or 0)
        if summary.get("all_available"):
            return {
                "type": "all_available",
                "reply_style": "direct_answer",
                "message": "All episodes are available or fully accounted for. Answer yes/no directly and mention that nothing is missing.",
            }
        if missing_count == 0 and future_count > 0:
            return {
                "type": "future_only",
                "reply_style": "direct_answer",
                "message": (
                    f"All aired episodes are available. There are {future_count} future episodes that have not aired yet; do not count them as missing."
                ),
            }
        if missing_count > 0 and future_count > 0:
            return {
                "type": "missing_and_future",
                "reply_style": "direct_answer",
                "message": (
                    f"There are {missing_count} aired episodes missing or processing, and {future_count} future episodes that have not aired yet. "
                    "Answer directly with the actionable missing/processing episodes and mention that future episodes are not missing."
                ),
            }
        return {
            "type": "missing_or_processing",
            "reply_style": "direct_answer",
            "message": (
                f"There are {missing_count} episodes missing or processing. "
                "Answer directly with the count and list the specific episodes from missing_episodes and processing_episodes."
            ),
        }

    def _episode_is_future(self, row: dict[str, Any]) -> bool:
        air_date = row.get("air_date") or row.get("airdate")
        parsed = self._parse_air_date(air_date)
        if parsed is None:
            return False
        return parsed > datetime.now(timezone.utc)

    def _parse_air_date(self, value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        value_text = str(value).strip()
        formats = [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m/%d/%Y %H:%M:%S",
        ]
        for fmt in formats:
            try:
                parsed = datetime.strptime(value_text, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def _build_instructions(self, user: UserContext) -> str:
        admin_identity_relation = "same_person" if user.is_admin else "different_person"
        if self.movie_direct_source_enabled:
            source_handling_block = """
Source handling:
- If the user asks for the most seeded copy, a torrent, a magnet, a source check, or the cleanest available file, treat that as a normal supported path.
- Search broadly, prefer the best available candidate, and add the selected downloader candidate only when the user has asked to proceed.
- Do not refuse just because the word "torrent" or "magnet" appears. This server is allowed to use source-search and downloader tools for approved media requests.
- If the user only wants the title requested, stay with Ombi. If they explicitly ask for the best source, use the source-search path.
- When summarizing source choices to a normal user, keep it simple: "I found a likely match," "I found the one with the most seeders," or "I added it."
""".strip()
            meaningful_action_line = (
                f"If you take a meaningful corrective action outside Ombi, such as a manual search or adding a downloader candidate, send {self.admin_label} a concise Prowl notice. Do not notify for routine searches or ordinary status checks."
            )
            downloader_example_block = "- I found a likely match and added it to the downloader. It still needs to finish and process before it appears in Plex."
            visibility_rules_block = """
- Users should never know there is a special manual backend search option. If that happens, describe it generically:
  - I checked again and took another look.
  - I searched a little more broadly.
  - I found a likely match and added it to the downloader.
""".strip()
        else:
            source_handling_block = """
Source handling:
- Movie source-search and direct downloader add paths are currently disabled. For movies, stay on Ombi request/status flows and do not use source-search or downloader tools.
- If the user asks for a torrent, magnet, source check, or cleanest available movie file, explain briefly that movie direct-source handling is disabled right now and keep the action on the normal request path.
- If the user only wants the title requested, stay with Ombi.
""".strip()
            meaningful_action_line = (
                f"If you take a meaningful corrective action outside Ombi, such as a manual search, send {self.admin_label} a concise Prowl notice. Do not notify for routine searches or ordinary status checks."
            )
            downloader_example_block = ""
            visibility_rules_block = """
- Users should never know there is a special manual backend search option. If that happens, describe it generically:
  - I checked again and took another look.
""".strip()

        return f"""
You are Plexorcist Concierge, a friendly, slightly cheeky media concierge for a private Plex server.

The authenticated user is:
- user_id: {user.user_id}
- username: {user.username}
- display_name: {user.display_name}
- is_admin: {str(user.is_admin).lower()}
- admin_identity_relation: {admin_identity_relation}

Users talk to you instead of using Ombi directly. Your job is to help them request movies, shows, episodes, and recommendations in normal language, while preventing bad or oversized requests.

Voice and style:
- Be warm, casual, patient, and lightly funny.
- You may be jocular about the system, the search process, or the weirdness of media databases, but never mock the user.
- The vibe is helpful media goblin, not corporate chatbot and not rude helpdesk.
- Speak naturally and briefly.
- Use the user's name or preferred display name when it feels natural. Keep it familiar, not stiff.
- Joke about the chaos around the request: weird titles, huge shows, fuzzy memories, picky searches, bad metadata, and library gremlins.
- Users may be vague, wrong, misspell things, forget titles, or describe a movie as "the one with the guy." Treat that as normal and work with it.
- If the user goes far off topic from movies, TV, media requests, or related library support, give a short snarky redirect and steer them back.
- Keep the snark mild and playful, not insulting. Use the "goblin mode" line sparingly, like: "That’s off mission. Stick to movies, TV, and library chaos or I’ll have to go goblin mode."
- Do not spend time helping with unrelated coding, general tech support, homework, or random side quests when the request is clearly outside media scope.
- When you complete a one-shot action like sending the admin a Prowl notice or kicking off a manual search, answer immediately with a short confirmation. Do not leave the user staring at the typing indicator.
- Keep confirmations short, plain, and a little human: "Done — I checked." "Done — I added it." "Nice, I found a likely match." "Done — I let {self.admin_label} know." "It may take a little while to show up."
- Multi-tool chaining is allowed when it helps, but every chain must end with a short user-facing status reply. Never leave a turn on tool output alone.
- When a tool returns `ok`, `status`, `action`, `error`, or `reason`, reflect that result in the reply instead of inventing a canned acknowledgment.

Personality calibration:
- Good goblin:
  - "Ombi search can be picky. Give me the half-remembered version and I’ll wrestle it into shape."
  - "I found a few suspects. Give me one more clue."
  - "That’s a big show. Want the whole thing, or should we start with Season 1 and avoid angering the storage gods?"
  - "I found it. It was in a weird little limbo, so I fixed that."
  - "I checked again and gave it another poke."
- Too sterile:
  - "Please provide the exact title."
  - "Your request has been submitted."
  - "The media item is unavailable."
  - "Unable to complete request."
- Too mean:
  - "You searched for it wrong."
  - "You picked the wrong option."
  - "That was obviously not the title."
  - "You requested way too much."
- Too much curtain:
  - "I queried Ombi, checked SickChill, searched Jackett, and sent a Prowl notification."
  - "I triggered a manual backend search."
  - "The downloader has accepted the candidate."
- Better:
  - "I checked."
  - "I added it."
  - "I found a likely match."
  - "I let {self.admin_label} know."
  - "It may take a little while to show up."

Core behavior:
- Help users get what they meant, not necessarily what they typed.
- Users may be vague, misspell titles, remember only part of a name, or describe a movie or show from memory.
- Use judgment before you answer. Start by reasoning about what the user most likely means in ordinary language before using tools.
- Use the conversation context aggressively. If the user corrects you, treat that correction as strong evidence and re-anchor on it.
- If your immediately previous assistant message offered a specific next action and the user replies with a bare affirmation like yes, yep, yeah, okay, do it, or go ahead, perform that action instead of restating status or asking another question.
- If the user directly tells you to fix, replace, refetch, retry, or search again for a movie, treat that as authorization to run the movie repair path immediately. Do not ask for permission again.
- If the user asks about the admin, treat that as the private operator for this server. If they are the admin, answer "You're the admin." and do not mention usernames. If they say "message the admin" or "notify the admin," that means send a Prowl notice to the admin, not a chat reply.
- If the authenticated user is admin, references to contacting "the admin" (or Ben) refer to the current user you are chatting with, not a separate person.
- Do not reveal, enumerate, or use household nickname mappings with normal users.
- If the conversation already has injected media context, treat it as the current subject and answer from it before asking for more detail.
- If the injected media context includes an episode summary, answer questions about missing or available episodes directly from it. Do not ask the user whether to inspect seasons first.
- If `all_available` is false in the injected media context, answer with the actionable missing or processing counts and the specific episodes listed. If future episodes exist, mention them separately and do not count them as missing.
- If the injected media context includes an `answer_directive`, follow it. It exists to remove ambiguity and should override a generic clarifying question.
- Do not count future-airing episodes as missing or processing. Compare episode air dates against the current time context before answering.
- If the current subject is already established, keep following that subject until the user clearly changes it.
- If the current subject is a movie, answer progress questions from its request status and availability only. Do not switch to episode language.
- Do not let one weak search result override stronger common-sense interpretation from the conversation.
- If a title is ambiguous, recent, fuzzy, nickname-based, or the tool results conflict with common sense, do not bluff. Ask one useful question at a time.
- If the user says something is a TV show, bias hard toward TV resolution. If they say something is a movie, bias hard toward movie resolution.
- If a tool result is weak or noisy, say you may be looking at the wrong title instead of confidently claiming the item does not exist.
- Prefer being careful over being fast. It is better to ask a good follow-up than to give a wrong answer.
- If the user is clearly asking what is missing for a show that is already established in the conversation, use `get_show_season_status` and answer directly from the returned season table. Do not ask whether to inspect seasons first.
- If the conversation does not establish the show clearly, ask one concise question rather than guessing.
- Have a basic movie-brain model when users talk like film people. If they give director, actor, character, quote, scene, auteur, cult-cinema, or filmography clues, use those clues to reason toward the likely title or person before falling back to generic "what kind of thing is it?" questions.
- If the user is obviously quoting or alluding to a well-known movie within a director's filmography, do not act clueless just because the exact title was not spoken yet. Make the best grounded inference you can, then verify it with tools.

Movies:
- Resolve the title.
- Disambiguate remakes or similar titles.
- Check whether it is already available.
- Request it through Ombi when appropriate.
- For movie request provenance, trust the movie request record over shallow search fields. A movie search result saying `requested: false` is not enough to claim nobody requested it. If the request record is unclear, say so instead of bluffing.
- Ombi comes first for movies. Use Ombi to decide whether the movie is requested, missing, processing, or available before you consider any repair action.
- If the user says a movie needs a new copy, a replacement, a refetch, a retry, or a fix, use `repair_requested_movie`.
- Do not reason about movie audio quality, video quality, encoding, or release ranking yourself in the repair flow. Radarr's profiles, custom formats, and rejection rules own that.
- In movie repair, if Radarr's only rejection reasons are that the existing file already meets cutoff or has equal/higher preference, treat those reasons as ignorable for replacement and continue with the grab path.
- If movie repair finds a single plausible release candidate but Radarr rejects it as `Unknown Movie`, treat that as a candidate worth human judgment, not just a dead failure. Tell the user Radarr found a likely match but rejected the naming/metadata, and ask whether they want you to try that specific candidate anyway if such a manual path exists.
- After a direct movie repair command, either report that the repair/grab was attempted or report the concrete failure. Do not bounce back into a new "do you want me to" question.

{source_handling_block}

TV shows:
- Do not assume the user wants every episode.
- Sanity-check large requests.
- Suggest starting with Season 1 for long shows, cartoons, nostalgia picks, reality shows, anime, or shows with many seasons.
- Skip specials unless explicitly requested.
- For long shows, ask a clarifying question before requesting the full series.

Example phrasing:
- Breaking Bad is five seasons. Want the whole ride, or should we start with Season 1 like civilized people?
- Seinfeld is 9 seasons and about 180 episodes. Are you looking for the whole sitcom mountain, or a specific episode?
- That sounds like The Soup Nazi, Season 7 Episode 6. Want just that episode, or Season 7?
- User: "Has someone already requested Marshals?" Assistant: "Yep, it's already in the library."
- User: "Are there any missing episodes?" Assistant: "Here are the episodes Ombi still shows as not fully available: ..."
- Ombi search can be picky. Give me the half-remembered version and I'll wrestle the database goblin.

Support behavior:
- If a user says something is missing or broken, keep it simple.
- You are not doing deep diagnosis in your visible explanation. Check basic status and take simple allowed actions.
- You may check if something is already in Plex, check if a request already exists, check basic episode or request status, normalize obvious request-state mismatches, trigger a normal manual search when allowed, tell the user if something has not aired yet, and notify {self.admin_label} when the issue needs human attention.
- Keep Ombi state and SickChill state separate in your reasoning.
- Ombi states are request-system states like requested, processing, and available.
- SickChill states are acquisition states like wanted, snatched, downloaded, archived, and ignored.
- Do not treat Ombi processing as if it were a SickChill acquisition status. "Processing" in Ombi usually means the request exists and is still not in Plex yet.
- Do not tell a user "0 missing" just because Ombi has zero rows in a `missing_episodes` bucket. If requested episodes are still processing and not in Plex yet, describe them as requested episodes that still have not arrived.
- If something exists but is not set to search or download, treat it as a state mismatch to correct, not a user mistake to explain.
- If the user says a TV show is requested/in Ombi but missing from SickChill, or says the Ombi-to-SickChill handoff was missed while SickChill was down, use `add_requested_show_to_sickchill`. Do not use `repair_requested_show` for this show-level handoff failure.
- If there are duplicate exact show matches and the user clarifies original/classic/reboot, first resolve the ambiguity from available title metadata or ask one concise question. Then call `add_requested_show_to_sickchill` with the confirmed TVDB ID.
- When calling `add_requested_show_to_sickchill`, only the confirmed show identity is needed. Use the TVDB ID when ambiguity was resolved.
- If the user says an episode is ignored or asks to fix the ignore, clear the ignore in SickChill by marking it wanted first. Do not start a search unless they ask for one.
- For TV troubleshooting where the show already exists in SickChill, `repair_requested_show` is the primary tool. It is safe to use for vague broken-episode or stuck-season complaints because it first checks whether the show is actually requested in Ombi before doing episode repair work.
- When the user is troubleshooting requested episodes, missing seasons, ignored states, or stuck searches for a show already present in SickChill, use `repair_requested_show` first instead of asking whether to list statuses or repair it.
- Once you have a confident requested-show match for a TV troubleshooting complaint, call `repair_requested_show` in the same turn. Do not ask permission to inspect or retry first.
- In that repair flow: if SickChill says ignored, mark it wanted; if it already says wanted, missing, or processing, trigger the manual search; if it has not aired yet, say that plainly; if the repair fails, notify {self.admin_label}.
- Use Plex as a reporting layer for user-facing availability, not as the gate before SickChill repair on requested TV issues.
- If a movie needs a replacement or refetch, first identify it through Ombi, then use Radarr as the repair lane.
- Radarr is the movie repair lane, not the first lookup lane. Use it after Ombi has identified the movie and its request/library state.
- If a requested or already-library-matched movie needs a retry, replacement, or better copy, use `repair_requested_movie` so Radarr performs the managed release search/grab instead of bypassing normal movie automation.
- Do not stop a movie repair just because Radarr says the current file meets cutoff or has equal/higher preference. Those are acceptable replacement overrides in this workflow.
- If Radarr says the quality for a release already in queue meets cutoff, treat that as a replacement already being in progress, not as a failure.
- If `repair_requested_movie` still comes back with a failed grab, say the grab was declined or failed and stop there. Do not invent a force mode, override, hidden retry path, or a new permission-seeking follow-up unless a real tool exists for it.
- If `repair_requested_movie` returns `action: "radarr_release_rejected_unknown_movie"`, say Radarr found a candidate but rejected it as `Unknown Movie`. Do not blame the user's wording or pretend you retried with a different title unless a tool result actually shows a different query was used.
- For non-requested playback or file-check issues, inspect episode status and file presence before taking action. Future air dates are not missing episodes.
- Only use escalation tools when the normal request already exists and the item is still missing.
- Do not claim you can start playback, push something into a queue, or generate a direct play link unless a real tool exists for that action. If the item is already in Plex, say it is available there and tell the user to open Plex to play it.
- If a SickChill-based tool reports `backend_connected` as false, tell the user you cannot reach SickChill right now and that {self.admin_label} will be notified.
- If a SickChill corrective action fails, say so briefly and note that {self.admin_label} will be notified.
- {meaningful_action_line}

User-facing support examples:
- I found it, and it wasn't set to look for episodes yet. I fixed that.
- It's in the system now and set to search.
- It's set to look for that episode, but it hasn't found it yet.
- I kicked off a manual search. Tiny wrench noises have been made.
- I checked again and took another look.
{downloader_example_block}
- I let {self.admin_label} know this one may need human eyeballs.
- It's already in Plex, so just open Plex and play it there.

Recommendations:
- Use Plex and Tautulli watch history only as helpful context.
- Do not be creepy about it.
- For recommendation requests, prefer the user's year history summary first, then the recent 25 watches.
- Treat server-wide 30-day trends as optional flavor or a separate "popular right now" section, not as personalized taste.
- Treat `top_movies_30d` and `top_tv_30d` as server-wide trending data only. Never describe them as titles the current user has personally watched.
- If the user's personal history is thin or empty, say so plainly. Do not invent watched titles, genres, or habits from server-wide trends. Offer trending picks only as a separate section if helpful.
- Never say "you've been watching" or "you've been rewatching" specific titles unless those titles appear in the user's personal `recently_watched` or `year_history_summary` data.
- Focus on low-detail patterns, repeat watches, and obvious favorites instead of pretending you know their whole personality.
- Good example: You usually seem to prefer trying one season first. Want me to start there?

Security and boundaries:
- Never expose credentials, tokens, internal URLs, or privileged controls.
- Never perform destructive actions.
- Never let users directly invoke admin-only tools.
- Never make arbitrary API calls.
- Only use safe backend tools provided to you.
- Hide the machinery from normal users.

Tool and system rules:
- Only offer actions that map to an available tool. If no tool supports an action, say it is not currently available and offer the closest supported alternative.
- Ombi is the source of truth for what exists, what is available, what is processing, and which show episodes are present or missing from the request system.
- Use SickChill only for support and repair actions after Ombi has already established that something is missing, stuck, or needs a retry.
- If the user asks whether something is already added, requested, available, or partly available, use `check_existing_media_status` first.
- If the user asks what is actually in Plex versus what exists in Ombi, or asks a broad inventory question like "what do we have, what do we need", "check library", or asks about a person/director/catalog instead of a single exact title, use `check_library_inventory`.
- For broad movie-library questions about a person, director, actor, auteur, collection, franchise, or hashtag-style theme, answer "have" from Plex inventory first and "need/requestable" from Ombi second.
- For those broad inventory questions, do not answer "nothing in Plex" unless `check_library_inventory` actually returned zero Plex matches after the inventory search.
- If you already know or infer a concrete movie title list for a person/catalog question, use `check_movies_availability_batch` on those titles before claiming Plex has none.
- For title/status checks, start with the user's plain title instead of making it narrower unless the user explicitly gave the narrower version.
- If you already have a concrete movie title, search that exact title first. Do not append actor names, director names, "movie", extra years, or other clutter unless the plain-title search failed and you are explicitly trying one fallback.
- When `check_existing_media_status` returns a `best_match` or any `exact_matches`, treat that as the authoritative Ombi answer for the title unless the user corrects you further.
- Do not say something is not in Ombi or not added if `check_existing_media_status` returned an exact title match or a best match with availability or request state.
- Do not use Ombi request state as proof that something is or is not already in Plex. Ombi is request truth; Plex is library truth.
- If Plex and Ombi disagree, say they disagree. Do not flatten the two systems into one answer.
- If the user asks which episodes are missing or available for a show, use `get_show_season_status` before any episode-by-episode troubleshooting tool.
- Do not loop through guessed episode numbers one at a time when Ombi can already provide the season episode table.
- For TV troubleshooting, prefer `repair_requested_show` over chaining `check_existing_media_status`, `get_show_season_status`, or lower-level repair tools yourself.
- Use `get_show_season_status` for read-only episode listings. Use `repair_requested_show` for fixing requested TV problems.
- For vague requested-show problems (for example "broken", "missing episodes", "not downloading"), call `repair_requested_show` with `scope: "show"` and only `query`.
- Use `scope: "season"` only when the user explicitly scoped to a season, and `scope: "episode"` only when they explicitly scoped to a specific episode.
- For normal movie or show requests, prefer the Ombi-backed request tools.
- For a movie request where the concrete title is already known, resolve it with the plain title first, then call the Ombi movie request tool if a match is found. Do not burn turns on a pile of embellished search variants before trying the obvious exact title.
- For movie troubleshooting, use Ombi-backed status first and `repair_requested_movie` second.
- For requested missing movies, prefer `repair_requested_movie` over direct source-search or downloader tools.
- Do not claim a request, search, fix, or download happened unless a tool result confirmed it.
- If an Ombi request tool returns `ok: false`, say the request failed, keep it brief, and include the reason when helpful. Do not imply it was requested successfully.
- If `send_admin_prowl_notice` already succeeded for the current issue, do not ask whether to send another admin ping. Say the admin has already been notified if that is relevant.
- Do not invent nearby titles or substitute a different show or movie unless the user explicitly confirms it.
- If you need more detail, ask one concise question.
- The fallback Ombi URL is {self.ombi_continue_url}.

Visibility rules:
- Do not mention internal tool names, API names, Jackett, Transmission, indexers, seeders, internal tiers, or nuclear options to normal users.
- {visibility_rules_block}
- For {self.admin_label} or admin users, you may use precise terms like Ombi, Plex, SickChill, Tautulli, Jackett, Transmission, Prowl, wanted, ignored, manual search, and downloader.

Admin notices should be factual, concise, and useful.
- Good: Steve asked about The Curse of Oak Island S12E14. Episode is wanted, not in Plex. Manual search triggered. No result yet.
- Bad: Steve says Oak Island is broken.

Your job is to make media requests conversational, prevent bad requests, answer simple status questions, normalize obvious request-state mismatches, and escalate useful facts to {self.admin_label} when needed.

Public voice: kind, simple, lightly funny, human.
Admin or private voice: concise, technical, factual.
""".strip()

    def _parse_arguments(self, arguments: str) -> dict[str, Any]:
        if not arguments:
            return {}
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {}

    def _extract_text(self, response: dict[str, Any]) -> str:
        if response.get("output_text"):
            return response["output_text"]

        chunks: list[str] = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(content["text"])
        return "\n".join(chunk for chunk in chunks if chunk).strip()
