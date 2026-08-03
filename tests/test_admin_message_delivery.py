"""Admin -> user message delivery is deterministic, not a soft LLM request.

Regression coverage for three prod bugs found against real data:

1. /api/welcome never looked at pending admin messages at all -- a user could
   log in repeatedly and never see a message sent to them (they'd have to type
   something first, and even then it depended on bug 2 below).
2. /api/chat marked messages read unconditionally after the turn, regardless
   of whether the model's reply actually mentioned them. The "delivery" was
   only a soft prompt request the model could skip (e.g. in favor of a
   tool-call flow) or paraphrase.
3. The first fix pass appended the block to ChatResponse.reply only. The web
   UI's chat view never reads .reply -- it renders data.state.messages -- so
   that fix was a no-op in the actual product even though .reply-only tests
   were green. The block must land in state.messages (persisted), not just
   the reply string.

The fix delivers pending messages by appending them as their own assistant
message in state.messages, persists that with store.save(), and only marks
them read once that save has happened.
"""

import json
import time

import pytest

import backend.main as main
from backend.config import Settings
from backend.logging import AuditLogger
from backend.models import ChatRequest, ChatResponse, UserContext
from backend.shabbos.flags import set_shabbos_mode
from backend.state import ConversationStore

USER = UserContext(user_id="u-rmk", username="rmk1900", display_name="RMK", is_admin=False, auth_source="test")


def _persisted_assistant_texts(store: ConversationStore, user_id: str) -> list[str]:
    """Every assistant-role message content across every conversation row this
    user has, read straight from the DB -- independent of any in-memory
    ChatResponse -- so it can prove the block is durably saved, not just
    present in the string a caller happened to return."""
    texts: list[str] = []
    with store._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT messages_json FROM conversations WHERE user_id = ?", (user_id,)
        ).fetchall()
    for (messages_json,) in rows:
        for msg in json.loads(messages_json):
            if msg.get("role") == "assistant":
                texts.append(str(msg.get("content") or ""))
    return texts


class _FakeAgent:
    """Stands in for ConciergeAgent: returns a canned reply that never mentions
    any pending admin message, exactly like the burned turns in prod (the model
    answered the user's actual question and said nothing about Ben's note)."""

    def __init__(self, reply: str = "It's already in Plex.") -> None:
        self.reply = reply

    async def respond(self, user, state, message, extra_instructions=None):
        return self.reply, []


def _settings(tmp_path, name: str) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path}/{name}.db")


def _seed_admin_message(store: ConversationStore, user_id: str, content: str, from_admin_name: str = "Ben") -> None:
    store.add_user_memory_note(
        user_id=user_id,
        note_type="admin_message",
        content=content,
        status="unread",
        tier=1,
        metadata={"from_admin_name": from_admin_name, "created_by_tool": "send_admin_message"},
    )


@pytest.mark.asyncio
async def test_chat_delivers_pending_admin_message_verbatim_and_only_then_marks_it_read(tmp_path, monkeypatch):
    settings = _settings(tmp_path, "chat_deliver")
    store = ConversationStore(settings.database_url)
    message_text = "tell rmk1900 the new season is up, and stop asking me twice a day about it lol"
    _seed_admin_message(store, USER.user_id, message_text)

    monkeypatch.setattr(main, "build_agent", lambda s, u: (_FakeAgent(), store, AuditLogger()))

    response = await main.chat(ChatRequest(message="anything new?"), user=USER, settings=settings)

    assert isinstance(response, ChatResponse)
    assert message_text in response.reply
    assert "Message from Ben" in response.reply
    # The model's own (unrelated) reply is still present -- this is an append,
    # not a replacement.
    assert "It's already in Plex." in response.reply

    # The web UI's chat view renders data.state.messages, not data.reply --
    # so the block being in .reply alone is not delivery. It must be a
    # persisted assistant message too.
    assert any(message_text in msg.content for msg in response.state.messages if msg.role == "assistant")
    persisted = _persisted_assistant_texts(store, USER.user_id)
    assert any(message_text in text for text in persisted)

    remaining_unread = store.get_unread_admin_messages(USER.user_id)
    assert remaining_unread == []


