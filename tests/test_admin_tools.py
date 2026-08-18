"""Task resolution and friendly-name management on AdminTools/FriendlyNameDirectory."""

import json

import pytest

from backend.auth_context import FriendlyNameDirectory
from backend.auth_store import PlexAuthSessionStore
from backend.models import PlexAuthSession
from backend.state import ConversationStore
from tools.admin_tools import AdminTools
from tools.schemas import AdminSummaryScope


def _store(tmp_path) -> ConversationStore:
    db_url = f"sqlite:///{tmp_path / 'admin_tools.db'}"
    # AdminTools' user-label lookups query plex_auth_sessions, which only
    # ConversationStore's sibling PlexAuthSessionStore creates — instantiate
    # it here (no rows needed) so that table exists even in tests that never
    # save a session.
    PlexAuthSessionStore(db_url)
    return ConversationStore(db_url)


def _add_note(store: ConversationStore, *, user_id: str, content: str, status: str = "open") -> None:
    store.add_user_memory_note(user_id=user_id, note_type="interaction_note", content=content, status=status, tier=1)


def _admin_tools(tmp_path, store: ConversationStore) -> AdminTools:
    names_path = tmp_path / "friendlynames.json"
    names_path.write_text(json.dumps({"EXCLUDED_USERS": [], "USER_FRIENDLY_NAMES": {"steve1": "Steve"}}), encoding="utf-8")
    return AdminTools(transmission=None, store=store, friendly_names=FriendlyNameDirectory(str(names_path)))


def _note_id(store: ConversationStore, user_id: str) -> int:
    # get_user_memory_context doesn't expose the numeric row id; query directly.
    with store._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT id FROM user_memory_notes WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


# --- resolve by note_id -------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_by_note_id_closes_task(tmp_path):
    store = _store(tmp_path)
    _add_note(store, user_id="u1", content="Oak Island episode stuck")
    note_id = _note_id(store, "u1")
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(note_id=note_id)

    assert result["ok"] is True
    assert result["note_id"] == note_id
    assert result["verified_closed"] is True
    assert result["remaining_open_task_count"] == 0
    assert result["board_empty"] is True
    summary = await tools.get_admin_task_summary(scope="all_users")
    assert summary["task_count"] == 0


@pytest.mark.asyncio
async def test_resolve_by_unknown_note_id_reports_not_found(tmp_path):
    store = _store(tmp_path)
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(note_id=999999)

    assert result["ok"] is False
    assert result["reason"] == "task_not_found"


# --- resolve by task_query ----------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_by_task_query_single_match(tmp_path):
    store = _store(tmp_path)
    _add_note(store, user_id="u1", content="Heat is missing from Plex")
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(task_query="Heat")

    assert result["ok"] is True
    summary = await tools.get_admin_task_summary(scope="all_users")
    assert summary["task_count"] == 0


@pytest.mark.asyncio
async def test_resolve_one_match_reports_other_open_tasks_remaining(tmp_path):
    store = _store(tmp_path)
    _add_note(store, user_id="u1", content="Goblin-related movies need a follow-up")
    _add_note(store, user_id="u2", content="Top Chef request is still pending")
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(task_query="goblin-related movies", resolve_all_matches=True)

    assert result["ok"] is True
    assert result["resolved_count"] == 1
    assert result["remaining_open_task_count"] == 1
    assert result["board_empty"] is False
    assert "1 open task(s) remain" in result["user_summary"]


@pytest.mark.asyncio
async def test_resolve_by_task_query_no_match(tmp_path):
    store = _store(tmp_path)
    _add_note(store, user_id="u1", content="Heat is missing from Plex")
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(task_query="Interstellar")

    assert result["ok"] is False
    assert result["reason"] == "task_not_found"


@pytest.mark.asyncio
async def test_resolve_by_task_query_ambiguous_does_not_guess(tmp_path):
    store = _store(tmp_path)
    _add_note(store, user_id="u1", content="Oak Island season 12 stuck")
    _add_note(store, user_id="u2", content="Oak Island missing episode 3")
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(task_query="Oak Island")

    assert result["ok"] is False
    assert result["reason"] == "task_ambiguous"
    assert len(result["candidates"]) == 2
    # neither task was actually closed
    summary = await tools.get_admin_task_summary(scope="all_users")
    assert summary["task_count"] == 2


@pytest.mark.asyncio
async def test_summary_accepts_enum_scope_and_lists_verbatim_ids(tmp_path):
    store = _store(tmp_path)
    PlexAuthSessionStore(f"sqlite:///{tmp_path / 'admin_tools.db'}").save(
        PlexAuthSession(session_id="s-jeff", user_id="u-jeff", username="jeff1", display_name="Jeff", is_admin=False)
    )
    exact = "Dutton Ranch S1E8 — reset to wanted / look again"
    _add_note(store, user_id="u-jeff", content=exact)
    _add_note(store, user_id="u-other", content="Unrelated old task")
    tools = _admin_tools(tmp_path, store)

    result = await tools.get_admin_task_summary(
        scope=AdminSummaryScope.SPECIFIC_USER,
        user_query="Jeff",
    )

    assert result["scope"] == "specific_user"
    assert result["task_count"] == 1
    note_id = result["tasks"][0]["note_id"]
    assert result["user_summary"] == (
        "Found 1 live open user task(s), verbatim:\n"
        f"- [{note_id}] Jeff (jeff1): {exact}"
    )
    assert result["tasks"][0]["note_id"] == note_id
    assert result["tasks"][0]["user_id"] == "u-jeff"


