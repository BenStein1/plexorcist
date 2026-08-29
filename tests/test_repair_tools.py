"""RepairTools.repair_requested_show / add_requested_show_to_sickchill against fake
Ombi/SickChill clients. Covers the note [652] regression: a show present in
SickChill/Plex must be repairable even with no Ombi request record on file.
"""

from __future__ import annotations

from typing import Any

import pytest

from tools.repair_tools import RepairTools


class FakeOmbi:
    def __init__(self, best_match: dict[str, Any]) -> None:
        self._best_match = best_match

    async def check_existing_media_status(self, query: str) -> dict[str, Any]:
        return {"best_match": self._best_match}


class FakeSickChill:
    def __init__(self) -> None:
        self.repair_calls: list[dict[str, Any]] = []

    async def repair_episode_targets(
        self,
        *,
        show: str,
        season: int,
        episodes: list[int],
        expected_indexer_id: int | None,
    ) -> dict[str, Any]:
        self.repair_calls.append(
            {"show": show, "season": season, "episodes": episodes, "expected_indexer_id": expected_indexer_id}
        )
        return {
            "ok": True,
            "changed_count": 1,
            "search_results": [
                {"episode": episode, "ok": True, "action": "manual_search_started"} for episode in episodes
            ],
        }

    async def episode_numbers_for_season(self, *, show: str, season: int, expected_indexer_id: int | None) -> dict[str, Any]:
        return {"ok": True, "show": show, "episodes": [1, 2, 3]}


@pytest.mark.asyncio
async def test_repair_requested_show_proceeds_for_library_present_show_with_no_ombi_request():
    """note [652]: Law and Order is in SickChill/Plex (available=True) but has no
    Ombi request record. Previously this hit a bare refusal (show_not_requested_in_ombi)
    even though there is nothing stopping a direct SickChill repair -- the same soft-gate
    mechanism already used when Ombi itself errors out. Repairing a specific episode must
    now go through rather than refuse.
    """
    ombi = FakeOmbi(
        {
            "type": "show",
            "title": "Law and Order",
            "tvdb_id": 79590,
            "requested": False,
            "available": True,
        }
    )
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill)

    result = await tools.repair_requested_show(query="Law and Order", scope="episode", season=10, episode=1)

    assert result["ok"] is True
    assert result["action"] == "repair_requested_show"
    assert result["requested"] is None
    assert result["queued_count"] == 1
    assert result["failure_count"] == 0
    assert sickchill.repair_calls == [
        {"show": "Law and Order", "season": 10, "episodes": [1], "expected_indexer_id": 79590}
    ]


@pytest.mark.asyncio
async def test_repair_requested_show_whole_show_scope_with_no_request_is_a_structured_refusal():
    """The soft gate has nothing concrete to enumerate for a whole-show scope (no season
    or episode target), so this one case still can't proceed -- but it must fail with a
    structured, actionable result rather than a bare refusal, and it must not name a
    backend service to the user.
    """
    ombi = FakeOmbi(
        {
            "type": "show",
            "title": "Law and Order",
            "tvdb_id": 79590,
            "requested": False,
            "available": True,
        }
    )
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill)

    result = await tools.repair_requested_show(query="Law and Order")

    assert result["ok"] is False
    assert result["reason"] == "show_not_requested_in_ombi"
    assert result["candidates"] == [ombi._best_match]
    assert sickchill.repair_calls == []
    assert "ombi" not in result["user_summary"].lower()
    assert "sickchill" not in result["user_summary"].lower()


@pytest.mark.asyncio
async def test_add_requested_show_to_sickchill_redirects_to_repair_when_already_available():
    ombi = FakeOmbi(
        {
            "type": "show",
            "title": "Law and Order",
            "tvdb_id": 79590,
            "requested": False,
            "available": True,
        }
    )
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill)

    result = await tools.add_requested_show_to_sickchill(query="Law and Order")

    assert result["ok"] is False
    assert result["reason"] == "show_not_requested_in_ombi"
    assert result["available"] is True
    assert "repair it directly" in result["user_summary"]
    assert "ombi" not in result["user_summary"].lower()
    assert "sickchill" not in result["user_summary"].lower()


class TrackingOmbi(FakeOmbi):
    """Ombi that records whether it was consulted at all."""

    def __init__(self, best_match: dict[str, Any] | None = None) -> None:
        super().__init__(best_match or {})
        self.searches: list[str] = []

    async def check_existing_media_status(self, query: str) -> dict[str, Any]:
        self.searches.append(query)
        return {"best_match": self._best_match}


