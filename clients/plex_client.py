from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx


class PlexClient:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def check_availability(self, title: str) -> dict:
        matches = await self.search(title)
        best_match = matches[0] if matches else None
        available = bool(best_match and self._normalize(best_match.get("title")) == self._normalize(title))
        return {
            "title": title,
            "available": available,
            "best_match": best_match,
            "matches": matches,
        }

    async def check_movie_availability(self, title: str) -> dict:
        matches = await self.search(title, section_types={"movie"})
        best_match = matches[0] if matches else None
        available = bool(best_match and self._normalize(best_match.get("title")) == self._normalize(title))
        return {
            "title": title,
            "available": available,
            "library": self._match_library(best_match) if best_match else "Movies",
            "best_match": best_match,
            "matches": matches,
        }

    async def check_episode_availability(self, show: str, season: int, episode: int) -> dict:
        show_match = await self.find_show(show)
        if not show_match:
            return {
                "show": show,
                "season": season,
                "episode": episode,
                "present": False,
                "show_found": False,
                "season_found": False,
                "episode_found": False,
                "reason": "show_not_found_in_plex",
            }

        episodes = await self._fetch_show_episodes(show_match["ratingKey"])
        season_match = None
        episode_match = None
        for item in episodes:
            if int(item.get("parentIndex") or item.get("parentindex") or 0) != season:
                continue
            if int(item.get("index") or item.get("episode") or 0) == episode:
                episode_match = item
                break
            if season_match is None:
                season_match = item

        return {
            "show": show,
            "season": season,
            "episode": episode,
            "present": episode_match is not None,
            "show_found": True,
            "season_found": season_match is not None or episode_match is not None,
            "episode_found": episode_match is not None,
            "show_match": show_match,
            "episode_match": episode_match,
            "matches": episodes,
        }

    async def find_show(self, title: str) -> dict[str, Any] | None:
        matches = await self.search(title, section_types={"show"})
        if not matches:
            return None

        normalized = self._normalize(title)
        exact_matches = [item for item in matches if self._normalize(item.get("title")) == normalized]
        if exact_matches:
            return exact_matches[0]
        return matches[0]

    async def search(self, title: str, section_types: set[str] | None = None) -> list[dict[str, Any]]:
        sections = await self.list_sections()
        title_matches: list[dict[str, Any]] = []
        for section in sections:
            if section_types and section.get("type") not in section_types:
                continue
            section_matches = await self._search_section(section["key"], title)
            for item in section_matches:
                item = dict(item)
                item["library"] = section.get("title")
                item["library_type"] = section.get("type")
                item["library_key"] = section.get("key")
                title_matches.append(item)
        return self._rank_matches(title_matches, title)

    async def search_catalog(self, query: str, section_types: set[str] | None = None) -> list[dict[str, Any]]:
        matches = await self.search(query, section_types=section_types)
        global_matches = await self._search_global(query)
        deep_matches = await self._search_deep_catalog(query, section_types=section_types)
        by_rating_key: dict[str, dict[str, Any]] = {}
        for item in matches + global_matches + deep_matches:
            if section_types and item.get("type") not in section_types:
                continue
            rating_key = str(item.get("ratingKey") or item.get("key") or "")
            if not rating_key:
                continue
            by_rating_key[rating_key] = item
        return self._rank_matches(list(by_rating_key.values()), query)

    async def list_sections(self) -> list[dict[str, Any]]:
        payload = await self._request_json("/library/sections")
        container = payload.get("MediaContainer", payload)
        sections = []
        for item in self._collect_items(container):
            if item.get("key") and item.get("type") in {"movie", "show"}:
                sections.append(
                    {
                        "key": str(item["key"]).lstrip("/"),
                        "title": item.get("title") or item.get("title1") or "",
                        "type": item.get("type"),
                    }
                )
        return sections

    async def _search_section(self, section_key: str, title: str) -> list[dict[str, Any]]:
        payload = await self._request_json(
            f"/library/sections/{section_key}/all",
            params={
                "title": title,
                "includeGuids": 1,
                "includeDetails": 1,
            },
        )
        container = payload.get("MediaContainer", payload)
        items = []
        for item in self._collect_items(container):
            if item.get("title") and item.get("ratingKey"):
                items.append(item)
        return items

    async def _search_global(self, query: str) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "/search",
            params={
                "query": query,
                "limit": 50,
                "includeGuids": 1,
            },
        )
        container = payload.get("MediaContainer", payload)
        items = []
        for item in self._collect_items(container):
            if item.get("title") and item.get("ratingKey") and item.get("type") in {"movie", "show"}:
                items.append(item)
        return items

    async def _search_deep_catalog(self, query: str, section_types: set[str] | None = None) -> list[dict[str, Any]]:
        sections = await self.list_sections()
        matches: list[dict[str, Any]] = []
        for section in sections:
            if section_types and section.get("type") not in section_types:
                continue
            section_items = await self._scan_section_catalog(section["key"])
            for item in section_items:
                if not self._matches_catalog_query(item, query):
                    continue
                current = dict(item)
                current["library"] = section.get("title")
                current["library_type"] = section.get("type")
                current["library_key"] = section.get("key")
                matches.append(current)
        return matches

    async def _scan_section_catalog(self, section_key: str) -> list[dict[str, Any]]:
        payload = await self._request_json(
            f"/library/sections/{section_key}/all",
            params={
                "includeGuids": 1,
                "includeDetails": 1,
            },
        )
        container = payload.get("MediaContainer", payload)
        items = []
        for item in self._collect_items(container):
            if item.get("title") and item.get("ratingKey") and item.get("type") in {"movie", "show"}:
                items.append(item)
        return items

    async def _fetch_show_episodes(self, rating_key: str) -> list[dict[str, Any]]:
        payload = await self._request_json(f"/library/metadata/{rating_key}/children")
        container = payload.get("MediaContainer", payload)
        season_nodes = [item for item in self._collect_items(container) if item.get("ratingKey")]
        episodes: list[dict[str, Any]] = []
        for season in season_nodes:
            season_number = season.get("index") or season.get("parentIndex") or season.get("season")
            season_rating_key = season.get("ratingKey")
            if season_rating_key is None:
                continue
            season_payload = await self._request_json(f"/library/metadata/{season_rating_key}/children")
            season_container = season_payload.get("MediaContainer", season_payload)
            for item in self._collect_items(season_container):
                if not item.get("ratingKey"):
                    continue
                if item.get("type") not in {"episode", None} and not item.get("index"):
                    continue
                current = dict(item)
                if season_number is not None and current.get("parentIndex") is None:
                    current["parentIndex"] = season_number
                episodes.append(current)
        if episodes:
            return self._dedupe_by_rating_key(episodes)

        # Fallback for shows that expose episodes directly.
        all_leaves_payload = await self._request_json(f"/library/metadata/{rating_key}/allLeaves")
        all_leaves_container = all_leaves_payload.get("MediaContainer", all_leaves_payload)
        all_leaves = [item for item in self._collect_items(all_leaves_container) if item.get("ratingKey")]
        return self._dedupe_by_rating_key(all_leaves)

    async def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Plex-Token"] = self.token

        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            response = await client.get(path, params=params, headers=headers)
            response.raise_for_status()
            if not response.content:
                return {}
            try:
                return response.json()
            except json.JSONDecodeError:
                return self._parse_xml(response.text)

    def _parse_xml(self, text: str) -> dict[str, Any]:
        root = ET.fromstring(text)
        return {root.tag: self._element_to_data(root)}

    def _element_to_data(self, element: ET.Element) -> dict[str, Any]:
        data: dict[str, Any] = dict(element.attrib)
        children = list(element)
        if element.text and element.text.strip():
            data["value"] = element.text.strip()
        for child in children:
            child_data = self._element_to_data(child)
            data.setdefault(child.tag, [])
            data[child.tag].append(child_data)
        return data

    def _collect_items(self, node: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if self._looks_like_media_item(value):
                    items.append(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(node)
        return items

    def _looks_like_media_item(self, value: dict[str, Any]) -> bool:
        return bool(value.get("title") and (value.get("ratingKey") or value.get("key")))

    def _dedupe_by_rating_key(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in items:
            rating_key = str(item.get("ratingKey") or item.get("key") or "")
            if rating_key in seen:
                continue
            seen.add(rating_key)
            deduped.append(item)
        return self._rank_matches(deduped, "")

    def _rank_matches(self, matches: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        normalized_query = self._normalize(query)

        def score(item: dict[str, Any]) -> tuple[int, int, str]:
            title = self._normalize(item.get("title"))
            if title == normalized_query and normalized_query:
                rank = 300
            elif normalized_query and title.startswith(normalized_query):
                rank = 220
            elif normalized_query and normalized_query in title:
                rank = 180
            elif normalized_query and title in normalized_query:
                rank = 160
            else:
                rank = 100

            for field in ("originalTitle", "summary", "tagline"):
                text = self._normalize(item.get(field))
                if normalized_query and text and normalized_query in text:
                    rank = max(rank, 170)

            for tag_field in ("Role", "Director", "Writer", "Producer", "Guid"):
                tag_values = item.get(tag_field)
                if isinstance(tag_values, list):
                    joined = " ".join(
                        str(tag.get("tag") or tag.get("id") or "")
                        for tag in tag_values
                        if isinstance(tag, dict)
                    )
                    if normalized_query and normalized_query in self._normalize(joined):
                        rank = max(rank, 260)

            if item.get("type") == "show":
                rank += 20

            year = item.get("year")
            year_rank = 1 if isinstance(year, int) or (isinstance(year, str) and year.isdigit()) else 0
            return (rank, year_rank, title)

        return sorted(matches, key=score, reverse=True)

    def _matches_catalog_query(self, item: dict[str, Any], query: str) -> bool:
        normalized_query = self._normalize(query)
        if not normalized_query:
            return False

        for field in ("title", "originalTitle", "summary", "tagline"):
            text = self._normalize(item.get(field))
            if text and normalized_query in text:
                return True

        for tag_field in ("Role", "Director", "Writer", "Producer"):
            tag_values = item.get(tag_field)
            if not isinstance(tag_values, list):
                continue
            for tag in tag_values:
                if not isinstance(tag, dict):
                    continue
                if normalized_query in self._normalize(tag.get("tag")):
                    return True

        return False

    def _match_library(self, match: dict[str, Any] | None) -> str:
        if not match:
            return "Movies"
        library = match.get("library")
        if library:
            return str(library)
        item_type = match.get("type")
        if item_type == "show":
            return "Television"
        return "Movies"

    def _normalize(self, value: Any) -> str:
        text = str(value or "").lower()
        return re.sub(r"[^a-z0-9]+", "", text)
