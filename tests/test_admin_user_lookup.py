"""Admin user lookup can see everyone Ben knows, not just everyone who logged in.

Prod bug, found from a real transcript:

    Ben:  "Can you send an admin message to Mike Young for me?"
    Nova: "I couldn't find a user matching Mike Young."
    Ben:  "Take a look at the friendly names for Mike."
    Nova: "I poked the name ledger and came up empty."

Two separate defects produced that:

1. `_load_user_labels()` built the entire user universe from `plex_auth_sessions`
   alone -- the LOGIN table. In prod that's 15 people against 64 names in the
   friendly-names ledger, so ~49 of Ben's known people could not be found at
   all, and the honest answer ("he's on file, he's just never logged in") was
   reported as the misleading "I could not find a user matching Mike".
2. `_resolve_user_query()` matched exact-equal then plain substring, so the
   whole query string had to appear inside one value. "Mike Young" could never
   reach someone on file as "Mike" no matter how the universe was built.

Usernames here are invented. Never fixture a real person of Ben's.
"""

import json

import pytest

from backend.auth_context import FriendlyNameDirectory
from backend.auth_store import PlexAuthSessionStore
from backend.config import Settings
from backend.main import build_agent
from backend.models import PlexAuthSession, UserContext
from backend.state import ConversationStore
from tools.admin_tools import AdminTools

# In the ledger, never logged in -- the shape of the bug.
LEDGER_ONLY = {"qq_mikeston": "Mikeston", "zz_pairfolk": "Mikeston and Nicolina"}


def _store(tmp_path) -> ConversationStore:
    db_url = f"sqlite:///{tmp_path / 'lookup.db'}"
    # plex_auth_sessions is created by PlexAuthSessionStore, not ConversationStore.
    PlexAuthSessionStore(db_url)
    return ConversationStore(db_url)


def _tools(tmp_path, store: ConversationStore, names: dict[str, str] | None = None) -> AdminTools:
    names_path = tmp_path / "friendlynames.json"
    names_path.write_text(
        json.dumps({"EXCLUDED_USERS": ["N/A"], "USER_FRIENDLY_NAMES": dict(names or LEDGER_ONLY)}),
        encoding="utf-8",
    )
    return AdminTools(transmission=None, store=store, friendly_names=FriendlyNameDirectory(str(names_path)))


def _seed_login(tmp_path, *, user_id: str, username: str, display_name: str) -> None:
    PlexAuthSessionStore(f"sqlite:///{tmp_path / 'lookup.db'}").save(
        PlexAuthSession(
            session_id=f"s-{user_id}", user_id=user_id, username=username, display_name=display_name, is_admin=False
        )
    )


def _admin_note_count(store: ConversationStore) -> int:
    with store._connect() as conn:  # noqa: SLF001
        return int(
            conn.execute("SELECT COUNT(*) FROM user_memory_notes WHERE note_type = 'admin_message'").fetchone()[0]
        )


@pytest.mark.asyncio
async def test_multi_word_query_offers_the_closest_names_instead_of_no_such_user(tmp_path):
    """Ben's literal query. "Mike Young" shares no substring with "Mike", so the
    old matcher returned a flat "I could not find a user matching Mike Young"
    even once the ledger was in the universe. A partial-token hit is a guess, so
    it comes back as a pick-one suggestion, never an auto-selected recipient."""
    store = _store(tmp_path)
    tools = _tools(tmp_path, store)

    result = tools._resolve_user_query("Mikeston Youngberg")  # noqa: SLF001

    assert result["ok"] is False
    assert result["reason"] == "user_not_found"
    labels = {item["label"] for item in result["candidates"]}
    assert labels == {"Mikeston (qq_mikeston)", "Mikeston and Nicolina (zz_pairfolk)"}
    assert "Closest I have" in result["user_summary"]
    # Ledger-only people are flagged as such in what Ben reads, so "pick one"
    # isn't offering him two recipients that will both refuse him next turn.
    assert result["user_summary"].count("[no account yet]") == 2
    # Neither can receive anything, so don't ask him to pick one -- he'd have to
    # ask twice to find out the real state. Say it on the first answer.
    assert "Neither has ever logged into Plexorcist" in result["user_summary"]
    assert "Which one?" not in result["user_summary"]


@pytest.mark.asyncio
async def test_a_real_account_among_the_near_matches_still_gets_a_pick_one(tmp_path):
    """The dead-end wording is only honest when it's actually a dead end."""
    store = _store(tmp_path)
    _seed_login(tmp_path, user_id="u-1", username="youngberg", display_name="Youngberg Reed")
    tools = _tools(tmp_path, store)

    result = tools._resolve_user_query("Mikeston Youngberg")  # noqa: SLF001

    assert result["ok"] is False
    assert "Which one?" in result["user_summary"]
    assert "never logged into Plexorcist" not in result["user_summary"]


