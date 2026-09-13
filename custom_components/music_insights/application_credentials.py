"""Application Credentials platform for Music Insights (Spotify OAuth2)."""
from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import SPOTIFY_AUTHORIZE_URL, SPOTIFY_TOKEN_URL


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the Spotify OAuth2 authorization server endpoints."""
    return AuthorizationServer(
        authorize_url=SPOTIFY_AUTHORIZE_URL,
        token_url=SPOTIFY_TOKEN_URL,
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Return placeholders shown in the Application Credentials setup UI."""
    return {
        "developer_dashboard_url": "https://developer.spotify.com/dashboard",
        "redirect_url": "https://my.home-assistant.io/redirect/oauth",
    }
