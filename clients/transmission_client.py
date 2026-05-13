from __future__ import annotations

import httpx


class TransmissionClient:
    def __init__(self, host: str, username: str | None = None, password: str | None = None) -> None:
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self._session_id: str | None = None

    async def add_torrent(self, magnet_or_url: str, label: str) -> dict:
        payload = {
            "method": "torrent-add",
            "arguments": {
                "filename": magnet_or_url,
                "labels": [label] if label else [],
            },
        }
        result = await self._rpc(payload)
        arguments = result.get("arguments", {}) if isinstance(result, dict) else {}
        return {
            "ok": result.get("result") == "success" if isinstance(result, dict) else False,
            "label": label,
            "source": magnet_or_url,
            "torrent_added": arguments.get("torrent-added") or arguments.get("torrent-duplicate"),
            "raw": result,
        }

    async def _rpc(self, payload: dict) -> dict:
        auth = (self.username, self.password) if self.username or self.password else None
        headers = {"Content-Type": "application/json"}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self.host}/transmission/rpc",
                json=payload,
                headers=headers,
                auth=auth,
            )
            if response.status_code == 409:
                self._session_id = response.headers.get("X-Transmission-Session-Id")
                if not self._session_id:
                    response.raise_for_status()
                headers["X-Transmission-Session-Id"] = self._session_id
                response = await client.post(
                    f"{self.host}/transmission/rpc",
                    json=payload,
                    headers=headers,
                    auth=auth,
                )
            response.raise_for_status()
            return response.json() if response.content else {}
