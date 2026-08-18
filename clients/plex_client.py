from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx


class PlexClient:
    _recommendation_catalog_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._recommendation_catalog_ttl_seconds = 300.0

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

    async def verify_recommendation_candidates(self, candidates: list[dict[str, Any]]) -> dict:
        catalog = await self._get_recommendation_catalog()
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            title = str(candidate.get("title") or "").strip()
            raw_media_type = candidate.get("media_type")
            if hasattr(raw_media_type, "value"):
                raw_media_type = raw_media_type.value
            media_type = str(raw_media_type or "").strip().lower() or None
            if media_type == "any":
                media_type = None
            year = self._safe_int(candidate.get("year"))
            normalized_title = self._normalize(title)
            title_matches = [
                item
                for item in catalog
                if self._normalize(item.get("title")) == normalized_title
                and (media_type is None or item.get("media_type") == media_type)
            ]
            matches = [item for item in title_matches if year is None or self._safe_int(item.get("year")) == year]
            if len(matches) == 1:
                status = "available"
                match = matches[0]
            elif len(matches) > 1:
                status = "ambiguous"
                match = None
            else:
                status = "unavailable"
                match = None
            results.append(
                {
                    "candidate": {"title": title, "year": year, "media_type": media_type},
                    "status": status,
                    "available": status == "available",
                    "match": match,
                    "matches": matches if status == "ambiguous" else title_matches[:5],
                }
            )
        return {
            "ok": True,
            "results": results,
            "available": [item for item in results if item["status"] == "available"],
            "unavailable": [item for item in results if item["status"] == "unavailable"],
            "ambiguous": [item for item in results if item["status"] == "ambiguous"],
        }

    async def search_recommendation_pool(
        self,
        *,
        media_type: str = "any",
        genres: list[str] | None = None,
        keywords: list[str] | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        limit: int = 50,
    ) -> dict:
        if hasattr(media_type, "value"):
            media_type = media_type.value
        media_type = str(media_type or "any").lower()
        catalog = await self._get_recommendation_catalog()
        requested_genres = [str(value).strip() for value in (genres or []) if str(value).strip()]
        requested_keywords = [str(value).strip() for value in (keywords or []) if str(value).strip()]
        normalized_genres = [self._normalize(value) for value in requested_genres]
        normalized_keywords = [self._normalize(value) for value in requested_keywords]
        safe_limit = max(1, min(int(limit), 50))

        hard_filtered: list[dict[str, Any]] = []
        for item in catalog:
            if media_type != "any" and item.get("media_type") != media_type:
                continue
            item_year = self._safe_int(item.get("year"))
            if year_min is not None and (item_year is None or item_year < year_min):
                continue
            if year_max is not None and (item_year is None or item_year > year_max):
                continue
            item_genres = {self._normalize(value) for value in item.get("genres") or []}
            if normalized_genres and not all(genre in item_genres for genre in normalized_genres):
                continue
            hard_filtered.append(item)

        scored: list[tuple[int, dict[str, Any], list[str]]] = []
        for item in hard_filtered:
            fields = {
                "title": self._normalize(item.get("title")),
                "tagline": self._normalize(item.get("tagline")),
                "summary": self._normalize(item.get("summary")),
                "genres": self._normalize(" ".join(item.get("genres") or [])),
            }
            score = 0
            reasons: list[str] = []
            for original, keyword in zip(requested_keywords, normalized_keywords, strict=True):
                if not keyword:
                    continue
                matched_fields = [name for name, text in fields.items() if keyword in text]
                if not matched_fields:
                    continue
                score += max({"title": 8, "tagline": 5, "summary": 3, "genres": 2}[name] for name in matched_fields)
                reasons.append(f"{original}: {', '.join(matched_fields)}")
            scored.append((score, item, reasons))

        def rank(entry: tuple[int, dict[str, Any], list[str]]) -> tuple[float, float, str]:
            score, item, _ = entry
            rating = self._safe_float(item.get("audience_rating")) or self._safe_float(item.get("rating")) or 0.0
            return (-float(score), -rating, str(item.get("title") or "").lower())

        scored.sort(key=rank)
        keyword_matches = [
            {**item, "match_score": score, "match_reasons": reasons}
            for score, item, reasons in scored
            if score > 0
        ][:safe_limit]
        selected_keys = {str(item.get("rating_key")) for item in keyword_matches}
        broader_candidates = [
            {**item, "match_score": score, "match_reasons": reasons}
            for score, item, reasons in scored
            if str(item.get("rating_key")) not in selected_keys
        ][: max(0, safe_limit - len(keyword_matches))]
        candidates = keyword_matches + broader_candidates
        return {
            "ok": True,
            "library_verified": True,
            "constraints": {
                "media_type": media_type,
                "genres": requested_genres,
                "keywords": requested_keywords,
                "year_min": year_min,
                "year_max": year_max,
            },
            "catalog_match_count": len(hard_filtered),
            "keyword_match_count": len(keyword_matches),
            "candidates": candidates,
            "keyword_matches": keyword_matches,
            "broader_candidates": broader_candidates,
        }

    async def _get_recommendation_catalog(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        cache_key = (self.base_url, self.token or "")
        cached_entry = self._recommendation_catalog_cache.get(cache_key)
        if cached_entry is not None:
            cached_at, cached = cached_entry
            if now - cached_at < self._recommendation_catalog_ttl_seconds:
                return cached

        catalog: list[dict[str, Any]] = []
        for section in await self.list_sections():
            for item in await self._scan_section_catalog(section["key"]):
                media_type = str(item.get("type") or section.get("type") or "").lower()
                if media_type not in {"movie", "show"}:
                    continue
                summary = str(item.get("summary") or "").strip()
                catalog.append(
                    {
                        "title": item.get("title"),
                        "year": self._safe_int(item.get("year")),
                        "media_type": media_type,
                        "summary": summary[:600],
                        "tagline": str(item.get("tagline") or "").strip()[:300],
                        "genres": self._tag_values(item.get("Genre")),
                        "content_rating": item.get("contentRating"),
                        "rating": self._safe_float(item.get("rating")),
                        "audience_rating": self._safe_float(item.get("audienceRating")),
                        "library": section.get("title"),
                        "rating_key": str(item.get("ratingKey") or item.get("key") or ""),
                        "library_verified": True,
                    }
                )
        self._recommendation_catalog_cache[cache_key] = (now, catalog)
        return catalog

    def _tag_values(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            str(item.get("tag") or "").strip()
            for item in value
            if isinstance(item, dict) and str(item.get("tag") or "").strip()
        ]

    def _safe_int(self, value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _safe_float(self, value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

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
