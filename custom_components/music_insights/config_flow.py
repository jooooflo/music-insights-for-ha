"""Config flow for Music Insights (MI-HA).

Provider selection is prepared for the future (multiple providers), but only
Spotify is implemented in v0.1. Authentication happens entirely through
Home Assistant's Application Credentials + OAuth2 flow - no manual token or
shell-script handling.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import config_entry_oauth2_flow

from .const import CONF_PROVIDER, DOMAIN, PROVIDER_SPOTIFY, SPOTIFY_SCOPE_STRING

_LOGGER = logging.getLogger(__name__)


class MusicInsightsConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle the OAuth2 config flow for Music Insights."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra data appended to the authorize URL (scopes)."""
        return {
            "scope": SPOTIFY_SCOPE_STRING,
            # Spotify returns a stale cached consent unless this is forced
            # the first time a user connects, which can silently omit scopes.
            "show_dialog": "true",
        }

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> Any:
        """Create the config entry once OAuth2 tokens have been obtained."""
        data[CONF_PROVIDER] = PROVIDER_SPOTIFY

        # Resolve the Spotify profile directly from the freshly issued token
        # so the entry gets a human-friendly title and a stable unique_id,
        # instead of asking the user to type an account id. The coordinator
        # performs the authoritative, refresh-aware account upsert later.
        account_id = ""
        display_name = "Spotify"
        try:
            profile = await _fetch_profile_with_raw_token(self.hass, data)
            account_id = profile.get("id", "")
            display_name = profile.get("display_name") or account_id or "Spotify"
        except Exception:  # noqa: BLE001 - best effort only, never block setup
            _LOGGER.debug("Could not pre-fetch Spotify profile during setup", exc_info=True)

        if account_id:
            await self.async_set_unique_id(f"{PROVIDER_SPOTIFY}:{account_id}")
            self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Spotify ({display_name})",
            data=data,
        )


async def _fetch_profile_with_raw_token(hass, data: dict[str, Any]) -> dict[str, Any]:
    """Best-effort direct call to /me using the freshly obtained token.

    Used only during config flow to build a friendly entry title / unique_id.
    The coordinator later uses the proper OAuth2Session-backed client.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .const import SPOTIFY_API_BASE_URL

    token = data.get("token", {})
    access_token = token.get("access_token")
    if not access_token:
        return {}

    http = async_get_clientsession(hass)
    async with http.get(
        f"{SPOTIFY_API_BASE_URL}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    ) as resp:
        if resp.status != 200:
            return {}
        return await resp.json()
