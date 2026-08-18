"""ConciergeAgent.respond() driven by a scriptable fake LlmClient over a real
ToolBridge (fake handlers). Covers the Phase 3 rewrite guarantees: every tool
call gets an answer the model can see and react to (never swallowed), the
model's final text is authoritative when present, and max-turns exhaustion
falls back cleanly instead of hanging or crashing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.agent import ConciergeAgent
from backend.config import Settings
from backend.models import ConversationState, UserContext
from backend.state import ConversationStore
from clients.llm_providers import LlmResponse, ToolCall, ToolResultsTurn
from tools import schemas
from tools.bridge import ToolBridge, build_bridge
from tools.catalog import ADMIN, CATALOG, ToolSpec


class FakeLlmClient:
    """Returns scripted LlmResponses in order; records each call's inputs."""

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def generate_response(
        self,
        *,
        instructions,
        conversation,
        tools,
        usage_context=None,
        tool_choice=None,
    ) -> LlmResponse:
        # Snapshot now: the agent mutates one shared conversation list turn by
        # turn, so a live reference would make every recorded call look like
        # the final state.
        self.calls.append(
            {
                "instructions": instructions,
                "conversation": list(conversation),
                "tools": list(tools),
                "usage_context": usage_context,
                "tool_choice": tool_choice,
            }
        )
        if not self._responses:
            raise AssertionError("FakeLlmClient exhausted its scripted responses")
        return self._responses.pop(0)


class FakeToolkit:
    """Stands in for tools.catalog.Toolkit: just needs the bound methods the
    fake ToolSpecs below resolve to."""

    async def echo(self, query: str | None = None) -> dict:
        return {"ok": True, "query": query}

    async def boom(self) -> dict:
        raise RuntimeError("handler exploded")

    async def notice(self, summary: str, priority: int | None = None) -> dict:
        return {
            "ok": True,
            "summary": summary,
            "delivery_confirmation": {
                "direction": "user_to_admin",
                "status": "sent",
                "recipient_label": "the admin",
                "message": summary,
            },
        }

    async def exit_task_mode(self) -> dict:
        return {"ok": True, "action": "admin_task_mode_exited"}


FAKE_SPECS = [
    ToolSpec(
        name="search_media",
        description="fake search",
        input_model=schemas.SearchMediaInput,
        resolve=lambda tk: tk.echo,
    ),
    ToolSpec(
        name="raising_tool",
        description="always raises",
        input_model=schemas.EmptyInput,
        resolve=lambda tk: tk.boom,
    ),
    ToolSpec(
        name="send_admin_prowl_notice",
        description="fake admin notice",
        input_model=schemas.ProwlNoticeInput,
        resolve=lambda tk: tk.notice,
    ),
    ToolSpec(
        name="exit_admin_task_mode",
        description="fake task-mode exit",
        input_model=schemas.EmptyInput,
        resolve=lambda tk: tk.exit_task_mode,
    ),
]


def _bridge() -> ToolBridge:
    return ToolBridge(FakeToolkit(), FAKE_SPECS, after_call=None)


def _user(is_admin: bool = False) -> UserContext:
    return UserContext(user_id="u1", username="dana", display_name="Dana", is_admin=is_admin)


def _state() -> ConversationState:
    return ConversationState(user_id="u1")


def _agent(client: FakeLlmClient, bridge: ToolBridge, max_turns: int = 6) -> ConciergeAgent:
    return ConciergeAgent(
        bridge,
        ombi_continue_url="http://ombi.example",
        llm_client=client,
        admin_label="the admin",
        prowl=None,
        movie_direct_source_enabled=False,
        max_turns=max_turns,
    )


def _tool_results_turn(conversation: list) -> ToolResultsTurn:
    matches = [item for item in conversation if isinstance(item, ToolResultsTurn)]
    assert matches, "expected a ToolResultsTurn in the conversation"
    return matches[-1]


# --- (a) handler exception -> is_error result, loop continues ---------------


