"""Movie repair resolves identity against Plex, not the Ombi request catalogue.

Same defect as the TV path: Ombi is a request service, so using its catalogue
search to decide what a movie *is* -- and its request record to decide whether a
broken copy may be refetched -- is the wrong authority. Plex is what exists on
the server; Radarr does the repair.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tools.movie_repair_tools import MovieRepairTools


class TrackingOmbi:
    def __init__(self, best_match: dict[str, Any] | None = None) -> None:
        self._best_match = best_match or {}
        self.searches: list[str] = []

    async def check_existing_media_status(self, query: str) -> dict[str, Any]:
        self.searches.append(query)
        return {"best_match": self._best_match}


class FakePlex:
    def __init__(self, movies: list[dict[str, Any]]) -> None:
        self._movies = movies

    async def resolve_movie(self, title: str) -> dict[str, Any]:
        normalized = self._normalize(title)
        matches = [m for m in self._movies if normalized in self._normalize(m["movie"])]
        if not matches:
            return {"ok": False, "reason": "movie_not_in_plex", "candidates": []}
        return {"ok": True, "reason": "exact_title_match", "candidates": matches, **matches[0]}

    @staticmethod
    def _normalize(value: str) -> str:
        text = str(value or "").lower().replace("&", " and ")
        return "".join(ch for ch in text if ch.isalnum())


class FakeRadarr:
    def __init__(self, movies: list[dict[str, Any]] | None = None) -> None:
        self.movies = movies if movies is not None else []
        self.release_calls: list[int] = []

    async def get_managed_movies(self) -> list[dict[str, Any]]:
        return self.movies

    async def get_releases(self, movie_id: int) -> list[dict[str, Any]]:
        self.release_calls.append(movie_id)
        return []


@pytest.mark.asyncio
async def test_movie_on_the_server_repairs_via_plex_identity_without_asking_ombi():
    ombi = TrackingOmbi()
    plex = FakePlex([{"movie": "Blade Runner", "tmdb_id": 78, "year": 1982}])
    radarr = FakeRadarr([{"id": 5, "tmdbId": 78, "title": "Blade Runner", "year": 1982}])
    tools = MovieRepairTools(ombi=ombi, radarr=radarr, plex=plex)

    result = await tools.repair_requested_movie(title="Blade Runner")

    assert ombi.searches == [], "Ombi must not gate a movie already on the server"
    assert radarr.release_calls == [5], "Radarr owns the actual repair"
    assert result["plex_match"]["movie"] == "Blade Runner"


@pytest.mark.asyncio
async def test_movie_absent_from_plex_falls_through_to_the_request_lookup():
    ombi = TrackingOmbi({"type": "movie", "title": "Nope", "requested": False, "available": False})
    plex = FakePlex([])
    radarr = FakeRadarr()
    tools = MovieRepairTools(ombi=ombi, radarr=radarr, plex=plex)

    result = await tools.repair_requested_movie(title="Nope")

    assert result["ok"] is False
    assert result["reason"] == "movie_not_requested_in_ombi"
    assert result["in_plex"] is False
    assert ombi.searches == ["Nope"]
    assert "requested" in result["user_summary"]
    assert "ombi" not in result["user_summary"].lower()
    assert "radarr" not in result["user_summary"].lower()


@pytest.mark.asyncio
async def test_plex_outage_does_not_block_a_movie_repair():
    class BrokenPlex:
        async def resolve_movie(self, title: str) -> dict[str, Any]:
            raise httpx.ConnectError("plex down")

    ombi = TrackingOmbi(
        {"type": "movie", "title": "Blade Runner", "tmdb_id": 78, "requested": True, "available": True}
    )
    radarr = FakeRadarr([{"id": 5, "tmdbId": 78, "title": "Blade Runner"}])
    tools = MovieRepairTools(ombi=ombi, radarr=radarr, plex=BrokenPlex())

    await tools.repair_requested_movie(title="Blade Runner")

    assert ombi.searches == ["Blade Runner"], "a Plex outage degrades to the request lookup"
    assert radarr.release_calls == [5]
