from __future__ import annotations

from clients.plex_client import PlexClient
from clients.sickchill_client import SickChillClient


class EpisodeTools:
    def __init__(self, plex: PlexClient, sickchill: SickChillClient) -> None:
        self.plex = plex
        self.sickchill = sickchill

    async def check_episode_status(self, show: str, season: int, episode: int) -> dict:
        plex_status = await self.plex.check_episode_availability(show=show, season=season, episode=episode)
        sickchill_status = await self.sickchill.check_episode_status(show=show, season=season, episode=episode)
        return {
            "show": show,
            "season": season,
            "episode": episode,
            "status": sickchill_status.get("status"),
            "aired": sickchill_status.get("aired"),
            "present_in_plex": plex_status.get("present"),
            "manual_search_eligible": bool(
                sickchill_status.get("manual_search_eligible") and not plex_status.get("present")
            ),
            "backend_connected": bool(sickchill_status.get("backend_connected")),
            "plex": plex_status,
            "sickchill": sickchill_status,
        }

    async def trigger_sickchill_manual_search(self, show: str, season: int, episode: int) -> dict:
        plex_status = await self.plex.check_episode_availability(show=show, season=season, episode=episode)
        if plex_status.get("present"):
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "already_available_in_plex",
                "backend_connected": True,
                "plex": plex_status,
                "reason": "episode_already_present_in_plex",
            }
        result = await self.sickchill.trigger_manual_search(show=show, season=season, episode=episode)
        result["plex"] = plex_status
        return result

    async def clear_sickchill_ignored_episodes(self, show: str, season: int | None = None) -> dict:
        result = await self.sickchill.clear_ignored_episodes(show=show, season=season)
        result["plex"] = await self.plex.check_availability(show)
        return result

    async def repair_requested_missing_episode(self, show: str, season: int, episode: int) -> dict:
        sickchill_status = await self.sickchill.check_episode_status(show=show, season=season, episode=episode)
        if not sickchill_status.get("backend_connected"):
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "repair_unavailable",
                "backend_connected": False,
                "sickchill": sickchill_status,
                "reason": sickchill_status.get("reason", "sickchill_unreachable"),
            }

        status = str(sickchill_status.get("status") or "unknown").lower()
        aired = sickchill_status.get("aired")
        if aired is False:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "not_aired_yet",
                "backend_connected": True,
                "sickchill": sickchill_status,
            }
        if aired is None and status == "unknown":
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "repair_unavailable",
                "backend_connected": bool(sickchill_status.get("backend_connected")),
                "sickchill": sickchill_status,
                "reason": sickchill_status.get("reason", "unable_to_determine_sickchill_state"),
            }

        if sickchill_status.get("present_in_sickchill") and status in {"downloaded", "archived"}:
            plex_status = await self.plex.check_episode_availability(show=show, season=season, episode=episode)
            return {
                "ok": True,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "already_downloaded_in_sickchill",
                "backend_connected": True,
                "plex": plex_status,
                "sickchill": sickchill_status,
            }

        if status == "ignored":
            wanted_result = await self.sickchill.ensure_episode_wanted(show=show, season=season, episode=episode)
            wanted_result["sickchill"] = sickchill_status
            wanted_result["action"] = "marked_wanted"
            return wanted_result

        if status in {"wanted", "missing", "processing"}:
            result = await self.sickchill.trigger_manual_search(show=show, season=season, episode=episode)
            result["sickchill"] = sickchill_status
            result["action"] = result.get("action") or "manual_search_started"
            return result

        return {
            "ok": False,
            "show": show,
            "season": season,
            "episode": episode,
            "action": "no_repair_action_taken",
            "backend_connected": True,
            "sickchill": sickchill_status,
            "reason": f"unsupported_episode_status:{status}",
        }

    async def repair_requested_missing_season(self, show: str, season: int, episodes: list[int] | None = None) -> dict:
        target_episodes = sorted({int(ep) for ep in (episodes or [])})
        result = await self.sickchill.repair_episode_targets(show=show, season=season, episodes=target_episodes)
        return result

    async def check_episode_file(self, show: str, season: int, episode: int) -> dict:
        plex_status = await self.plex.check_episode_availability(show=show, season=season, episode=episode)
        sickchill_status = await self.sickchill.check_episode_file(show=show, season=season, episode=episode)
        return {
            "show": show,
            "season": season,
            "episode": episode,
            "exists": sickchill_status.get("exists"),
            "path": sickchill_status.get("path"),
            "backend_connected": bool(sickchill_status.get("backend_connected")),
            "present_in_plex": plex_status.get("present"),
            "plex": plex_status,
            "sickchill": sickchill_status,
        }