@pytest.mark.asyncio
async def test_natural_paraphrase_closes_matching_task(tmp_path):
    store = _store(tmp_path)
    _add_note(
        store,
        user_id="u1",
        content="Seven Days in May (1964) — queued for replacement/request, waiting on import",
    )
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(
        task_query="Seven Days in May 1964 replacement request waiting import"
    )

    assert result["ok"] is True
    assert result["verified_closed"] is True
    assert result["resolved_count"] == 1


@pytest.mark.asyncio
async def test_same_user_title_duplicates_close_together_without_touching_other_user(tmp_path):
    store = _store(tmp_path)
    auth_store = PlexAuthSessionStore(f"sqlite:///{tmp_path / 'admin_tools.db'}")
    auth_store.save(
        PlexAuthSession(session_id="s-jeff", user_id="u-jeff", username="jeff1", display_name="Jeff", is_admin=False)
    )
    auth_store.save(
        PlexAuthSession(session_id="s-jared", user_id="u-jared", username="jared1", display_name="Jared", is_admin=False)
    )
    _add_note(store, user_id="u-jeff", content="Dutton Ranch S1E8 — reset to wanted / look again")
    _add_note(store, user_id="u-jeff", content="Dutton Ranch S1E9 — reminder to download after July 3")
    _add_note(store, user_id="u-jared", content="Dutton Ranch S1E8 is missing")
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task(task_query="Jeff Dutton Ranch")

    assert result["ok"] is True
    assert result["verified_closed"] is True
    assert result["resolved_count"] == 2
    remaining = await tools.get_admin_task_summary(scope="all_users")
    assert [task["user_label"] for task in remaining["tasks"]] == ["Jared (jared1, u-jared)"]


@pytest.mark.asyncio
async def test_resolve_requires_note_id_or_task_query(tmp_path):
    store = _store(tmp_path)
    tools = _admin_tools(tmp_path, store)

    result = await tools.resolve_admin_task()

    assert result["ok"] is False
    assert result["reason"] == "target_required"


# --- friendly names ------------------------------------------------------------


def test_friendly_name_directory_set_and_resolve(tmp_path):
    path = tmp_path / "friendlynames.json"
    path.write_text(json.dumps({"EXCLUDED_USERS": [], "USER_FRIENDLY_NAMES": {"alice1": "Alice"}}), encoding="utf-8")
    directory = FriendlyNameDirectory(str(path))

    assert directory.resolve("alice1") == "Alice"

    directory.set_friendly_name("alice1", "Ali")
    assert directory.resolve("alice1") == "Ali"

    # persisted to disk, not just the in-memory cache
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["USER_FRIENDLY_NAMES"]["alice1"] == "Ali"


def test_friendly_name_directory_set_preserves_other_entries(tmp_path):
    path = tmp_path / "friendlynames.json"
    path.write_text(
        json.dumps({"EXCLUDED_USERS": ["N/A"], "USER_FRIENDLY_NAMES": {"bob1": "Bob", "carl1": "Carl"}}),
        encoding="utf-8",
    )
    directory = FriendlyNameDirectory(str(path))

    directory.set_friendly_name("bob1", "Robert")

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["USER_FRIENDLY_NAMES"]["bob1"] == "Robert"
    assert on_disk["USER_FRIENDLY_NAMES"]["carl1"] == "Carl"
    assert on_disk["EXCLUDED_USERS"] == ["N/A"]


def test_friendly_name_directory_rejects_empty_name(tmp_path):
    path = tmp_path / "friendlynames.json"
    path.write_text(json.dumps({"EXCLUDED_USERS": [], "USER_FRIENDLY_NAMES": {}}), encoding="utf-8")
    directory = FriendlyNameDirectory(str(path))

    with pytest.raises(ValueError):
        directory.set_friendly_name("someone", "   ")


@pytest.mark.asyncio
async def test_set_user_friendly_name_by_admin_resolves_via_plex_session(tmp_path):
    store = _store(tmp_path)
    db_path = str(tmp_path / "admin_tools.db")
    PlexAuthSessionStore(f"sqlite:///{db_path}").save(
        PlexAuthSession(session_id="s1", user_id="u1", username="steve1", display_name="Steve", is_admin=False)
    )
    tools = _admin_tools(tmp_path, store)

    result = await tools.set_user_friendly_name(user_query="steve1", friendly_name="Steven")

    assert result["ok"] is True
    assert result["username"] == "steve1"
    assert tools.friendly_names.resolve("steve1") == "Steven"


@pytest.mark.asyncio
async def test_set_user_friendly_name_unknown_user(tmp_path):
    store = _store(tmp_path)
    tools = _admin_tools(tmp_path, store)

    result = await tools.set_user_friendly_name(user_query="nobody-here", friendly_name="Whoever")

    assert result["ok"] is False
    assert result["reason"] == "user_not_found"
