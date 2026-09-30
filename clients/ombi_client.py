import httpx
import re
from urllib.parse import quote

from clients.base import BaseHttpClient


class OmbiClient(BaseHttpClient):
    # Marks a get_tv_detail payload that came back from the TheMovieDb-keyed endpoint
    # while we were holding a TheTVDB id, i.e. a different show entirely.
    DETAIL_NAMESPACE_MISMATCH = "_id_namespace_mismatch"

    # Ombi states that mean "the request exists and has not been refused".
    REQUEST_LANDED_STATES = frozenset(
        {"requested", "approved", "available", "fully_available", "partly_available"}
    )

    async def find_user_by_identity(self, username: str, user_id: str | None = None) -> dict:
        normalized_username = self._normalize_text(username or "")
        normalized_user_id = str(user_id or "").strip().lower()
        if not normalized_username:
            return {"ok": False, "exists": False, "reason": "missing_username"}

        try:
            payload = await self.get_json("/api/v1/Identity/Users")
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "exists": False,
                "reason": "ombi_users_unreachable",
                "error": self._describe_http_error(exc),
            }

        users = payload if isinstance(payload, list) else []

        if normalized_user_id:
            for user in users:
                if not isinstance(user, dict):
                    continue
                claims = user.get("claims")
                if isinstance(claims, list):
                    for claim in claims:
                        if not isinstance(claim, dict):
                            continue
                        value = str(claim.get("value") or "").strip().lower()
                        if value and value == normalized_user_id:
                            return {
                                "ok": True,
                                "exists": True,
                                "matched_on": "user_id_claim",
                                "user": user,
                            }

        for user in users:
            if not isinstance(user, dict):
                continue
            candidate_username = self._normalize_text(str(user.get("userName") or user.get("username") or ""))
            candidate_alias = self._normalize_text(str(user.get("alias") or ""))
            if normalized_username and normalized_username in {candidate_username, candidate_alias}:
                return {
                    "ok": True,
                    "exists": True,
                    "matched_on": "username",
                    "user": user,
                }

        return {"ok": True, "exists": False, "matched_on": None}

    async def trigger_plex_user_importer(self) -> dict:
        # Ombi's native onboarding path: sweep the Plex server's shared users and
        # create Ombi accounts (userType 2, no password) for any that are missing.
        # Normally scheduled daily; we trigger it on demand so a just-shared user
        # gets an account immediately instead of waiting for the cron run.
        try:
            await self.post_json("/api/v1/Job/plexuserimporter", {})
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "reason": "importer_trigger_failed",
                "error": self._describe_http_error(exc),
            }
        return {"ok": True}

    def _user_headers(self, username: str) -> dict[str, str]:
        # Ombi attributes API-key calls to a user via UserName.
        return {"UserName": username}

    async def search_media(self, query: str) -> dict:
        effective_query = query
        attempted_queries = [query]
        payload = await self._search_multi(query)
        if not payload:
            payload = await self._search_fallback(query)
        broader_query = self._strip_year(query)
        if not payload and broader_query and broader_query != query:
            attempted_queries.append(broader_query)
            payload = await self._search_multi(broader_query)
            if not payload:
                payload = await self._search_fallback(broader_query)
            if payload:
                effective_query = broader_query
        if not isinstance(payload, list):
            return {"query": query, "effective_query": effective_query, "attempted_queries": attempted_queries, "results": [], "source": "ombi"}
        payload = self._rank_results(query, payload)
        return {
            "query": query,
            "effective_query": effective_query,
            "attempted_queries": attempted_queries,
            "results": [self._normalize_search_item(item) for item in payload],
            "source": "ombi",
        }

    async def request_movie_for_user(
        self,
        username: str,
        tmdb_id: int | None = None,
        title: str | None = None,
        year: int | None = None,
    ) -> dict:
        normalized_title = str(title or "").strip()
        safe_year = self._safe_int(year)
        if normalized_title and safe_year:
            resolved = await self._resolve_movie_tmdb_id_by_title_year(normalized_title, safe_year)
            if not resolved.get("ok"):
                resolved.update({"username": username, "title": normalized_title, "year": safe_year})
                return resolved
            tmdb_id = self._safe_int(resolved.get("tmdb_id"))

        safe_tmdb_id = self._safe_int(tmdb_id)
        if not safe_tmdb_id or safe_tmdb_id <= 0:
            return {
                "ok": False,
                "username": username,
                "tmdb_id": tmdb_id,
                "title": normalized_title or None,
                "year": safe_year,
                "status": "missing_movie_identifier",
                "reason": "movie_requests_require_title_and_year_or_positive_tmdb_id",
                "user_summary": "I need either a TMDB ID or both the movie title and year before I can request that safely.",
            }
        tmdb_id = safe_tmdb_id
        detail = await self.get_movie_detail(tmdb_id)
        gate = self._gate_request(detail)
        if gate is not None:
            return {
                "ok": False,
                "username": username,
                "tmdb_id": tmdb_id,
                "status": gate,
                "title": detail.get("title"),
                "ombi": detail,
            }
        payload = {
            "theMovieDbId": tmdb_id,
            "is4kRequest": False,
        }
        try:
            result = await self.post_json(
                "/api/v1/Request/movie",
                payload,
                headers=self._user_headers(username),
            )
        except httpx.HTTPError as exc:
            reconciled = await self._reconcile_movie_request_failure(
                query=detail.get("title") or str(tmdb_id),
                tmdb_id=tmdb_id,
            )
            if reconciled is not None:
                return self._merge_request_context(
                    reconciled,
                    {
                        "username": username,
                        "tmdb_id": tmdb_id,
                        "title": detail.get("title"),
                        "ombi_detail": detail,
                    },
                )
            failed_context: dict[str, object] = {
                "username": username,
                "tmdb_id": tmdb_id,
                "title": detail.get("title"),
            }
            return {
                "ok": False,
                "status": "error",
                "error": self._describe_http_error(exc),
                "ombi_detail": detail,
                **failed_context,
                "user_summary": self._request_failure_summary(failed_context),
            }
        context: dict[str, object] = {
            "username": username,
            "tmdb_id": tmdb_id,
            "title": detail.get("title"),
            "ombi_detail": detail,
        }
        normalized = self._normalize_request_engine_result(
            result=result,
            success_status="requested",
            error_context=context,
        )
        if normalized.get("status") == "unconfirmed":
            reconciled = await self._reconcile_movie_request_failure(
                query=detail.get("title") or str(tmdb_id),
                tmdb_id=tmdb_id,
            )
            if reconciled is not None:
                return self._merge_request_context(reconciled, context)
        return normalized

    async def _resolve_movie_tmdb_id_by_title_year(self, title: str, year: int) -> dict:
        query = f"{title} ({year})"
        search = await self.search_media(query)
        matches: list[dict] = []
        candidates: list[dict] = []
        normalized_title = self._normalize_text(title)
        for item in search.get("results", []):
            if item.get("type") != "movie":
                continue
            tmdb_id = self._safe_int(item.get("tmdb_id"))
            if not tmdb_id or tmdb_id <= 0:
                continue
            item_title = str(item.get("title") or "").strip()
            raw = item.get("raw") or {}
            item_year = self._extract_year(
                str(raw.get("releaseDate") or raw.get("firstAired") or raw.get("title") or item_title)
            )
            if item_year is None:
                detail = await self.get_movie_detail(tmdb_id)
                item_year = self._extract_year(
                    str(detail.get("releaseDate") or detail.get("digitalRelease") or detail.get("physicalRelease") or "")
                )
            candidate = {"title": item_title, "year": item_year, "tmdb_id": tmdb_id}
            candidates.append(candidate)
            if self._normalize_text(item_title) == normalized_title and item_year == year:
                matches.append(candidate)

        if len(matches) == 1:
            return {
                "ok": True,
                "status": "resolved",
                "query": query,
                "title": matches[0]["title"],
                "year": matches[0]["year"],
                "tmdb_id": matches[0]["tmdb_id"],
            }
        if len(matches) > 1:
            return {
                "ok": False,
                "status": "ambiguous_movie",
                "reason": "multiple_exact_title_year_matches",
                "query": query,
                "candidates": matches[:5],
                "user_summary": f"I found multiple exact movie matches for {title} ({year}), so I need the TMDB ID.",
            }
        return {
            "ok": False,
            "status": "movie_not_found",
            "reason": "no_exact_title_year_match",
            "query": query,
            "candidates": candidates[:5],
            "user_summary": f"I could not find an exact movie match for {title} ({year}).",
        }

    async def request_show_scope_for_user(self, username: str, tvdb_id: int, scope: str) -> dict:
        safe_tvdb_id = self._safe_int(tvdb_id)
        if not safe_tvdb_id or safe_tvdb_id <= 0:
            return self._missing_show_identifier(username=username, tvdb_id=tvdb_id, scope=scope)
        tvdb_id = safe_tvdb_id
        detail, unverified_title = self._split_detail_namespace(await self.get_tv_detail(tvdb_id))
        if scope == "full_series":
            gate = self._gate_request(detail, tv_scope=scope)
            if gate is not None:
                return {
                    "ok": False,
                    "username": username,
                    "tvdb_id": tvdb_id,
                    "scope": scope,
                    "status": gate,
                    "title": detail.get("title"),
                    "ombi": detail,
                }
        payload = self._build_tv_request_payload(detail, tvdb_id=tvdb_id, scope=scope)
        context: dict[str, object] = {
            "username": username,
            "tvdb_id": tvdb_id,
            "scope": scope,
            "title": detail.get("title"),
            "ombi_detail": detail,
            "request_payload": payload,
        }
        if unverified_title:
            # Ombi could not resolve this TVDB id and only answered from the TMDB id
            # space. Surfaced so the agent can say the id looks wrong instead of
            # confidently naming an unrelated show.
            context["unresolved_tvdb_id"] = True
            context["unverified_title"] = unverified_title
        try:
            result = await self.post_json(
                "/api/v1/Request/tv",
                payload,
                headers=self._user_headers(username),
            )
        except httpx.HTTPError as exc:
            reconciled = await self._reconcile_show_request_failure(
                query=detail.get("title") or str(tvdb_id),
                tvdb_id=tvdb_id,
            )
            if reconciled is not None:
                return self._merge_request_context(reconciled, context)
            return {
                "ok": False,
                "status": "error",
                "error": self._describe_http_error(exc),
                **context,
                "user_summary": self._request_failure_summary(context),
            }
        normalized = self._normalize_request_engine_result(
            result=result,
            success_status="requested",
            error_context=context,
        )
        if not normalized.get("ok"):
            reconciled = await self._reconcile_show_request_failure(
                query=detail.get("title") or str(tvdb_id),
                tvdb_id=tvdb_id,
            )
            if reconciled is not None:
                reconciled = self._merge_request_context(reconciled, context)
                return reconciled
        return normalized

    async def request_episode_for_user(self, username: str, tvdb_id: int, season: int, episode: int) -> dict:
        safe_tvdb_id = self._safe_int(tvdb_id)
        if not safe_tvdb_id or safe_tvdb_id <= 0:
            return self._missing_show_identifier(
                username=username,
                tvdb_id=tvdb_id,
                scope="episode",
                season=season,
                episode=episode,
            )
        tvdb_id = safe_tvdb_id
        detail, _unverified_title = self._split_detail_namespace(await self.get_tv_detail(tvdb_id))
        episode_state = self._find_episode(detail, season, episode)
        if episode_state is not None:
            gate = self._gate_episode_request(episode_state)
            if gate is not None:
                return {
                    "ok": False,
                    "username": username,
                    "tvdb_id": tvdb_id,
                    "season": season,
                    "episode": episode,
                    "status": gate,
                    "title": detail.get("title"),
                    "ombi": episode_state,
                }
        payload = {
            "tvDbId": tvdb_id,
            "requestAll": False,
            "latestSeason": False,
            "firstSeason": False,
            "seasons": [
                {
                    "seasonNumber": season,
                    "episodes": [{"episodeNumber": episode}],
                }
            ],
        }
        try:
            result = await self.post_json(
                "/api/v1/Request/tv",
                payload,
                headers=self._user_headers(username),
            )
        except httpx.HTTPError as exc:
            failed_context: dict[str, object] = {
                "username": username,
                "tvdb_id": tvdb_id,
                "season": season,
                "episode": episode,
                "title": detail.get("title"),
            }
            return {
                "ok": False,
                "status": "error",
                "error": self._describe_http_error(exc),
                "ombi_detail": detail,
                "request_payload": payload,
                **failed_context,
                "user_summary": self._request_failure_summary(failed_context),
            }
        context: dict[str, object] = {
            "username": username,
            "tvdb_id": tvdb_id,
            "season": season,
            "episode": episode,
            "title": detail.get("title"),
            "ombi_detail": detail,
            "request_payload": payload,
        }
        return self._normalize_request_engine_result(
            result=result,
            success_status="requested",
            error_context=context,
        )

    async def check_movie_request_status(
        self,
        query: str,
        username: str | None = None,
        tmdb_id: int | None = None,
    ) -> dict:
        try:
            payload = await self.get_json(f"/api/v1/Request/movie/search/{self._path_segment(query)}")
        except httpx.HTTPError as exc:
            return {
                "query": query,
                "username": username,
                "exists_in_ombi": False,
                "status": "error",
                "error": str(exc),
            }
        results = [item for item in (payload if isinstance(payload, list) else []) if isinstance(item, dict)]
        # Title-ranked, so results[0] is whatever sorted first, not what was asked for.
        # When a tmdb id is known, the whole list is searched for it: id-checking only
        # the first hit would call a request that landed but ranked second "missing".
        match: dict = {}
        matched_by = None
        safe_tmdb_id = self._safe_int(tmdb_id)
        if safe_tmdb_id:
            match = next((item for item in results if self._movie_tmdb_id(item) == safe_tmdb_id), {})
            matched_by = "tmdb_id" if match else None
        if not match and results:
            match = results[0]
            matched_by = "title_rank"
        return {
            "query": query,
            "username": username,
            "exists_in_ombi": bool(match),
            "matched_by": matched_by,
            "requested_tmdb_id": safe_tmdb_id,
            "status": self._extract_movie_request_record_status(match),
            "tmdb_id": self._movie_tmdb_id(match),
            "title": match.get("title") or query.title(),
            "raw": match,
        }

    def _movie_tmdb_id(self, item: dict) -> int | None:
        """Ombi spells this key both ways depending on which endpoint answered."""
        if not isinstance(item, dict):
            return None
        return self._safe_int(item.get("theMovieDbId") or item.get("themoviedbId"))

    async def check_show_request_status(
        self,
        query: str,
        username: str | None = None,
        tvdb_id: int | None = None,
    ) -> dict:
        try:
            search = await self.search_media(query)
        except httpx.HTTPError as exc:
            return {
                "query": query,
                "username": username,
                "exists_in_ombi": False,
                "status": "error",
                "error": str(exc),
            }
        shows = [item for item in search.get("results", []) if item.get("type") == "show"]
        match = {}
        matched_by = None
        if tvdb_id:
            match = next(
                (show for show in shows if self._safe_int(show.get("tvdb_id")) == tvdb_id),
                {},
            )
            matched_by = "tvdb_id" if match else None
        if not match and shows:
            match = shows[0]
            matched_by = "title_rank"
        detail = {}
        if match.get("tvdb_id"):
            try:
                detail = await self.get_tv_detail(int(match["tvdb_id"]))
            except httpx.HTTPError as exc:
                return {
                    "query": query,
                    "username": username,
                    "exists_in_ombi": bool(match),
                    "status": "error",
                    "title": match.get("title") or query.title(),
                    "error": str(exc),
                }
        return {
            "query": query,
            "effective_query": search.get("effective_query") or query,
            "attempted_queries": search.get("attempted_queries") or [query],
            "username": username,
            "exists_in_ombi": bool(match),
            "matched_by": matched_by,
            "requested_tvdb_id": tvdb_id,
            "status": self._extract_request_status(detail or match.get("raw") or {}),
            "tvdb_id": match.get("tvdb_id"),
            "title": detail.get("title") or match.get("title") or query.title(),
            "candidates": [
                self._summarize_show_candidate(item)
                for item in shows[:5]
                if item.get("tvdb_id")
            ],
            "raw": detail or match.get("raw") or {},
        }

    async def get_show_season_status(self, query: str, season: int | None = None) -> dict:
        existing = await self.check_existing_media_status(query)
        best_match = existing.get("best_match") or {}
        if best_match.get("type") != "show" or not best_match.get("tvdb_id"):
            return {
                "query": query,
                "season": season,
                "found": False,
                "reason": "show_not_found",
                "episodes": [],
            }

        detail = await self.get_tv_request_detail(
            tvdb_id=self._safe_int(best_match.get("tvdb_id")),
            title=best_match.get("title") or query,
        )
        if not detail:
            return {
                "query": query,
                "season": season,
                "found": False,
                "reason": "show_request_not_found",
                "episodes": [],
            }

        seasons = self._collect_season_requests(detail)
        episode_rows: list[dict] = []
        for season_row in seasons:
            season_number = self._safe_int(season_row.get("seasonNumber"))
            if season is not None and season_number != season:
                continue
            for episode in season_row.get("episodes") or []:
                episode_number = self._safe_int(episode.get("episodeNumber"))
                status = self._extract_episode_status(episode)
                episode_rows.append(
                    {
                        "season": season_number,
                        "episode": episode_number,
                        "title": episode.get("title") or episode.get("name"),
                        "air_date": episode.get("airDate") or episode.get("firstAired"),
                        "requested": bool(episode.get("requested")),
                        "available": bool(episode.get("available")),
                        "approved": bool(episode.get("approved")),
                        "request_status": episode.get("requestStatus") or episode.get("request_status"),
                        "status": status,
                    }
                )

        missing_episodes = [row for row in episode_rows if row.get("status") == "missing"]
        processing_episodes = [row for row in episode_rows if row.get("status") == "processing"]
        return {
            "query": query,
            "title": detail.get("title") or best_match.get("title"),
            "tvdb_id": best_match.get("tvdb_id"),
            "season": season,
            "found": True,
            "request_status": detail.get("requestStatus") or detail.get("request_status"),
            "available": bool(detail.get("available")),
            "partly_available": bool(detail.get("partlyAvailable")),
            "fully_available": bool(detail.get("fullyAvailable")),
            "episodes": episode_rows,
            "season_table": episode_rows,
            "missing_episodes": missing_episodes,
            "processing_episodes": processing_episodes,
        }

    async def get_movie_detail(self, tmdb_id: int) -> dict:
        payload = await self.get_json(f"/api/v2/Search/movie/{tmdb_id}")
        return payload if isinstance(payload, dict) else {}

    async def get_tv_detail(self, tvdb_id: int) -> dict:
        # Every caller here holds a TVDB id (it is what POST /api/v1/Request/tv wants).
        # /api/v2/Search/tv/moviedb/{id} is explicitly TheMovieDb-keyed, so handing it a
        # TVDB id resolves to whatever unrelated show owns that number on TMDB -- 53243
        # came back as "Cinta 7 Susun". Ask the unprefixed route first and treat a
        # moviedb answer as untrustworthy.
        #
        # Measured on prod 2026-08-04: BOTH routes answered 204 for 332331 (a TVDB id
        # that POST /api/v1/Request/tv accepts and resolves to Altered Carbon), so this
        # usually returns {} and the pre-flight gate simply does not fire. Ombi enforces
        # already-requested / already-available itself on the POST, which is what the
        # errorCode handling below is for. Unverified: whether the unprefixed route is
        # TVDB-keyed at all, or a second TMDB-keyed alias -- settle it by curling
        # /api/v2/Search/tv/53243 on the box and seeing whether it also says
        # "Cinta 7 Susun".
        payload = await self.get_json(f"/api/v2/Search/tv/{tvdb_id}")
        if isinstance(payload, dict) and payload:
            return payload
        fallback = await self.get_json(f"/api/v2/Search/tv/moviedb/{tvdb_id}")
        if isinstance(fallback, dict) and fallback:
            # Answered from the wrong id namespace, so its title, flags and episode
            # list describe some other show. Flagged so callers drop it instead of
            # gating on it or naming it to the user.
            return {**fallback, self.DETAIL_NAMESPACE_MISMATCH: True}
        return {}

    def _split_detail_namespace(self, detail: dict) -> tuple[dict, str | None]:
        """Return (usable detail, title we could not trust) for a get_tv_detail result."""
        if not isinstance(detail, dict):
            return {}, None
        if not detail.pop(self.DETAIL_NAMESPACE_MISMATCH, False):
            return detail, None
        return {}, detail.get("title") or None

    async def get_tv_request_detail(self, tvdb_id: int | None = None, title: str | None = None) -> dict:
        payload = await self.get_json("/api/v1/Request/tv")
        records = payload if isinstance(payload, list) else []
        if not records:
            return {}

        normalized_title = self._normalize_text(title or "")
        for record in records:
            if tvdb_id is not None and self._safe_int(record.get("tvDbId")) == tvdb_id:
                return record
            if normalized_title and self._normalize_text(str(record.get("title") or "")) == normalized_title:
                return record
        return {}

    async def check_existing_media_status(self, query: str) -> dict:
        search = await self.search_media(query)
        effective_query = query
        if not search.get("results"):
            broader_query = self._strip_year(query)
            if broader_query and broader_query != query:
                fallback = await self.search_media(broader_query)
                if fallback.get("results"):
                    search = fallback
                    effective_query = fallback.get("effective_query") or broader_query
        candidates: list[dict] = []
        for item in search.get("results", []):
            if item.get("type") == "movie" and item.get("tmdb_id"):
                raw = item.get("raw") or {}
                detail = raw if self._has_status_fields(raw) else await self.get_movie_detail(int(item["tmdb_id"]))
                request_lookup = {}
                if not detail.get("requested") and not detail.get("approved") and not detail.get("requestId"):
                    request_lookup = await self.check_movie_request_status(
                        query=detail.get("title") or item.get("title") or query,
                        tmdb_id=self._safe_int(item.get("tmdb_id")),
                    )
                # request_lookup is check_movie_request_status's envelope, not an Ombi
                # record: its verdict lives under "status"/"raw", so passing the whole
                # dict to a record extractor matched none of the flag keys and always
                # returned "missing" -- the lookup's own HTTP call was paid for and
                # then discarded. Only an id-matched hit may speak, because the search
                # behind it is title-ranked and would otherwise offer a stranger.
                matched = (
                    bool(request_lookup.get("exists_in_ombi"))
                    and request_lookup.get("matched_by") == "tmdb_id"
                )
                if matched:
                    request_status = str(request_lookup.get("status") or "missing")
                    request_id = (request_lookup.get("raw") or {}).get("requestId")
                else:
                    request_status = self._extract_request_status(detail)
                    request_id = detail.get("requestId")
                requested = bool(detail.get("requested")) or request_status in {"requested", "approved"}
                movie_year = self._extract_year(
                    str(
                        detail.get("releaseDate")
                        or detail.get("digitalRelease")
                        or detail.get("physicalRelease")
                        or raw.get("releaseDate")
                        or raw.get("firstAired")
                        or ""
                    )
                )
                candidates.append(
                    {
                        "title": detail.get("title") or item.get("title"),
                        "year": movie_year,
                        "type": "movie",
                        "tmdb_id": item.get("tmdb_id"),
                        "requested": requested,
                        "available": bool(detail.get("available")),
                        "partly_available": False,
                        "fully_available": bool(detail.get("available")),
                        "request_id": request_id,
                        "status": request_status,
                    }
                )
                continue

            if item.get("type") == "show" and item.get("tvdb_id"):
                raw = item.get("raw") or {}
                detail = raw if self._has_status_fields(raw) else await self.get_tv_detail(int(item["tvdb_id"]))
                request_lookup = {}
                if not detail.get("requested") and not detail.get("available") and not detail.get("requestId"):
                    request_lookup = await self.get_tv_request_detail(
                        tvdb_id=self._safe_int(item.get("tvdb_id")),
                        title=detail.get("title") or item.get("title") or query,
                    )
                request_status = self._extract_tv_status(request_lookup or detail)
                request_id = request_lookup.get("requestId") if request_lookup else detail.get("requestId")
                requested = (
                    bool(detail.get("requested"))
                    or bool(request_lookup)
                    or request_status in {"requested", "approved", "processing", "partly_available", "fully_available", "available"}
                )
                candidates.append(
                    {
                        "title": (request_lookup.get("title") if request_lookup else None) or detail.get("title") or item.get("title"),
                        "year": self._extract_year(str(detail.get("firstAired") or raw.get("firstAired") or "")),
                        "type": "show",
                        "tvdb_id": item.get("tvdb_id"),
                        "network": detail.get("network") or raw.get("network"),
                        "overview": detail.get("overview") or raw.get("overview"),
                        "requested": requested,
                        "available": bool(detail.get("available")),
                        "partly_available": bool(detail.get("partlyAvailable")),
                        "fully_available": bool(detail.get("fullyAvailable")),
                        "request_id": request_id,
                        "status": request_status,
                    }
                )
        base_query = self._normalize_text(self._strip_year(query))
        exact_matches = [
            candidate
            for candidate in candidates
            if self._normalize_text(candidate.get("title", "")) == base_query
        ]
        best_match = exact_matches[0] if exact_matches else (candidates[0] if candidates else None)
        return {
            "query": query,
            "effective_query": effective_query,
            "best_match": best_match,
            "exact_matches": exact_matches,
            "candidates": candidates,
        }

    def _normalize_search_item(self, item: dict) -> dict:
        media_type = self._infer_media_type(item)
        total_seasons = item.get("numberOfSeasons") or item.get("totalSeasons") or 1
        if media_type == "show" and item.get("seasonRequests"):
            total_seasons = len(item.get("seasonRequests") or []) or total_seasons
        child_requests = item.get("childRequests") or []
        episodes = 0
        for child in child_requests:
            season_requests = child.get("seasonRequests") or []
            for season in season_requests:
                episodes += len(season.get("episodes") or [])
        if media_type == "show" and not episodes:
            for season in item.get("seasonRequests") or []:
                episodes += len(season.get("episodes") or [])
        return {
            "title": item.get("title") or item.get("name") or "Unknown Title",
            "year": self._extract_year(str(item.get("releaseDate") or item.get("firstAired") or "")),
            "type": media_type,
            "tmdb_id": item.get("theMovieDbId") or item.get("movieDbId") or item.get("id"),
            "tvdb_id": (
                item.get("theMovieDbId")
                if media_type == "show"
                else item.get("tvDbId") or item.get("tvdbId") or self._safe_int(item.get("theTvDbId")) or item.get("seriesId")
            ),
            "seasons": total_seasons,
            "episodes": episodes,
            "is_ongoing": bool(item.get("status") in {"Returning Series", "Continuing"}),
            "raw": item,
        }

    def _missing_show_identifier(
        self,
        username: str,
        tvdb_id: object,
        scope: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict:
        payload: dict[str, object] = {
            "ok": False,
            "username": username,
            "tvdb_id": tvdb_id,
            "scope": scope,
            "status": "missing_show_identifier",
            "action": "show_identifier_required",
            "reason": "show_requests_require_positive_tvdb_id",
            "next_step": "resolve_show_candidate",
            # No service name and no id talk: the caller is supposed to resolve the show
            # and retry rather than hand the user an internal identifier to debug.
            "user_summary": "I do not have a solid match for that show yet, so nothing was requested.",
        }
        if season is not None:
            payload["season"] = season
        if episode is not None:
            payload["episode"] = episode
        return payload

    def _summarize_show_candidate(self, item: dict) -> dict:
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        return {
            "title": item.get("title"),
            "year": item.get("year") or self._extract_year(str(raw.get("firstAired") or "")),
            "type": item.get("type"),
            "tvdb_id": item.get("tvdb_id"),
            "network": raw.get("network"),
            "overview": raw.get("overview"),
            "requested": bool(raw.get("requested")),
            "available": bool(raw.get("available") or raw.get("fullyAvailable")),
        }

    def _extract_request_status(self, item: dict) -> str:
        if not item:
            return "missing"
        if item.get("partlyAvailable"):
            return "partly_available"
        if item.get("fullyAvailable"):
            return "fully_available"
        if item.get("available"):
            return "available"
        if item.get("denied"):
            return "denied"
        if item.get("requested"):
            return "requested"
        if item.get("approved"):
            return "approved"
        return item.get("requestStatus") or "missing"

    def _extract_movie_request_record_status(self, record: dict) -> str:
        """Status of a /api/v1/Request/movie/search record.

        The search-hit extractor above is the wrong tool for these. Presence in Ombi's
        request list already means the request exists, so the floor is "requested", not
        "missing" -- and a real record carries no `requested` key at all. Measured from
        prod: an approved one is {"approved": true, "available": false, "denied": false,
        "requestStatus": "Common.ProcessingRequest"}, with no `requested` anywhere. So a
        request that had landed but was still awaiting approval fell through to
        "Common.ProcessingRequest", which is in no landed state, and the reconcile called
        it unconfirmed -- the mirror of the false success this path already guards.
        Same reasoning as _extract_tv_request_record_status.
        """
        if not record:
            return "missing"
        if record.get("denied"):
            return "denied"
        if record.get("fullyAvailable"):
            return "fully_available"
        if record.get("partlyAvailable"):
            return "partly_available"
        if record.get("available"):
            return "available"
        if record.get("approved"):
            return "approved"
        return "requested"

    def _extract_episode_status(self, item: dict) -> str:
        request_status = str(item.get("requestStatus") or item.get("request_status") or "").lower()
        if item.get("denied"):
            return "denied"
        if item.get("available") or "available" in request_status:
            return "available"
        if "processing" in request_status:
            return "processing"
        if item.get("requested"):
            return "requested"
        if item.get("approved"):
            return "approved"
        return "missing"

    def _extract_tv_status(self, item: dict) -> str:
        if not item:
            return "missing"
        request_status = str(item.get("requestStatus") or item.get("request_status") or "").lower()
        if item.get("fullyAvailable"):
            return "fully_available"
        if item.get("partlyAvailable"):
            return "partly_available"
        if item.get("available"):
            return "available"
        if item.get("denied"):
            return "denied"
        if "processing" in request_status:
            return "processing"
        if item.get("requested"):
            return "requested"
        if item.get("approved"):
            return "approved"
        return "missing"

    def _describe_http_error(self, exc: httpx.HTTPError) -> dict[str, object]:
        description: dict[str, object] = {"message": str(exc)}
        if isinstance(exc, httpx.HTTPStatusError):
            description["http_status"] = exc.response.status_code
            description["reason"] = exc.response.reason_phrase
            try:
                response_text = exc.response.text.strip()
            except Exception:
                response_text = ""
            if response_text:
                description["response_text"] = response_text[:2000]
        return description

    def _build_tv_request_payload(self, detail: dict, tvdb_id: int, scope: str) -> dict:
        seasons = self._build_request_seasons(detail, scope)
        payload: dict[str, object] = {
            "tvDbId": tvdb_id,
            "requestAll": scope == "full_series",
            "latestSeason": scope == "latest_season",
            "firstSeason": scope == "first_season",
            "seasons": seasons,
        }
        language_profile = self._safe_int(detail.get("languageProfile") or detail.get("language_profile"))
        if language_profile is not None:
            payload["languageProfile"] = language_profile
        return payload

    async def _reconcile_movie_request_failure(
        self,
        query: str,
        tmdb_id: int | None = None,
    ) -> dict[str, object] | None:
        status = await self.check_movie_request_status(query=query, tmdb_id=tmdb_id)
        if not status.get("exists_in_ombi"):
            return None
        # Never let a title-ranked hit vouch for an id we did not ask about -- the same
        # false success the TV path was hardened against, which answered a question
        # about Altered Carbon with Cinta 7 Susun.
        safe_tmdb_id = self._safe_int(tmdb_id)
        if safe_tmdb_id and self._safe_int(status.get("tmdb_id")) != safe_tmdb_id:
            return None
        request_state = status.get("status")
        if request_state in self.REQUEST_LANDED_STATES:
            return {
                "ok": True,
                "status": request_state,
                "title": status.get("title"),
                "tmdb_id": status.get("tmdb_id"),
                "ombi": status.get("raw") or {},
                "request_reconciled": True,
                "reconciled_by": status.get("matched_by") or "title_search",
            }
        return None

    async def _reconcile_show_request_failure(
        self,
        query: str,
        tvdb_id: int | None = None,
    ) -> dict[str, object] | None:
        # Ombi's own request list is the authoritative "did a request get created for
        # this id". The search index is not: it is title-ranked, so it used to hand
        # back whatever show sorted first and answer a question about Altered Carbon
        # with Cinta 7 Susun.
        if tvdb_id:
            try:
                record = await self.get_tv_request_detail(tvdb_id=tvdb_id)
            except httpx.HTTPError:
                record = {}
            if record:
                title = record.get("title") or query
                request_state = self._extract_tv_request_record_status(record)
                if request_state == "denied":
                    return {
                        "ok": False,
                        "status": "denied",
                        "title": title,
                        "tvdb_id": tvdb_id,
                        "ombi": record,
                        "request_reconciled": True,
                        "reconciled_by": "tvdb_id",
                        "user_summary": f"{title} is in Ombi, but the request was denied.",
                    }
                return {
                    "ok": True,
                    "status": request_state,
                    "title": title,
                    "tvdb_id": tvdb_id,
                    "ombi": record,
                    "request_reconciled": True,
                    "reconciled_by": "tvdb_id",
                    "user_summary": f"{title} is requested in Ombi.",
                }

        status = await self.check_show_request_status(query=query, tvdb_id=tvdb_id)
        request_state = status.get("status")
        if not status.get("exists_in_ombi"):
            return None
        # Never let a title-ranked hit vouch for an id we did not ask about.
        if tvdb_id and self._safe_int(status.get("tvdb_id")) != tvdb_id:
            return None
        if request_state in self.REQUEST_LANDED_STATES:
            title = status.get("title") or query
            return {
                "ok": True,
                "status": request_state,
                "title": title,
                "tvdb_id": status.get("tvdb_id"),
                "ombi": status.get("raw") or {},
                "request_reconciled": True,
                "reconciled_by": "title_search",
                "user_summary": f"{title} is now requested in Ombi.",
            }
        return None

    def _merge_request_context(
        self,
        reconciled: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        """Attach request context to a reconciled result without clobbering its findings.

        A reconciled record came from Ombi's own request list, so its title is the real
        one; context["title"] is whatever the (often empty) search detail knew, and
        blindly assigning it used to blank out a title we had just confirmed.
        """
        merged = dict(reconciled)
        for key, value in context.items():
            if key in {"title", "tvdb_id", "tmdb_id"} and merged.get(key):
                continue
            merged[key] = value
        return merged

    def _extract_tv_request_record_status(self, record: dict) -> str:
        """Status of a /api/v1/Request/tv record.

        Unlike a search hit, presence in that list already means the request exists,
        so the floor here is "requested" rather than "missing". Availability and
        approval live on the child requests, not the parent.
        """
        children = [child for child in (record.get("childRequests") or []) if isinstance(child, dict)]
        scopes = children or [record]
        if any(scope.get("denied") for scope in scopes):
            return "denied"
        if record.get("fullyAvailable"):
            return "fully_available"
        if record.get("partlyAvailable"):
            return "partly_available"
        if record.get("available") or any(scope.get("available") for scope in scopes):
            return "available"
        if any(scope.get("approved") for scope in scopes):
            return "approved"
        return "requested"

    def _request_subject(self, context: dict[str, object]) -> str:
        """`" for <thing>"` when we can name the request honestly, else `""`.

        `title` comes from get_tv_detail(), which returns {} for most TVDB ids (both
        v2 search routes answer 204), so it is commonly absent here. Falling back to
        the raw id keeps the sentence specific; "Unknown title" or "that" reads like
        a second bug on top of the one being reported.
        """
        title = str(context.get("title") or "").strip()
        if title:
            return f" for {title}"
        tvdb_id = self._safe_int(context.get("tvdb_id"))
        if tvdb_id and tvdb_id > 0:
            return f" for TVDB {tvdb_id}"
        tmdb_id = self._safe_int(context.get("tmdb_id"))
        if tmdb_id and tmdb_id > 0:
            return f" for TMDB {tmdb_id}"
        return ""

    def _request_failure_summary(self, context: dict[str, object]) -> str:
        """User-facing prose for a request that did not go through.

        It never names Ombi. Users reach this app precisely because they have no Ombi
        access -- they do not know it exists and cannot go look at it -- so naming it,
        or telling them to check it, hands them a dead end. The real detail goes to the
        admin through the alert path instead.
        """
        return f"I could not put in the request{self._request_subject(context)} — nothing was added."

    def _normalize_request_engine_result(
        self,
        result: dict | object,
        success_status: str,
        error_context: dict[str, object],
    ) -> dict[str, object]:
        payload = result if isinstance(result, dict) else {}
        error_code = payload.get("errorCode")
        # `message` is Ombi's single human-readable field and it carries BOTH outcomes.
        # A movie POST that lands answers `{"result": true, "message": "<Title> (<year>)
        # has been successfully added!"}`, so reading it unconditionally made the success
        # sentence the error text: Ben was paged "the request failed against Ombi: The
        # Commitments (1991) has been successfully added!" for a movie that downloaded
        # fine. Only a non-success payload may speak through `message`, which keeps it
        # available to _map_request_error_status for the already-requested and permission
        # wording Ombi does not always duplicate into errorMessage.
        error_message = str(payload.get("errorMessage") or "")
        if not error_message.strip() and payload.get("result") is not True:
            error_message = str(payload.get("message") or "")
        # Ombi's RequestEngineResult derives isError from ErrorMessage being non-empty,
        # so a genuine failure always carries one of these three.
        if payload.get("isError") or error_message.strip() or error_code:
            status = self._map_request_error_status(error_code=error_code, error_message=error_message)
            normalized: dict[str, object] = {
                "ok": status in {"already_requested", "already_available"},
                "status": status,
                "error": {
                    "message": error_message,
                    "error_code": error_code,
                },
                "ombi": payload,
                **error_context,
            }
            if not normalized["ok"]:
                normalized["user_summary"] = self._request_failure_summary(error_context)
            return normalized
        if payload.get("result") is False:
            # Observed in production: {"result": false, "isError": false,
            # "errorMessage": null, "requestId": 332331} for a request that DID land --
            # SickChill built the show seconds later. `result` on its own is not a
            # verdict, and this used to be reported to the user as "nothing was added".
            # Say so honestly; the caller reconciles against Ombi's request list, and an
            # unconfirmed request alerts the admin (backend/agent.py::_build_admin_alert),
            # because the user cannot chase it themselves.
            return {
                "ok": False,
                "status": "unconfirmed",
                "ombi": payload,
                **error_context,
                "user_summary": (
                    f"I sent the request{self._request_subject(error_context)}, but it did not come "
                    "back confirmed and it is not showing in the queue yet. It may still land — give "
                    "it a bit before asking for it again."
                ),
            }
        return {"ok": True, "status": success_status, "ombi": payload, **error_context}

    def _map_request_error_status(self, error_code: object, error_message: str) -> str:
        code = str(error_code or "").strip().lower()
        message = error_message.strip().lower()
        if code == "alreadyrequested" or "already been requested" in message:
            return "already_requested"
        if "already available" in message:
            return "already_available"
        if "correct permissions" in message or "permission" in message:
            return "permission_denied"
        return "error"

    def _build_request_seasons(self, detail: dict, scope: str) -> list[dict]:
        seasons = self._collect_season_requests(detail)
        if not seasons:
            return []

        if scope == "first_season":
            numbers = [
                number
                for number in (self._safe_int(season.get("seasonNumber")) for season in seasons)
                if number is not None
            ]
            # Season 0 is specials, and "just the first season" means season 1, so the
            # bare numeric minimum is the wrong pick. Latent guard only: in production
            # `seasons` has always been empty here (Ombi's v2 search returns
            # `seasonRequests: []`), so the payload carries `firstSeason: true` with
            # `seasons: []` and Ombi picks the season itself.
            regular = [number for number in numbers if number > 0]
            target = min(regular) if regular else min(numbers, default=None)
            seasons = [season for season in seasons if self._safe_int(season.get("seasonNumber")) == target]
        elif scope == "latest_season":
            target = max((self._safe_int(season.get("seasonNumber")) for season in seasons if self._safe_int(season.get("seasonNumber")) is not None), default=None)
            seasons = [season for season in seasons if self._safe_int(season.get("seasonNumber")) == target]

        request_seasons: list[dict] = []
        for season in seasons:
            season_number = self._safe_int(season.get("seasonNumber"))
            if season_number is None:
                continue
            episodes = []
            for episode in season.get("episodes") or []:
                episode_number = self._safe_int(episode.get("episodeNumber"))
                if episode_number is None:
                    continue
                episodes.append({"episodeNumber": episode_number})
            request_seasons.append({"seasonNumber": season_number, "episodes": episodes or None})
        return request_seasons

    async def _search_multi(self, query: str) -> list[dict]:
        payload = await self.post_json(f"/api/v2/Search/multi/{self._path_segment(query)}", {})
        return payload if isinstance(payload, list) else []

    async def _search_fallback(self, query: str) -> list[dict]:
        encoded_query = self._path_segment(query)
        movies = await self.get_json(f"/api/v1/Search/movie/{encoded_query}")
        shows = await self.get_json(f"/api/v1/Search/tv/{encoded_query}")
        merged: list[dict] = []
        if isinstance(movies, list):
            merged.extend(movies)
        if isinstance(shows, list):
            merged.extend(shows)
        return merged

    def _infer_media_type(self, item: dict) -> str:
        if item.get("mediaType") == "movie":
            return "movie"
        if item.get("mediaType") == "tv":
            return "show"
        if item.get("theMovieDbId") and not item.get("theTvDbId") and not item.get("seriesId"):
            return "movie"
        return "show"

    def _gate_request(self, item: dict, tv_scope: str | None = None) -> str | None:
        if not item:
            return None
        if item.get("available") or item.get("fullyAvailable"):
            return "already_available"
        if item.get("requested") and tv_scope != "first_season" and tv_scope != "latest_season":
            return "already_requested"
        if item.get("denied"):
            return "denied"
        return None

    def _gate_episode_request(self, item: dict) -> str | None:
        if item.get("available"):
            return "already_available"
        if item.get("requested"):
            return "already_requested"
        if item.get("denied"):
            return "denied"
        return None

    def _find_episode(self, tv_detail: dict, season_number: int, episode_number: int) -> dict | None:
        for season in self._collect_season_requests(tv_detail):
            if int(season.get("seasonNumber", -1)) != season_number:
                continue
            for episode in season.get("episodes") or []:
                if int(episode.get("episodeNumber", -1)) == episode_number:
                    return episode
        return None

    def _collect_season_requests(self, tv_detail: dict) -> list[dict]:
        seasons: list[dict] = []
        for child in tv_detail.get("childRequests") or []:
            seasons.extend(child.get("seasonRequests") or [])
        if not seasons:
            seasons.extend(tv_detail.get("seasonRequests") or [])
        return seasons

    @staticmethod
    def _path_segment(value: object) -> str:
        """Percent-encode user/media text before embedding it in an Ombi URL path."""
        return quote(str(value or ""), safe="")

    def _safe_int(self, value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _has_status_fields(self, item: dict) -> bool:
        return any(
            key in item
            for key in ("requested", "available", "partlyAvailable", "fullyAvailable", "requestStatus")
        )

    def _rank_results(self, query: str, items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda item: self._score_result(query, item), reverse=True)

    def _score_result(self, query: str, item: dict) -> tuple[int, int, int]:
        title = (item.get("title") or item.get("name") or "").strip()
        normalized_title = self._normalize_text(title)
        query_year = self._extract_year(query)
        normalized_query = self._normalize_text(query)
        base_query = self._normalize_text(self._strip_year(query))
        status_bonus = 0
        if item.get("fullyAvailable"):
            status_bonus += 50
        elif item.get("partlyAvailable"):
            status_bonus += 40
        elif item.get("available"):
            status_bonus += 30
        elif item.get("requested"):
            status_bonus += 20

        score = 0
        if base_query and normalized_title == base_query:
            score += 1000
        elif normalized_query and normalized_title == normalized_query:
            score += 950
        elif base_query and normalized_title.startswith(base_query):
            score += 800
        elif base_query and base_query in normalized_title:
            score += 600

        item_year = self._extract_year(item.get("firstAired") or item.get("releaseDate") or "")
        if query_year and item_year == query_year:
            score += 120

        if self._infer_media_type(item) == "show" and base_query and normalized_title == base_query:
            score += 25

        popularity = self._safe_int(item.get("popularity")) or 0
        return (score + status_bonus, popularity, len(title))

    def _normalize_text(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def _extract_year(self, value: str) -> int | None:
        match = re.search(r"(19|20)\d{2}", value or "")
        if not match:
            return None
        return int(match.group(0))

    def _strip_year(self, value: str) -> str:
        return re.sub(r"\b(19|20)\d{2}\b", "", value or "").strip()
