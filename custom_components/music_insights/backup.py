"""Automatic snapshot scheduling for the Music Insights database.

This is a lightweight, integration-owned snapshot mechanism (a consistent
copy of the SQLite file via ``sqlite3.Connection.backup()``) kept under
``/config/music_insights/backups/``. It is intentionally independent of Home
Assistant's own Backup integration so MI-HA's history is protected even if
the user never runs a full HA backup. A tighter integration with HA's
native Backup platform (``async_pre_backup`` / ``async_post_backup`` hooks)
is a natural follow-up once the storage layer has stabilised.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import SNAPSHOT_INTERVAL
from .storage import MusicInsightsStore

_LOGGER = logging.getLogger(__name__)


def async_setup_snapshot_schedule(
    hass: HomeAssistant, store: MusicInsightsStore
) -> Callable[[], None]:
    """Schedule periodic snapshots; returns an unsubscribe callable."""

    async def _snapshot_tick(_now) -> None:
        try:
            await hass.async_add_executor_job(store.create_snapshot)
        except Exception:  # noqa: BLE001 - a failed snapshot must not crash HA
            _LOGGER.exception("Music Insights: periodic snapshot failed")

    return async_track_time_interval(hass, _snapshot_tick, SNAPSHOT_INTERVAL)
