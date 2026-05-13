from __future__ import annotations

from xml.etree import ElementTree

import httpx

from clients.base import BaseHttpClient


class JackettClient(BaseHttpClient):
    async def broad_search(self, query_variants: list[str]) -> dict:
        return await self._search(query_variants)

    async def broad_movie_search(self, query_variants: list[str]) -> dict:
        return await self._search(query_variants)

    async def _search(self, query_variants: list[str]) -> dict:
        results: list[dict] = []
        for query in query_variants:
            xml_text = await self._torznab_search(query)
            results.extend(self._parse_results(xml_text, query))

        deduped: list[dict] = []
        seen: set[str] = set()
        for result in sorted(results, key=lambda item: (item["seeders"], item["title"]), reverse=True):
            key = result.get("guid") or result.get("magnet") or result["title"]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(result)

        return {
            "variants": query_variants,
            "results": deduped,
            "confidence_threshold_met": bool(deduped),
        }

    async def _torznab_search(self, query: str) -> str:
        if not self.api_key:
            return ""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.get(
                "/api/v2.0/indexers/all/results/torznab/api",
                params={"t": "search", "q": query, "apikey": self.api_key},
            )
            response.raise_for_status()
            return response.text

    def _parse_results(self, xml_text: str, query: str) -> list[dict]:
        if not xml_text:
            return []
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            return []

        ns = {"torznab": "http://torznab.com/schemas/2015/feed"}
        results: list[dict] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or "").strip()
            enclosure = item.find("enclosure")
            magnet = enclosure.get("url", "").strip() if enclosure is not None else ""

            attrs = {
                attr.attrib.get("name", ""): attr.attrib.get("value", "")
                for attr in item.findall("torznab:attr", ns)
            }
            seeders = self._safe_int(attrs.get("seeders")) or 0
            size = self._safe_int(attrs.get("size")) or 0
            quality = self._detect_quality(title)
            confidence = self._score_confidence(query, title)

            results.append(
                {
                    "title": title,
                    "guid": guid,
                    "link": link,
                    "magnet": magnet or link,
                    "seeders": seeders,
                    "size": size,
                    "quality": quality,
                    "quality_is_metadata_only": True,
                    "confidence": confidence,
                    "query": query,
                }
            )
        return results

    def _score_confidence(self, query: str, title: str) -> float:
        normalized_query = self._normalize(query)
        normalized_title = self._normalize(title)
        if normalized_query and normalized_query == normalized_title:
            return 0.99
        if normalized_query and normalized_query in normalized_title:
            return 0.92
        query_terms = [term for term in query.lower().split() if term]
        if query_terms and all(term in title.lower() for term in query_terms):
            return 0.8
        return 0.5

    def _detect_quality(self, title: str) -> str:
        lowered = title.lower()
        for marker in ("2160p", "1080p", "720p", "480p"):
            if marker in lowered:
                return marker
        return "unknown"

    def _normalize(self, value: str) -> str:
        return "".join(char for char in value.lower() if char.isalnum())

    def _safe_int(self, value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except ValueError:
            return None