class FakePlex:
    """Stands in for the Plex library -- the server's own record of what exists."""

    def __init__(self, shows: list[dict[str, Any]]) -> None:
        self._shows = shows
        self.queries: list[str] = []

    async def resolve_show(self, title: str) -> dict[str, Any]:
        self.queries.append(title)
        normalized = self._normalize(title)
        matches = [show for show in self._shows if self._normalize(show["show"]) == normalized]
        if not matches:
            return {"ok": False, "reason": "show_not_in_plex", "candidates": []}
        return {"ok": True, "reason": "exact_title_match", "candidates": matches, **matches[0]}

    @staticmethod
    def _normalize(value: str) -> str:
        text = str(value or "").lower().replace("&", " and ")
        return "".join(ch for ch in text if ch.isalnum())


@pytest.mark.asyncio
async def test_show_on_the_server_repairs_via_plex_identity_without_asking_ombi():
    """Ben's rule: if a user reports a problem with media that isn't 'it doesn't exist',
    it is already in Plex. Plex therefore owns identity, and the repair goes straight to
    SickChill -- Ombi is a request path and must not be consulted or allowed to gate it.
    Also covers the '&' vs 'and' spelling that made the old lookup miss entirely.
    """
    ombi = TrackingOmbi()
    plex = FakePlex([{"show": "Law & Order", "tvdb_id": 79590, "year": 1990}])
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill, plex=plex)

    result = await tools.repair_requested_show(query="Law and Order", scope="episode", season=10, episode=1)

    assert result["ok"] is True
    assert ombi.searches == [], "Ombi must not be consulted for a show already on the server"
    # The SickChill repair runs against the library's own title, not the typed one.
    assert sickchill.repair_calls == [
        {"show": "Law & Order", "season": 10, "episodes": [1], "expected_indexer_id": 79590}
    ]
    assert result["plex_match"]["show"] == "Law & Order"
    assert "ombi_soft_error" not in result


@pytest.mark.asyncio
async def test_explicit_tvdb_id_still_wins_over_the_plex_guid():
    ombi = TrackingOmbi()
    plex = FakePlex([{"show": "Law & Order", "tvdb_id": 79590}])
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill, plex=plex)

    await tools.repair_requested_show(query="Law and Order", scope="season", season=10, tvdb_id=12345)

    assert sickchill.repair_calls[0]["expected_indexer_id"] == 12345


@pytest.mark.asyncio
async def test_whole_show_scope_on_a_plex_show_asks_for_a_narrower_target():
    """A whole-show refetch is a big operation, so it still asks for a season or
    episode -- but it now does so knowing the show exists, instead of refusing on a
    missing request record.
    """
    ombi = TrackingOmbi()
    plex = FakePlex([{"show": "Law & Order", "tvdb_id": 79590}])
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill, plex=plex)

    result = await tools.repair_requested_show(query="Law and Order")

    assert result["ok"] is False
    assert result["action"] == "scope_too_broad"
    assert result["in_plex"] is True
    assert result["show"] == "Law & Order"
    assert ombi.searches == []
    assert sickchill.repair_calls == []
    assert "ombi" not in result["user_summary"].lower()
    assert "sickchill" not in result["user_summary"].lower()


@pytest.mark.asyncio
async def test_show_absent_from_plex_falls_through_to_the_request_lookup():
    """Not on the server is the one case Ombi genuinely answers: it was never
    requested, so requesting it -- not repairing it -- is the fix.
    """
    ombi = TrackingOmbi({"type": "movie", "title": "Some Film"})
    plex = FakePlex([])
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill, plex=plex)

    result = await tools.repair_requested_show(query="Nonexistent Show")

    assert result["ok"] is False
    assert result["reason"] == "show_not_found"
    assert result["in_plex"] is False
    assert ombi.searches == ["Nonexistent Show"], "the request lookup is correct once Plex says no"
    assert "requested" in result["user_summary"]


@pytest.mark.asyncio
async def test_plex_outage_does_not_block_a_repair():
    """A Plex failure must degrade to the old request-lookup path, not harden into
    a refusal that leaves the user unable to fix anything.
    """
    import httpx

    class BrokenPlex:
        async def resolve_show(self, title: str) -> dict[str, Any]:
            raise httpx.ConnectError("plex down")

    ombi = TrackingOmbi(
        {"type": "show", "title": "Law and Order", "tvdb_id": 79590, "requested": False, "available": True}
    )
    sickchill = FakeSickChill()
    tools = RepairTools(ombi=ombi, sickchill=sickchill, plex=BrokenPlex())

    result = await tools.repair_requested_show(query="Law and Order", scope="episode", season=10, episode=1)

    assert result["ok"] is True
    assert ombi.searches == ["Law and Order"]
