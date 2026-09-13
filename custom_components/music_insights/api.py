"""Thin async Spotify Web API client used by MI-HA.

Token handling (refresh, storage) is entirely delegated to Home Assistant's
``config_entry_oauth2_flow.OAuth2Session`` via Application Credentials - this
module never touches tokens or files directly.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import SPOTIFY_API_BASE_URL

_LOGGER = logging.getLogger(__name__)


class SpotifyApiError(Exception):
    """Raised when the Spotify API returns an unexpected response."""


class SpotifyAuthError(SpotifyApiError):
    """Raised when the Spotify API rejects the current token (401/403)."""


class SpotifyRateLimitedError(SpotifyApiError):
    """Raised on HTTP 429; carries the Retry-After value in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class SpotifyClient:
    """Async wrapper around the Spotify Web API endpoints MI-HA needs."""

    def __init__(self, session: config_entry_oauth2_flow.OAuth2Session) -> None:
        self._session = session

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        await self._session.async_ensure_token_valid()
        headers = {
            "Authorization": f"Bearer {self._session.token['access_token']}",
        }

        http = async_get_clientsession(self._session.hass)
        url = f"{SPOTIFY_API_BASE_URL}{path}"

        async with http.request(method, url, headers=headers, params=params) as resp:
            if resp.status == 204:
                return None
            if resp.status == 401:
                raise SpotifyAuthError(f"Unauthorized calling {path}")
            if resp.status == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                raise SpotifyRateLimitedError(retry_after)
            if resp.status >= 400:
                body = await resp.text()
                raise SpotifyApiError(f"{resp.status} calling {path}: {body[:200]}")
            if resp.content_length == 0:
                return None
            return await resp.json()

    async def async_get_profile(self) -> dict[str, Any]:
        """Return the current user's Spotify profile (id, display_name, ...)."""
        data = await self._request("GET", "/me")
        return data or {}

    async def async_get_currently_playing(self) -> dict[str, Any] | None:
        """Return the currently-playing object, or None if nothing is playing."""
        return await self._request(
            "GET",
            "/me/player/currently-playing",
            params={"additional_types": "track"},
        )

    async def async_get_playback_state(self) -> dict[str, Any] | None:
        """Return the full playback state (device, shuffle, repeat, progress)."""
        return await self._request(
            "GET", "/me/player", params={"additional_types": "track"}
        )

    async def async_get_recently_played(
        self, after_ms: int | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Return recently played tracks, optionally only items after a cursor."""
        params: dict[str, Any] = {"limit": min(limit, 50)}
        if after_ms is not None:
            params["after"] = after_ms
        data = await self._request("GET", "/me/player/recently-played", params=params)
        return data or {"items": []}

    async def async_get_top_items(
        self, item_type: str, term: str, limit: int = 50
    ) -> dict[str, Any]:
        """Return top tracks or top artists for a given time range."""
        data = await self._request(
            "GET",
            f"/me/top/{item_type}",
            params={"time_range": term, "limit": min(limit, 50)},
        )
        return data or {"items": []}
