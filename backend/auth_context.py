from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Protocol

from fastapi import Depends, Header, HTTPException, Request

from backend.auth_store import PlexAuthSessionStore
from backend.config import Settings, get_settings

from backend.models import UserContext


class UserContextProvider(Protocol):
    async def resolve_user(
        self,
        request: Request | None,
        x_plex_user: str | None,
        x_plex_user_id: str | None,
        x_plex_display_name: str | None,
        x_plex_is_admin: str | None,
    ) -> UserContext: ...


class FriendlyNameDirectory:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self._cached: dict[str, str] | None = None
        self._excluded: set[str] | None = None
        self._cached_mtime: float | None = None

    def resolve(self, username: str | None, display_name: str | None = None) -> str:
        if not username:
            return display_name.strip() if display_name and display_name.strip() else "Unknown"

        mapping = self._load_mapping()
        normalized_username = username.strip()
        excluded = self._excluded or set()
        if normalized_username.lower() in {item.lower() for item in excluded}:
            return display_name.strip() if display_name and display_name.strip() else normalized_username

        if normalized_username in mapping:
            return mapping[normalized_username]

        lowered = normalized_username.lower()
        for key, value in mapping.items():
            if key.lower() == lowered:
                return value

        return display_name.strip() if display_name and display_name.strip() else normalized_username

    def summarize_aliases(self, max_names: int = 20) -> str:
        mapping = self._load_mapping()
        reverse: dict[str, list[str]] = {}
        for username, friendly_name in mapping.items():
            reverse.setdefault(friendly_name, []).append(username)

        lines: list[str] = []
        for friendly_name in sorted(reverse):
            aliases = sorted(reverse[friendly_name])
            if not aliases:
                continue
            alias_list = ", ".join(aliases[:max_names])
            if len(aliases) > max_names:
                alias_list = f"{alias_list}, ..."
            lines.append(f"- {friendly_name}: {alias_list}")
        return "\n".join(lines)

    def _load_mapping(self) -> dict[str, str]:
        if self.path is not None and self._cached is not None:
            try:
                current_mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                current_mtime = None
            if current_mtime is not None and self._cached_mtime == current_mtime:
                return self._cached
        elif self._cached is not None:
            return self._cached

        self._cached = {}
        self._excluded = set()
        self._cached_mtime = None
        if self.path is None:
            return self._cached

        try:
            self._cached_mtime = self.path.stat().st_mtime
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._cached
        except Exception:
            return self._cached

        if isinstance(payload, dict):
            raw_map = payload.get("USER_FRIENDLY_NAMES")
            if isinstance(raw_map, dict):
                self._cached = {
                    str(key): str(value)
                    for key, value in raw_map.items()
                    if key is not None and value is not None
                }
            raw_excluded = payload.get("EXCLUDED_USERS")
            if isinstance(raw_excluded, list):
                self._excluded = {
                    str(item).strip()
                    for item in raw_excluded
                    if str(item).strip()
                }
        return self._cached


class DevUserContextProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.impersonation_store = Path(settings.dev_impersonation_store)
        self.friendly_names = FriendlyNameDirectory(settings.friendly_names_path)
        self._cached_override: dict[str, str] | None = None

    async def resolve_user(
        self,
        request: Request | None,
        x_plex_user: str | None,
        x_plex_user_id: str | None,
        x_plex_display_name: str | None,
        x_plex_is_admin: str | None,
    ) -> UserContext:
        override = self._load_override()
        user_id = override.get("user_id") or self.settings.dev_impersonate_user_id or self.settings.dev_user_id
        username = override.get("username") or self.settings.dev_impersonate_username or self.settings.dev_username
        display_name = override.get("display_name") or self.settings.dev_impersonate_display_name or self.settings.dev_display_name
        if override.get("is_admin") is not None:
            is_admin = self._coerce_bool(override.get("is_admin")) or False
        elif self.settings.dev_impersonate_is_admin is not None:
            is_admin = self.settings.dev_impersonate_is_admin
        else:
            is_admin = self.settings.dev_user_is_admin
        is_admin = is_admin or self.settings.is_admin_identity(x_plex_user_id)
        return UserContext(
            user_id=user_id,
            username=username,
            display_name=self.friendly_names.resolve(username, display_name),
            is_admin=is_admin,
            auth_source="dev-single-user",
        )

    def set_override(self, user_id: str, username: str, display_name: str, is_admin: bool) -> None:
        self._cached_override = {
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "is_admin": str(bool(is_admin)).lower(),
        }
        self.impersonation_store.write_text(json.dumps(self._cached_override), encoding="utf-8")

    def _load_override(self) -> dict[str, str]:
        if self._cached_override is not None:
            return self._cached_override
        try:
            raw = self.impersonation_store.read_text(encoding="utf-8")
            payload = json.loads(raw)
            if isinstance(payload, dict):
                self._cached_override = {
                    key: str(value) for key, value in payload.items() if value is not None
                }
                return self._cached_override
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self._cached_override = {}
        return self._cached_override

    def _coerce_bool(self, value: str | None) -> bool:
        return str(value or "").lower() in {"1", "true", "yes", "on"}


class OmbiSessionUserContextProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.friendly_names = FriendlyNameDirectory(settings.friendly_names_path)

    async def resolve_user(
        self,
        request: Request | None,
        x_plex_user: str | None,
        x_plex_user_id: str | None,
        x_plex_display_name: str | None,
        x_plex_is_admin: str | None,
    ) -> UserContext:
        # Placeholder for trusted Ombi session validation or a server-side signed token.
        if not x_plex_user or not x_plex_user_id:
            raise HTTPException(status_code=401, detail="Missing trusted auth context")

        return UserContext(
            user_id=x_plex_user_id,
            username=x_plex_user,
            display_name=self.friendly_names.resolve(x_plex_user, x_plex_display_name),
            is_admin=(x_plex_is_admin or "").lower() in {"1", "true", "yes"} or self.settings.is_admin_identity(x_plex_user_id),
            auth_source="ombi-session-placeholder",
        )


class PlexOAuthUserContextProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session_store = PlexAuthSessionStore(settings.database_url)
        self.friendly_names = FriendlyNameDirectory(settings.friendly_names_path)

    async def resolve_user(
        self,
        request: Request | None,
        x_plex_user: str | None,
        x_plex_user_id: str | None,
        x_plex_display_name: str | None,
        x_plex_is_admin: str | None,
    ) -> UserContext:
        if request is None:
            raise HTTPException(status_code=401, detail="Missing auth request context")

        session_id = self._verify_cookie(request.cookies.get("plexorcist_session"))
        if not session_id:
            raise HTTPException(status_code=401, detail="Not signed in with Plex")

        session = self.session_store.get(str(session_id))
        if session is None:
            raise HTTPException(status_code=401, detail="Not signed in with Plex")

        return UserContext(
            user_id=session.user_id,
            username=session.username,
            display_name=self.friendly_names.resolve(session.username, session.display_name),
            is_admin=session.is_admin or self.settings.is_admin_identity(session.user_id),
            auth_source=session.auth_source,
        )

    def sign_cookie(self, value: str) -> str:
        signature = hmac.new(
            self.settings.session_secret_key.encode("utf-8"),
            value.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        encoded = base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
        return f"{value}.{encoded}"

    def _verify_cookie(self, cookie_value: str | None) -> str | None:
        if not cookie_value or "." not in cookie_value:
            return None
        value, signature = cookie_value.rsplit(".", 1)
        expected = self.sign_cookie(value).rsplit(".", 1)[1]
        if not hmac.compare_digest(signature, expected):
            return None
        return value


def get_user_context_provider(settings: Settings = Depends(get_settings)) -> UserContextProvider:
    if settings.is_dev_impersonation_mode():
        return DevUserContextProvider(settings)
    if settings.is_plex_oauth_mode():
        return PlexOAuthUserContextProvider(settings)
    return OmbiSessionUserContextProvider(settings)


async def get_user_context(
    request: Request,
    x_plex_user: str | None = Header(default=None),
    x_plex_user_id: str | None = Header(default=None),
    x_plex_display_name: str | None = Header(default=None),
    x_plex_is_admin: str | None = Header(default=None),
    provider: UserContextProvider = Depends(get_user_context_provider),
) -> UserContext:
    return await provider.resolve_user(
        request=request,
        x_plex_user=x_plex_user,
        x_plex_user_id=x_plex_user_id,
        x_plex_display_name=x_plex_display_name,
        x_plex_is_admin=x_plex_is_admin,
    )


async def get_optional_user_context(request: Request, settings: Settings) -> UserContext | None:
    provider = get_user_context_provider(settings)
    try:
        return await provider.resolve_user(
            request=request,
            x_plex_user=None,
            x_plex_user_id=None,
            x_plex_display_name=None,
            x_plex_is_admin=None,
        )
    except HTTPException:
        return None