@pytest.mark.asyncio
async def test_ambiguous_ledger_only_match_says_so_instead_of_pick_one(tmp_path):
    """Same rule on the ambiguity path: two solid matches that can both only be
    refused is not a choice, it's an answer."""
    store = _store(tmp_path)
    tools = _tools(tmp_path, store, names={"qq_a": "Mikeston Reed", "qq_b": "Mikeston Vance"})

    result = tools._resolve_user_query("Mikeston")  # noqa: SLF001

    assert result["reason"] == "user_ambiguous"
    assert "Neither has ever logged into Plexorcist" in result["user_summary"]
    assert "Pick one" not in result["user_summary"]


@pytest.mark.asyncio
async def test_a_lone_ledger_only_near_match_reads_as_a_sentence(tmp_path):
    """One candidate is the case the plural wordings don't cover. "They have
    ever logged in" is not English and inverts the meaning, so pin the actual
    sentence rather than a fragment that both spellings would satisfy."""
    store = _store(tmp_path)
    tools = _tools(tmp_path, store, names={"qq_a": "Mikeston Reed"})

    result = tools._resolve_user_query("Mikeston Vance")  # noqa: SLF001

    assert result["reason"] == "user_not_found"
    assert "They have never logged into Plexorcist" in result["user_summary"]
    assert "Which one?" not in result["user_summary"]


@pytest.mark.asyncio
async def test_multi_word_query_does_not_queue_a_message(tmp_path):
    """The failure that actually costs something: a resolve that guesses wrong
    (or stamps a synthetic key as the user_id) writes a note against an id that
    matches nobody, and Ben is told it was sent. Assert on the store, not on the
    returned reason string."""
    store = _store(tmp_path)
    tools = _tools(tmp_path, store)

    result = await tools.send_admin_message(user_query="Mikeston Youngberg", message="new season is up")

    assert result["ok"] is False
    assert _admin_note_count(store) == 0


@pytest.mark.asyncio
async def test_ledger_only_person_gets_an_honest_answer_not_no_such_user(tmp_path):
    store = _store(tmp_path)
    tools = _tools(tmp_path, store)

    result = await tools.send_admin_message(user_query="Mikeston", message="new season is up")

    assert result["ok"] is False
    assert result["reason"] == "user_not_registered"
    assert "never logged into Plexorcist" in result["user_summary"]
    # The label must not carry the empty user_id slot -- "Mikeston (qq_mikeston, )".
    assert "Mikeston (qq_mikeston)" in result["user_summary"]
    assert _admin_note_count(store) == 0


@pytest.mark.asyncio
async def test_ledger_only_match_never_leaks_its_synthetic_key_as_a_user_id(tmp_path):
    """Ledger rows are keyed "ledger:<username>" so they don't collide. If that
    key is ever stamped onto the result as user_id, callers write durable state
    against a string that matches no account, silently and permanently."""
    store = _store(tmp_path)
    tools = _tools(tmp_path, store)

    result = tools._resolve_user_query("Mikeston", require_account=False)  # noqa: SLF001

    assert result["ok"] is True
    assert result["user"]["user_id"] == ""
    assert result["user"]["has_account"] is False
    # ...and the key is not matchable text either.
    assert tools._resolve_user_query("ledger:qq_mikeston")["ok"] is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_registered_user_wins_over_a_ledger_only_near_match(tmp_path):
    """Mixed results must not become an ambiguity prompt: one candidate is a real
    account and the other could never receive anything."""
    store = _store(tmp_path)
    _seed_login(tmp_path, user_id="u-1", username="mikestonhall", display_name="Mikeston Hall")
    tools = _tools(tmp_path, store)

    result = await tools.send_admin_message(user_query="Mikeston Hall", message="it's in there now")

    assert result["ok"] is True
    assert result["sent_message"] == "it's in there now"
    assert result["delivery_confirmation"] == {
        "direction": "admin_to_user",
        "status": "queued",
        "recipient_label": "Mikeston Hall (mikestonhall, u-1)",
        "message": "it's in there now",
    }
    assert _admin_note_count(store) == 1
    with store._connect() as conn:  # noqa: SLF001
        assert conn.execute("SELECT user_id FROM user_memory_notes").fetchone()[0] == "u-1"


