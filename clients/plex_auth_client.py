from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx


class PlexAuthClient:
    def __init__(self, client_identifier_path: str | None = None, product_name: str = "Plexorcist Concierge") -> None:
        self.product_name = product_name
        self.client_identifier_path = client_identifier_path

    @staticmethod
    def build_auth_url(client_identifier: str, code: str, forward_url: str, product_name: str) -> str:
        params = urlencode(
            {
                "clientID": client_identifier,
                "code": code,
                "forwardUrl": forward_url,
                "context[device][product]": product_name,
            }
        )
        return f"https://app.plex.tv/auth#?{params}"

    async def create_pin(self, client_identifier: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://plex.tv/api/v2/pins?strong=true",
                headers=self._headers(client_identifier),
            )
            response.raise_for_status()
            return response.json()

    async def get_pin(self, client_identifier: str, pin_id: str | int) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"https://plex.tv/api/v2/pins/{pin_id}",
                headers=self._headers(client_identifier),
            )
            response.raise_for_status()
            return response.json()

    async def get_user(self, client_identifier: str, plex_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                "https://plex.tv/api/v2/user",
                headers={
                    **self._headers(client_identifier),
                    "X-Plex-Token": plex_token,
                },
            )
            response.raise_for_status()
            return response.json()

    def _headers(self, client_identifier: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": client_identifier,
            "X-Plex-Product": self.product_name,
        }
