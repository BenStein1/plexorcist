from __future__ import annotations

import re
from typing import Any

import httpx


_IMDB_ID_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)


class TmdbClient:
    """Small TMDB adapter used only for external-id resolution."""

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.themoviedb.org") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")

    async def resolve_movie_by_imdb_id(self, imdb_id: str) -> dict[str, Any]:
        normalized = str(imdb_id or "").strip().lower()
        if not _IMDB_ID_RE.fullmatch(normalized):
            return {
                "ok": False,
                "status": "invalid_imdb_id",
                "imdb_id": normalized or None,
                "reason": "IMDb movie ids must look like tt0089118.",
            }
        if not self.api_key:
            return {
                "ok": False,
                "status": "not_configured",
                "imdb_id": normalized,
                "reason": "TMDB_API_KEY is not configured.",
            }

        payload = await self._get_json(
            f"/3/find/{normalized}",
            params={"api_key": self.api_key, "external_source": "imdb_id"},
        )
        movies = payload.get("movie_results") if isinstance(payload, dict) else None
        movie = movies[0] if isinstance(movies, list) and movies and isinstance(movies[0], dict) else None
        if movie is None:
            return {
                "ok": False,
                "status": "not_found",
                "imdb_id": normalized,
                "reason": "TMDB returned no movie for that IMDb id.",
            }

        tmdb_id = self._safe_int(movie.get("id"))
        if tmdb_id is None or tmdb_id < 1:
            return {
                "ok": False,
                "status": "invalid_response",
                "imdb_id": normalized,
                "reason": "TMDB returned a movie without a usable TMDB id.",
            }

        release_date = str(movie.get("release_date") or "")
        year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
        return {
            "ok": True,
            "status": "resolved",
            "imdb_id": normalized,
            "tmdb_id": tmdb_id,
            "title": movie.get("title") or movie.get("original_title"),
            "year": year,
        }

    async def _get_json(self, path: str, *, params: dict[str, str]) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
            response = await client.get(path, params=params, headers={"accept": "application/json"})
            response.raise_for_status()
            return response.json() if response.content else {}

    @staticmethod
    def _safe_int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
