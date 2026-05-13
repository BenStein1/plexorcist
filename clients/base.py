from __future__ import annotations

from typing import Any

import httpx


class BaseHttpClient:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout or 15.0) as client:
            response = await client.get(path, params=params, headers=self._headers(headers))
            response.raise_for_status()
            return response.json() if response.content else {}

    async def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout or 15.0) as client:
            response = await client.post(path, json=payload, headers=self._headers(headers))
            response.raise_for_status()
            return response.json() if response.content else {}

    async def put_json(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout or 15.0) as client:
            response = await client.put(path, json=payload, headers=self._headers(headers))
            response.raise_for_status()
            return response.json() if response.content else {}

    def _headers(self, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if extra_headers:
            headers.update(extra_headers)
        return headers
