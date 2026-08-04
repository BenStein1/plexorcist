"""What happens when a request does not land.

Ben's rule, verbatim: "in plexorcist the user doesnt KNOW about ombi. They just ask
the machine to get it. They cant/wont go to ombi. The AI needs to pass along any api
error, self fix or resolve the error, or just work. If it fails. I need to be notified
that there was a real issue."

So: user-facing prose never names the request backend and never sends the user to it,
and a request that did not land pages the admin -- in the LLM path and in Shabbos Mode,
which has no model to decide to escalate.
"""

from __future__ import annotations

import pathlib

import httpx
import pytest

from backend.agent import ConciergeAgent
from backend.config import Settings
from backend.logging import AuditLogger
from backend.models import ConversationState, ToolCallRecord, UserContext
from backend.shabbos import render
from backend.shabbos.router import ShabbosRouter
from backend.state import ConversationStore
from clients.llm_providers import LlmResponse, ToolCall
from clients.ombi_client import OmbiClient
from tests.test_agent_loop import FakeLlmClient
from tools import schemas
from tools.bridge import ToolBridge
from tools.catalog import ToolSpec
from tools.request_tools import RequestTools, _stamp_request_failure

ALTERED_CARBON_TVDB = 332331
BACKEND_NAMES = ("ombi", "sickchill", "radarr", "tautulli", "jackett", "transmission", "prowl")

USER = UserContext(user_id="9001", username="richard", display_name="Richard", is_admin=False)


def assert_user_safe(text: str) -> None:
    """No backend names, no HTTP codes, no 'go check it yourself' in user prose."""
    lowered = text.lower()
    for name in BACKEND_NAMES:
        assert name not in lowered, f"user-facing text names {name}: {text!r}"
    assert "http " not in lowered, f"user-facing text quotes an HTTP status: {text!r}"


class FakeOmbi(OmbiClient):
    def __init__(self, get_routes: dict, post_result=None, post_error: Exception | None = None):
        super().__init__(base_url="http://ombi.test", api_key="test-key")
        self._get_routes = get_routes
        self._post_result = post_result
        self._post_error = post_error

    async def get_json(self, path, params=None, headers=None, timeout=None):
        value = self._get_routes.get(path, {})
        if isinstance(value, Exception):
            raise value
        return value

    async def post_json(self, path, payload, headers=None, timeout=None):
        if self._post_error is not None:
            raise self._post_error
        return self._post_result

    async def find_user_by_identity(self, username: str) -> dict:
        return {"ok": True, "exists": True}


UNCONFIRMED_POST = {"result": False, "isError": False, "errorMessage": None, "errorCode": None}


async def unconfirmed_result() -> dict:
    """A real unconfirmed show request, through the tool layer the agent sees."""
    tools = RequestTools(FakeOmbi(get_routes={"/api/v1/Request/tv": []}, post_result=UNCONFIRMED_POST))
    return await tools.request_show_scope_for_user(
        username="richard", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )


# -- 1. the user is never sent to a system they cannot reach --------------------


@pytest.mark.asyncio
async def test_unconfirmed_prose_never_names_the_backend():
    result = await unconfirmed_result()
    assert_user_safe(result["user_summary"])
    assert_user_safe(render.render_request_show_scope_for_user(result))


@pytest.mark.asyncio
async def test_http_failure_prose_never_names_the_backend():
    boom = httpx.HTTPStatusError(
        "Server error '500 Internal Server Error'",
        request=httpx.Request("POST", "http://ombi.test/api/v1/Request/tv"),
        response=httpx.Response(500, request=httpx.Request("POST", "http://ombi.test/api/v1/Request/tv")),
    )
    tools = RequestTools(FakeOmbi(get_routes={"/api/v1/Request/tv": []}, post_error=boom))

    result = await tools.request_show_scope_for_user(
        username="richard", tvdb_id=ALTERED_CARBON_TVDB, scope="full_series"
    )

    assert result["ok"] is False
    assert_user_safe(result["user_summary"])
    assert_user_safe(render.render_request_show_scope_for_user(result))
    # The machine-readable detail is still there for the admin path.
    assert result["service"] == "ombi"
    assert result["http_status"] == 500


