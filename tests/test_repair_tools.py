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
