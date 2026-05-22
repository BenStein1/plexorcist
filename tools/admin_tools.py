from __future__ import annotations

import asyncio
from typing import Any

import httpx

from clients.transmission_client import TransmissionClient


class AdminTools:
    def __init__(self, transmission: TransmissionClient, *, verify_wait_seconds: int = 30) -> None:
        self.transmission = transmission
        self.verify_wait_seconds = max(0, int(verify_wait_seconds))

    async def run_transmission_maintenance(self) -> dict[str, Any]:
        try:
            torrents = await self.transmission.get_torrents()
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "action": "transmission_maintenance_failed",
                "reason": "transmission_unreachable",
                "error": str(exc),
                "user_summary": "Transmission maintenance could not start because Transmission was unreachable.",
            }

        completed = [torrent for torrent in torrents if bool(torrent.get("isFinished"))]
        initial_errors = [torrent for torrent in torrents if int(torrent.get("error") or 0) != 0]

        verified: list[dict[str, Any]] = []
        verify_failures: list[dict[str, Any]] = []
        for torrent in completed:
            torrent_id = self._torrent_id(torrent)
            if torrent_id is None:
                continue
            try:
                await self.transmission.verify_torrent(torrent_id)
                verified.append(self._torrent_summary(torrent))
            except httpx.HTTPError as exc:
                verify_failures.append({**self._torrent_summary(torrent), "error": str(exc)})

        if completed and self.verify_wait_seconds:
            await asyncio.sleep(self.verify_wait_seconds)

        try:
            refreshed = await self.transmission.get_torrents()
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "action": "transmission_maintenance_partial",
                "reason": "transmission_unreachable_after_verify",
                "error": str(exc),
                "completed_count": len(completed),
                "verified_count": len(verified),
                "verify_failure_count": len(verify_failures),
                "user_summary": "Transmission maintenance verified completed torrents, but could not recheck the queue afterward.",
            }

        error_torrents = [torrent for torrent in refreshed if int(torrent.get("error") or 0) != 0]
        removed: list[dict[str, Any]] = []
        remove_failures: list[dict[str, Any]] = []
        for torrent in error_torrents:
            torrent_id = self._torrent_id(torrent)
            if torrent_id is None:
                continue
            try:
                await self.transmission.remove_torrent(torrent_id, delete_local_data=True)
                removed.append(self._torrent_summary(torrent))
            except httpx.HTTPError as exc:
                remove_failures.append({**self._torrent_summary(torrent), "error": str(exc)})

        try:
            refreshed = await self.transmission.get_torrents()
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "action": "transmission_maintenance_partial",
                "reason": "transmission_unreachable_after_remove",
                "error": str(exc),
                "completed_count": len(completed),
                "verified_count": len(verified),
                "removed_error_count": len(removed),
                "user_summary": "Transmission maintenance removed errored torrents, but could not recheck the queue for peer refreshes afterward.",
            }

        stalled = [
            torrent
            for torrent in refreshed
            if float(torrent.get("percentDone") or 0.0) == 0.0 and int(torrent.get("status") or 0) == 4
        ]
        reannounced: list[dict[str, Any]] = []
        reannounce_failures: list[dict[str, Any]] = []
        for torrent in stalled:
            torrent_id = self._torrent_id(torrent)
            if torrent_id is None:
                continue
            try:
                await self.transmission.reannounce_torrent(torrent_id)
                reannounced.append(self._torrent_summary(torrent))
            except httpx.HTTPError as exc:
                reannounce_failures.append({**self._torrent_summary(torrent), "error": str(exc)})

        failed_count = len(verify_failures) + len(remove_failures) + len(reannounce_failures)
        return {
            "ok": failed_count == 0,
            "action": "transmission_maintenance_completed",
            "completed_count": len(completed),
            "initial_error_count": len(initial_errors),
            "verified_count": len(verified),
            "removed_error_count": len(removed),
            "reannounced_stalled_count": len(reannounced),
            "verify_failure_count": len(verify_failures),
            "remove_failure_count": len(remove_failures),
            "reannounce_failure_count": len(reannounce_failures),
            "verified": verified[:20],
            "removed_errors": removed[:20],
            "reannounced_stalled": reannounced[:20],
            "failures": {
                "verify": verify_failures[:20],
                "remove": remove_failures[:20],
                "reannounce": reannounce_failures[:20],
            },
            "user_summary": (
                "Transmission maintenance ran: "
                f"verified {len(verified)} completed torrent(s), "
                f"removed {len(removed)} errored torrent(s), "
                f"and asked trackers for more peers on {len(reannounced)} stalled torrent(s)."
            ),
        }

    def _torrent_id(self, torrent: dict[str, Any]) -> int | None:
        try:
            return int(torrent.get("id"))
        except (TypeError, ValueError):
            return None

    def _torrent_summary(self, torrent: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": self._torrent_id(torrent),
            "name": str(torrent.get("name") or ""),
            "status": torrent.get("status"),
            "error": torrent.get("error"),
            "error_string": torrent.get("errorString"),
            "percent_done": torrent.get("percentDone"),
        }
