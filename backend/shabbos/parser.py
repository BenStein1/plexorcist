"""Strict, deterministic command parsing for Shabbos Mode.

This is an explicit command grammar, NOT natural-language understanding. It is a
deliberate, documented exception to the project's "no hardcoded NL parsing --
lean on the LLM" rule (see AGENTS.md): there is no model on this path, and the
whole point of the mode is that input is mechanically parsed. It must never grow
fuzzy matching, spelling correction, synonym tables, or intent inference.

Rules:
  - the command token is case-insensitive; argument text keeps its capitalization
  - whitespace is trimmed; nothing else is "helpfully" adjusted
  - anything that does not parse exactly returns a usage error, never a guess
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

# Flags that take no value.
BARE_FLAGS = frozenset({"first", "latest", "all"})

_EPISODE_RE = re.compile(r"^s(\d{1,3})e(\d{1,4})$", re.IGNORECASE)
_ID_RE = re.compile(r"^(tmdb|tvdb):(\d+)$", re.IGNORECASE)


class UsageError(ValueError):
    """Raised for anything that does not parse. Carries a usage string."""

    def __init__(self, message: str, usage: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    """Lowercased command, no leading slash."""

    args: list[str] = field(default_factory=list)
    """Positional tokens, capitalization preserved."""

    flags: dict[str, str | bool] = field(default_factory=dict)
    """`--season 3` -> {"season": "3"}; `--all` -> {"all": True}."""

    rest: str = ""
    """The raw argument text after the command, untouched. Used for literal notes."""

    def joined(self) -> str:
        """Positional args re-joined with single spaces (e.g. a title)."""
        return " ".join(self.args).strip()


def is_command(message: str) -> bool:
    return message.strip().startswith("/")


def parse(message: str) -> ParsedCommand:
    """Split `/cmd a b --flag v` into a ParsedCommand. Raises UsageError."""
    text = message.strip()
    if not text.startswith("/"):
        raise UsageError("Shabbos Mode only accepts explicit commands.")

    body = text[1:].strip()
    if not body:
        raise UsageError("Empty command.")

    head, _, tail = body.partition(" ")
    name = head.strip().lower()
    rest = tail.strip()

    try:
        # posix=True so quoted titles survive: /fix movie "The Thing"
        tokens = shlex.split(rest) if rest else []
    except ValueError as exc:  # unbalanced quote
        raise UsageError(f"Could not parse arguments: {exc}") from exc

    args: list[str] = []
    flags: dict[str, str | bool] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = token[2:].strip().lower()
            if not key:
                raise UsageError("Empty flag ('--').")
            if key in BARE_FLAGS:
                flags[key] = True
                index += 1
                continue
            # value flag: needs the next token, and it must not be another flag
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise UsageError(f"Flag --{key} needs a value.")
            flags[key] = tokens[index + 1]
            index += 2
            continue
        args.append(token)
        index += 1

    return ParsedCommand(name=name, args=args, flags=flags, rest=rest)


# -- typed token helpers -------------------------------------------------------
# Each raises UsageError rather than guessing. No coercion beyond exact forms.


def parse_episode_token(token: str) -> tuple[int, int]:
    """`s01e02` -> (1, 2). Exact form only."""
    match = _EPISODE_RE.match(token.strip())
    if not match:
        raise UsageError(f"'{token}' is not an episode reference. Use the form s01e02.")
    return int(match.group(1)), int(match.group(2))


def parse_id_token(token: str) -> tuple[str, int]:
    """`tmdb:1091` -> ("tmdb", 1091). Exact form only."""
    match = _ID_RE.match(token.strip())
    if not match:
        raise UsageError(f"'{token}' is not a media id. Use tmdb:<number> or tvdb:<number>.")
    return match.group(1).lower(), int(match.group(2))


def parse_int(value: str | bool, label: str) -> int:
    if isinstance(value, bool) or not str(value).strip().lstrip("-").isdigit():
        raise UsageError(f"{label} must be a whole number.")
    return int(str(value).strip())
