from __future__ import annotations

from clients.tautulli_client import TautulliClient


class RecommendationTools:
    def __init__(self, tautulli: TautulliClient) -> None:
        self.tautulli = tautulli

    async def get_user_watch_context(self, user_id: str | None = None, username: str | None = None) -> dict:
        return await self.tautulli.get_user_watch_context(user_id=user_id, username=username)

    async def get_plex_popularity(self, days: int = 30, limit: int = 10) -> dict:
        return await self.tautulli.get_plex_popularity(days=days, limit=limit)
