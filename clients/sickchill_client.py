from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from clients.base import BaseHttpClient

logger = logging.getLogger("plexorcist.sickchill")


class SickChillClient(BaseHttpClient):
    def __init__(self, base_url: str, api_key: str | None = None, tv_root: str | None = None) -> None:
        super().__init__(base_url, api_key)
        self.request_timeout_seconds = 45.0
        self.manual_search_timeout_seconds = 30.0
        self.add_show_timeout_seconds = 60.0
        self.tv_root = (tv_root or "").strip()

    async def add_show(
        self,
        tvdb_id: int,
        title: str | None = None,
        status: str = "wanted",
        future_status: str = "wanted",
    ) -> dict[str, Any]:
        existing = await self._resolve_show_by_indexer_id(tvdb_id)
        if isinstance(existing, dict) and existing.get("backend_connected") is False:
            return {
                "ok": False,
                "title": title,
                "tvdb_id": tvdb_id,
                "action": "add_show_unavailable",
                "backend_connected": False,
                "show_found": False,
                "reason": existing.get("error") or "sickchill_unreachable",
            }
        if existing:
            return {
                "ok": True,
                "title": title or existing.get("show_name"),
                "tvdb_id": tvdb_id,
                "action": "already_in_sickchill",
                "backend_connected": True,
                "show_found": True,
                "show_info": existing,
            }

        params: dict[str, Any] = {
            "indexerid": tvdb_id,
            "tvdbid": tvdb_id,
            "status": status,
            "future_status": future_status,
        }
        if self.tv_root:
            params["location"] = self.tv_root

        try:
            payload = await self._api_get("show.addnew", timeout=self.add_show_timeout_seconds, **params)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "title": title,
                "tvdb_id": tvdb_id,
                "action": "add_show_failed",
                "backend_connected": True,
                "show_found": False,
                "reason": str(exc),
            }

        result = str(payload.get("result") or "").lower()
        data = self._unwrap_data(payload)
        ok = result == "success"
        verified = await self._resolve_show_by_indexer_id(tvdb_id) if ok else None

        return {
            "ok": ok,
            "title": title,
            "tvdb_id": tvdb_id,
            "action": "show_add_submitted" if ok else "add_show_failed",
            "backend_connected": True,
            "show_found": bool(verified),
            "status": status,
            "future_status": future_status,
            "sickchill_result": data,
            "show_info": verified if isinstance(verified, dict) else None,
            "reason": None if ok else payload.get("message") or "show_add_failed",
        }

    async def check_episode_status(
        self,
        show: str,
        season: int,
        episode: int,
        expected_indexer_id: int | None = None,
    ) -> dict:
        show_info = (
            await self._resolve_show_by_indexer_id(expected_indexer_id)
            if expected_indexer_id is not None
            else None
        )
        if expected_indexer_id is None and not show_info:
            show_info = await self._resolve_show(show)
        if isinstance(show_info, dict) and show_info.get("backend_connected") is False:
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "aired": None,
                "present_in_sickchill": False,
                "manual_search_eligible": False,
                "backend_connected": False,
                "reason": show_info.get("error") or "sickchill_unreachable",
            }
        if self._is_show_resolution_error(show_info):
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "aired": None,
                "present_in_sickchill": False,
                "manual_search_eligible": False,
                "backend_connected": True,
                "show_found": False,
                "reason": show_info.get("reason"),
                "candidates": show_info.get("candidates", []),
            }
        if not show_info:
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "aired": None,
                "present_in_sickchill": False,
                "manual_search_eligible": False,
                "backend_connected": True,
                "show_found": False,
                "expected_indexer_id": expected_indexer_id,
                "reason": "expected_show_not_found_in_sickchill"
                if expected_indexer_id is not None
                else "show_not_found_in_sickchill",
            }

        indexer_id = self._show_indexer_id(show_info)
        if indexer_id is None:
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "aired": None,
                "present_in_sickchill": False,
                "manual_search_eligible": False,
                "backend_connected": True,
                "show_found": True,
                "show_info": show_info,
                "reason": "show_missing_indexer_id",
            }
        id_mismatch = expected_indexer_id is not None and expected_indexer_id != indexer_id

        episode_info = await self._get_episode(indexer_id, season, episode)
        if episode_info.get("error"):
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "aired": None,
                "present_in_sickchill": False,
                "manual_search_eligible": False,
                "backend_connected": True,
                "show_found": True,
                "show_info": show_info,
                "reason": episode_info["error"],
            }

        status = str(episode_info.get("status") or "unknown").lower()
        aired = self._episode_has_aired(episode_info.get("airdate"))
        present = bool(episode_info.get("location"))
        manual_search_eligible = aired and status in {"wanted", "missing", "processing"} and not present

        return {
            "show": show,
            "season": season,
            "episode": episode,
            "status": status,
            "aired": aired,
            "present_in_sickchill": present,
            "manual_search_eligible": manual_search_eligible,
            "backend_connected": True,
            "show_found": True,
            "show_info": show_info,
            "expected_indexer_id": expected_indexer_id,
            "indexer_id_mismatch": id_mismatch,
            "episode_info": episode_info,
        }

    async def ensure_episode_wanted(self, show: str, season: int, episode: int) -> dict:
        show_info = await self._resolve_show(show)
        if isinstance(show_info, dict) and show_info.get("backend_connected") is False:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "backend_connected": False,
                "show_found": False,
                "reason": show_info.get("error") or "sickchill_unreachable",
            }
        if self._is_show_resolution_error(show_info):
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "backend_connected": True,
                "show_found": False,
                "reason": show_info.get("reason"),
                "candidates": show_info.get("candidates", []),
            }
        if not show_info:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "backend_connected": True,
                "show_found": False,
                "reason": "show_not_found_in_sickchill",
            }

        indexer_id = self._show_indexer_id(show_info)
        if indexer_id is None:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "backend_connected": True,
                "show_found": True,
                "show_info": show_info,
                "reason": "show_missing_indexer_id",
            }

        episode_info = await self._get_episode(indexer_id, season, episode)
        if episode_info.get("error"):
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "status": "unknown",
                "backend_connected": True,
                "show_found": True,
                "show_info": show_info,
                "reason": episode_info["error"],
            }

        current_status = str(episode_info.get("status") or "unknown").lower()
        changed = current_status != "wanted"
        if changed:
            try:
                await self._api_get(
                    "episode.setstatus",
                    indexerid=indexer_id,
                    season=season,
                    episode=episode,
                    status="wanted",
                )
                episode_info = await self._get_episode(indexer_id, season, episode)
            except httpx.HTTPError as exc:
                return {
                    "ok": False,
                    "show": show,
                    "season": season,
                    "episode": episode,
                    "status": current_status,
                    "changed": False,
                    "backend_connected": True,
                    "show_found": True,
                    "show_info": show_info,
                    "episode_info": episode_info,
                    "reason": str(exc),
                }

        final_status = str(episode_info.get("status") or "unknown").lower()
        ok = final_status == "wanted"
        return {
            "ok": ok,
            "show": show,
            "season": season,
            "episode": episode,
            "status": final_status,
            "changed": changed and ok,
            "backend_connected": True,
            "show_found": True,
            "show_info": show_info,
            "episode_info": episode_info,
            "reason": None if ok else f"status_update_not_applied:{final_status}",
        }

    async def trigger_manual_search(self, show: str, season: int, episode: int) -> dict:
        wanted_result = await self.ensure_episode_wanted(show, season, episode)
        if not wanted_result.get("ok"):
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "manual_search_unavailable",
                "backend_connected": bool(wanted_result.get("backend_connected")),
                "reason": wanted_result.get("reason", "unable_to_prepare_episode"),
                "wanted_result": wanted_result,
            }

        show_info = wanted_result["show_info"]
        indexer_id = self._show_indexer_id(show_info)
        if indexer_id is None:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "manual_search_unavailable",
                "backend_connected": True,
                "reason": "show_missing_indexer_id",
                "wanted_result": wanted_result,
            }
        try:
            search_result = await self._api_get(
                "episode.search",
                indexerid=indexer_id,
                season=season,
                episode=episode,
                timeout=self.manual_search_timeout_seconds,
            )
        except httpx.ReadTimeout:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "manual_search_unconfirmed",
                "backend_connected": True,
                "timed_out": True,
                "wanted_result": wanted_result,
                "reason": "manual_search_timed_out_unconfirmed",
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "episode": episode,
                "action": "manual_search_failed",
                "backend_connected": True,
                "wanted_result": wanted_result,
                "reason": str(exc),
            }
        search_data = self._unwrap_data(search_result)
        success = str(search_result.get("result") or "").lower() == "success"

        return {
            "ok": success,
            "show": show,
            "season": season,
            "episode": episode,
            "action": "manual_search_started" if success else "manual_search_failed",
            "backend_connected": True,
            "wanted_result": wanted_result,
            "search_result": search_data,
        }

    async def clear_ignored_episodes(self, show: str, season: int | None = None) -> dict:
        show_info = await self._resolve_show(show)
        if isinstance(show_info, dict) and show_info.get("backend_connected") is False:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "clear_ignored_episodes_unavailable",
                "backend_connected": False,
                "show_found": False,
                "reason": show_info.get("error") or "sickchill_unreachable",
            }
        if self._is_show_resolution_error(show_info):
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "clear_ignored_episodes_unavailable",
                "backend_connected": True,
                "show_found": False,
                "reason": show_info.get("reason"),
                "candidates": show_info.get("candidates", []),
            }
        if not show_info:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "clear_ignored_episodes_unavailable",
                "backend_connected": True,
                "show_found": False,
                "reason": "show_not_found_in_sickchill",
            }

        indexer_id = self._show_indexer_id(show_info)
        if indexer_id is None:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "clear_ignored_episodes_unavailable",
                "backend_connected": True,
                "show_found": True,
                "show_info": show_info,
                "reason": "show_missing_indexer_id",
            }

        # Episode state comes from show.seasons, not the show row: `seasonRequests`
        # is an Ombi field and no SickChill show carries it, so this used to find
        # nothing to clear no matter how many episodes were ignored.
        raw_seasons = await self._show_seasons(indexer_id)
        target_seasons = [
            (season_number, rows)
            for season_number, rows in sorted(raw_seasons.items())
            if season is None or season_number == season
        ]

        changed_episodes: list[dict[str, Any]] = []
        skipped_episodes: list[dict[str, Any]] = []
        failed_episodes: list[dict[str, Any]] = []

        for season_number, rows in target_seasons:
            for episode_number in sorted(rows):
                episode_info = rows[episode_number]
                if self._is_phantom_row(episode_info):
                    continue
                status = self._normalize_episode_status(episode_info.get("status"))
                if status != "ignored":
                    skipped_episodes.append(
                        {
                            "season": season_number,
                            "episode": episode_number,
                            "status": status or "unknown",
                        }
                    )
                    continue
                try:
                    ensured = await self.ensure_episode_wanted(show=show, season=season_number or 0, episode=episode_number)
                    if not ensured.get("ok"):
                        failed_episodes.append(
                            {
                                "season": season_number,
                                "episode": episode_number,
                                "reason": ensured.get("reason", "status_update_not_applied"),
                            }
                        )
                        continue
                    refreshed = ensured.get("episode_info") or {}
                    changed_episodes.append(
                        {
                            "season": season_number,
                            "episode": episode_number,
                            "status": str(refreshed.get("status") or "unknown").lower(),
                            "title": refreshed.get("name") or episode_info.get("name"),
                        }
                    )
                except httpx.HTTPError as exc:
                    failed_episodes.append(
                        {
                            "season": season_number,
                            "episode": episode_number,
                            "reason": str(exc),
                        }
                    )

        return {
            "ok": not failed_episodes,
            "show": show,
            "season": season,
            "action": "clear_ignored_episodes",
            "backend_connected": True,
            "show_found": True,
            "show_info": show_info,
            "indexer_id": indexer_id,
            "changed_count": len(changed_episodes),
            "skipped_count": len(skipped_episodes),
            "failed_count": len(failed_episodes),
            "changed_episodes": changed_episodes[:20],
            "failed_episodes": failed_episodes[:20],
            "scope": "season" if season is not None else "all_seasons",
        }

    async def repair_episode_targets(
        self,
        show: str,
        season: int,
        episodes: list[int],
        expected_indexer_id: int | None = None,
    ) -> dict:
        original_expected_indexer_id = expected_indexer_id
        show_info = await self._resolve_show_by_indexer_id(expected_indexer_id) if expected_indexer_id is not None else None
        if expected_indexer_id is None and not show_info:
            show_info = await self._resolve_show(show)
        if isinstance(show_info, dict) and show_info.get("backend_connected") is False:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "season_repair_unavailable",
                "backend_connected": False,
                "show_found": False,
                "reason": show_info.get("error") or "sickchill_unreachable",
            }
        if self._is_show_resolution_error(show_info):
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "season_repair_unavailable",
                "backend_connected": True,
                "show_found": False,
                "reason": show_info.get("reason"),
                "candidates": show_info.get("candidates", []),
            }
        if not show_info:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "season_repair_unavailable",
                "backend_connected": True,
                "show_found": False,
                "expected_indexer_id": original_expected_indexer_id,
                "reason": "expected_show_not_found_in_sickchill"
                if original_expected_indexer_id is not None
                else "show_not_found_in_sickchill",
            }

        indexer_id = self._show_indexer_id(show_info)
        if indexer_id is None:
            return {
                "ok": False,
                "show": show,
                "season": season,
                "action": "season_repair_unavailable",
                "backend_connected": True,
                "show_found": True,
                "show_info": show_info,
                "reason": "show_missing_indexer_id",
            }
        resolved_show = str(show_info.get("show_name") or show)
        id_mismatch = original_expected_indexer_id is not None and original_expected_indexer_id != indexer_id
        results: list[dict[str, Any]] = []
        changed_count = 0
        for episode in sorted({int(ep) for ep in episodes}):
            row: dict[str, Any] = {"season": season, "episode": episode}
            try:
                before = await self._get_episode(indexer_id, season, episode)
                if before.get("error"):
                    row.update({"ok": False, "action": "season_repair_failed", "reason": before["error"]})
                    results.append(row)
                    continue
                before_status = self._normalize_episode_status(before.get("status")) or "unknown"
                row["from_status"] = before_status
                if before_status == "unaired" or not self._episode_has_aired(before.get("airdate")):
                    row.update({"ok": True, "action": "not_aired_yet"})
                elif before_status in {"ignored", "skipped"}:
                    wanted_result = await self.ensure_episode_wanted(show=resolved_show, season=season, episode=episode)
                    if wanted_result.get("ok"):
                        changed_count += 1
                        row.update({"ok": True, "action": "marked_wanted", "to_status": wanted_result.get("status")})
                    else:
                        row.update(
                            {
                                "ok": False,
                                "action": "season_repair_failed",
                                "reason": wanted_result.get("reason", "status_update_not_applied"),
                            }
                        )
                elif before_status in {"wanted", "missing", "processing"}:
                    search_result = await self.trigger_manual_search(show=resolved_show, season=season, episode=episode)
                    row.update(
                        {
                            "ok": bool(search_result.get("ok")),
                            "action": search_result.get("action") or "manual_search_failed",
                        }
                    )
                    if not search_result.get("ok"):
                        row["reason"] = search_result.get("reason", "manual_search_failed")
                elif before_status == "snatched" and not self._episode_has_file(before):
                    # Grabbed, but nothing on disk. Reported rather than
                    # re-searched: a snatch still downloading is indistinguishable
                    # from an abandoned one here, and re-searching a live one
                    # grabs a second copy of what is already coming in.
                    row.update({"ok": True, "action": "awaiting_download"})
                elif before_status in {"snatched", "downloaded", "archived"}:
                    row.update({"ok": True, "action": "already_in_sickchill"})
                else:
                    # An unrecognized status is not a failed repair -- reporting
                    # it as one made a whole show look broken when the only
                    # surprise was a spelling ("Snatched (Best)").
                    row.update({"ok": True, "action": "no_action_for_status", "reason": f"unrecognized_status:{before_status}"})
            except httpx.ReadTimeout:
                row.update({"ok": False, "action": "manual_search_unconfirmed", "reason": "manual_search_timed_out_unconfirmed"})
            except httpx.HTTPError as exc:
                row.update({"ok": False, "action": "season_repair_failed", "reason": str(exc)})
            results.append(row)

        return {
            "ok": all(row.get("ok") for row in results),
            "show": resolved_show,
            "season": season,
            "action": "season_repair_attempted",
            "backend_connected": True,
            "show_found": True,
            "show_info": show_info,
            "indexer_id": indexer_id,
            "expected_indexer_id": original_expected_indexer_id,
            "indexer_id_mismatch": id_mismatch,
            "changed_count": changed_count,
            "target_episode_count": len(results),
            "search_results": results,
        }

    async def _resolve_show_for_listing(
        self,
        show: str,
        expected_indexer_id: int | None,
        base: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int | None]:
        """Resolve a show for episode listing.

        Returns (failure, show_info, indexer_id): exactly one of failure and
        show_info is set. `base` supplies the caller-specific keys (show,
        season) that belong on the failure payload.
        """
        show_info = await self._resolve_show_by_indexer_id(expected_indexer_id) if expected_indexer_id is not None else None
        if expected_indexer_id is None and not show_info:
            show_info = await self._resolve_show(show)
        if isinstance(show_info, dict) and show_info.get("backend_connected") is False:
            return (
                {
                    **base,
                    "ok": False,
                    "backend_connected": False,
                    "show_found": False,
                    "reason": show_info.get("error") or "sickchill_unreachable",
                },
                None,
                None,
            )
        if self._is_show_resolution_error(show_info):
            return (
                {
                    **base,
                    "ok": False,
                    "backend_connected": True,
                    "show_found": False,
                    "reason": show_info.get("reason"),
                    "candidates": show_info.get("candidates", []),
                },
                None,
                None,
            )
        if not show_info:
            return (
                {
                    **base,
                    "ok": False,
                    "backend_connected": True,
                    "show_found": False,
                    "expected_indexer_id": expected_indexer_id,
                    "reason": "expected_show_not_found_in_sickchill"
                    if expected_indexer_id is not None
                    else "show_not_found_in_sickchill",
                },
                None,
                None,
            )

        indexer_id = self._show_indexer_id(show_info)
        if indexer_id is None:
            return (
                {
                    **base,
                    "ok": False,
                    "backend_connected": True,
                    "show_found": True,
                    "show_info": show_info,
                    "reason": "show_missing_indexer_id",
                },
                None,
                None,
            )
        return None, show_info, indexer_id

    async def _show_seasons(self, indexer_id: int) -> dict[int, dict[int, dict[str, Any]]]:
        """Every season and episode of a show, with status and file info, in one call.

        Asking per episode costs ~0.5s each; Law & Order has 948 rows, so the
        obvious loop is a nine-minute repair. `show.seasons` with no season
        param returns the whole show at once. Passing a season instead flips the
        response from nested to flat, so this always fetches everything and
        indexes locally rather than dealing with two shapes.
        """
        payload = await self._api_get("show.seasons", indexerid=indexer_id, timeout=90.0)
        data = self._unwrap_data(payload)
        seasons: dict[int, dict[int, dict[str, Any]]] = {}
        if not isinstance(data, dict):
            return seasons
        for season_key, episodes in data.items():
            season_number = self._maybe_int(season_key)
            if not isinstance(season_number, int) or not isinstance(episodes, dict):
                continue
            rows: dict[int, dict[str, Any]] = {}
            for episode_key, info in episodes.items():
                episode_number = self._maybe_int(episode_key)
                if isinstance(episode_number, int) and isinstance(info, dict):
                    rows[episode_number] = info
            if rows:
                seasons[season_number] = rows
        return seasons

    @staticmethod
    def _episode_has_file(info: dict[str, Any]) -> bool:
        try:
            size = int(info.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        return bool(str(info.get("location") or "").strip()) and size > 0

    @staticmethod
    def _is_phantom_row(info: dict[str, Any]) -> bool:
        """A row invented by a mis-parsed filename rather than a real episode.

        "Law & Order - S01E101.mkv" is season 1 episode 1, but it gets indexed
        as episode 101, which then looks like an episode the show is missing.
        Identified by having neither a title nor an airdate -- deliberately not
        by `episode > 100`, since a daytime or anime season really can run past
        100 episodes and those rows are genuine.
        """
        return not str(info.get("name") or "").strip() and str(info.get("airdate") or "").strip().lower() in {
            "never",
            "",
        }

    def _normalize_episode_status(self, status: Any) -> str:
        """SickChill reports "Snatched (Best)" as well as "Snatched".

        The parenthetical is quality provenance, not a different state, but the
        status dispatch matched on the whole string -- so 250 of Law & Order's
        episodes fell through to `unsupported_episode_status` and the repair
        reported failure for a show it had understood fine.
        """
        return str(status or "").split("(")[0].strip().lower()

    def classify_show_episodes(
        self,
        seasons: dict[int, dict[int, dict[str, Any]]],
        *,
        include_specials: bool = False,
        plex_present: dict[int, set[int]] | None = None,
    ) -> dict[str, Any]:
        """Sort a show's episodes into what is actually wrong with each one.

        Plex decides whether an episode exists. That is the whole rule: if Plex
        can play it, it is healthy, whatever SickChill's status column says and
        whatever number the file is under. SickChill status is metadata we report
        alongside a gap, never the thing that defines one.

        This matters because SickChill's own view is badly misleading here. On
        Law & Order it reports 304 episodes as fileless, of which Plex holds 269
        under a folded `season*100 + episode` number -- files SickChill renamed
        that way on purpose. Diagnosing from SickChill alone turned a show that
        is 512-of-544 complete into a 269-episode repair job. Against Plex the
        same show yields the 32 episodes that are genuinely gone.

        Without `plex_present` (Plex unreachable, or the show never matched) the
        fold is applied to SickChill's own phantom rows instead. That is the
        conservative fallback: it still refuses to re-download a file that looks
        present, it just cannot confirm playability.
        """
        healthy: list[tuple[int, int]] = []
        misfiled: list[dict[str, Any]] = []
        refetch: list[tuple[int, int]] = []
        stalled: list[dict[str, Any]] = []
        pending: list[tuple[int, int]] = []
        duplicates = 0

        for season in sorted(seasons):
            if season <= 0 and not include_specials:
                continue
            rows = seasons[season]
            real = {number: info for number, info in rows.items() if not self._is_phantom_row(info)}
            # A phantom at S01E101 holds the file that belongs to S01E01.
            phantom_files = {
                number % 100: info
                for number, info in rows.items()
                if self._is_phantom_row(info) and self._episode_has_file(info) and number > 100
            }
            in_plex: set[int] | None = None
            if plex_present is not None:
                in_plex = plex_present.get(season) or set()
            for number in sorted(real):
                info = real[number]
                if in_plex is not None:
                    folded = season * 100 + number
                    # The folded number only stands in for `number` when it is
                    # not itself a real aired episode -- a daytime or anime
                    # season that genuinely reaches 101 must not have its
                    # episode 1 covered by its episode 101.
                    if number in in_plex or (folded in in_plex and folded not in real):
                        healthy.append((season, number))
                        continue
                    status = self._normalize_episode_status(info.get("status"))
                    if not self._episode_has_aired(info.get("airdate")) or status in {"skipped", "ignored"}:
                        pending.append((season, number))
                    else:
                        # Plex cannot play it, so it is missing. A snatch that
                        # never landed is no longer held back: it is exactly the
                        # gap the user is complaining about.
                        refetch.append((season, number))
                    continue
                if self._episode_has_file(info):
                    healthy.append((season, number))
                    if number in phantom_files:
                        duplicates += 1
                    continue
                status = self._normalize_episode_status(info.get("status"))
                if number in phantom_files:
                    misfiled.append(
                        {
                            "season": season,
                            "episode": number,
                            "status": status,
                            "location": str(phantom_files[number].get("location") or ""),
                        }
                    )
                elif not self._episode_has_aired(info.get("airdate")):
                    pending.append((season, number))
                elif status in {"wanted", "failed"}:
                    refetch.append((season, number))
                elif status == "snatched":
                    # Grabbed at some point but nothing landed. Reported rather
                    # than re-searched: a snatch that is genuinely mid-download
                    # looks identical from here, and re-searching it would grab
                    # a second copy of something already on its way in.
                    stalled.append({"season": season, "episode": number, "status": status})
                elif status in {"skipped", "ignored"}:
                    pending.append((season, number))
                else:
                    refetch.append((season, number))

        return {
            "healthy": healthy,
            "misfiled_on_disk": misfiled,
            "needs_refetch": refetch,
            "stalled": stalled,
            "not_yet_aired_or_skipped": pending,
            "duplicate_files": duplicates,
            "episode_count": len(healthy) + len(misfiled) + len(refetch) + len(stalled) + len(pending),
        }

    async def all_episode_numbers(
        self,
        show: str,
        expected_indexer_id: int | None = None,
        plex_present: dict[int, set[int]] | None = None,
    ) -> dict[str, Any]:
        """List every episode across a show's real seasons, with a diagnosis.

        Season 0 is skipped: specials are not what someone means by "the show is
        broken". Previously this read `seasonRequests` off the show row, which
        is an Ombi field -- no SickChill show has it (0 of 849 on the live
        server), so whole-show repair enumerated nothing and refused.
        """
        original_expected_indexer_id = expected_indexer_id
        failure, show_info, indexer_id = await self._resolve_show_for_listing(
            show, expected_indexer_id, {"show": show, "season": None}
        )
        if failure is not None:
            return failure
        assert show_info is not None
        assert indexer_id is not None

        raw_seasons = await self._show_seasons(indexer_id)
        diagnosis = self.classify_show_episodes(raw_seasons, plex_present=plex_present)
        seasons: dict[int, list[int]] = {}
        for season, episode in diagnosis["needs_refetch"]:
            seasons.setdefault(season, []).append(episode)

        return {
            # A show with nothing broken listed fine; that is a healthy answer,
            # not a failure to enumerate. Only an empty payload is a refusal.
            "ok": bool(raw_seasons),
            "show": str(show_info.get("show_name") or show),
            "backend_connected": True,
            "show_found": True,
            "show_info": show_info,
            "indexer_id": indexer_id,
            "expected_indexer_id": original_expected_indexer_id,
            "indexer_id_mismatch": original_expected_indexer_id is not None
            and original_expected_indexer_id != indexer_id,
            "seasons": {season: sorted(seasons[season]) for season in sorted(seasons)},
            "episode_count": sum(len(episodes) for episodes in seasons.values()),
            "diagnosis": diagnosis,
            "reason": None if raw_seasons else "show_episode_list_unavailable",
        }

    async def episode_numbers_for_season(
        self,
        show: str,
        season: int,
        expected_indexer_id: int | None = None,
        plex_present: dict[int, set[int]] | None = None,
    ) -> dict[str, Any]:
        original_expected_indexer_id = expected_indexer_id
        failure, show_info, indexer_id = await self._resolve_show_for_listing(
            show, expected_indexer_id, {"show": show, "season": season}
        )
        if failure is not None:
            return failure
        assert show_info is not None
        assert indexer_id is not None
        id_mismatch = original_expected_indexer_id is not None and original_expected_indexer_id != indexer_id

        raw_seasons = await self._show_seasons(indexer_id)
        season_rows = raw_seasons.get(season) or {}
        diagnosis = self.classify_show_episodes(
            {season: season_rows} if season_rows else {},
            include_specials=True,
            plex_present=plex_present,
        )
        episodes = [episode for _, episode in diagnosis["needs_refetch"]]

        return {
            "ok": bool(season_rows),
            "show": str(show_info.get("show_name") or show),
            "season": season,
            "backend_connected": True,
            "show_found": True,
            "show_info": show_info,
            "indexer_id": indexer_id,
            "expected_indexer_id": original_expected_indexer_id,
            "indexer_id_mismatch": id_mismatch,
            "episodes": sorted(set(episodes)),
            "diagnosis": diagnosis,
            "reason": None if season_rows else "season_episode_list_unavailable",
        }

    async def check_episode_file(self, show: str, season: int, episode: int) -> dict:
        show_info = await self._resolve_show(show)
        if isinstance(show_info, dict) and show_info.get("backend_connected") is False:
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "exists": None,
                "path": None,
                "backend_connected": False,
                "show_found": False,
                "reason": show_info.get("error") or "sickchill_unreachable",
            }
        if self._is_show_resolution_error(show_info):
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "exists": None,
                "path": None,
                "backend_connected": True,
                "show_found": False,
                "reason": show_info.get("reason"),
                "candidates": show_info.get("candidates", []),
            }
        if not show_info:
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "exists": None,
                "path": None,
                "backend_connected": True,
                "show_found": False,
                "reason": "show_not_found_in_sickchill",
            }

        indexer_id = self._show_indexer_id(show_info)
        if indexer_id is None:
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "exists": None,
                "path": None,
                "backend_connected": True,
                "show_found": True,
                "reason": "show_missing_indexer_id",
            }

        episode_info = await self._get_episode(indexer_id, season, episode, full_path=True)
        if episode_info.get("error"):
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "exists": None,
                "path": None,
                "backend_connected": True,
                "show_found": True,
                "reason": episode_info["error"],
            }

        path = episode_info.get("location") or None
        return {
            "show": show,
            "season": season,
            "episode": episode,
            "exists": bool(path),
            "path": path,
            "backend_connected": True,
            "show_found": True,
            "episode_info": episode_info,
        }

    async def _resolve_show(self, show: str) -> dict[str, Any] | None:
        try:
            shows_payload = await self._api_get("shows")
        except httpx.HTTPError as exc:
            return {"error": str(exc), "backend_connected": False}
        data = self._unwrap_data(shows_payload)
        candidates = self._collect_show_candidates(data)
        normalized = self._normalize(show)
        if not normalized:
            return None
        exact = [item for item in candidates if self._normalize(item.get("show_name")) == normalized]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return {
                "resolution_error": True,
                "backend_connected": True,
                "reason": "show_ambiguous_in_sickchill",
                "candidates": self._summarize_show_candidates(exact),
            }
        return None

    async def _resolve_show_by_indexer_id(self, indexer_id: int) -> dict[str, Any] | None:
        try:
            shows_payload = await self._api_get("shows")
        except httpx.HTTPError as exc:
            return {"error": str(exc), "backend_connected": False}
        data = self._unwrap_data(shows_payload)
        for candidate in self._collect_show_candidates(data):
            if self._show_indexer_id(candidate) == indexer_id:
                return candidate
        return None

    async def _default_root_dir(self) -> str | None:
        try:
            payload = await self._api_get("sc.getrootdirs")
        except httpx.HTTPError:
            return None
        data = self._unwrap_data(payload)
        if not isinstance(data, list):
            return None
        valid = [item for item in data if isinstance(item, dict) and item.get("valid")]
        for item in valid:
            if item.get("default"):
                return str(item.get("location") or "").strip() or None
        if valid:
            return str(valid[0].get("location") or "").strip() or None
        return None

    async def _get_episode(self, indexerid: int, season: int, episode: int, full_path: bool = False) -> dict[str, Any]:
        try:
            payload = await self._api_get(
                "episode",
                indexerid=indexerid,
                season=season,
                episode=episode,
                full_path=int(full_path),
            )
        except httpx.HTTPError as exc:
            return {"error": str(exc)}
        data = self._unwrap_data(payload)
        if not isinstance(data, dict) or "status" not in data:
            return {"error": "episode_not_found"}
        return data

    async def _api_get(self, cmd: str, timeout: float | None = None, **params: Any) -> dict[str, Any]:
        query = {"cmd": cmd}
        query.update(params)
        path = "/api/"
        if self.api_key:
            path = f"/api/{self.api_key}/"
        request_timeout = timeout or self.request_timeout_seconds
        logger.info(
            "SickChill API attempt cmd=%s params=%s timeout=%s",
            cmd,
            {k: v for k, v in query.items() if k != "cmd"},
            request_timeout,
        )
        try:
            payload = await self.get_json(path, params=query, timeout=request_timeout)
        except httpx.HTTPError as exc:
            logger.warning("SickChill API error cmd=%s error=%s", cmd, exc)
            raise
        if not isinstance(payload, dict):
            return {}
        logger.info("SickChill API result cmd=%s result=%s", cmd, payload.get("result"))
        return payload

    def _unwrap_data(self, payload: dict[str, Any]) -> Any:
        if not isinstance(payload, dict):
            return payload
        if "data" in payload:
            return payload["data"]
        return payload

    def _collect_show_candidates(self, data: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict) and "show_name" in value:
                    item = dict(value)
                    item.setdefault("indexerid", self._extract_indexer_id(item) or self._maybe_int(key))
                    candidates.append(item)
                elif isinstance(value, list):
                    candidates.extend(self._collect_show_candidates(value))
        elif isinstance(data, list):
            for value in data:
                if isinstance(value, dict) and "show_name" in value:
                    item = dict(value)
                    item.setdefault("indexerid", self._extract_indexer_id(item))
                    candidates.append(item)
                else:
                    candidates.extend(self._collect_show_candidates(value))
        return candidates

    def _extract_indexer_id(self, value: dict[str, Any]) -> int | None:
        for key in ("indexerid", "indexerId", "indexer_id", "seriesid", "seriesId", "tvdbid", "tvdb_id"):
            parsed = self._maybe_int(value.get(key))
            if parsed is not None:
                return parsed
        return None

    def _show_indexer_id(self, show_info: dict[str, Any]) -> int | None:
        return self._extract_indexer_id(show_info)

    def _maybe_int(self, value: Any) -> Any:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    def _normalize(self, value: Any) -> str:
        # SickChill stores "Law & Order" while people type "Law and Order".
        # Fold the ampersand before stripping punctuation so the two forms
        # normalize to the same string instead of laworder/lawandorder.
        text = str(value or "").lower().replace("&", " and ")
        return "".join(ch for ch in text if ch.isalnum())

    def _is_show_resolution_error(self, value: Any) -> bool:
        return isinstance(value, dict) and bool(value.get("resolution_error"))

    def _summarize_show_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summarized: list[dict[str, Any]] = []
        for candidate in candidates[:10]:
            summarized.append(
                {
                    "show_name": candidate.get("show_name"),
                    "indexerid": self._show_indexer_id(candidate),
                    "network": candidate.get("network"),
                    "status": candidate.get("status"),
                }
            )
        return summarized

    def _episode_has_aired(self, airdate: Any) -> bool:
        if not airdate:
            return False
        if isinstance(airdate, str):
            try:
                parsed = datetime.fromisoformat(airdate)
            except ValueError:
                return False
            return parsed.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
        return bool(airdate)