@pytest.mark.asyncio
async def test_handler_exception_becomes_error_result_and_loop_continues():
    client = FakeLlmClient(
        [
            LlmResponse(
                text="",
                tool_calls=[ToolCall(call_id="c1", name="raising_tool", arguments_json="{}")],
                native_turn=[],
            ),
            LlmResponse(text="All done despite the error.", tool_calls=[], native_turn=[]),
        ]
    )
    agent = _agent(client, _bridge())
    reply, tool_calls = await agent.respond(_user(), _state(), "please run the raising tool")

    assert reply == "All done despite the error."
    assert tool_calls == []  # failed calls never become successful ToolCallRecords
    assert len(client.calls) == 2

    results_turn = _tool_results_turn(client.calls[1]["conversation"])
    assert len(results_turn.results) == 1
    result = results_turn.results[0]
    assert result.call_id == "c1"
    assert result.is_error is True


# --- (b) malformed arguments_json -> parse error round-trip -----------------


@pytest.mark.asyncio
async def test_malformed_arguments_json_becomes_error_result():
    client = FakeLlmClient(
        [
            LlmResponse(
                text="",
                tool_calls=[ToolCall(call_id="c2", name="search_media", arguments_json="{not json")],
                native_turn=[],
            ),
            LlmResponse(text="Recovered after bad JSON.", tool_calls=[], native_turn=[]),
        ]
    )
    agent = _agent(client, _bridge())
    reply, tool_calls = await agent.respond(_user(), _state(), "search for something")

    assert reply == "Recovered after bad JSON."
    assert tool_calls == []
    assert len(client.calls) == 2

    result = _tool_results_turn(client.calls[1]["conversation"]).results[0]
    assert result.call_id == "c2"
    assert result.is_error is True
    assert "search_media" in str(result.content)
    assert "parse" in str(result.content).lower()


# --- (c) missing required argument -> validation error names the field ------


@pytest.mark.asyncio
async def test_missing_required_argument_names_the_field():
    client = FakeLlmClient(
        [
            LlmResponse(
                text="",
                tool_calls=[ToolCall(call_id="c3", name="search_media", arguments_json="{}")],
                native_turn=[],
            ),
            LlmResponse(text="Asked for the missing detail.", tool_calls=[], native_turn=[]),
        ]
    )
    agent = _agent(client, _bridge())
    reply, tool_calls = await agent.respond(_user(), _state(), "search for something")

    assert reply == "Asked for the missing detail."
    assert tool_calls == []

    result = _tool_results_turn(client.calls[1]["conversation"]).results[0]
    assert result.is_error is True
    assert "query" in str(result.content)


# --- (d) model's final text is authoritative, not overwritten ---------------


@pytest.mark.asyncio
async def test_final_text_not_overwritten_when_tool_calls_succeeded():
    # send_admin_prowl_notice is one of the tools _plain_support_reply_from_tool_calls
    # actively synthesizes a reply for ("Done — I let the admin know.") when the
    # model leaves the reply blank. Driving it here and asserting the model's own
    # text wins is what actually catches a regression to the old "synthesis can
    # overwrite a real model reply" bug — a tool outside that synthesis set (like
    # search_media) would pass this assertion even with the bug present.
    client = FakeLlmClient(
        [
            LlmResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        call_id="c4",
                        name="send_admin_prowl_notice",
                        arguments_json=json.dumps({"summary": "issue"}),
                    )
                ],
                native_turn=[],
            ),
            LlmResponse(text="Custom final reply from the model.", tool_calls=[], native_turn=[]),
        ]
    )
    agent = _agent(client, _bridge())
    reply, tool_calls = await agent.respond(_user(), _state(), "let the admin know about this")

    assert reply == (
        "Custom final reply from the model.\n\n"
        "To the admin: “issue”"
    )
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "send_admin_prowl_notice"


def test_admin_message_confirmation_echoes_exact_multiline_text():
    exact = "The new season is ready.\nThanks for the cat food. : )"
    call = SimpleNamespace(
        result={
            "ok": True,
            "delivery_confirmation": {
                "direction": "admin_to_user",
                "status": "queued",
                "recipient_label": "Alice",
                "message": exact,
            },
        }
    )

    agent = _agent(FakeLlmClient([]), _bridge())
    reply = agent._append_delivery_confirmations("Done.", [call])

    assert reply == f"Done.\n\nTo Alice:\n“{exact}”"


