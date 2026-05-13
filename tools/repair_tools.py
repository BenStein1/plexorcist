from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from clients.ombi_client import OmbiClient
from clients.sickchill_client import SickChillClient


class RepairTools:
    def __init__(self, ombi: OmbiClient, sickchill: SickChillClient) -> None:
        self.ombi = ombi
        self.sickchill = sickchill

    async def add_requested_show_to_sickchill(
        self,
        query: str,
        tvdb_id: int | None = None,
        season: int | None = None,
    ) -> dict[str, Any]:
        target_season = self._normalize_scope_value(season)
        try:
            existing = await self.ombi.check_existing_media_status(query)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "query": query,
                "tvdb_id": tvdb_id,
                "season": target_season,
                "action": "ombi_unreachable",
                "reason": str(exc),
            }

        candidates = self._show_candidates(existing)
        selected = self._select_show_candidate(candidates, tvdb_id)
        if selected is None:
            return {
                "ok": False,
                "query": query,
                "tvdb_id": tvdb_id,
                "season": target_season,
                "action": "show_ambiguous" if candidates else "show_not_found",
                "reason": "show_ambiguous" if candidates else "show_not_found",
                "candidates": candidates,
            }

        selected_tvdb_id = self._safe_int(selected.get("tvdb_id"))
        title = str(selected.get("title") or query)
        if selected_tvdb_id is None:
            return {
                "ok": False,
                "query": query,
                "show": title,
                "season": target_season,
                "action": "show_missing_tvdb_id",
                "reason": "show_missing_tvdb_id",
                "match": selected,
            }

        if not selected.get("requested"):
            return {
                "ok": False,
                "query": query,
                "show": title,
                "tvdb_id": selected_tvdb_id,
                "season": target_season,
                "requested": False,
                "available": bool(selected.get("available")),
                "action": "not_requested",
                "reason": "show_not_requested_in_ombi",
                "match": selected,
            }

        add_result = await self.sickchill.add_show(
            tvdb_id=selected_tvdb_id,
            title=title,
            status="ignored" if target_season is not None else "wanted",
            future_status="ignored" if target_season is not None else "wanted",
        )
        clear_result: dict[str, Any] | None = None
        if add_result.get("ok") and target_season is not None:
            clear_result = await self.sickchill.clear_ignored_episodes(show=title, season=target_season)

        ok = bool(add_result.get("ok"))
        if target_season is not None and clear_result is not None:
            ok = ok and bool(clear_result.get("ok"))

        return {
            "ok": ok,
            "query": query,
            "show": title,
            "tvdb_id": selected_tvdb_id,
            "season": target_season,
            "requested": True,
            "available": bool(selected.get("available")),
            "action": "add_requested_show_to_sickchill",
            "sickchill_action": add_result.get("action"),
            "backend_connected": add_result.get("backend_connected"),
            "show_found": add_result.get("show_found"),
            "reason": add_result.get("reason"),
            "scope_mode": "season" if target_season is not None else "show",
            "match": selected,
            "sickchill": add_result,
            "clear_ignored_result": clear_result,
        }

    async def repair_requested_show(
        self,
        query: str,
        scope: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        normalized_scope = self._normalize_scope(scope)
        season = self._normalize_scope_value(season)
        episode = self._normalize_scope_value(episode)

        effective_scope = normalized_scope or "show"
        effective_season: int | None = None
        effective_episode: int | None = None

        if effective_scope == "season":
            if season is None:
                return {
                    "ok": False,
                    "query": query,
                    "scope": "season",
                    "season": season,
                    "episode": episode,
                    "action": "season_required",
                    "reason": "season_required",
                }
            effective_season = season
        elif effective_scope == "episode":
            if season is None or episode is None:
                return {
                    "ok": False,
                    "query": query,
                    "scope": "episode",
                    "season": season,
                    "episode": episode,
                    "action": "season_and_episode_required",
                    "reason": "season_and_episode_required",
                }
            effective_season = season
            effective_episode = episode

        if normalized_scope is None and (season is not None or episode is not None):
            # Guardrail: if scope is not explicitly provided, treat this as whole-show repair.
            effective_scope = "show"
            effective_season = None
            effective_episode = None

        try:
            existing = await self.ombi.check_existing_media_status(query)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "query": query,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "action": "ombi_unreachable",
                "reason": str(exc),
            }

        best_match = existing.get("best_match") or {}
        if best_match.get("type") != "show" or not best_match.get("title"):
            return {
                "ok": False,
                "query": query,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "action": "show_not_found",
                "reason": "show_not_found",
                "match": best_match,
            }

        title = str(best_match.get("title"))
        expected_tvdb_id = self._safe_int(best_match.get("tvdb_id"))
        if not best_match.get("requested"):
            return {
                "ok": False,
                "query": query,
                "show": title,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "action": "not_requested",
                "requested": False,
                "available": bool(best_match.get("available")),
                "reason": "show_not_requested_in_ombi",
                "match": best_match,
            }

        try:
            season_status = await self.ombi.get_show_season_status(title, season=effective_season)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "query": query,
                "show": title,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "action": "ombi_unreachable",
                "requested": True,
                "reason": str(exc),
            }

        if not season_status.get("found"):
            return {
                "ok": False,
                "query": query,
                "show": title,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "action": "request_status_unavailable",
                "requested": True,
                "reason": season_status.get("reason", "show_request_not_found"),
            }

        episode_rows = season_status.get("episodes") or []
        targeted_rows: list[dict[str, Any]] = []
        future_rows: list[dict[str, Any]] = []
        skipped_rows: list[dict[str, Any]] = []

        for row in episode_rows:
            row_season = self._safe_int(row.get("season"))
            row_episode = self._safe_int(row.get("episode"))
            if row_season is None or row_episode is None:
                continue
            if effective_season is not None and row_season != effective_season:
                continue
            if effective_episode is not None and row_episode != effective_episode:
                continue

            normalized = {
                "season": row_season,
                "episode": row_episode,
                "title": row.get("title"),
                "status": str(row.get("status") or "").lower(),
                "requested": bool(row.get("requested")),
                "available": bool(row.get("available")),
                "air_date": row.get("air_date"),
            }

            if self._episode_is_future(normalized.get("air_date")):
                future_rows.append(normalized)
                continue

            if normalized["requested"] and not normalized["available"]:
                targeted_rows.append(normalized)
            else:
                skipped_rows.append(normalized)

        if effective_scope == "episode" and not targeted_rows and future_rows:
            return {
                "ok": False,
                "query": query,
                "show": title,
                "scope": "episode",
                "season": effective_season,
                "episode": effective_episode,
                "action": "not_aired_yet",
                "requested": True,
                "future_episode_count": len(future_rows),
                "reason": "episode_has_not_aired",
            }

        if effective_scope == "episode" and not targeted_rows:
            return {
                "ok": False,
                "query": query,
                "show": title,
                "scope": "episode",
                "season": effective_season,
                "episode": effective_episode,
                "action": "episode_not_requested_or_already_available",
                "requested": True,
                "skipped_count": len(skipped_rows),
                "reason": "episode_not_requested_or_already_available",
            }

        if not targeted_rows:
            return {
                "ok": True,
                "query": query,
                "show": title,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "action": "nothing_to_repair",
                "requested": True,
                "target_episode_count": 0,
                "requested_not_available_count": 0,
                "future_episode_count": len(future_rows),
                "skipped_count": len(skipped_rows),
            }

        season_targets: dict[int, list[int]] = {}
        for row in targeted_rows:
            season_targets.setdefault(int(row["season"]), []).append(int(row["episode"]))

        season_results: list[dict[str, Any]] = []
        flattened_results: list[dict[str, Any]] = []
        changed_count = 0
        queued_count = 0
        no_action_count = 0
        unconfirmed_count = 0
        failure_count = 0

        for season_number in sorted(season_targets):
            result = await self.sickchill.repair_episode_targets(
                show=title,
                season=season_number,
                episodes=sorted(set(season_targets[season_number])),
                expected_indexer_id=expected_tvdb_id,
            )
            season_results.append(result)
            changed_count += int(result.get("changed_count") or 0)
            search_rows = result.get("search_results") or []
            if not search_rows and not result.get("ok"):
                failure_count += len(season_targets[season_number])
                flattened_results.append(
                    {
                        "season": season_number,
                        "episode": None,
                        "ok": False,
                        "action": result.get("action") or "season_repair_failed",
                        "reason": result.get("reason") or "season_repair_failed",
                    }
                )
                continue
            for row in search_rows:
                flattened = dict(row)
                flattened.setdefault("season", season_number)
                flattened_results.append(flattened)
                action = str(flattened.get("action") or "")
                if action == "manual_search_started" and flattened.get("ok"):
                    queued_count += 1
                elif action == "manual_search_unconfirmed":
                    unconfirmed_count += 1
                    failure_count += 1
                elif action in {"already_in_sickchill", "not_aired_yet", "marked_wanted"} and flattened.get("ok"):
                    no_action_count += 1
                elif not flattened.get("ok"):
                    failure_count += 1

        fallback_result: dict[str, Any] | None = None
        # If a narrowly scoped repair (single episode) failed with no concrete progress,
        # try clearing ignored flags for that season so the next retry has a sane state.
        if (
            effective_scope == "episode"
            and effective_episode is not None
            and effective_season is not None
            and queued_count == 0
            and changed_count == 0
            and failure_count > 0
        ):
            fallback_result = await self.sickchill.clear_ignored_episodes(show=title, season=effective_season)
            fallback_changed = int(fallback_result.get("changed_count") or 0)
            changed_count += fallback_changed

        return {
            "ok": failure_count == 0,
            "query": query,
            "show": title,
            "scope": effective_scope,
            "season": effective_season,
            "episode": effective_episode,
            "requested": True,
            "action": "repair_requested_show",
            "tvdb_id": best_match.get("tvdb_id"),
            "target_episode_count": len(targeted_rows),
            "requested_not_available_count": len(targeted_rows),
            "checked_season_count": len(season_targets),
            "future_episode_count": len(future_rows),
            "skipped_count": len(skipped_rows),
            "changed_count": changed_count,
            "queued_count": queued_count,
            "no_action_count": no_action_count,
            "unconfirmed_count": unconfirmed_count,
            "failure_count": failure_count,
            "fallback_result": fallback_result,
            "season_results": season_results,
            "search_results": flattened_results,
        }

    def _safe_int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _normalize_scope_value(self, value: Any) -> int | None:
        normalized = self._safe_int(value)
        if normalized is None or normalized <= 0:
            return None
        return normalized

    def _normalize_scope(self, value: Any) -> str | None:
        text = str(value or "").strip().lower()
        if text in {"show", "season", "episode"}:
            return text
        return None

    def _show_candidates(self, existing: dict[str, Any]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        candidates: list[dict[str, Any]] = []
        raw_candidates: list[dict[str, Any]] = []
        for key in ("exact_matches", "candidates"):
            values = existing.get(key)
            if isinstance(values, list):
                raw_candidates.extend([item for item in values if isinstance(item, dict)])
        best_match = existing.get("best_match")
        if isinstance(best_match, dict):
            raw_candidates.insert(0, best_match)

        for item in raw_candidates:
            if item.get("type") != "show":
                continue
            tvdb_id = str(item.get("tvdb_id") or "")
            title = str(item.get("title") or "")
            key = (title, tvdb_id)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(item)
        return candidates

    def _select_show_candidate(self, candidates: list[dict[str, Any]], tvdb_id: int | None) -> dict[str, Any] | None:
        if tvdb_id is not None:
            for candidate in candidates:
                if self._safe_int(candidate.get("tvdb_id")) == tvdb_id:
                    return candidate
            return None
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _episode_is_future(self, value: Any) -> bool:
        parsed = self._parse_air_date(value)
        if parsed is None:
            return False
        return parsed > datetime.now(timezone.utc)

    def _parse_air_date(self, value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = str(value).strip()
        formats = (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m/%d/%Y %H:%M:%S",
        )
        for fmt in formats:
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
