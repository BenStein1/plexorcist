from __future__ import annotations

from clients.ombi_client import OmbiClient
from clients.plex_client import PlexClient


class MediaSearchTools:
    def __init__(self, ombi: OmbiClient, plex: PlexClient) -> None:
        self.ombi = ombi
        self.plex = plex

    async def search_media(self, query: str) -> dict:
        results = await self.ombi.search_media(query)
        top = results["results"][0] if results["results"] else None
        plex_query = top.get("title") if isinstance(top, dict) and top.get("title") else query
        plex = await self.plex.check_availability(plex_query)
        return {
            "query": query,
            "effective_query": results.get("effective_query") or query,
            "attempted_queries": results.get("attempted_queries") or [query],
            "candidate": top,
            "candidates": results.get("results", [])[:5],
            "plex": plex,
        }

    async def check_movie_availability(self, title: str) -> dict:
        return await self.plex.check_movie_availability(title=title)

    async def check_movies_availability_batch(self, titles: list[str]) -> dict:
        results = []
        for title in titles:
            if not str(title or "").strip():
                continue
            availability = await self.plex.check_movie_availability(title=str(title))
            results.append(availability)
        return {
            "titles": titles,
            "results": results,
        }

    async def check_existing_media_status(self, query: str) -> dict:
        return await self.ombi.check_existing_media_status(query=query)

    async def check_library_inventory(self, query: str) -> dict:
        plex_matches = await self.plex.search_catalog(query)
        ombi_search = await self.ombi.search_media(query)
        ombi_results = ombi_search.get("results", []) if isinstance(ombi_search, dict) else []
        return {
            "query": query,
            "plex_matches": [
                {
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "type": item.get("type"),
                    "library": item.get("library"),
                    "library_type": item.get("library_type"),
                    "rating_key": item.get("ratingKey"),
                    "original_title": item.get("originalTitle"),
                }
                for item in plex_matches
            ],
            "ombi_candidates": [
                {
                    "title": item.get("title"),
                    "type": item.get("type"),
                    "tmdb_id": item.get("tmdb_id"),
                    "tvdb_id": item.get("tvdb_id"),
                }
                for item in ombi_results
            ],
        }
