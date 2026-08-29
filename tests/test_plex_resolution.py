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


def _client_with_library(titles: list[dict[str, Any]]) -> tuple[PlexClient, list[str]]:
    """Client stubbed at the HTTP layer, so search() itself really runs.

    Plex's `title=` filter matches the stored string, so this fake matches
    case-insensitive substrings the way the server does -- which is what makes
    "Law and Order" find nothing against a library holding "Law & Order".
    """
    client = PlexClient(base_url="http://plex.invalid:32400", token="token")
    asked: list[str] = []

    async def fake_list_sections() -> list[dict[str, Any]]:
        return [{"key": "2", "title": "TV Shows", "type": "show"}]

    async def fake_search_section(section_key: str, title: str) -> list[dict[str, Any]]:
        asked.append(title)
        return [item for item in titles if title.lower() in str(item.get("title", "")).lower()]

    client.list_sections = fake_list_sections  # type: ignore[method-assign]
    client._search_section = fake_search_section  # type: ignore[method-assign]
    return client, asked


@pytest.mark.asyncio
async def test_search_asks_plex_for_the_ampersand_spelling_too():
    """The production bug. Normalization ranks rows Plex already returned, so it
    could never fix this: `title=Law and Order` matched nothing server-side and
    there was nothing to rank. Verified against the real server before/after.
    """
    client, asked = _client_with_library([_show("Law & Order", 1990, tvdb_id=72368)])

    result = await client.resolve_show("Law and Order")

    assert result["ok"] is True
    assert result["show"] == "Law & Order"
    assert result["tvdb_id"] == 72368
    assert "Law & Order" in asked, "the ampersand spelling has to actually be asked for"


@pytest.mark.asyncio
async def test_search_finds_a_spinoff_by_its_leading_words():
    """"Law and Order SVU" is not a substring of the stored title in any spelling,
    so the exact variants all miss and the leading-word fallback has to carry it.
    """
    client, _ = _client_with_library(
        [
            _show("Law & Order", 1990, tvdb_id=72368),
            _show("Law & Order: Special Victims Unit", 1999, tvdb_id=75692),
        ]
    )

    result = await client.resolve_show("Law and Order SVU")

    assert result["ok"] is True
    assert result["show"] == "Law & Order: Special Victims Unit"


@pytest.mark.asyncio
async def test_loose_fallback_does_not_invent_a_match_for_an_absent_show():
    """The regression this guard exists for: searching leading words alone made
    "Some Show That Does Not Exist" match "Make Some Noise". A false positive is
    worse than the original bug -- it breaks "not in Plex means request it", so
    genuinely missing media would get repaired instead of requested.
    """
    client, _ = _client_with_library([_show("Make Some Noise", 2011), _show("Law & Order", 1990)])

    result = await client.resolve_show("Some Show That Does Not Exist At All")

    assert result["ok"] is False
    assert result["reason"] == "show_not_in_plex"


@pytest.mark.asyncio
async def test_the_typed_spelling_is_tried_first_and_alone_when_it_works():
    """A title that already matches must not fan out into extra library calls."""
    client, asked = _client_with_library([_show("Heat", 1995)])

    await client.resolve_show("Heat")

    assert asked == ["Heat"]


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