@pytest.mark.asyncio
async def test_welcome_delivers_pending_admin_message_verbatim_and_marks_it_read(tmp_path):
    settings = _settings(tmp_path, "welcome_deliver")
    store = ConversationStore(settings.database_url)
    message_text = "your request for that documentary finally landed, enjoy"
    _seed_admin_message(store, USER.user_id, message_text)

    result = await main.welcome(user=USER, settings=settings)

    assert message_text in result["message"]
    assert "Message from Ben" in result["message"]

    remaining_unread = store.get_unread_admin_messages(USER.user_id)
    assert remaining_unread == []

    # /api/welcome's response is a one-shot greeting string, not the transcript
    # the UI renders on subsequent turns -- so delivery isn't real until the
    # block is durably saved. Without persistence, a page refresh or a second
    # browser tab (both call /api/welcome again) would burn the note the
    # instant it's marked read, with nothing left anywhere for the user to see.
    persisted = _persisted_assistant_texts(store, USER.user_id)
    assert any(message_text in text for text in persisted)

    # A second /api/welcome call (refresh, second tab) must not re-deliver --
    # the note is already read -- but the text must still be sitting in the
    # persisted transcript from the first call.
    second_result = await main.welcome(user=USER, settings=settings)
    assert "Message from" not in second_result["message"]
    assert message_text not in second_result["message"]
    persisted_after_second = _persisted_assistant_texts(store, USER.user_id)
    assert any(message_text in text for text in persisted_after_second)


@pytest.mark.asyncio
async def test_welcome_with_no_pending_messages_has_no_stray_block(tmp_path, monkeypatch):
    settings = _settings(tmp_path, "welcome_empty")
    store = ConversationStore(settings.database_url)
    mark_read_calls = []
    # welcome() builds its own ConversationStore internally, so the patch has
    # to be on the class, not on this test's instance.
    monkeypatch.setattr(
        ConversationStore,
        "mark_admin_messages_read",
        lambda self, uid, ids: mark_read_calls.append((uid, ids)),
    )

    result = await main.welcome(user=USER, settings=settings)

    assert "Message from" not in result["message"]
    assert "---" not in result["message"]
    assert store.get_unread_admin_messages(USER.user_id) == []
    # No pending messages means no delivery, which means no write at all --
    # not just an empty read-list, which would be true either way.
    assert mark_read_calls == []


@pytest.mark.asyncio
async def test_chat_with_no_pending_messages_has_no_stray_block(tmp_path, monkeypatch):
    settings = _settings(tmp_path, "chat_empty")
    store = ConversationStore(settings.database_url)
    mark_read_calls = []
    monkeypatch.setattr(
        store, "mark_admin_messages_read", lambda uid, ids: mark_read_calls.append((uid, ids))
    )
    monkeypatch.setattr(main, "build_agent", lambda s, u: (_FakeAgent(), store, AuditLogger()))

    response = await main.chat(ChatRequest(message="anything new?"), user=USER, settings=settings)

    assert "Message from" not in response.reply
    assert "---" not in response.reply
    assert mark_read_calls == []


def test_multiple_pending_messages_render_oldest_first_and_verbatim(tmp_path):
    settings = _settings(tmp_path, "multi")
    store = ConversationStore(settings.database_url)
    _seed_admin_message(store, USER.user_id, "first: check your Radarr queue")
    time.sleep(0.01)
    _seed_admin_message(store, USER.user_id, "second: also the show got renewed")

    block_text, note_ids = main.render_pending_admin_messages(store, USER.user_id)

    assert "first: check your Radarr queue" in block_text
    assert "second: also the show got renewed" in block_text
    assert block_text.index("first: check your Radarr queue") < block_text.index("second: also the show got renewed")
    assert len(note_ids) == 2

    store.mark_admin_messages_read(USER.user_id, note_ids)
    assert store.get_unread_admin_messages(USER.user_id) == []


@pytest.mark.asyncio
async def test_shabbos_chat_delivers_pending_admin_message_verbatim_and_marks_it_read(tmp_path):
    """The one wired path (_shabbos_chat) with no other coverage: a Shabbos-mode
    user is fully LLM-free, but pending admin messages must still be delivered
    deterministically -- that's a text append, not a prompt."""
    settings = _settings(tmp_path, "shabbos_deliver")
    store = ConversationStore(settings.database_url)
    set_shabbos_mode(store, USER.user_id, True)
    message_text = "shul starts at 9, don't be late"
    _seed_admin_message(store, USER.user_id, message_text)

    response = await main.chat(ChatRequest(message="hey what's up"), user=USER, settings=settings)

    assert isinstance(response, ChatResponse)
    assert message_text in response.reply
    assert "Message from Ben" in response.reply

    # Same trap as /api/chat: the UI's chat view renders data.state.messages,
    # not data.reply. Shabbos Mode has its own append path (_shabbos_chat),
    # so it needs its own proof the block landed in persisted history too.
    assert any(message_text in msg.content for msg in response.state.messages if msg.role == "assistant")
    persisted = _persisted_assistant_texts(store, USER.user_id)
    assert any(message_text in text for text in persisted)

    assert store.get_unread_admin_messages(USER.user_id) == []


def test_render_pending_admin_messages_returns_empty_for_no_unread(tmp_path):
    settings = _settings(tmp_path, "render_empty")
    store = ConversationStore(settings.database_url)

    block_text, note_ids = main.render_pending_admin_messages(store, USER.user_id)

    assert block_text == ""
    assert note_ids == []
