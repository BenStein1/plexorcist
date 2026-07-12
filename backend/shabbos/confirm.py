"""Short-lived, user-bound, single-use confirmation tokens.

Expensive or destructive commands (the repair lane) do not act immediately: they
describe exactly what they would do and mint a token. `/confirm <TOKEN>` executes
the *stored* action -- the user's second message cannot change its arguments.

The token is bound to the user_id and to the fully-resolved action. It expires,
and it is consumed on first use. Stored in the conversation's support_context,
which is already persisted per conversation.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

PENDING_KEY = "shabbos_pending"
TOKEN_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class PendingAction:
    token: str
    user_id: str
    tool: str
    kwargs: dict[str, Any]
    description: str
    expires_at: datetime
    note: str = ""
    """The admin-facing task text, carried through so a confirmed action leaves a
    readable note ("Repair requested for movie X") rather than the UI preview
    string ("This will ask Radarr to...")."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
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
                token=str(raw["token"]),
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
    """Create and store a pending action, replacing any previous one."""
    action = PendingAction(
        token=secrets.token_hex(3).upper(),
        user_id=str(user_id),
        tool=tool,
        kwargs=kwargs,
        description=description,
        note=note,
        expires_at=_now() + TOKEN_TTL,
    )
    support_context[PENDING_KEY] = action.to_dict()
    return action


def consume(support_context: dict[str, Any], *, user_id: str, token: str) -> PendingAction:
    """Return the pending action for this token, or raise ValueError.

    Rejects (deterministically, with no fallback): no pending action, a token
    that doesn't match, a token belonging to another user, and an expired token.
    The action is removed from the context on any terminal outcome, so a token is
    strictly single-use.
    """
    raw = support_context.get(PENDING_KEY)
    if not isinstance(raw, dict):
        raise ValueError("There is nothing waiting to be confirmed.")

    action = PendingAction.from_dict(raw)
    if action is None:
        support_context.pop(PENDING_KEY, None)
        raise ValueError("There is nothing waiting to be confirmed.")

    supplied = token.strip().upper()
    if not secrets.compare_digest(action.token, supplied):
        raise ValueError("That confirmation code doesn't match the pending action.")

    # Bound to the authenticated user, not just to the conversation.
    if action.user_id != str(user_id):
        support_context.pop(PENDING_KEY, None)
        raise ValueError("That confirmation code doesn't match the pending action.")

    support_context.pop(PENDING_KEY, None)  # single use, even if expired

    if _now() > action.expires_at:
        raise ValueError("That confirmation code has expired. Run the command again to get a new one.")

    return action
