from __future__ import annotations

from typing import Any

import httpx

from clients.ombi_client import OmbiClient
from clients.radarr_client import RadarrClient
from tools.error_helpers import classify_http_error, service_action, user_error_summary


class MovieRepairTools:
    def __init__(self, ombi: OmbiClient, radarr: RadarrClient) -> None:
        self.ombi = ombi
        self.radarr = radarr

    async def repair_requested_movie(
        self,
        title: str | None = None,
        year: int | None = None,
        issue: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        lookup_query = self._build_lookup_query(title=title, year=year, query=query)
        issue = str(issue or "").strip() or None
        if not lookup_query:
            return {
                "ok": False,
                "tool_name": "repair_requested_movie",
                "query": query,
                "title": title,
                "year": year,
                "issue": issue,
                "action": "movie_identity_required",
                "reason": "movie_repair_requires_title_or_query",
                "user_summary": "I need the movie title before I can run a Radarr repair.",
            }
        try:
            existing = await self.ombi.check_existing_media_status(query=lookup_query)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="multi_search", exc=exc)
            return {
                "ok": False,
                "tool_name": "repair_requested_movie",
                "query": query,
                "lookup_query": lookup_query,
                "requested_title": title,
                "requested_year": year,
                "issue": issue,
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="Movie repair",
                    error=error,
                    title=query,
                    change_status="Radarr repair did not start; nothing was changed.",
                ),
            }

        best_match = existing.get("best_match") or {}
        if best_match.get("type") != "movie" or not best_match.get("title"):
            return {
                "ok": False,
                "tool_name": "repair_requested_movie",
                "query": query,
                "lookup_query": lookup_query,
                "requested_title": title,
                "requested_year": year,
                "issue": issue,
                "action": "movie_not_found",
                "reason": "movie_not_found",
                "match": best_match,
            }

        title = str(best_match.get("title"))
        matched_year = self._safe_int(best_match.get("year")) or year
        display_title = self._display_title(title, matched_year)
        tmdb_id = self._safe_int(best_match.get("tmdb_id"))
        requested = bool(best_match.get("requested"))
        available = bool(best_match.get("available"))

        if not requested and not available:
            return {
                "ok": False,
                "tool_name": "repair_requested_movie",
                "query": query,
                "lookup_query": lookup_query,
                "title": title,
                "year": matched_year,
                "issue": issue,
                "tmdb_id": tmdb_id,
                "requested": False,
                "available": available,
                "action": "not_requested",
                "reason": "movie_not_requested_in_ombi",
            }

        try:
            movies = await self.radarr.get_managed_movies()
        except httpx.HTTPError as exc:
            error = classify_http_error(service="radarr", operation="managed_movie_lookup", exc=exc)
            return {
                "ok": False,
                "tool_name": "repair_requested_movie",
                "query": query,
                "lookup_query": lookup_query,
                "title": title,
                "year": matched_year,
                "issue": issue,
                "tmdb_id": tmdb_id,
                "requested": requested,
                "available": available,
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="Movie repair",
                    error=error,
                    title=title,
                    change_status="Nothing was changed.",
                ),
            }

        managed_movie = self._find_managed_movie(movies, tmdb_id=tmdb_id, title=title)
        if not managed_movie:
            return {
                "ok": False,
                "query": query,
                "lookup_query": lookup_query,
                "title": title,
                "year": matched_year,
                "issue": issue,
                "tmdb_id": tmdb_id,
                "requested": requested,
                "available": available,
                "action": "movie_not_managed_in_radarr",
                "reason": "movie_not_managed_in_radarr",
            }

        movie_id = self._safe_int(managed_movie.get("id"))
        if movie_id is None:
            return {
                "ok": False,
                "query": query,
                "lookup_query": lookup_query,
                "title": title,
                "year": matched_year,
                "issue": issue,
                "tmdb_id": tmdb_id,
                "requested": requested,
                "available": available,
                "action": "radarr_movie_missing_id",
                "reason": "radarr_movie_missing_id",
                "radarr_movie": managed_movie,
            }

        try:
            releases = await self.radarr.get_releases(movie_id=movie_id)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="radarr", operation="release_search", exc=exc)
            return {
                "ok": False,
                "query": query,
                "lookup_query": lookup_query,
                "title": title,
                "year": matched_year,
                "issue": issue,
                "tmdb_id": tmdb_id,
                "requested": requested,
                "available": available,
                "action": service_action(error),
                "reason": str(exc),
                "radarr_movie_id": movie_id,
                **error,
                "user_summary": user_error_summary(
                    tool_family="Movie repair",
                    error=error,
                    title=title,
                    change_status="Nothing was changed.",
                ),
            }

        normalized_releases = [self._normalize_release(item) for item in releases if isinstance(item, dict)]
        torrent_releases = [item for item in normalized_releases if item["protocol"] == "torrent"]
        torrent_releases.sort(key=lambda item: (-item["seeders"], item["age"], item["title"]))

        approved = [item for item in torrent_releases if item["approved"] and item["download_allowed"]]
        override_eligible = [
            item
            for item in torrent_releases
            if item["download_allowed"] and self._only_has_ignorable_rejections(item.get("rejections") or [])
        ]
        selected = approved[0] if approved else (override_eligible[0] if override_eligible else None)

        if selected is None:
            queued_release = next(
                (
                    item
                    for item in torrent_releases
                    if self._has_queue_cutoff_rejection(item.get("rejections") or [])
                ),
                None,
            )
            if queued_release is not None:
                release_title = str(queued_release.get("title") or title)
                return {
                    "ok": True,
                    "query": query,
                    "lookup_query": lookup_query,
                    "title": title,
                    "year": matched_year,
                    "issue": issue,
                    "tmdb_id": tmdb_id,
                    "requested": requested,
                    "available": available,
                    "action": "radarr_replacement_already_queued",
                    "reason": "replacement_already_queued_in_radarr",
                    "corrective_action_taken": True,
                    "download_in_progress": True,
                    "user_summary": (
                        f"Radarr already has a download working for {display_title}: "
                        f"'{release_title}'. It should arrive after that download/import completes."
                    ),
                    "radarr_movie_id": movie_id,
                    "release_count": len(torrent_releases),
                    "approved_count": len(approved),
                    "override_eligible_count": len(override_eligible),
                    "queued_release": queued_release,
                    "top_release": torrent_releases[0] if torrent_releases else None,
                }
            unknown_movie_release = next(
                (
                    item
                    for item in torrent_releases
                    if self._has_unknown_movie_rejection(item.get("rejections") or [])
                ),
                None,
            )
            if unknown_movie_release is not None:
                return {
                    "ok": False,
                    "query": query,
                    "lookup_query": lookup_query,
                    "title": title,
                    "year": matched_year,
                    "issue": issue,
                    "tmdb_id": tmdb_id,
                    "requested": requested,
                    "available": available,
                    "action": "radarr_release_rejected_unknown_movie",
                    "reason": "radarr_rejected_release_as_unknown_movie",
                    "radarr_movie_id": movie_id,
                    "release_count": len(torrent_releases),
                    "approved_count": len(approved),
                    "override_eligible_count": len(override_eligible),
                    "manual_judgment_candidate": True,
                    "top_release": unknown_movie_release,
                }
            return {
                "ok": False,
                "query": query,
                "lookup_query": lookup_query,
                "title": title,
                "year": matched_year,
                "issue": issue,
                "tmdb_id": tmdb_id,
                "requested": requested,
                "available": available,
                "action": "no_approved_radarr_release",
                "reason": "no_approved_radarr_release",
                "radarr_movie_id": movie_id,
                "release_count": len(torrent_releases),
                "approved_count": len(approved),
                "override_eligible_count": len(override_eligible),
                "top_release": torrent_releases[0] if torrent_releases else None,
            }

        grab_payload = {
            "guid": selected["guid"],
            "indexerId": selected["indexer_id"],
            "movieId": movie_id,
            "quality": selected.get("quality_payload"),
            "languages": selected.get("languages_payload") or [],
            "shouldOverride": selected not in approved,
        }

        try:
            grab_result = await self.radarr.grab_release(grab_payload)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="radarr", operation="release_grab", exc=exc)
            return {
                "ok": False,
                "query": query,
                "lookup_query": lookup_query,
                "title": title,
                "year": matched_year,
                "issue": issue,
                "tmdb_id": tmdb_id,
                "requested": requested,
                "available": available,
                "action": service_action(error),
                "reason": str(exc),
                "radarr_movie_id": movie_id,
                "selected_release": selected,
                "grab_payload": grab_payload,
                **error,
                "user_summary": user_error_summary(
                    tool_family="Movie repair",
                    error=error,
                    title=title,
                    change_status="Nothing was changed.",
                ),
            }

        return {
            "ok": True,
            "query": query,
            "lookup_query": lookup_query,
            "title": title,
            "year": matched_year,
            "issue": issue,
            "tmdb_id": tmdb_id,
            "requested": requested,
            "available": available,
            "action": "radarr_release_grab_submitted",
            "corrective_action_taken": True,
            "download_in_progress": True,
            "user_summary": (
                f"Submitted a Radarr download for {display_title}: '{selected['title']}'. "
                "It should arrive after that download/import completes."
            ),
            "radarr_movie_id": movie_id,
            "release_count": len(torrent_releases),
            "approved_count": len(approved),
            "override_eligible_count": len(override_eligible),
            "selection_mode": "approved" if selected in approved else "ignorable_rejection_override",
            "selected_release": selected,
            "grab_payload": grab_payload,
            "grab_result": grab_result,
        }

    def _build_lookup_query(self, title: str | None, year: int | None, query: str | None) -> str:
        clean_title = str(title or "").strip()
        if clean_title:
            safe_year = self._safe_int(year)
            if safe_year:
                return f"{clean_title} ({safe_year})"
            return clean_title
        return str(query or "").strip()

    def _display_title(self, title: str, year: int | None) -> str:
        if year:
            return f"{title} ({year})"
        return title

    def _find_managed_movie(
        self,
        movies: list[dict[str, Any]],
        tmdb_id: int | None,
        title: str,
    ) -> dict[str, Any] | None:
        if tmdb_id is not None:
            for movie in movies:
                if self._safe_int(movie.get("tmdbId")) == tmdb_id:
                    return movie

        normalized_title = self._normalize(title)
        for movie in movies:
            movie_title = str(movie.get("title") or "")
            if self._normalize(movie_title) == normalized_title:
                return movie
        return None

    def _normalize_release(self, release: dict[str, Any]) -> dict[str, Any]:
        return {
            "guid": str(release.get("guid") or ""),
            "indexer_id": self._safe_int(release.get("indexerId")) or 0,
            "title": str(release.get("title") or ""),
            "seeders": self._safe_int(release.get("seeders")) or 0,
            "age": self._safe_int(release.get("age")) or 0,
            "size": self._safe_int(release.get("size")) or 0,
            "indexer": str(release.get("indexer") or ""),
            "approved": bool(release.get("approved")),
            "download_allowed": bool(release.get("downloadAllowed")),
            "rejected": bool(release.get("rejected")),
            "rejections": list(release.get("rejections") or []),
            "quality": (((release.get("quality") or {}).get("quality") or {}).get("name")) or "unknown",
            "quality_payload": release.get("quality"),
            "languages_payload": list(release.get("languages") or []),
            "protocol": str(release.get("protocol") or ""),
        }

    def _normalize(self, value: str) -> str:
        return "".join(char for char in value.lower() if char.isalnum())

    def _only_has_ignorable_rejections(self, rejections: list[Any]) -> bool:
        if not rejections:
            return False
        normalized = [str(item or "").strip().lower() for item in rejections if str(item or "").strip()]
        if not normalized:
            return False
        for reason in normalized:
            if "existing file meets cutoff" in reason:
                continue
            if "quality for existing file on disk is of equal or higher preference" in reason:
                continue
            return False
        return True

    def _has_queue_cutoff_rejection(self, rejections: list[Any]) -> bool:
        normalized = [str(item or "").strip().lower() for item in rejections if str(item or "").strip()]
        for reason in normalized:
            if "quality for release in queue already meets cutoff" in reason:
                return True
        return False

    def _has_unknown_movie_rejection(self, rejections: list[Any]) -> bool:
        normalized = [str(item or "").strip().lower() for item in rejections if str(item or "").strip()]
        return any("unknown movie" in reason for reason in normalized)

    def _grab_failure_reason(self, grab_result: Any, selected_release: dict[str, Any]) -> str:
        if isinstance(grab_result, dict):
            if grab_result.get("rejections"):
                return "; ".join(str(item) for item in grab_result.get("rejections") or [])
            if grab_result.get("downloadAllowed") is False:
                selected_rejections = selected_release.get("rejections") or []
                if selected_rejections:
                    return "; ".join(str(item) for item in selected_rejections)
                return "radarr_declined_grab"
        return "radarr_declined_grab"

    def _safe_int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