def test_failed_message_delivery_has_no_confirmation_echo():
    call = SimpleNamespace(
        result={
            "ok": False,
            "delivery_confirmation": {
                "status": "sent",
                "recipient_label": "the admin",
                "message": "This was not delivered",
            },
        }
    )

    agent = _agent(FakeLlmClient([]), _bridge())
    assert agent._append_delivery_confirmations("It failed.", [call]) == "It failed."


def test_task_close_confirmation_lists_exact_tasks_and_remaining_count():
    calls = [
        SimpleNamespace(
            name="resolve_admin_task",
            result={
                "ok": True,
                "verified_closed": True,
                "remaining_open_task_count": 51,
                "resolved_note_ids": [378],
                "closed_task_lines": [
                    "- [378] Erin (chrislschwimmer): User asked about goblin-related movies."
                ],
            },
        )
    ]

    reply = ConciergeAgent._append_task_closure_confirmations("Done.", calls)

    assert reply == (
        "Done.\n\nClosed:\n"
        "- [378] Erin (chrislschwimmer): User asked about goblin-related movies.\n"
        "51 open task(s) remain."
    )


@pytest.mark.asyncio
async def test_active_admin_task_mode_forces_a_structured_routing_tool_before_text():
    client = FakeLlmClient(
        [
            LlmResponse(
                text="",
                tool_calls=[ToolCall(call_id="exit-1", name="exit_admin_task_mode", arguments_json="{}")],
                native_turn=[],
            ),
            LlmResponse(text="Changed subjects normally.", tool_calls=[], native_turn=[]),
        ]
    )
    state = _state()
    state.support_context["admin_task_mode"] = True
    agent = _agent(client, _bridge())

    reply, tool_calls = await agent.respond(_user(is_admin=True), state, "Actually, tell me about a movie")

    assert reply == "Changed subjects normally."
    assert tool_calls[0].name == "exit_admin_task_mode"
    assert client.calls[0]["tool_choice"] == "required"
    assert {tool.name for tool in client.calls[0]["tools"]} == {"exit_admin_task_mode"}
    assert client.calls[1]["tool_choice"] is None
    assert state.support_context["admin_task_mode"] is False


# --- (e) max-turns exhaustion -> fallback reply, no crash/hang --------------


@pytest.mark.asyncio
async def test_max_turns_exhaustion_returns_fallback_without_hanging():
    max_turns = 2
    client = FakeLlmClient(
        [
            LlmResponse(
                text="",
                tool_calls=[
                    ToolCall(call_id=f"loop-{i}", name="search_media", arguments_json=json.dumps({"query": "heat"}))
                ],
                native_turn=[],
            )
            for i in range(max_turns)
        ]
    )
    agent = _agent(client, _bridge(), max_turns=max_turns)
    reply, tool_calls = await agent.respond(_user(), _state(), "keep searching forever")

    assert len(client.calls) == max_turns
    assert isinstance(reply, str) and reply.strip()
    assert len(tool_calls) == max_turns


# --- (f) non-admin bridge has no admin-tagged tools --------------------------


def test_non_admin_bridge_has_no_admin_tools(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'agent_loop.db'}")
    store = ConversationStore(settings.database_url)
    bridge = build_bridge(settings, store, _user(is_admin=False), after_call=None)

    schema_names = {schema.name for schema in bridge.tool_schemas()}
    admin_names = {spec.name for spec in CATALOG if ADMIN in spec.tags}

    assert admin_names, "expected at least one admin-tagged tool in the catalog"
    assert not (schema_names & admin_names)


# --- (g) unknown/hallucinated tool name --------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_name_names_available_tools_and_continues():
    client = FakeLlmClient(
        [
            LlmResponse(
                text="",
                tool_calls=[ToolCall(call_id="c5", name="totally_made_up_tool", arguments_json="{}")],
                native_turn=[],
            ),
            LlmResponse(text="Recovered after unknown tool.", tool_calls=[], native_turn=[]),
        ]
    )
    agent = _agent(client, _bridge())
    reply, tool_calls = await agent.respond(_user(), _state(), "do the unknown thing")

    assert reply == "Recovered after unknown tool."
    assert tool_calls == []

    result = _tool_results_turn(client.calls[1]["conversation"]).results[0]
    assert result.is_error is True
    assert "search_media" in str(result.content)
    assert "raising_tool" in str(result.content)