def test_successful_request_prose_never_names_the_backend():
    rendered = render.render_request_show_scope_for_user(
        {"ok": True, "status": "requested", "title": "Altered Carbon", "scope": "full_series"}
    )
    assert_user_safe(rendered)


def _user_summary_literals(path: str) -> list[tuple[str, str]]:
    """Every `user_summary`/`change_status` string written anywhere in a module.

    Whole-module, not just the tool entry points: the worst offender lived in a
    private helper (`_sickchill_error_metadata`-style), and a leak is a leak
    whichever function assembled it.

    Source-level on purpose: reaching these strings at runtime means faking a
    specific upstream failure per branch, and the branches that leak are exactly
    the rare ones nobody drives in a test.
    """
    import ast

    found: list[tuple[str, str]] = []

    def literals(node) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):  # f-string: keep the fixed parts
            return ["".join(p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str))]
        if isinstance(node, ast.IfExp):
            return literals(node.body) + literals(node.orelse)
        if isinstance(node, ast.BinOp):
            return literals(node.left) + literals(node.right)
        return []

    tree = ast.parse(pathlib.Path(path).read_text())
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "user_summary":
                        found += [(func.name, text) for text in literals(value)]
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in {"change_status", "user_summary"}:
                        found += [(func.name, text) for text in literals(keyword.value)]
    return found


