from __future__ import annotations

import pytest

from clients.plex_client import PlexClient


MOVIES = [
    {
        "title": "Jaws",
        "year": "1975",
        "type": "movie",
        "ratingKey": "1",
        "summary": "A giant shark terrorizes a New England beach town.",
        "tagline": "You'll never go in the water again.",
        "Genre": [{"tag": "Horror"}, {"tag": "Thriller"}],
        "audienceRating": "9.0",
    },
    {
        "title": "Deep Blue Sea",
        "year": "1999",
        "type": "movie",
        "ratingKey": "2",
        "summary": "Scientists are hunted by intelligent sharks.",
        "Genre": [{"tag": "Horror"}, {"tag": "Action"}],
        "audienceRating": "6.5",
    },
    {
        "title": "The Reef",
        "year": "2010",
        "type": "movie",
        "ratingKey": "3",
        "summary": "Survivors cross dangerous open water.",
        "Genre": [{"tag": "Horror"}],
        "audienceRating": "5.8",
    },
    {
        "title": "Crash",
        "year": "1996",
        "type": "movie",
        "ratingKey": "4",
        "summary": "People form an unusual obsession.",
        "Genre": [{"tag": "Drama"}],
    },
    {
        "title": "Crash",
        "year": "2004",
        "type": "movie",
        "ratingKey": "5",
        "summary": "Lives intersect in Los Angeles.",
        "Genre": [{"tag": "Drama"}],
    },
]

SHOWS = [
    {
        "title": "Shark",
        "year": "2006",
        "type": "show",
        "ratingKey": "6",
        "summary": "A defense attorney changes sides.",
        "Genre": [{"tag": "Drama"}],
    }
]


class FakePlex(PlexClient):
    def __init__(self, *, reset_cache: bool = True) -> None:
        if reset_cache:
            self._recommendation_catalog_cache.clear()
        super().__init__("http://plex.invalid", "token")
        self.scans = 0

    async def list_sections(self) -> list[dict]:
        return [
            {"key": "1", "title": "Movies", "type": "movie"},
            {"key": "2", "title": "Television", "type": "show"},
        ]

    async def _scan_section_catalog(self, section_key: str) -> list[dict]:
        self.scans += 1
        return MOVIES if section_key == "1" else SHOWS


@pytest.mark.asyncio
async def test_inventory_first_pool_uses_plex_metadata_and_keeps_broader_matches():
    plex = FakePlex()

    result = await plex.search_recommendation_pool(
        media_type="movie",
        genres=["Horror"],
        keywords=["shark"],
        limit=10,
    )

    assert result["library_verified"] is True
    assert [item["title"] for item in result["keyword_matches"]] == ["Jaws", "Deep Blue Sea"]
    assert [item["title"] for item in result["broader_candidates"]] == ["The Reef"]
    assert all(item["library_verified"] for item in result["candidates"])
    assert "shark: summary" in result["keyword_matches"][0]["match_reasons"]


@pytest.mark.asyncio
async def test_inventory_pool_applies_year_and_type_as_hard_filters():
    plex = FakePlex()

    result = await plex.search_recommendation_pool(
        media_type="movie",
        genres=["Horror"],
        year_min=1990,
        year_max=2005,
    )

    assert [item["title"] for item in result["candidates"]] == ["Deep Blue Sea"]


@pytest.mark.asyncio
async def test_existing_shortlist_filter_preserves_order_and_fails_ambiguous_closed():
    plex = FakePlex()

    result = await plex.verify_recommendation_candidates(
        [
            {"title": "Deep Blue Sea", "year": 1999, "media_type": "movie"},
            {"title": "Crash", "media_type": "movie"},
            {"title": "Open Water", "year": 2003, "media_type": "movie"},
            {"title": "Jaws", "year": 1975, "media_type": "movie"},
        ]
    )

    assert [item["status"] for item in result["results"]] == [
        "available",
        "ambiguous",
        "unavailable",
        "available",
    ]
    assert [item["candidate"]["title"] for item in result["available"]] == ["Deep Blue Sea", "Jaws"]


@pytest.mark.asyncio
async def test_recommendation_catalog_is_cached_for_five_minutes():
    plex = FakePlex()

    await plex.search_recommendation_pool(media_type="movie", genres=["Horror"])
    await plex.verify_recommendation_candidates([{"title": "Jaws", "media_type": "movie"}])

    assert plex.scans == 2  # one scan per library section, not one scan per tool call


@pytest.mark.asyncio
async def test_recommendation_catalog_cache_survives_per_request_client_instances():
    first = FakePlex()
    await first.search_recommendation_pool(media_type="movie", genres=["Horror"])
    second = FakePlex(reset_cache=False)

    result = await second.search_recommendation_pool(media_type="movie", genres=["Horror"])

    assert result["catalog_match_count"] == 3
    assert first.scans == 2
    assert second.scans == 0
