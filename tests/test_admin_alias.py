"""Per-user admin alias: cosmetic output only, no routing/input-recognition changes."""

import pytest

from backend.config import Settings
from backend.main import build_agent
from backend.models import UserContext
from backend.state import ConversationStore
from tools.catalog import Toolkit, build_toolkit


def _user(user_id: str, username: str, is_admin: bool = False) -> UserContext:
    return UserContext(user_id=user_id, username=username, display_name=username.title(), is_admin=is_admin)


def _toolkit_for(store: ConversationStore, settings: Settings, user: UserContext) -> Toolkit:
    return build_toolkit(settings, store, user)


@pytest.mark.asyncio
async def test_set_admin_nickname_writes_the_admin_alias_flag(tmp_path):
    db_url = f"sqlite:///{tmp_path}/alias.db"
    settings = Settings(database_url=db_url)
    store = ConversationStore(db_url)
    erin = _user("erin-id", "chrislschwimmer")
    toolkit = _toolkit_for(store, settings, erin)

    result = await toolkit.set_admin_nickname("Goblin Overlord")

    assert result["ok"] is True
    assert result["friendly_name"] == "Goblin Overlord"
    assert store.get_user_flag("erin-id", "admin_alias") == "Goblin Overlord"


@pytest.mark.asyncio
async def test_set_admin_nickname_reset_clears_the_flag(tmp_path):
    db_url = f"sqlite:///{tmp_path}/alias2.db"
    settings = Settings(database_url=db_url)
    store = ConversationStore(db_url)
    erin = _user("erin-id", "chrislschwimmer")
    toolkit = _toolkit_for(store, settings, erin)

    await toolkit.set_admin_nickname("Goblin Overlord")
    result = await toolkit.set_admin_nickname("reset")

    assert result["ok"] is True
    assert store.get_user_flag("erin-id", "admin_alias") is None


def test_build_agent_admin_label_is_per_user_and_admin_is_unaffected(tmp_path):
    # Note: the alias here deliberately avoids "Goblin Overlord" -- that phrase
    # appears verbatim as an illustrative example in the agent.py prompt text
    # itself, independent of any per-user resolution, which would make it a
    # false positive in every prompt.
    db_url = f"sqlite:///{tmp_path}/alias3.db"
    settings = Settings(database_url=db_url, admin_display_name="Ben")
    store = ConversationStore(db_url)
    store.set_user_flag("erin-id", "admin_alias", "Grand Poobah")

    erin = _user("erin-id", "chrislschwimmer")
    other = _user("other-id", "steve")
    admin = _user("admin-id", "ben", is_admin=True)

    erin_agent, _, _ = build_agent(settings, erin)
    other_agent, _, _ = build_agent(settings, other)
    admin_agent, _, _ = build_agent(settings, admin)

    assert erin_agent.admin_label == "Grand Poobah"
    assert other_agent.admin_label == "Ben"
    assert admin_agent.admin_label == "Ben"

    erin_prompt = erin_agent._build_instructions(erin)
    other_prompt = other_agent._build_instructions(other)
    admin_prompt = admin_agent._build_instructions(admin)

    assert "Grand Poobah" in erin_prompt
    assert "Grand Poobah" not in other_prompt
    assert "Grand Poobah" not in admin_prompt
    assert "Ben" in other_prompt
    assert "Ben" in admin_prompt
