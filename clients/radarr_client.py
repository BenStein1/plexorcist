from __future__ import annotations

from typing import Any

import httpx


class RadarrClient:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()

    async def get_managed_movies(self) -> list[dict[str, Any]]:
        return await self._get_json("/api/v3/movie")

    async def get_releases(self, movie_id: int) -> list[dict[str, Any]]:
        return await self._get_json("/api/v3/release", params={"movieId": movie_id}, timeout=45.0)

    async def grab_release(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/api/v3/release", payload, timeout=45.0)

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as client:
            response = await client.get(path, params=self._params(params), headers=self._headers())
            response.raise_for_status()
            return response.json() if response.content else {}

    async def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float = 20.0,
    ) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as client:
            response = await client.post(path, params=self._params(), json=payload, headers=self._headers())
            response.raise_for_status()
            return response.json() if response.content else {}

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    def _params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(params or {})
        if self.api_key:
            merged.setdefault("apikey", self.api_key)
        return merged
