"""PlexClient identity resolution -- the real thing, not a fake.

This is the bug Ben reported: asking to fix "Law and Order" matched a 1978 BBC
show. Resolution now goes through Plex, so these tests exercise the actual
normalization, ranking, and guid extraction rather than a test double that
reimplements them.
"""

from __future__ import annotations

from typing import Any

import pytest

from clients.plex_client import PlexClient


def _show(title: str, year: int, tvdb_id: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "year": year,
        "type": "show",
        "ratingKey": str(year),
        "library": "TV Shows",
        # Modern Plex: opaque guid, external ids in a Guid list.
        "guid": f"plex://show/{year}",
    }
    if tvdb_id is not None:
        item["Guid"] = [{"id": f"tvdb://{tvdb_id}"}, {"id": f"imdb://tt{tvdb_id}"}]
    return item


def _client_returning(matches: list[dict[str, Any]]) -> PlexClient:
    client = PlexClient(base_url="http://plex.invalid:32400", token="token")

    async def fake_search(query: str, section_types: set[str] | None = None) -> list[dict[str, Any]]:
        return matches

    client.search = fake_search  # type: ignore[method-assign]
    return client


def test_ampersand_and_and_normalize_to_the_same_string():
    """Libraries store "Law & Order"; people type "Law and Order". Stripping
    punctuation alone yields laworder vs lawandorder, which never match.
    """
    client = PlexClient(base_url="http://plex.invalid:32400", token="token")

    assert client._normalize("Law & Order") == client._normalize("Law and Order")
    assert client._normalize("Law & Order: SVU") == client._normalize("Law and Order: SVU")
    # The fold must not collapse genuinely different titles.
    assert client._normalize("Law & Order") != client._normalize("Law & Order: SVU")


@pytest.mark.asyncio
async def test_law_and_order_resolves_to_the_base_show_not_a_spinoff():
    """The reported failure: "Law and Order" has to land on the show by that name,
    not a spinoff and not an obscure same-ish title.
    """
    client = _client_returning(
        [
            _show("Law & Order: Special Victims Unit", 1999, tvdb_id=75692),
            _show("Law & Order", 1990, tvdb_id=71489),
            _show("Law & Order: Criminal Intent", 2001, tvdb_id=76706),
        ]
    )

    result = await client.resolve_show("Law and Order")

    assert result["ok"] is True
    assert result["show"] == "Law & Order"
    assert result["reason"] == "exact_title_match"
    assert result["tvdb_id"] == 71489


@pytest.mark.asyncio
async def test_resolution_reports_candidates_instead_of_silently_guessing():
    """With no exact title match the caller gets the ranked alternatives, so a
    wrong pick is visible rather than silent.
    """
    client = _client_returning([_show("Law & Order: Special Victims Unit", 1999, tvdb_id=75692)])

    result = await client.resolve_show("Law and Order")

    assert result["ok"] is True
    assert result["reason"] == "best_ranked_match"
    assert [candidate["title"] for candidate in result["candidates"]] == [
        "Law & Order: Special Victims Unit"
    ]


@pytest.mark.asyncio
async def test_show_absent_from_plex_is_reported_as_absent():
    client = _client_returning([])

    result = await client.resolve_show("Law and Order")

    assert result["ok"] is False
    assert result["reason"] == "show_not_in_plex"
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_tvdb_id_is_read_from_the_modern_guid_list():
    """Plex keeps external ids in a Guid list, not the top-level guid. The repair
    path hands this id to SickChill, so returning None here is a dead end.
    """
    client = _client_returning([_show("Law & Order", 1990, tvdb_id=71489)])

    result = await client.resolve_show("Law and Order")

    assert result["tvdb_id"] == 71489


@pytest.mark.asyncio
async def test_tvdb_id_is_also_read_from_a_legacy_flat_guid():
    client = PlexClient(base_url="http://plex.invalid:32400", token="token")
    item = {"title": "Law & Order", "guid": "com.plexapp.agents.thetvdb://71489/1/1?lang=en"}

    assert client._extract_tvdb_id(item) == 71489


@pytest.mark.asyncio
async def test_movie_resolution_reads_the_tmdb_id():
    client = PlexClient(base_url="http://plex.invalid:32400", token="token")

    async def fake_search(query: str, section_types: set[str] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "title": "Heat",
                "year": 1995,
                "type": "movie",
                "ratingKey": "9",
                "library": "Movies",
                "guid": "plex://movie/heat",
                "Guid": [{"id": "tmdb://949"}, {"id": "imdb://tt0113277"}],
            }
        ]

    client.search = fake_search  # type: ignore[method-assign]

    result = await client.resolve_movie("Heat")

    assert result["ok"] is True
    assert result["movie"] == "Heat"
    assert result["tmdb_id"] == 949
    assert result["year"] == 1995
