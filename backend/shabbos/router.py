"""The deterministic Shabbos Mode router.

This module is the AI-free half of the application. It is a THIRD consumer of
tools/catalog.py, alongside tools/bridge.py (the LLM agent) and tools/server.py
(the MCP server) -- so it reuses the real tools, the real pydantic validation and
the real permission gate, and duplicates no business logic.

THE INVARIANT: nothing reachable from here is ever handed an LlmClient. This
module must not import clients.llm_providers, tools.bridge, or backend.agent --
directly or transitively. You cannot call what you were never given. A test
enforces both halves of this: an import tripwire, and a spy asserting zero model
calls across every command in the registry.

If a command cannot be handled deterministically, it FAILS. It never falls back
to the conversational agent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from backend.config import Settings
from backend.logging import AuditLogger
from backend.models import ConversationState, UserContext
from backend.shabbos import render
from backend.shabbos.commands import COMMANDS, COMMANDS_BY_NAME, LAST_SEARCH_KEY, BuildContext, CommandSpec, Invocation
from backend.shabbos.confirm import consume, mint
from backend.shabbos.parser import UsageError, is_command, parse
from backend.state import ConversationStore
from tools.admin_alerts import (
    REQUEST_TOOL_NAMES,
    alert_media_id,
    build_command_failure_alert,
    build_request_alert,
    clear_admin_alert_cooldown,
    should_send_admin_alert,
)
from tools.catalog import build_toolkit, get_spec, visible_specs
from tools.error_helpers import classify_http_error, user_error_summary

logger = logging.getLogger(__name__)

BANNER = (
    "Shabbos Mode: ON\n"
    "No language models. No conversational interpretation.\n"
    '"I don\'t roll on Shabbos."'
)

FREEFORM_REPLY = "Shabbos Mode only accepts explicit commands.\nUse /help to see them."

UNAVAILABLE_REPLY = (
    "That action is not available in Shabbos Mode.\n"
    "No language model was used. Use /help, or contact the admin."
)

# Search candidate fields we keep. Everything else (notably `raw`, the full Ombi
# item) is dropped before it is ever stored or shown.
_CANDIDATE_FIELDS = ("title", "year", "type", "tmdb_id", "tvdb_id", "seasons")


class ShabbosRouter:
    """Handles one Shabbos Mode turn. Constructed without any LLM client."""

    def __init__(
        self,
        settings: Settings,
        store: ConversationStore,
        audit: AuditLogger,
        user: UserContext,
    ) -> None:
        self.settings = settings
        self.store = store
        self.audit = audit
        self.user = user
        self.toolkit = build_toolkit(settings, store, user)
        # The same permission gate the LLM and MCP surfaces use. A command whose
        # tool is not visible to this principal simply cannot run.
        self.visible = {spec.name for spec in visible_specs(user, settings)}

    # -- entry point -----------------------------------------------------------

    async def handle(self, state: ConversationState, message: str) -> str:
        text = (message or "").strip()

        if not is_command(text):
            self._audit("<free-form>", ok=False, target="", note="rejected_freeform")
            return FREEFORM_REPLY

        try:
            parsed = parse(text)
        except UsageError as exc:
            self._audit("<unparsed>", ok=False, target="", note="usage_error")
            return self._usage_error(exc)

        spec = COMMANDS_BY_NAME.get(parsed.name)
        if spec is None:
            self._audit(parsed.name, ok=False, target="", note="unknown_command")
            return f"Unknown command: /{parsed.name}\n\n{self._help()}"

        if spec.local:
            return await self._run_local(spec, parsed, state)

        assert spec.build is not None
        ctx = BuildContext(user_id=self.user.user_id, support_context=state.support_context)
        try:
            invocation = spec.build(ctx, parsed)
        except UsageError as exc:
            self._audit(spec.name, ok=False, target="", note="usage_error")
            return self._usage_error(exc, spec)

        # Confirm-gated: describe, mint a token, and do NOT act.
        if invocation.confirm:
            action = mint(
                state.support_context,
                user_id=self.user.user_id,
                tool=invocation.tool,
                kwargs=invocation.kwargs,
                description=invocation.confirm,
                note=invocation.note_text,
            )
            self._audit(spec.name, ok=True, target=invocation.target, note="awaiting_confirmation")
            del action  # stored; /confirm replays it from support_context
            return f"{invocation.confirm}\n\nConfirm with:  /confirm\n(Expires in 5 minutes.)"

        return await self._execute(spec.name, invocation, state)

    # -- local commands --------------------------------------------------------

    async def _run_local(self, spec: CommandSpec, parsed, state: ConversationState) -> str:
        if spec.name == "help":
            self._audit("help", ok=True, target="")
            if parsed.args:
                target = COMMANDS_BY_NAME.get(parsed.args[0].lstrip("/").lower())
                if target is None:
                    return f"No such command: /{parsed.args[0]}\n\n{self._help()}"
                return f"{target.usage}\n  {target.summary}"
            return self._help()

        if spec.name == "whoami":
            self._audit("whoami", ok=True, target="")
            name = self.user.display_name or self.user.username
            return f"You are signed in as {name}.\nShabbos Mode is ON for this account."

        if spec.name == "logout":
            self._audit("logout", ok=True, target="")
            return "Open /auth/logout to sign out."

        if spec.name == "confirm":
            return await self._run_confirm(parsed, state)

        return UNAVAILABLE_REPLY

    async def _run_confirm(self, parsed, state: ConversationState) -> str:
        try:
            action = consume(state.support_context, user_id=self.user.user_id)
        except ValueError as exc:
            self._audit("confirm", ok=False, target="", note="nothing_pending")
            return str(exc)

        # Rebuild the invocation from the STORED action, not from user input, so
        # the second message cannot alter what was confirmed.
        invocation = Invocation(
            tool=action.tool,
            kwargs=action.kwargs,
            target=action.description,
            writes_note=True,
            note_text=action.note or action.description,
        )
        return await self._execute("confirm", invocation, state)

    # -- execution -------------------------------------------------------------

    async def _execute(self, command: str, invocation: Invocation, state: ConversationState) -> str:
        tool = invocation.tool

        # Permission gate: reuse visible_specs. Fails closed.
        if tool not in self.visible:
            self._audit(command, ok=False, target=invocation.target, note="not_permitted")
            return UNAVAILABLE_REPLY

        spec = get_spec(tool)
        if spec is None or tool not in render.RENDERERS:
            # A tool with no renderer must never be exposed -- fail closed rather
            # than dump a raw dict at the user.
            self._audit(command, ok=False, target=invocation.target, note="no_renderer")
            return UNAVAILABLE_REPLY

        # Validate against the tool's REAL input model (same contract as the LLM
        # bridge), so schema preconditions are enforced identically.
        try:
            parsed_input = spec.input_model.model_validate(invocation.kwargs)
        except ValidationError as exc:
            self._audit(command, ok=False, target=invocation.target, note="validation_error")
            return self._validation_error(exc)

        handler = spec.resolve(self.toolkit)
        try:
            result = await handler(**parsed_input.model_dump(exclude_unset=True))
        except httpx.HTTPError as exc:
            # An upstream service is down. Report it plainly; never retry via AI.
            error = classify_http_error(service=self._service_of(tool), operation=tool, exc=exc)
            if tool in REQUEST_TOOL_NAMES:
                # Requesters have no access to the request backend, so they get the
                # outcome without the plumbing -- and the admin gets paged instead.
                text = f"The request for {invocation.target or 'that'} did not go through. Nothing was added."
                if await self._alert_admin_request_failure(
                    tool,
                    {"ok": False, "status": "error", **error},
                    invocation.target,
                ):
                    text = f"{text}\nThe admin has been notified."
            else:
                # /fix, /status, /search, /seasons, /episode. The user is no longer told
                # which service broke -- they cannot reach it -- so this alert is the only
                # remaining signal that it broke at all.
                text = user_error_summary(
                    tool_family=command.capitalize(),
                    error=error,
                    title=invocation.target or "that",
                    change_status="Nothing was changed.",
                )
                if await self._send_admin_alert(
                    build_command_failure_alert(
                        user_label=self._user_label(),
                        command=command,
                        tool=tool,
                        # note_text first: on the /confirm path `target` is the whole
                        # preview sentence, while the note is the admin-facing task text.
                        target=invocation.note_text or invocation.target,
                        error=error,
                    )
                ):
                    text = f"{text}\nThe admin has been notified."
            self._audit(command, ok=False, target=invocation.target, note="service_unavailable")
            self._write_note(invocation, ok=False, summary=text)
            return text
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the user
            logger.exception("Shabbos handler failed for tool %s", tool)
            self._audit(command, ok=False, target=invocation.target, note="handler_error")
            text = "That didn't work, and nothing was changed. No language model was used."
            # A crash in our own code, not an upstream outage: the user's reply says
            # nothing an admin could act on, so it has to be carried by the alert.
            if await self._send_admin_alert(
                build_command_failure_alert(
                    user_label=self._user_label(),
                    command=command,
                    tool=tool,
                    target=invocation.note_text or invocation.target,
                    error={
                        "service": "plexorcist",
                        "failure_type": "handler_error",
                        "error_message": f"{type(exc).__name__}: {exc}",
                    },
                )
            ):
                text = f"{text}\nThe admin has been notified."
            return text

        if not isinstance(result, dict):
            self._audit(command, ok=False, target=invocation.target, note="bad_result")
            return UNAVAILABLE_REPLY

        if invocation.result_filter:
            result = self._filter_candidates(result, invocation.result_filter)

        if tool == "search_media":
            self._remember_search(state, result)

        ok = result.get("ok") is not False and result.get("status") != "account_not_ready"
        text = render.render(tool, result)

        if not ok and tool in REQUEST_TOOL_NAMES:
            # There is no model here to decide to escalate, and the user is never told to
            # go check the request backend, so a request that did not land has to page the
            # admin from the router itself.
            if await self._alert_admin_request_failure(tool, result, invocation.target):
                text = f"{text}\nThe admin has been notified."

        self._audit(command, ok=ok, target=invocation.target)
        self._write_note(invocation, ok=ok, summary=invocation.note_text or text)
        return text

    # -- result post-processing ------------------------------------------------

    def _filter_candidates(self, result: dict[str, Any], media_type: str) -> dict[str, Any]:
        candidates = [
            item
            for item in (result.get("candidates") or [])
            if isinstance(item, dict) and str(item.get("type") or "").lower() == media_type
        ]
        return {**result, "candidates": candidates}

    def _remember_search(self, state: ConversationState, result: dict[str, Any]) -> None:
        """Cache the candidates so /request <n> is an exact index lookup.

        Only whitelisted fields are stored -- the Ombi `raw` passthrough never
        enters conversation state.
        """
        state.support_context[LAST_SEARCH_KEY] = [
            {field: item.get(field) for field in _CANDIDATE_FIELDS}
            for item in (result.get("candidates") or [])
            if isinstance(item, dict)
        ]

    # -- audit + admin tasks ---------------------------------------------------

    async def _alert_admin_request_failure(
        self,
        tool: str,
        result: dict[str, Any],
        target: str,
    ) -> bool:
        """Page the admin about a request that did not land. Returns True if it went out.

        This calls the escalation tool directly rather than through the command gate:
        it is a server-side consequence of the failure, not something the user asked
        for, and it must work for users who cannot invoke /issue themselves. No model
        is involved -- the alert text is built from the result, same as the LLM path.
        """
        subject = str(result.get("title") or alert_media_id(result) or target or "that title")
        alert = build_request_alert(
            user_label=self._user_label(),
            name=tool,
            result=result,
            subject=subject,
        )
        return await self._send_admin_alert(alert)

    def _user_label(self) -> str:
        return str(self.user.display_name or self.user.username or self.user.user_id)

    async def _send_admin_alert(self, alert: tuple[str, str, str, int] | None) -> bool:
        """Send one built alert. Returns True only if it actually went out.

        The caller appends "The admin has been notified." on True, so a Prowl failure
        must never come back True -- telling a user help is coming when it is not is
        worse than the original error.
        """
        if alert is None:
            return False
        key, event, summary, priority = alert
        if not should_send_admin_alert(key):
            return False  # same failure, same target, within the cooldown window
        try:
            notice = await self.toolkit.escalation.send_admin_prowl_notice(
                summary=f"[Shabbos Mode] {summary}",
                priority=priority,
                event=event,
            )
        except Exception:  # noqa: BLE001 - alerting must never break the user's turn
            logger.exception("Failed to send Shabbos admin alert")
            clear_admin_alert_cooldown(key)
            return False
        if isinstance(notice, dict) and notice.get("ok"):
            return True
        # Nothing was delivered, so do not let a phantom send hold the cooldown.
        clear_admin_alert_cooldown(key)
        return False

    def _audit(self, command: str, *, ok: bool, target: str, note: str | None = None) -> None:
        payload: dict[str, Any] = {
            "user_id": self.user.user_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "ok": ok,
            "target": target,
            "router": "deterministic",
            # There is no code path from this router to a model client. This is a
            # literal, and the tests prove it matches reality.
            "ai_invoked": False,
        }
        if note:
            payload["note"] = note
        try:
            self.audit.log("shabbos_command", payload)
        except Exception:  # noqa: BLE001 - auditing must never break the user's turn
            logger.exception("Failed to write Shabbos audit event")

    def _write_note(self, invocation: Invocation, *, ok: bool, summary: str) -> None:
        """Leave the admin a note, deterministically.

        The LLM sweeper (which infers tasks from conversation prose) never runs
        for these users, so the router records what happened at the moment it
        happens -- which is strictly more accurate. Notes with status='open' are
        exactly what get_admin_task_summary reads.
        """
        if not invocation.writes_note:
            return

        needs_attention = invocation.always_open_task or not ok
        status = "open" if needs_attention else "logged"
        try:
            self.store.add_user_memory_note(
                user_id=self.user.user_id,
                note_type="task" if needs_attention else "shabbos_activity",
                content=(invocation.note_text or summary)[:500],
                task_id=str(uuid.uuid4()) if needs_attention else None,
                status=status,
                tier=1,
                metadata={
                    "source": "shabbos",
                    "tool": invocation.tool,
                    "target": invocation.target,
                    "ai_invoked": False,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write Shabbos task note")

    # -- text helpers ----------------------------------------------------------

    def _service_of(self, tool: str) -> str:
        if "sickchill" in tool or tool == "repair_requested_show":
            return "sickchill"
        if tool == "repair_requested_movie":
            return "radarr"
        if tool == "check_episode_status":
            return "plex"
        if tool == "get_user_watch_context":
            return "tautulli"
        if tool == "send_admin_prowl_notice":
            return "prowl"
        return "ombi"

    def _usage_error(self, exc: UsageError, spec: CommandSpec | None = None) -> str:
        usage = exc.usage or (spec.usage if spec else None)
        if usage:
            return f"{exc.message}\n\nUsage:  {usage}"
        return f"{exc.message}\n\nUse /help to see the commands."

    def _validation_error(self, exc: ValidationError) -> str:
        lines = []
        for error in exc.errors():
            field = ".".join(str(part) for part in error.get("loc", ())) or "input"
            lines.append(f"  {field}: {error.get('msg', 'invalid value')}")
        return "That didn't validate, so nothing was done:\n" + "\n".join(lines)

    def _help(self) -> str:
        lines = ["Commands:", ""]
        for spec in COMMANDS:
            lines.append(f"  {spec.usage}")
            lines.append(f"      {spec.summary}")
        lines += ["", "Free-form text is not interpreted. Commands only."]
        return "\n".join(lines)
