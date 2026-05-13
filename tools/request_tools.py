from __future__ import annotations

from clients.ombi_client import OmbiClient


class RequestTools:
    def __init__(self, ombi: OmbiClient) -> None:
        self.ombi = ombi

    async def request_movie_for_user(self, username: str, tmdb_id: int) -> dict:
        return await self.ombi.request_movie_for_user(username=username, tmdb_id=tmdb_id)

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
        return await self.ombi.check_show_request_status(query=query, username=username)

    async def get_show_season_status(self, query: str, season: int | None = None) -> dict:
        return await self.ombi.get_show_season_status(query=query, season=season)
