"""The pending action behind `/confirm`.

Expensive or destructive commands (the repair lane) do not act immediately: they
describe exactly what they would do, and wait for a bare `/confirm`.

There is no code to type. The safety that matters is not a shared secret -- the
user is already authenticated -- it is that `/confirm` executes the action that
was STORED, so the second message cannot change what was agreed to. The action is
still bound to the user, still single-use, and still expires.

Stored in the conversation's support_context, which is already persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

PENDING_KEY = "shabbos_pending"
TOKEN_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class PendingAction:
    user_id: str
    tool: str
    kwargs: dict[str, Any]
    description: str
    expires_at: datetime
    note: str = ""
    """The admin-facing task text, carried through so a confirmed action leaves a
    readable note ("Repair requested for movie X") rather than the UI preview
    string ("This will try to re-fetch...")."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tool": self.tool,
            "kwargs": self.kwargs,
            "description": self.description,
            "note": self.note,
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingAction | None":
        try:
            return cls(
                user_id=str(raw["user_id"]),
                tool=str(raw["tool"]),
                kwargs=dict(raw["kwargs"]),
                description=str(raw["description"]),
                note=str(raw.get("note") or ""),
                expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            )
        except Exception:  # noqa: BLE001 - a malformed pending action is simply absent
            return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mint(
    support_context: dict[str, Any],
    *,
    user_id: str,
    tool: str,
    kwargs: dict[str, Any],
    description: str,
    note: str = "",
) -> PendingAction:
    """Store a pending action, replacing any previous one.

    Only one action can be pending at a time, so a bare `/confirm` is never
    ambiguous: it always means "the thing I was just shown".
    """
    action = PendingAction(
        user_id=str(user_id),
        tool=tool,
        kwargs=kwargs,
        description=description,
        note=note,
        expires_at=_now() + TOKEN_TTL,
    )
    support_context[PENDING_KEY] = action.to_dict()
    return action


def consume(support_context: dict[str, Any], *, user_id: str) -> PendingAction:
    """Return and clear the pending action, or raise ValueError.

    Rejects deterministically (never with a fallback): nothing pending, an action
    belonging to another user, or an expired one. Cleared on any terminal outcome,
    so it is strictly single-use -- a second `/confirm` cannot repeat a repair.
    """
    raw = support_context.get(PENDING_KEY)
    action = PendingAction.from_dict(raw) if isinstance(raw, dict) else None
    if action is None:
        support_context.pop(PENDING_KEY, None)
        raise ValueError("There is nothing waiting to be confirmed.")

    # Bound to the authenticated user, not merely to the conversation.
    if action.user_id != str(user_id):
        support_context.pop(PENDING_KEY, None)
        raise ValueError("There is nothing waiting to be confirmed.")

    support_context.pop(PENDING_KEY, None)  # single use, even when expired

    if _now() > action.expires_at:
        raise ValueError("That confirmation expired. Run the command again.")

    return action
