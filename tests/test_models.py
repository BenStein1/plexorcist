"""Model round-trip tests under pydantic v2."""

from backend.models import (
    ChatMessage,
    ConversationState,
    Intent,
    ToolCallRecord,
    UserContext,
)


def test_conversation_state_json_round_trip():
    state = ConversationState(user_id="u1", intent=Intent.REQUEST_MOVIE)
    state.messages.append(ChatMessage(role="user", content="hi"))
    state.last_tool_actions.append({"name": "search_media", "result": {"ok": True}})

    dumped = state.model_dump(mode="json")
    restored = ConversationState.model_validate(dumped)

    assert restored.conversation_id == state.conversation_id
    assert restored.intent is Intent.REQUEST_MOVIE
    assert restored.messages[0].content == "hi"
    assert restored.last_tool_actions == state.last_tool_actions


def test_tool_call_record_defaults():
    record = ToolCallRecord(name="search_media")
    assert record.arguments == {}
    assert record.result == {}
    assert record.model_dump(mode="json")["name"] == "search_media"


def test_user_context_defaults():
    user = UserContext(user_id="u1", username="ben", display_name="Ben")
    assert user.is_admin is False
    assert user.auth_source == "dev-header"
