from __future__ import annotations

import httpx

from clients.ombi_client import OmbiClient
from tools.error_helpers import classify_http_error, service_action, user_error_summary


class RequestTools:
    def __init__(self, ombi: OmbiClient) -> None:
        self.ombi = ombi

    async def request_movie_for_user(
        self,
        username: str,
        tmdb_id: int | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> dict:
        return await self.ombi.request_movie_for_user(
            username=username,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
        )

    async def request_show_scope_for_user(self, username: str, tvdb_id: int, scope: str) -> dict:
        return await self.ombi.request_show_scope_for_user(username=username, tvdb_id=tvdb_id, scope=scope)

    async def request_episode_for_user(self, username: str, tvdb_id: int, season: int, episode: int) -> dict:
        return await self.ombi.request_episode_for_user(
            username=username,
            tvdb_id=tvdb_id,
            season=season,
            episode=episode,
        )

    async def check_movie_request_status(self, query: str, username: str | None = None) -> dict:
        return await self.ombi.check_movie_request_status(query=query, username=username)

    async def check_show_request_status(self, query: str, username: str | None = None) -> dict:
        try:
            return await self.ombi.check_show_request_status(query=query, username=username)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="show_request_status", exc=exc)
            return {
                "ok": False,
                "query": query,
                "username": username,
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="Show request status",
                    error=error,
                    title=query,
                    change_status="No request status was returned.",
                ),
            }

    async def get_show_season_status(self, query: str, season: int | None = None) -> dict:
        try:
            return await self.ombi.get_show_season_status(query=query, season=season)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="tv_request_status", exc=exc)
            return {
                "ok": False,
                "query": query,
                "season": season,
                "found": False,
                "episodes": [],
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="TV status check",
                    error=error,
                    title=query,
                    change_status="No season status was returned.",
                ),
            }
