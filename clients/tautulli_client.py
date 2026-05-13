from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clients.base import BaseHttpClient


class TautulliClient(BaseHttpClient):
    async def get_users(self) -> list[dict]:
        users = await self._api("get_user_names")
        return users if isinstance(users, list) else []

    async def get_user_watch_context(self, user_id: str | None = None, username: str | None = None) -> dict:
        user = await self._resolve_user(user_id=user_id, username=username)
        if not user:
            return {
                "user_id": user_id,
                "username": username,
                "resolved": False,
                "recently_watched": [],
                "preferences": [],
                "watch_time_stats": [],
            }

        history = await self._api(
            "get_history",
            user_id=str(user["user_id"]),
            length="25",
            start="0",
        )
        year_start = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
        year_history = await self._api(
            "get_history",
            user_id=str(user["user_id"]),
            start_date=year_start,
            length="1000",
            start="0",
            order_column="date",
            order_dir="desc",
        )
        watch_time_stats = await self._api(
            "get_user_watch_time_stats",
            user_id=str(user["user_id"]),
            grouping="0",
        )
        home_stats = await self._api(
            "get_home_stats",
            time_range="30",
            stats_type="plays",
            stats_count="10",
        )
        recently_watched = self._build_recently_watched(self._extract_rows(history))
        year_history_summary = self._build_year_history_summary(self._extract_rows(year_history))
        top_media_30d = self._build_top_media(home_stats)
        return {
            "user_id": user_id,
            "username": username,
            "resolved": True,
            "tautulli_user_id": user["user_id"],
            "friendly_name": user.get("friendly_name"),
            "recently_watched": recently_watched,
            "year_history_summary": year_history_summary,
            "preferences": [],
            "watch_time_stats": watch_time_stats if isinstance(watch_time_stats, list) else [],
            "serverwide_top_picks_note": "Top picks are server-wide for the last 30 days, not user-personal.",
            "top_movies_30d_by_plays": top_media_30d["top_movies_by_plays"],
            "top_movies_30d_by_users": top_media_30d["top_movies_by_users"],
            "top_tv_30d_by_plays": top_media_30d["top_tv_by_plays"],
            "top_tv_30d_by_users": top_media_30d["top_tv_by_users"],
            "top_movies_30d": {
                "scope": "server_wide",
                "label": "Top Movies (server-wide, last 30 days)",
                "by_plays": top_media_30d["top_movies_by_plays"],
                "by_users": top_media_30d["top_movies_by_users"],
            },
            "top_tv_30d": {
                "scope": "server_wide",
                "label": "Top TV (server-wide, last 30 days)",
                "by_plays": top_media_30d["top_tv_by_plays"],
                "by_users": top_media_30d["top_tv_by_users"],
            },
        }

    async def _resolve_user(self, user_id: str | None = None, username: str | None = None) -> dict | None:
        users = await self.get_users()

        normalized_ids = {
            value.strip().lower()
            for value in (user_id, username)
            if value and value.strip()
        }
        for user in users:
            candidate_id = str(user.get("user_id", "")).strip().lower()
            candidate_name = str(user.get("friendly_name", "")).strip().lower()
            if normalized_ids.intersection({candidate_id, candidate_name}):
                return user
        return None

    def _build_recently_watched(self, history: object) -> list[dict]:
        if not isinstance(history, list):
            return []

        seen: set[str] = set()
        recent: list[dict] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            title = self._history_title(item)
            if not title:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            recent.append(
                {
                    "title": title,
                    "media_type": item.get("media_type"),
                    "year": item.get("year"),
                    "originally_available_at": item.get("originally_available_at"),
                    "rating_key": item.get("rating_key"),
                    "play_count": item.get("play_count"),
                }
            )
            if len(recent) >= 25:
                break
        return recent

    def _build_year_history_summary(self, history: object) -> list[dict]:
        if not isinstance(history, list):
            return []

        counts: dict[tuple[str, str], dict] = {}
        for item in history:
            if not isinstance(item, dict):
                continue
            title = self._history_title(item)
            if not title:
                continue
            media_type = str(item.get("media_type") or "").strip().lower() or None
            year = item.get("year") or self._guess_year(item)
            key = (title.lower(), media_type or "")
            if key not in counts:
                counts[key] = {
                    "title": title,
                    "media_type": media_type,
                    "year": year,
                    "play_count": 0,
                }
            counts[key]["play_count"] += int(item.get("group_count") or item.get("plays") or 1)

        ranked = sorted(
            counts.values(),
            key=lambda row: (row.get("play_count", 0), str(row.get("title") or "").lower()),
            reverse=True,
        )
        return ranked[:50]

    def _extract_rows(self, payload: object) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "rows", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    rows = self._extract_rows(value)
                    if rows:
                        return rows
            nested_response = payload.get("response")
            if isinstance(nested_response, dict):
                rows = self._extract_rows(nested_response)
                if rows:
                    return rows
        return []

    def _guess_year(self, item: dict) -> str | int | None:
        value = str(item.get("originally_available_at") or "").strip()
        if len(value) >= 4 and value[:4].isdigit():
            return value[:4]
        return None

    def _history_title(self, item: dict) -> str | None:
        title = str(
            item.get("full_title")
            or item.get("last_played")
            or item.get("title")
            or item.get("sort_title")
            or ""
        ).strip()
        if title:
            return title

        media_type = str(item.get("media_type") or "").strip().lower()
        if media_type == "episode":
            show = str(item.get("grandparent_title") or item.get("parent_title") or "").strip()
            season = item.get("parent_media_index")
            episode = item.get("media_index")
            if show and season is not None and episode is not None:
                return f"{show} S{int(season):02d}E{int(episode):02d}"
            if show:
                return show
        if media_type == "season":
            show = str(item.get("grandparent_title") or item.get("parent_title") or "").strip()
            season = item.get("media_index")
            if show and season is not None:
                return f"{show} Season {int(season)}"
            if show:
                return show
        if media_type in {"movie", "clip", "photo", "track"}:
            fallback = str(item.get("sort_title") or item.get("title") or "").strip()
            if fallback:
                return fallback
        return None

    def _build_top_media(self, home_stats: object) -> dict[str, list[dict]]:
        if not isinstance(home_stats, list):
            return {
                "top_movies_by_plays": [],
                "top_movies_by_users": [],
                "top_tv_by_plays": [],
                "top_tv_by_users": [],
            }

        top_movies_by_plays: list[dict] = []
        top_movies_by_users: list[dict] = []
        top_tv_by_plays: list[dict] = []
        top_tv_by_users: list[dict] = []
        for block in home_stats:
            if not isinstance(block, dict):
                continue
            stat_id = str(block.get("stat_id") or "").strip()
            rows = block.get("rows")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or row.get("full_title") or "").strip()
                if not title:
                    continue
                record = {
                    "title": title,
                    "media_type": row.get("media_type"),
                    "year": row.get("year"),
                    "rating_key": row.get("rating_key"),
                    "play_count": row.get("total_plays") or row.get("plays"),
                    "users_watched": row.get("users_watched"),
                }
                if stat_id == "top_movies":
                    top_movies_by_plays.append(record)
                elif stat_id == "popular_movies":
                    top_movies_by_users.append(record)
                elif stat_id == "top_tv":
                    top_tv_by_plays.append(record)
                elif stat_id == "popular_tv":
                    top_tv_by_users.append(record)
        return {
            "top_movies_by_plays": top_movies_by_plays[:10],
            "top_movies_by_users": top_movies_by_users[:10],
            "top_tv_by_plays": top_tv_by_plays[:10],
            "top_tv_by_users": top_tv_by_users[:10],
        }

    async def _api(self, cmd: str, **params: str) -> object:
        if not self.api_key:
            return None
        payload = await self.get_json(
            "/api/v2",
            params={
                "apikey": self.api_key,
                "cmd": cmd,
                **params,
            },
        )
        response = payload.get("response", {}) if isinstance(payload, dict) else {}
        return response.get("data")
