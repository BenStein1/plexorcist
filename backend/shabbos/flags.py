"""The per-user Shabbos Mode flag.

Rides the existing `user_flags` table (backend/state.py), same as
`onboarding_tour_seen` and `nilbog_portal_seen` — so there is no migration.
"""

from __future__ import annotations

from backend.state import ConversationStore

SHABBOS_FLAG = "shabbos_mode"


def is_shabbos_user(store: ConversationStore, user_id: str | None) -> bool:
    """True when this account is in Shabbos Mode.

    Consulted by the /api/chat fork, the UI renderer, and the memory sweeper.
    Fails safe: any lookup error means "not in Shabbos Mode" for the UI, but the
    chat fork treats a raised error as a hard failure rather than silently
    handing the user to the LLM (see router.py).
    """
    if not user_id:
        return False
    return store.get_user_flag(str(user_id), SHABBOS_FLAG) == "true"


def set_shabbos_mode(store: ConversationStore, user_id: str, enabled: bool) -> bool:
    if enabled:
        store.set_user_flag(str(user_id), SHABBOS_FLAG, "true")
    else:
        store.clear_user_flag(str(user_id), SHABBOS_FLAG)
    return enabled
