"""Music Insights for Home Assistant (MI-HA).

Long-term, provider-agnostic listening history and statistics. Spotify is
the first provider. See README.md for architecture and setup details.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow

from .api import SpotifyClient
from .backup import async_setup_snapshot_schedule
from .const import (
    BACKUP_DIR_NAME,
    CONF_PROVIDER,
    DB_FILE_NAME,
    DOMAIN,
    LEGACY_JSONL_FILENAME,
    PLATFORMS,
    SERVICE_CREATE_SNAPSHOT,
    SERVICE_EXPORT_DATA,
    SERVICE_IMPORT_LEGACY_JSONL,
    SERVICE_RUN_INTEGRITY_CHECK,
    STORAGE_DIR_NAME,
)
from .coordinator import (
    MusicInsightsPlaybackCoordinator,
    MusicInsightsRecentlyPlayedCoordinator,
    MusicInsightsTopItemsCoordinator,
)
from .storage import MusicInsightsStore

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class MusicInsightsRuntimeData:
    store: MusicInsightsStore
    client: SpotifyClient
    playback_coordinator: MusicInsightsPlaybackCoordinator
    recently_played_coordinator: MusicInsightsRecentlyPlayedCoordinator
    top_items_coordinator: MusicInsightsTopItemsCoordinator
    account_external_id: str


# Plain alias (not a PEP 695 generic) so this stays importable on the widest
# range of Python versions; type checkers still see the ConfigEntry shape
# via the "-> MusicInsightsConfigEntry" annotations below thanks to
# `from __future__ import annotations`.
MusicInsightsConfigEntry = ConfigEntry


def _store_paths(hass: HomeAssistant) -> tuple[Path, Path]:
    """Return (db_path, backup_dir), rooted at /config/music_insights/."""
    base_dir = Path(hass.config.path(STORAGE_DIR_NAME))
    return base_dir / DB_FILE_NAME, base_dir / BACKUP_DIR_NAME


async def async_setup_entry(hass: HomeAssistant, entry: MusicInsightsConfigEntry) -> bool:
    """Set up Music Insights from a config entry."""
    if entry.data.get(CONF_PROVIDER) != "spotify":
        _LOGGER.error("Unsupported provider in config entry: %s", entry.data.get(CONF_PROVIDER))
        return False

    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    client = SpotifyClient(session)

    db_path, backup_dir = _store_paths(hass)
    store = MusicInsightsStore(db_path, backup_dir)

    try:
        await hass.async_add_executor_job(store.open)
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"Could not open Music Insights database: {err}") from err

    integrity = await hass.async_add_executor_job(store.run_integrity_check)
    if not integrity["ok"]:
        _LOGGER.error(
            "Music Insights database failed integrity check on startup: %s", integrity
        )

    try:
        profile = await client.async_get_profile()
    except Exception as err:  # noqa: BLE001
        await hass.async_add_executor_job(store.close)
        raise ConfigEntryNotReady(f"Could not reach Spotify API: {err}") from err

    account_external_id = profile.get("id") or entry.unique_id or entry.entry_id
    await hass.async_add_executor_job(
        store.upsert_account, "spotify", account_external_id, profile.get("display_name")
    )

    playback_coordinator = MusicInsightsPlaybackCoordinator(
        hass, client, store, account_external_id
    )
    recently_played_coordinator = MusicInsightsRecentlyPlayedCoordinator(
        hass, client, store, account_external_id
    )
    top_items_coordinator = MusicInsightsTopItemsCoordinator(
        hass, client, store, account_external_id
    )

    await playback_coordinator.async_config_entry_first_refresh()
    await recently_played_coordinator.async_config_entry_first_refresh()
    await top_items_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = MusicInsightsRuntimeData(
        store=store,
        client=client,
        playback_coordinator=playback_coordinator,
        recently_played_coordinator=recently_played_coordinator,
        top_items_coordinator=top_items_coordinator,
        account_external_id=account_external_id,
    )

    entry.async_on_unload(async_setup_snapshot_schedule(hass, store))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: MusicInsightsConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and entry.runtime_data:
        await hass.async_add_executor_job(entry.runtime_data.store.close)
    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_CREATE_SNAPSHOT):
        return

    def _entries() -> list[MusicInsightsConfigEntry]:
        return [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.runtime_data is not None
        ]

    async def _handle_create_snapshot(call: ServiceCall) -> None:
        for entry in _entries():
            await hass.async_add_executor_job(entry.runtime_data.store.create_snapshot)

    async def _handle_integrity_check(call: ServiceCall) -> None:
        for entry in _entries():
            result = await hass.async_add_executor_job(
                entry.runtime_data.store.run_integrity_check
            )
            _LOGGER.info("Music Insights integrity check (%s): %s", entry.title, result)

    async def _handle_export_data(call: ServiceCall) -> None:
        for entry in _entries():
            store = entry.runtime_data.store

            def _export() -> Path:
                account_id = store.upsert_account(
                    "spotify", entry.runtime_data.account_external_id, None
                )
                data = store.export_account_json(account_id)
                export_dir = Path(hass.config.path(STORAGE_DIR_NAME, "exports"))
                export_dir.mkdir(parents=True, exist_ok=True)
                timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                target = export_dir / f"export_{entry.entry_id}_{timestamp}.json"
                target.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                return target

            path = await hass.async_add_executor_job(_export)
            _LOGGER.info("Music Insights: exported data for %s to %s", entry.title, path)

    async def _handle_import_legacy(call: ServiceCall) -> None:
        path_str = call.data.get("path", hass.config.path(LEGACY_JSONL_FILENAME))
        path = Path(path_str)
        for entry in _entries():
            store = entry.runtime_data.store
            account_external_id = entry.runtime_data.account_external_id

            def _import() -> dict:
                if not path.exists():
                    return {"imported": 0, "skipped": 0, "failed": 0, "error": "file_not_found"}
                with path.open("r", encoding="utf-8") as handle:
                    return store.import_legacy_jsonl("spotify", account_external_id, handle)

            result = await hass.async_add_executor_job(_import)
            _LOGGER.info(
                "Music Insights: legacy import for %s from %s: %s", entry.title, path, result
            )

    hass.services.async_register(DOMAIN, SERVICE_CREATE_SNAPSHOT, _handle_create_snapshot)
    hass.services.async_register(
        DOMAIN, SERVICE_RUN_INTEGRITY_CHECK, _handle_integrity_check
    )
    hass.services.async_register(DOMAIN, SERVICE_EXPORT_DATA, _handle_export_data)
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_LEGACY_JSONL,
        _handle_import_legacy,
        schema=vol.Schema({vol.Optional("path"): str}),
    )
