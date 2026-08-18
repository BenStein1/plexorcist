from __future__ import annotations

import httpx

from backend.state import ConversationStore
from clients.ombi_client import OmbiClient
from tools.error_helpers import classify_http_error, classify_service_result, service_action, user_error_summary


# Request statuses that mean "the user asked for something and did not get it".
# Everything else a request can return (requested, already_available,
# already_requested, account_not_ready, movie_not_found, ambiguous_movie) is a normal
# outcome the user can act on, and must not be reported as a fault.
_REQUEST_FAILURE_STATUSES = {"error", "unconfirmed", "missing_show_identifier"}


def _stamp_request_failure(result: dict, *, operation: str) -> dict:
    """Give a failed request the {service, operation, failure_type} shape the admin
    alerting path keys on.

    Without it, a request that never landed is visible only to the user -- who has no
    Ombi access and can do nothing about it -- while the admin hears nothing at all.
    """
    if not isinstance(result, dict) or result.get("ok") or result.get("service"):
        return result
    status = str(result.get("status") or "")
    if status not in _REQUEST_FAILURE_STATUSES:
        return result

    if status == "error":
        detail = result.get("error") if isinstance(result.get("error"), dict) else {}
        error = classify_service_result(
            service="ombi",
            operation=operation,
            reason=detail.get("message") or result.get("reason"),
        )
    elif status == "unconfirmed":
        error = {
            "service": "ombi",
            "operation": operation,
            "failure_type": "unconfirmed_request",
            "error_message": "request was neither confirmed nor listed afterwards",
        }
    else:
        error = {
            "service": "ombi",
            "operation": operation,
            "failure_type": "missing_identifier",
            "error_message": "no usable show id, nothing was sent",
        }
    return {**result, **error, "action": result.get("action") or service_action(error)}


class RequestTools:
    def __init__(
        self,
        ombi: OmbiClient,
        store: ConversationStore | None = None,
        user_id: str | None = None,
    ) -> None:
        self.ombi = ombi
        self.store = store
        self.user_id = user_id

    def _record_confirmed_request(
        self,
        result: dict,
        *,
        user_id: str | None,
        username: str,
        media_type: str,
        request_scope: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict:
        stamped = dict(result)
        if not (result.get("ok") is True and result.get("status") == "requested"):
            return stamped
        if self.store is None or not user_id:
            return {**stamped, "history_recorded": False, "history_reason": "request_history_store_unavailable"}

        ombi = result.get("ombi") if isinstance(result.get("ombi"), dict) else {}
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        source_request_id = (
            ombi.get("requestId")
            or ombi.get("id")
            or raw.get("requestId")
            or raw.get("id")
            or result.get("request_id")
        )
        detail = result.get("ombi_detail") if isinstance(result.get("ombi_detail"), dict) else {}
        year = result.get("year") or detail.get("year")
        try:
            stored = self.store.record_media_request(
                user_id=user_id,
                username=username,
                media_type=media_type,
                title=result.get("title") or detail.get("title"),
                year=int(year) if year is not None else None,
                tmdb_id=result.get("tmdb_id"),
                tvdb_id=result.get("tvdb_id"),
                request_scope=request_scope,
                season=season,
                episode=episode,
                source="ombi",
                source_request_id=source_request_id,
            )
        except Exception as exc:  # noqa: BLE001 - request success must not be reversed by ledger failure
            return {**stamped, "history_recorded": False, "history_reason": type(exc).__name__}
        return {
            **stamped,
            "history_recorded": stored.get("history_id") is not None,
            "history_created": bool(stored.get("recorded")),
            "history_id": stored.get("history_id"),
            "history_duplicate": bool(stored.get("duplicate")),
        }

    async def _account_not_ready(self, username: str) -> dict | None:
        """Guard for brand-new users: if their Ombi account hasn't finished
        provisioning yet (a just-made Plex share still importing), nudge the
        importer again and ask them to retry in a moment -- rather than
        misattributing or failing the request. Never blocks on a check hiccup."""
        try:
            check = await self.ombi.find_user_by_identity(username=username)
        except Exception:  # noqa: BLE001
            return None
        if check.get("ok") and not check.get("exists"):
            await self.ombi.trigger_plex_user_importer()
            return {
                "ok": False,
                "username": username,
                "status": "account_not_ready",
                "reason": "ombi_account_still_provisioning",
                "user_summary": (
                    "I'm still finishing setting up your account for requests — "
                    "give me a few seconds and try that again."
                ),
            }
        return None

    async def request_movie_for_user(
        self,
        username: str,
        tmdb_id: int | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> dict:
        not_ready = await self._account_not_ready(username)
        if not_ready is not None:
            return not_ready
        result = await self.ombi.request_movie_for_user(
            username=username,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
        )
        stamped = _stamp_request_failure(result, operation="movie_request")
        return self._record_confirmed_request(
            stamped,
            user_id=self.user_id,
            username=username,
            media_type="movie",
            request_scope="movie",
        )

    async def request_show_scope_for_user(self, username: str, tvdb_id: int, scope: str) -> dict:
        not_ready = await self._account_not_ready(username)
        if not_ready is not None:
            return not_ready
        result = await self.ombi.request_show_scope_for_user(username=username, tvdb_id=tvdb_id, scope=scope)
        stamped = _stamp_request_failure(result, operation="tv_request")
        return self._record_confirmed_request(
            stamped,
            user_id=self.user_id,
            username=username,
            media_type="show",
            request_scope=scope,
        )

    async def request_episode_for_user(
        self,
        username: str,
        tvdb_id: int,
        season: int,
        episode: int,
    ) -> dict:
        not_ready = await self._account_not_ready(username)
        if not_ready is not None:
            return not_ready
        result = await self.ombi.request_episode_for_user(
            username=username,
            tvdb_id=tvdb_id,
            season=season,
            episode=episode,
        )
        stamped = _stamp_request_failure(result, operation="episode_request")
        return self._record_confirmed_request(
            stamped,
            user_id=self.user_id,
            username=username,
            media_type="episode",
            request_scope="episode",
            season=season,
            episode=episode,
        )

    async def check_movie_request_status(self, query: str, username: str | None = None) -> dict:
        return await self.ombi.check_movie_request_status(query=query, username=username)

    async def check_show_request_status(self, query: str, username: str | None = None) -> dict:
        try:
            return await self.ombi.check_show_request_status(query=query, username=username)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="show_request_status", exc=exc)
            return {
                "ok": False,
                "query": query,
                "username": username,
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="Show request status",
                    error=error,
                    title=query,
                    change_status="No request status was returned.",
                ),
            }

    async def get_show_season_status(self, query: str, season: int | None = None) -> dict:
        try:
            return await self.ombi.get_show_season_status(query=query, season=season)
        except httpx.HTTPError as exc:
            error = classify_http_error(service="ombi", operation="tv_request_status", exc=exc)
            return {
                "ok": False,
                "query": query,
                "season": season,
                "found": False,
                "episodes": [],
                "action": service_action(error),
                "reason": str(exc),
                **error,
                "user_summary": user_error_summary(
                    tool_family="TV status check",
                    error=error,
                    title=query,
                    change_status="No season status was returned.",
                ),
            }
