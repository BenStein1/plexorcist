from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from clients.ombi_client import OmbiClient
from clients.plex_client import PlexClient
from clients.sickchill_client import SickChillClient
from tools.error_helpers import classify_http_error, classify_service_result, service_action, user_error_summary

logger = logging.getLogger("plexorcist.repair")


class RepairTools:
    def __init__(
        self,
        ombi: OmbiClient,
        sickchill: SickChillClient,
        plex: PlexClient | None = None,
    ) -> None:
        self.ombi = ombi
        self.sickchill = sickchill
        self.plex = plex

    async def add_requested_show_to_sickchill(
        self,
        query: str,
        tvdb_id: int | None = None,
        season: int | None = None,
    ) -> dict[str, Any]:
        tvdb_id = self._normalize_scope_value(tvdb_id)
        target_season = self._normalize_scope_value(season)
        try:
            existing = await self.ombi.check_existing_media_status(query)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="multi_search", exc=exc)
            return {
                "ok": False,
                "tool_name": "add_requested_show_to_sickchill",
                "query": query,
                "tvdb_id": tvdb_id,
                "season": target_season,
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="TV show handoff repair",
                    error=error,
                    title=query,
                    change_status="The show add did not start; nothing was changed.",
                ),
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
            # This tool's job is specifically the Ombi-requested-but-missing-from-SickChill
            # handoff, so it needs a request record to hand off from. A show with no Ombi
            # request is either not repairable at all, or -- if it's already available (in
            # Plex/SickChill) -- a job for repair_requested_show instead, which works without
            # an Ombi request. Either way the caller needs a concrete next step, not a refusal.
            available = bool(selected.get("available"))
            return {
                "ok": False,
                "query": query,
                "show": title,
                "tvdb_id": selected_tvdb_id,
                "season": target_season,
                "requested": False,
                "available": available,
                "action": "not_requested",
                "reason": "show_not_requested_in_ombi",
                "match": selected,
                "user_summary": (
                    f"{title} was never requested, so there's nothing to hand off yet. "
                    + (
                        "It's already available, so I can repair it directly instead."
                        if available
                        else "It also isn't available yet, so it needs to be requested before it can be fixed."
                    )
                ),
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

        add_ok = bool(add_result.get("ok"))
        ok = add_ok
        if target_season is not None and clear_result is not None:
            ok = ok and bool(clear_result.get("ok"))
        activation_pending = (
            add_ok
            and target_season is not None
            and clear_result is not None
            and not clear_result.get("ok")
            and str(clear_result.get("reason") or "") == "show_not_found_in_sickchill"
        )
        if activation_pending:
            ok = True

        return {
            "ok": ok,
            "tool_name": "add_requested_show_to_sickchill",
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
            "corrective_action_taken": add_ok,
            "activation_pending": activation_pending,
            "post_add_verification_failed": activation_pending,
            "match": selected,
            "sickchill": add_result,
            "clear_ignored_result": clear_result,
            **self._sickchill_error_metadata(
                title=title,
                operation="add_show",
                result=clear_result if clear_result is not None and not clear_result.get("ok") and not activation_pending else add_result,
                tool_family="TV show handoff repair",
                include_when_ok=False,
            ),
        }

    async def repair_requested_show(
        self,
        query: str,
        scope: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        tvdb_id: int | None = None,
    ) -> dict[str, Any]:
        normalized_scope = self._normalize_scope(scope)
        season = self._normalize_scope_value(season)
        episode = self._normalize_scope_value(episode)
        tvdb_id = self._normalize_scope_value(tvdb_id)

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
                "tvdb_id": tvdb_id,
                "action": "season_required",
                    "reason": "season_required",
                }
            effective_season = season
            if episode is not None:
                # Tool callers sometimes provide scope=season with a concrete
                # episode. Treat that as the narrower, safer episode repair.
                effective_scope = "episode"
                effective_episode = episode
        elif effective_scope == "episode":
            if season is None or episode is None:
                return {
                    "ok": False,
                "query": query,
                "scope": "episode",
                "season": season,
                "episode": episode,
                "tvdb_id": tvdb_id,
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

        # Plex is the server's own record of what exists, and plenty of media
        # reaches it without ever passing through Ombi. So identity and
        # existence are resolved here first: if someone reports a problem with
        # something they watched, it is already on the server, and the job is a
        # repair (SickChill), not a request (Ombi).
        library_match = await self._resolve_show_in_library(query)
        if library_match.get("ok"):
            title = str(library_match.get("show") or query)
            expected_tvdb_id = tvdb_id or self._safe_int(library_match.get("tvdb_id"))
            repaired = await self._repair_with_sickchill_soft_gate(
                query=query,
                title=title,
                effective_scope=effective_scope,
                effective_season=effective_season,
                effective_episode=effective_episode,
                expected_tvdb_id=expected_tvdb_id,
                soft_error=None,
                library_match=library_match,
            )
            if repaired is not None:
                return repaired
            return {
                "ok": False,
                "tool_name": "repair_requested_show",
                "query": query,
                "show": title,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "tvdb_id": expected_tvdb_id,
                "in_plex": True,
                # Defensive only: show/season/episode all resolve to a target
                # above, so an unrecognised scope is the sole way to land here.
                "action": "repair_target_unclear",
                "reason": "unrecognized_repair_scope",
                "plex_match": library_match,
                "candidates": library_match.get("candidates") or [],
                "user_summary": (
                    f"{title} is already on the server, so this is a repair rather than a new "
                    "request, but I couldn't work out what to target. Tell me which season or "
                    "episode is broken and I'll go after that."
                ),
            }

        try:
            existing = await self.ombi.check_existing_media_status(query)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="multi_search", exc=exc)
            fallback = await self._repair_with_sickchill_soft_gate(
                query=query,
                title=query,
                effective_scope=effective_scope,
                effective_season=effective_season,
                effective_episode=effective_episode,
                expected_tvdb_id=tvdb_id,
                soft_error=error,
            )
            if fallback is not None:
                return fallback
            return {
                "ok": False,
                "tool_name": "repair_requested_show",
                "query": query,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "tvdb_id": tvdb_id,
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="TV repair",
                    error=error,
                    title=query,
                    change_status="The repair did not start; nothing was changed.",
                ),
            }

        best_match = existing.get("best_match") or {}
        if best_match.get("type") != "show" or not best_match.get("title"):
            return {
                "ok": False,
                "tool_name": "repair_requested_show",
                "query": query,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "action": "show_not_found",
                "reason": "show_not_found",
                "in_plex": False,
                "plex_match": library_match,
                "match": best_match,
                "user_summary": (
                    f"I couldn't find anything matching \"{query}\" on the server, and it doesn't "
                    "match a known show either. There's nothing to repair yet -- if it should "
                    "exist, it needs to be requested first."
                ),
            }

        title = str(best_match.get("title"))
        expected_tvdb_id = self._safe_int(best_match.get("tvdb_id")) or tvdb_id
        if not best_match.get("requested"):
            # No Ombi request record is not proof the show is unrepairable -- it's
            # frequently a show that's already in SickChill/Plex and was simply
            # never routed through an Ombi request. Try the same direct-SickChill
            # path used when Ombi itself is unreachable before giving up.
            soft_error = {
                "service": "ombi",
                "operation": "tv_request_status",
                "failure_type": "not_requested",
                "reason": "show_not_requested_in_ombi",
                "error_message": f"{title} has no Ombi request record",
            }
            fallback = await self._repair_with_sickchill_soft_gate(
                query=query,
                title=title,
                effective_scope=effective_scope,
                effective_season=effective_season,
                effective_episode=effective_episode,
                expected_tvdb_id=expected_tvdb_id,
                soft_error=soft_error,
            )
            if fallback is not None:
                return fallback
            return {
                "ok": False,
                "tool_name": "repair_requested_show",
                "query": query,
                "show": title,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "tvdb_id": expected_tvdb_id,
                "action": "not_requested",
                "requested": False,
                "available": bool(best_match.get("available")),
                "reason": "show_not_requested_in_ombi",
                "in_plex": False,
                "plex_match": library_match,
                "match": best_match,
                "candidates": [best_match],
                "user_summary": (
                    f"{title} isn't on the server and was never requested, and this is a whole-show "
                    "repair with no specific season or episode to target yet. Say which season or "
                    "episode you mean (and include that scope explicitly) and I can work it directly."
                ),
            }

        try:
            season_status = await self.ombi.get_show_season_status(title, season=effective_season)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="tv_request_status", exc=exc)
            fallback = await self._repair_with_sickchill_soft_gate(
                query=query,
                title=title,
                effective_scope=effective_scope,
                effective_season=effective_season,
                effective_episode=effective_episode,
                expected_tvdb_id=expected_tvdb_id,
                soft_error=error,
            )
            if fallback is not None:
                return fallback
            return {
                "ok": False,
                "query": query,
                "show": title,
                "scope": effective_scope,
                "season": effective_season,
                "episode": effective_episode,
                "tvdb_id": expected_tvdb_id,
                "action": service_action(error),
                "requested": True,
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="TV repair",
                    error=error,
                    title=title,
                    change_status="The repair did not start; nothing was changed.",
                ),
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

        return await self._run_sickchill_repair(
            query=query,
            title=title,
            effective_scope=effective_scope,
            effective_season=effective_season,
            effective_episode=effective_episode,
            expected_tvdb_id=expected_tvdb_id,
            targeted_rows=targeted_rows,
            future_rows=future_rows,
            skipped_rows=skipped_rows,
            requested=True,
            ombi_soft_error=None,
        )

    async def _repair_with_sickchill_soft_gate(
        self,
        *,
        query: str,
        title: str,
        effective_scope: str,
        effective_season: int | None,
        effective_episode: int | None,
        expected_tvdb_id: int | None,
        soft_error: dict[str, Any] | None,
        library_match: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        targeted_rows: list[dict[str, Any]] = []
        if effective_scope == "episode" and effective_season is not None and effective_episode is not None:
            targeted_rows.append(
                {
                    "season": effective_season,
                    "episode": effective_episode,
                    "title": None,
                    "status": "unknown",
                    "requested": None,
                    "available": None,
                    "air_date": None,
                }
            )
        elif effective_scope == "season" and effective_season is not None:
            episode_list = await self.sickchill.episode_numbers_for_season(
                show=title,
                season=effective_season,
                expected_indexer_id=expected_tvdb_id,
                plex_present=await self._plex_episode_index(library_match),
            )
            if not episode_list.get("ok"):
                gate_error = soft_error or {
                    "service": "sickchill",
                    "operation": "episode_numbers_for_season",
                    "failure_type": "unavailable",
                    "reason": "season_episode_list_unavailable",
                    "error_message": f"Could not list season {effective_season} episodes for {title}",
                }
                return {
                    "ok": False,
                    "tool_name": "repair_requested_show",
                    "query": query,
                    "show": title,
                    "scope": effective_scope,
                    "season": effective_season,
                    "episode": effective_episode,
                    "tvdb_id": expected_tvdb_id,
                    "action": service_action(gate_error),
                    "reason": gate_error.get("reason") or "soft_gate_failed",
                    **gate_error,
                    "sickchill_probe": episode_list,
                    "plex_match": library_match,
                    "user_summary": user_error_summary(
                        tool_family="TV repair",
                        error=gate_error,
                        title=title,
                        change_status="The season repair could not work out which episodes to target; nothing was changed.",
                    ),
                }
            title = str(episode_list.get("show") or title)
            season_diagnosis = episode_list.get("diagnosis") or {}
            if not (episode_list.get("episodes") or []) and season_diagnosis:
                # "Season 1 is broken" is a normal way to ask, and for a season
                # that is entirely misfiled it is the only way the user would
                # ever hear why. Without this the season path runs a repair over
                # zero targets and reports nothing useful.
                return self._nothing_to_refetch_diagnosis(
                    query=query,
                    title=title,
                    expected_tvdb_id=expected_tvdb_id,
                    diagnosis=season_diagnosis,
                    library_match=library_match,
                    soft_error=soft_error,
                    scope="season",
                    season=effective_season,
                )
            targeted_rows.extend(
                {
                    "season": effective_season,
                    "episode": episode_number,
                    "title": None,
                    "status": "unknown",
                    "requested": None,
                    "available": None,
                    "air_date": None,
                }
                for episode_number in episode_list.get("episodes") or []
            )
        elif effective_scope == "show":
            # A vague "it's broken" is the common case, so whole-show repair has to
            # work rather than bounce the user for a season number. Sweeping every
            # episode is cheap in effect: episodes already downloaded are left
            # alone, so only genuinely broken ones get touched.
            show_episodes = await self.sickchill.all_episode_numbers(
                show=title,
                expected_indexer_id=expected_tvdb_id,
                plex_present=await self._plex_episode_index(library_match),
            )
            if not show_episodes.get("ok"):
                gate_error = soft_error or {
                    "service": "sickchill",
                    "operation": "all_episode_numbers",
                    "failure_type": "unavailable",
                    "reason": show_episodes.get("reason") or "show_episode_list_unavailable",
                    "error_message": f"Could not list episodes for {title}",
                }
                return {
                    "ok": False,
                    "tool_name": "repair_requested_show",
                    "query": query,
                    "show": title,
                    "scope": effective_scope,
                    "season": effective_season,
                    "episode": effective_episode,
                    "tvdb_id": expected_tvdb_id,
                    "action": service_action(gate_error),
                    "reason": gate_error.get("reason") or "soft_gate_failed",
                    **gate_error,
                    "sickchill_probe": show_episodes,
                    "plex_match": library_match,
                    "user_summary": user_error_summary(
                        tool_family="TV repair",
                        error=gate_error,
                        title=title,
                        change_status="The repair could not work out which episodes to target; nothing was changed.",
                    ),
                }
            title = str(show_episodes.get("show") or title)
            diagnosis = show_episodes.get("diagnosis") or {}
            if not (show_episodes.get("seasons") or {}) and diagnosis:
                return self._nothing_to_refetch_diagnosis(
                    query=query,
                    title=title,
                    expected_tvdb_id=expected_tvdb_id,
                    diagnosis=diagnosis,
                    library_match=library_match,
                    soft_error=soft_error,
                )
            targeted_rows.extend(
                {
                    "season": season_number,
                    "episode": episode_number,
                    "title": None,
                    "status": "unknown",
                    "requested": None,
                    "available": None,
                    "air_date": None,
                }
                for season_number, episode_numbers in (show_episodes.get("seasons") or {}).items()
                for episode_number in episode_numbers
            )
        else:
            return None

        return await self._run_sickchill_repair(
            query=query,
            title=title,
            effective_scope=effective_scope,
            effective_season=effective_season,
            effective_episode=effective_episode,
            expected_tvdb_id=expected_tvdb_id,
            targeted_rows=targeted_rows,
            future_rows=[],
            skipped_rows=[],
            requested=None,
            ombi_soft_error=soft_error,
            library_match=library_match,
        )

    def _nothing_to_refetch_diagnosis(
        self,
        *,
        query: str,
        title: str,
        expected_tvdb_id: int | None,
        diagnosis: dict[str, Any],
        library_match: dict[str, Any] | None,
        soft_error: dict[str, Any] | None,
        scope: str = "show",
        season: int | None = None,
    ) -> dict[str, Any]:
        """Answer for a show Plex can already play all the way through.

        When Plex answers, reaching here means there is nothing wrong: every
        aired episode plays, so the honest report is "complete". The odd episode
        numbering some files carry is not a defect -- Plex resolves it and the
        user never sees it -- so it is deliberately not mentioned.

        The remaining counts only appear when Plex could not be consulted and the
        diagnosis fell back to SickChill's records, where "on disk but not under
        this number" is the best it can say.
        """
        misfiled = diagnosis.get("misfiled_on_disk") or []
        stalled = diagnosis.get("stalled") or []
        healthy = diagnosis.get("healthy") or []

        subject = f"{title} season {season}" if scope == "season" and season is not None else title
        parts: list[str] = []
        if misfiled:
            parts.append(f"{len(misfiled)} more are on the server already")
        if stalled:
            parts.append(f"{len(stalled)} were grabbed at some point but never finished arriving")
        if not parts:
            summary = f"{subject} looks complete -- all {len(healthy)} episodes are present, so nothing needed fixing."
        else:
            summary = (
                f"{subject} looks complete -- {len(healthy)} episodes are present and "
                + "; ".join(parts)
                + ", so nothing needed fixing."
            )

        return {
            "ok": True,
            "tool_name": "repair_requested_show",
            "query": query,
            "show": title,
            "scope": scope,
            "season": season,
            "episode": None,
            "tvdb_id": expected_tvdb_id,
            "action": "repair_requested_show",
            "reason": "no_refetchable_episodes",
            "queued_count": 0,
            "failure_count": 0,
            "healthy_count": len(healthy),
            "misfiled_count": len(misfiled),
            "stalled_count": len(stalled),
            "misfiled_examples": misfiled[:5],
            "stalled_examples": stalled[:5],
            "requested": None,
            "plex_match": library_match,
            **({"ombi_soft_error": soft_error} if soft_error else {}),
            "user_summary": summary,
        }

    async def _run_sickchill_repair(
        self,
        *,
        query: str,
        title: str,
        effective_scope: str,
        effective_season: int | None,
        effective_episode: int | None,
        expected_tvdb_id: int | None,
        targeted_rows: list[dict[str, Any]],
        future_rows: list[dict[str, Any]],
        skipped_rows: list[dict[str, Any]],
        requested: bool | None,
        ombi_soft_error: dict[str, Any] | None,
        library_match: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

        repair_result = {
            "ok": failure_count == 0,
            "tool_name": "repair_requested_show",
            "query": query,
            "show": title,
            "scope": effective_scope,
            "season": effective_season,
            "episode": effective_episode,
            "requested": requested,
            "action": "repair_requested_show",
            "tvdb_id": expected_tvdb_id,
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
        if library_match is not None:
            repair_result["plex_match"] = library_match
        if ombi_soft_error is not None:
            repair_result["ombi_soft_error"] = ombi_soft_error
            repair_result["request_gate_soft_failed"] = True
        if self._repair_result_is_missing_show_handoff(
            repair_result=repair_result,
            requested=requested,
            expected_tvdb_id=expected_tvdb_id,
        ):
            return await self._add_show_from_repair_handoff(
                query=query,
                title=title,
                expected_tvdb_id=expected_tvdb_id,
                effective_scope=effective_scope,
                effective_season=effective_season,
                repair_result=repair_result,
            )
        if not repair_result["ok"]:
            repair_result.update(
                self._repair_failure_metadata(
                    title=title,
                    season_results=season_results,
                    fallback_result=fallback_result,
                )
            )
        return repair_result

    def _repair_result_is_missing_show_handoff(
        self,
        *,
        repair_result: dict[str, Any],
        requested: bool | None,
        expected_tvdb_id: int | None,
    ) -> bool:
        if requested is not True or expected_tvdb_id is None:
            return False
        if repair_result.get("ok"):
            return False
        season_results = repair_result.get("season_results")
        if not isinstance(season_results, list) or not season_results:
            return False
        missing_reasons = {"show_not_found_in_sickchill", "expected_show_not_found_in_sickchill"}
        return all(
            isinstance(row, dict)
            and row.get("show_found") is False
            and str(row.get("reason") or "") in missing_reasons
            for row in season_results
        )

    async def _add_show_from_repair_handoff(
        self,
        *,
        query: str,
        title: str,
        expected_tvdb_id: int,
        effective_scope: str,
        effective_season: int | None,
        repair_result: dict[str, Any],
    ) -> dict[str, Any]:
        season_limited = effective_scope in {"season", "episode"} and effective_season is not None
        add_result = await self.sickchill.add_show(
            tvdb_id=expected_tvdb_id,
            title=title,
            status="ignored" if season_limited else "wanted",
            future_status="ignored" if season_limited else "wanted",
        )
        clear_result: dict[str, Any] | None = None
        if add_result.get("ok") and season_limited:
            clear_result = await self.sickchill.clear_ignored_episodes(show=title, season=effective_season)

        add_ok = bool(add_result.get("ok"))
        activation_pending = (
            add_ok
            and clear_result is not None
            and not clear_result.get("ok")
            and str(clear_result.get("reason") or "") == "show_not_found_in_sickchill"
        )
        ok = add_ok and (clear_result is None or bool(clear_result.get("ok")) or activation_pending)
        result = {
            **repair_result,
            "ok": ok,
            "tool_name": "repair_requested_show",
            "query": query,
            "show": title,
            "action": "missing_show_added_to_sickchill" if ok else "missing_show_add_failed",
            "reason": add_result.get("reason"),
            "repair_detected_missing_show": True,
            "handoff_add_attempted": True,
            "handoff_tool": "add_requested_show_to_sickchill",
            "sickchill_action": add_result.get("action"),
            "sickchill": add_result,
            "clear_ignored_result": clear_result,
            "corrective_action_taken": add_ok,
            "activation_pending": activation_pending,
            "post_add_verification_failed": activation_pending,
            # Read verbatim by a Shabbos user (render.py prefers user_summary) and
            # echoed by the model, so it says what changed without naming a system
            # the user cannot reach. The admin's detail is in the structured fields
            # above (`sickchill_action`, `sickchill`, `clear_ignored_result`).
            "user_summary": (
                f"{title} was requested, but it was never set up to download. I fixed that, so it can start looking."
                if ok
                else f"{title} was requested, but it was never set up to download, and setting that up failed."
            ),
        }
        if not ok:
            result.update(
                self._sickchill_error_metadata(
                    title=title,
                    operation="add_show",
                    result=clear_result if clear_result is not None and not clear_result.get("ok") else add_result,
                    tool_family="TV show handoff repair",
                    include_when_ok=False,
                )
            )
        return result

    def _repair_failure_metadata(
        self,
        *,
        title: str,
        season_results: list[dict[str, Any]],
        fallback_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        source = fallback_result if fallback_result and not fallback_result.get("ok") else None
        if source is None:
            source = next((row for row in season_results if not row.get("ok")), None)
        if not isinstance(source, dict):
            return {}
        return self._sickchill_error_metadata(
            title=title,
            operation=str(source.get("action") or "episode_repair"),
            result=source,
            tool_family="TV repair",
            include_when_ok=False,
        )

    def _sickchill_error_metadata(
        self,
        *,
        title: str,
        operation: str,
        result: dict[str, Any],
        tool_family: str,
        include_when_ok: bool,
    ) -> dict[str, Any]:
        if result.get("ok") and not include_when_ok:
            return {}
        reason = result.get("reason") or result.get("error") or "unknown error"
        error = classify_service_result(
            service="sickchill",
            operation=operation,
            reason=reason,
            backend_connected=result.get("backend_connected"),
        )
        return {
            "action": service_action(error),
            "reason": str(reason),
            **error,
            "user_summary": user_error_summary(
                tool_family=tool_family,
                error=error,
                title=title,
                change_status="Nothing was changed.",
            ),
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

    async def _plex_episode_index(self, library_match: dict[str, Any] | None) -> dict[int, set[int]] | None:
        """What Plex actually holds for this show, or None if it cannot say.

        None is meaningfully different from an empty index: it means "unknown",
        and the diagnosis falls back to reading SickChill's own file records. An
        empty index would mean "Plex holds nothing", which would mark the entire
        show as missing and queue a re-download of all of it.
        """
        if self.plex is None or not library_match or not library_match.get("ok"):
            return None
        rating_key = library_match.get("rating_key")
        if not rating_key:
            return None
        try:
            return await self.plex.episode_index(str(rating_key))
        except Exception:
            logger.warning("plex episode index unavailable for %s", library_match.get("show"), exc_info=True)
            return None

    async def _resolve_show_in_library(self, query: str) -> dict[str, Any]:
        """Ask Plex whether this show is actually on the server, and what it is called there.

        Never raises: a Plex outage must not block a repair, it just means the
        caller falls back to the Ombi request lookup.
        """
        if self.plex is None:
            return {"ok": False, "reason": "plex_client_unavailable", "candidates": []}
        try:
            return await self.plex.resolve_show(query)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "reason": "plex_unreachable",
                "error_message": str(exc),
                "candidates": [],
            }

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