@pytest.mark.parametrize(
    "module", ["tools/request_tools.py", "tools/repair_tools.py", "tools/movie_repair_tools.py"]
)
def test_no_tool_writes_a_backend_name_into_user_summary(module):
    """`user_summary` IS the text a Shabbos user reads -- render.py prefers it over
    its own prose (`_failure`, `_render_repair`, `_render_request`), and there is no
    model in that loop to filter it. The admin loses nothing: the classified error
    fields (`service`, `http_status`, `error_message`) sit in the same dict.

    Scoped to modules that hold no admin-only tool -- asserted below, so adding one
    here fails loudly instead of silently over-asserting. admin_tools.py is out for
    exactly that reason: its Transmission maintenance prose names Transmission to
    the only person who can act on it.
    """
    import ast

    from backend.config import Settings
    from tools.catalog import visible_specs

    settings = Settings(movie_direct_source_enabled=True)
    admin_context = UserContext(user_id="1", username="ben", display_name="Ben", is_admin=True)
    admin_only = {spec.name for spec in visible_specs(admin_context, settings)} - {
        spec.name for spec in visible_specs(USER, settings)
    }
    functions = {
        node.name
        for node in ast.walk(ast.parse(pathlib.Path(module).read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not functions & admin_only, f"{module} now holds an admin-only tool -- re-scope this test"

    summaries = _user_summary_literals(module)
    assert summaries, f"no user_summary literals found in {module} -- did the shape change?"
    for tool_name, text in summaries:
        try:
            assert_user_safe(text)
        except AssertionError as exc:  # name the tool, not just the string
            raise AssertionError(f"{tool_name}: {exc}") from None


# -- 2. failures carry the shape the alert path keys on -------------------------


@pytest.mark.asyncio
async def test_unconfirmed_is_stamped_as_a_failure():
    result = await unconfirmed_result()
    assert result["service"] == "ombi"
    assert result["operation"] == "tv_request"
    assert result["failure_type"] == "unconfirmed_request"


def test_benign_outcomes_are_not_stamped_as_failures():
    for status in ("already_requested", "already_available", "account_not_ready", "movie_not_found"):
        stamped = _stamp_request_failure({"ok": False, "status": status}, operation="tv_request")
        assert "failure_type" not in stamped, status
    assert "failure_type" not in _stamp_request_failure({"ok": True, "status": "requested"}, operation="tv_request")


# -- 3. the admin actually gets told --------------------------------------------


def agent_with(prowl=None) -> ConciergeAgent:
    return ConciergeAgent(
        bridge=None,
        ombi_continue_url="http://ombi.example",
        llm_client=None,
        admin_label="Ben",
        prowl=prowl,
        movie_direct_source_enabled=False,
    )


def record(result: dict, name: str = "request_show_scope_for_user") -> ToolCallRecord:
    return ToolCallRecord(name=name, arguments={}, result=result)


@pytest.mark.asyncio
async def test_unconfirmed_request_alerts_the_admin():
    result = await unconfirmed_result()
    alert = agent_with()._build_admin_alert(USER, record(result))

    assert alert is not None
    key, event, summary, priority = alert
    assert event == "Request Unconfirmed"
    assert priority == 1
    assert "Richard" in summary
    # No title on this path (Ombi's v2 search 204s on a TVDB id), so the id has to
    # carry the message -- "Unknown show" is neither actionable nor unique.
    assert f"TVDB {ALTERED_CARBON_TVDB}" in summary
    assert "Unknown show" not in summary
    assert str(ALTERED_CARBON_TVDB) in key


def test_alert_keys_do_not_collide_across_titleless_shows():
    """A shared key would let the 15-minute cooldown swallow the next show's alert."""
    agent = agent_with()
    first = agent._build_admin_alert(USER, record({"ok": False, "status": "unconfirmed", "tvdb_id": 1}))
    second = agent._build_admin_alert(USER, record({"ok": False, "status": "unconfirmed", "tvdb_id": 2}))
    assert first is not None and second is not None
    assert first[0] != second[0]


@pytest.mark.asyncio
async def test_http_failure_alerts_with_the_real_status():
    result = _stamp_request_failure(
        {
            "ok": False,
            "status": "error",
            "title": "Altered Carbon",
            "tvdb_id": ALTERED_CARBON_TVDB,
            "error": {"message": "Server error '500 Internal Server Error' for url"},
        },
        operation="tv_request",
    )
    alert = agent_with()._build_admin_alert(USER, record(result))

    assert alert is not None
    _key, event, summary, priority = alert
    assert event == "Request Failed"
    assert priority == 1
    assert "Altered Carbon" in summary
    assert "500" in summary


def test_missing_show_identifier_alerts_quietly():
    result = _stamp_request_failure(
        {"ok": False, "status": "missing_show_identifier", "tvdb_id": 0}, operation="tv_request"
    )
    alert = agent_with()._build_admin_alert(USER, record(result))

    assert alert is not None
    _key, event, summary, priority = alert
    assert event == "Request Blocked"
    # Prowl's quietest priority: the model is told to resolve the id and retry in the
    # same turn, so this fires even when the retry lands. Log it, do not buzz his phone.
    assert priority == -2
    assert "Richard" in summary


def test_landed_requests_stay_quiet():
    agent = agent_with()
    for result in (
        {"ok": True, "status": "requested", "title": "Altered Carbon"},
        {"ok": True, "status": "already_requested", "title": "Altered Carbon"},
        {"ok": False, "status": "account_not_ready"},
    ):
        assert agent._build_admin_alert(USER, record(result)) is None, result


@pytest.mark.asyncio
async def test_plain_reply_after_a_failed_request_still_explains_it():
    """When the model returns no text, the user still hears what happened."""
    result = {**await unconfirmed_result(), "admin_alert_sent": True}
    reply = agent_with()._plain_support_reply_from_tool_calls(USER, [record(result)])

    assert reply is not None
    assert_user_safe(reply)
    assert "Ben" in reply


# -- 4. "notified" must mean notified -------------------------------------------


class FakeProwl:
    def __init__(self, response: dict | Exception):
        self.response = response
        self.calls: list[dict] = []

    async def send_notice(self, summary: str, priority: int = 0, event: str = "Concierge Alert") -> dict:
        self.calls.append({"summary": summary, "priority": priority, "event": event})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.asyncio
async def test_a_failed_send_leaves_nothing_on_cooldown():
    """A send that never happened must not silence the next 10 minutes of that alert."""
    unconfirmed = await unconfirmed_result()

    class OneToolToolkit:
        async def request(self, **kwargs) -> dict:
            return dict(unconfirmed)

    specs = [
        ToolSpec(
            name="request_show_scope_for_user",
            description="fake request",
            input_model=schemas.EmptyInput,
            resolve=lambda tk: tk.request,
        )
    ]

    def call_then_answer() -> FakeLlmClient:
        return FakeLlmClient(
            [
                LlmResponse(
                    text="",
                    tool_calls=[ToolCall(call_id="c1", name="request_show_scope_for_user", arguments_json="{}")],
                    native_turn=[],
                ),
                LlmResponse(text="I put that in.", tool_calls=[], native_turn=[]),
            ]
        )

    prowl = FakeProwl({"ok": False, "error": "missing_api_key"})
    agent = ConciergeAgent(
        ToolBridge(OneToolToolkit(), specs, after_call=None),
        ombi_continue_url="http://ombi.example",
        llm_client=call_then_answer(),
        admin_label="Ben",
        prowl=prowl,
        movie_direct_source_enabled=False,
    )
    _reply, calls = await agent.respond(USER, ConversationState(user_id=USER.user_id), "add altered carbon")

    assert calls[0].result.get("admin_alert_error") == "missing_api_key"
    assert "admin_alert_sent" not in calls[0].result

    # Same failure again, immediately: it must try again rather than sit on a cooldown
    # started by a notice that was never delivered.
    agent.client = call_then_answer()
    await agent.respond(USER, ConversationState(user_id=USER.user_id), "add altered carbon")
    assert len(prowl.calls) == 2


@pytest.mark.asyncio
async def test_send_admin_alert_reports_whether_it_actually_sent():
    assert await agent_with(prowl=None)._send_admin_alert("E", "s") == "prowl_unavailable"
    assert await agent_with(prowl=FakeProwl({"ok": True}))._send_admin_alert("E", "s") is None
    failed = await agent_with(prowl=FakeProwl({"ok": False, "error": "missing_api_key"}))._send_admin_alert("E", "s")
    assert failed == "missing_api_key"
    raised = await agent_with(prowl=FakeProwl(RuntimeError("network down")))._send_admin_alert("E", "s")
    assert raised == "network down"


# -- 5. Shabbos Mode has no model to escalate for it ----------------------------


@pytest.fixture
def shabbos_router(tmp_path):
    settings = Settings(
        ombi_base_url="http://127.0.0.1:1",
        plex_base_url="http://127.0.0.1:1",
        prowl_api_key=None,
        friendly_names_path=str(tmp_path / "friendlynames.json"),
    )
    store = ConversationStore(f"sqlite:///{tmp_path}/shabbos.db")
    return ShabbosRouter(settings, store, AuditLogger(path="/dev/null"), USER)


@pytest.mark.asyncio
async def test_shabbos_request_failure_pages_the_admin(shabbos_router):
    sent: list[dict] = []

    async def fake_request(username: str, tvdb_id: int, scope: str) -> dict:
        return await unconfirmed_result()

    async def fake_notice(summary: str, priority: int = 0, event: str = "Concierge Alert") -> dict:
        sent.append({"summary": summary, "priority": priority, "event": event})
        return {"ok": True}

    shabbos_router.toolkit.requests.request_show_scope_for_user = fake_request
    shabbos_router.toolkit.escalation.send_admin_prowl_notice = fake_notice

    reply = await shabbos_router.handle(
        ConversationState(user_id=USER.user_id),
        f"/request tvdb:{ALTERED_CARBON_TVDB} --all",
    )

    assert sent, "a Shabbos request failure must still reach the admin"
    assert sent[0]["event"] == "Request Unconfirmed"
    assert f"TVDB {ALTERED_CARBON_TVDB}" in sent[0]["summary"]
    assert "The admin has been notified." in reply
    assert_user_safe(reply.replace("The admin has been notified.", ""))
