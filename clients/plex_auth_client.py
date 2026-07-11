from __future__ import annotations

import xml.etree.ElementTree as ET
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

    async def list_shared_users(self, client_identifier: str, admin_token: str) -> dict[str, set[str]]:
        """Return the user ids and (lowercased) usernames/emails the admin has
        shared the Plex server with.

        Authoritative and immediate: plex.tv reflects a share the moment it is
        made, unlike Tautulli which only knows a user after they have streamed.
        """
        ids: set[str] = set()
        names: set[str] = set()
        headers = {**self._headers(client_identifier), "X-Plex-Token": admin_token}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resources = await client.get(
                "https://plex.tv/api/v2/resources",
                params={"includeHttps": 1},
                headers=headers,
            )
            resources.raise_for_status()
            servers = [
                res
                for res in resources.json()
                if isinstance(res, dict) and "server" in (res.get("provides") or "")
            ]
            # Bind to the admin's OWN server -- never a server they're merely a
            # guest on -- so we can't return a bogus membership set. Fall back to
            # the first server only if none is flagged owned.
            owned = [res for res in servers if res.get("owned")]
            candidate = (owned or servers or [{}])[0]
            machine_id = str(candidate.get("clientIdentifier") or "")
            if not machine_id:
                return {"ids": ids, "names": names}
            shared = await client.get(
                f"https://plex.tv/api/servers/{machine_id}/shared_servers",
                headers=headers,
            )
            shared.raise_for_status()
            root = ET.fromstring(shared.text)
        for node in root.iter("SharedServer"):
            uid = str(node.get("userID") or "").strip()
            uname = str(node.get("username") or "").strip()
            email = str(node.get("email") or "").strip()
            if uid:
                ids.add(uid)
            if uname:
                names.add(uname.lower())
            if email:
                names.add(email.lower())
        return {"ids": ids, "names": names}

    def _headers(self, client_identifier: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": client_identifier,
            "X-Plex-Product": self.product_name,
        }
