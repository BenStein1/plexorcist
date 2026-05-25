from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.models import ToolCallRecord, UserContext
from clients.prowl_client import ProwlClient


_ALERT_COOLDOWN_SECONDS = 15 * 60
_ADMIN_ALERT_LAST_SENT: dict[str, datetime] = {}


class AdminAlertReporter:
    def __init__(self, prowl: ProwlClient | None) -> None:
        self.prowl = prowl

    async def report_tool_call(self, user: UserContext | None, tool_record: ToolCallRecord) -> None:
        result = tool_record.result if isinstance(tool_record.result, dict) else {}
        if user is None or not isinstance(result, dict) or result.get("admin_alert_attempted"):
            return
        alert = self.build_alert(user, tool_record)
        if alert is None:
            return
        alert_key, event, summary, priority = alert
        result["admin_alert_attempted"] = True
        result["admin_alert_event"] = event
        if not self._should_send_admin_alert(alert_key):
            result["admin_alert_suppressed"] = "cooldown"
            return
        if self.prowl is None:
            result["admin_alert_error"] = "prowl_unavailable"
            return
        try:
            notice = await self.prowl.send_notice(summary=summary, priority=priority, event=event)
        except Exception as exc:  # noqa: BLE001
            result["admin_alert_error"] = str(exc)
            return
        if notice.get("ok"):
            result["admin_alert_sent"] = True
        else:
            result["admin_alert_error"] = notice.get("error") or notice.get("response_text") or "prowl_send_failed"

    def build_alert(self, user: UserContext, tool_record: ToolCallRecord) -> tuple[str, str, str, int] | None:
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

        if result.get("failure_type") and result.get("service"):
            service = self._service_label(str(result.get("service")))
            operation = str(result.get("operation") or "operation").replace("_", " ")
            failure_type = str(result.get("failure_type") or "error")
            if failure_type == "http_error":
                problem = f"HTTP {result.get('http_status')} {result.get('http_reason') or ''}".strip()
            else:
                problem = str(result.get("error_message") or result.get("reason") or failure_type)
            family = self._tool_family_label(name)
            return (
                f"{name}:{service}:{operation}:{problem}:{show}:{season}:{episode}",
                "Corrective Action Failed",
                (
                    f"User {self._user_label(user)} asked about {subject}; "
                    f"{family} failed while talking to {service} during {operation}: {problem}."
                ),
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
            if result.get("ok") and result.get("action") == "missing_show_added_to_sickchill":
                action = str(result.get("sickchill_action") or result.get("action") or "")
                return (
                    f"sickchill-add-show-from-repair:{show}:{result.get('tvdb_id')}:{action}",
                    "Corrective Action Taken",
                    (
                        f"User {self._user_label(user)} reported {show} as requested but missing; "
                        "SickChill did not have the show, so I submitted the show add to SickChill."
                    ),
                    0,
                )
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
            if action == "radarr_release_rejected_unknown_movie":
                top_release = result.get("top_release") or {}
                top_title = str(top_release.get("title") or "none")
                return (
                    f"radarr-movie-unknown:{show}:{top_title}",
                    "Corrective Action Failed",
                    (
                        f"User {self._user_label(user)} reported missing requested movie {show}; "
                        f"Radarr found a likely candidate but rejected it as Unknown Movie. Top release: {top_title}."
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

        if result.get("corrective_action_taken"):
            action = str(result.get("action") or name)
            title = str(result.get("title") or result.get("show") or result.get("query") or "unknown item")
            return (
                f"corrective-action:{name}:{title}:{action}",
                "Corrective Action Taken",
                (
                    f"User {self._user_label(user)} triggered corrective action {name} for {title}; "
                    f"result action: {action}."
                ),
                0,
            )
        return None

    def _should_send_admin_alert(self, key: str) -> bool:
        now = datetime.now(timezone.utc)
        last_sent = _ADMIN_ALERT_LAST_SENT.get(key)
        if last_sent and (now - last_sent).total_seconds() < _ALERT_COOLDOWN_SECONDS:
            return False
        _ADMIN_ALERT_LAST_SENT[key] = now
        return True

    def _tool_family_label(self, tool_name: str) -> str:
        if tool_name in {"repair_requested_show", "add_requested_show_to_sickchill"}:
            return "TV repair"
        if tool_name == "repair_requested_movie":
            return "Movie repair"
        return tool_name.replace("_", " ")

    def _service_label(self, service: str) -> str:
        labels = {
            "ombi": "Ombi",
            "sickchill": "SickChill",
            "radarr": "Radarr",
            "jackett": "Jackett",
            "transmission": "Transmission",
            "plex": "Plex",
            "tautulli": "Tautulli",
            "prowl": "Prowl",
        }
        return labels.get(service.lower(), service)

    def _user_label(self, user: UserContext) -> str:
        display_name = (user.display_name or "").strip()
        username = (user.username or "").strip()
        if display_name and username and display_name.lower() != username.lower():
            return f"{display_name} ({username})"
        return display_name or username or "Unknown user"