@pytest.mark.asyncio
async def test_ledger_only_person_can_still_be_renamed(tmp_path):
    """Friendly names are keyed by username, so this caller has no reason to
    demand an account -- and renaming is how a ledger-only entry gets a name Ben
    would actually search for next time."""
    store = _store(tmp_path)
    tools = _tools(tmp_path, store)

    result = await tools.set_user_friendly_name(user_query="qq_mikeston", friendly_name="Mikeston Youngberg")

    assert result["ok"] is True
    assert result["username"] == "qq_mikeston"
    assert tools.friendly_names.resolve("qq_mikeston") == "Mikeston Youngberg"
    # And now Ben's original two-word query resolves cleanly on its own.
    assert tools._resolve_user_query("Mikeston Youngberg", require_account=False)["ok"] is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_genuinely_unknown_name_still_says_so(tmp_path):
    """The suggestion pass must not turn every miss into a hopeful list."""
    store = _store(tmp_path)
    tools = _tools(tmp_path, store)

    result = tools._resolve_user_query("Bartholomew Quigley")  # noqa: SLF001

    assert result["ok"] is False
    assert result["reason"] == "user_not_found"
    assert "candidates" not in result
    assert result["user_summary"] == "I could not find a user matching Bartholomew Quigley."


def test_user_universe_is_the_union_of_logins_and_the_ledger(tmp_path):
    store = _store(tmp_path)
    _seed_login(tmp_path, user_id="u-1", username="qq_mikeston", display_name="Mikeston Real")
    _seed_login(tmp_path, user_id="u-2", username="someoneelse", display_name="Someone Else")
    tools = _tools(tmp_path, store)

    labels = tools._load_user_labels()  # noqa: SLF001

    by_username = {entry["username"]: entry for entry in labels.values()}
    assert set(by_username) == {"qq_mikeston", "someoneelse", "zz_pairfolk"}
    # Someone in both lists is one person with an account, not a duplicate.
    assert by_username["qq_mikeston"]["has_account"] is True
    assert by_username["qq_mikeston"]["user_id"] == "u-1"
    assert by_username["zz_pairfolk"]["has_account"] is False


# --- find_users: there was no way to LOOK at the roster at all ----------------


@pytest.mark.asyncio
async def test_find_users_shows_ledger_only_people_for_a_partial_name(tmp_path):
    """The second half of Ben's transcript: "take a look at the friendly names
    for Mike". Resolution is all-or-nothing by design, so a browse tool is the
    only thing that can answer that question."""
    store = _store(tmp_path)
    _seed_login(tmp_path, user_id="u-1", username="someoneelse", display_name="Someone Else")
    tools = _tools(tmp_path, store)

    result = await tools.find_users(query="Mikeston")

    assert result["ok"] is True
    assert result["match_count"] == 2
    assert result["registered_count"] == 0
    assert {item["username"] for item in result["users"]} == {"qq_mikeston", "zz_pairfolk"}
    assert "no account yet" in result["user_summary"]
    # Someone who doesn't match is not padding the answer.
    assert "someoneelse" not in result["user_summary"]


@pytest.mark.asyncio
async def test_find_users_lists_registered_accounts_first(tmp_path):
    store = _store(tmp_path)
    _seed_login(tmp_path, user_id="u-1", username="mikestonhall", display_name="Mikeston Hall")
    tools = _tools(tmp_path, store)

    result = await tools.find_users(query="Mikeston")

    assert [item["has_account"] for item in result["users"]] == [True, False, False]
    assert result["registered_count"] == 1


@pytest.mark.asyncio
async def test_find_users_with_no_query_lists_everyone(tmp_path):
    store = _store(tmp_path)
    _seed_login(tmp_path, user_id="u-1", username="someoneelse", display_name="Someone Else")
    tools = _tools(tmp_path, store)

    result = await tools.find_users()

    assert result["match_count"] == 3
    assert result["registered_count"] == 1


@pytest.mark.asyncio
async def test_find_users_says_so_when_nothing_matches(tmp_path):
    store = _store(tmp_path)
    tools = _tools(tmp_path, store)

    result = await tools.find_users(query="Bartholomew")

    assert result["ok"] is True
    assert result["users"] == []
    assert result["user_summary"] == "Nobody on file matches Bartholomew."


def test_admin_prompt_tells_the_model_to_browse_after_a_failed_lookup(tmp_path):
    """A working backend the model never calls is the same bug from Ben's side.
    Both halves of his transcript were tool-SELECTION failures: nothing told the
    model to widen a not-found lookup, and nothing told it that "look at the
    friendly names for Mike" is a tool call rather than something to answer from
    memory -- which is exactly what it did ("I poked the name ledger")."""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/prompt.db", admin_display_name="Ben")
    admin = UserContext(user_id="admin-id", username="ben", display_name="Ben", is_admin=True)

    agent, *_ = build_agent(settings, admin)
    prompt = agent._build_instructions(admin)  # noqa: SLF001

    assert "find_users" in prompt
    assert "not-found or ambiguous" in prompt
    # The second half of the transcript: a "who do I have on file" question is a
    # tool call, never an answer from prior chat prose.
    assert "names or friendly names you have on file" in prompt
    # And the model is told what a ledger-only person means, so it reports
    # "no account yet" instead of "no such user".
    assert "[no account yet]" in prompt
