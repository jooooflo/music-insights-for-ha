"""Sensor entities for Music Insights (MI-HA) v0.1."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MusicInsightsConfigEntry
from .const import DOMAIN, TOP_ITEM_TERMS

_ATTRIBUTION = "Data provided by Spotify"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MusicInsightsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Music Insights sensors from a config entry."""
    data = entry.runtime_data
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Music Insights for HA",
        model="Spotify",
        entry_type=DeviceEntryType.SERVICE,
    )

    entities: list[SensorEntity] = [
        CurrentlyPlayingSensor(data.playback_coordinator, entry, device_info),
        TodayListeningTimeSensor(data, entry, device_info),
        YearListeningTimeSensor(data, entry, device_info),
        RecentlyPlayedSyncSensor(data.recently_played_coordinator, entry, device_info),
    ]
    for term in TOP_ITEM_TERMS:
        entities.append(TopTrackSensor(data, entry, device_info, term=term))
        entities.append(TopArtistSensor(data, entry, device_info, term=term))
    async_add_entities(entities)


class CurrentlyPlayingSensor(CoordinatorEntity, SensorEntity):
    """Shows the track currently playing, mirroring the playback coordinator."""

    _attr_has_entity_name = True
    _attr_name = "Currently playing"
    _attr_attribution = _ATTRIBUTION

    def __init__(self, coordinator, entry: MusicInsightsConfigEntry, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_currently_playing"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str | None:
        item = (self.coordinator.data or {}).get("item")
        if not item:
            return None
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
        return f"{item.get('name')} - {artists}" if artists else item.get("name")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        item = data.get("item") or {}
        return {
            "is_playing": data.get("is_playing", False),
            "progress_ms": data.get("progress_ms"),
            "duration_ms": item.get("duration_ms"),
            "album": (item.get("album") or {}).get("name"),
            "device": (data.get("device") or {}).get("name"),
        }


class _StoreBackedSensor(SensorEntity):
    """Base class for sensors that read aggregated data from the DB.

    These are not driven by a coordinator's fetched payload directly; they
    recompute an aggregate from SQLite. They stay in sync by listening to
    the playback coordinator (whose refresh cadence already tracks live vs.
    idle listening) rather than polling on their own.
    """

    _attr_has_entity_name = True
    _attr_attribution = _ATTRIBUTION
    _attr_should_poll = False

    def __init__(self, data, entry: MusicInsightsConfigEntry, device_info: DeviceInfo) -> None:
        self._data = data
        self._entry = entry
        self._attr_device_info = device_info
        self._value: str | int | float | None = None
        self._attrs: dict = {}

    @property
    def native_value(self):
        return self._value

    @property
    def extra_state_attributes(self) -> dict:
        return self._attrs

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._data.playback_coordinator.async_add_listener(self._handle_update)
        )
        await self._async_refresh_and_write()

    def _handle_update(self) -> None:
        self.hass.async_create_task(self._async_refresh_and_write())

    async def _async_refresh_and_write(self) -> None:
        await self.hass.async_add_executor_job(self._refresh)
        self.async_write_ha_state()

    def _refresh(self) -> None:
        raise NotImplementedError


class TodayListeningTimeSensor(_StoreBackedSensor):
    _attr_name = "Listening time today"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-outline"

    def __init__(self, data, entry, device_info) -> None:
        super().__init__(data, entry, device_info)
        self._attr_unique_id = f"{entry.entry_id}_listening_time_today"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        store.recompute_daily_stats(account_id, today)
        row = store.conn.execute(
            "SELECT total_ms, play_count FROM daily_stats WHERE account_id = ? AND date = ?",
            (account_id, today),
        ).fetchone()
        if row:
            self._value = round(row["total_ms"] / 60000, 1)
            self._attrs = {"play_count": row["play_count"], "date": today}
        else:
            self._value = 0
            self._attrs = {"play_count": 0, "date": today}


class YearListeningTimeSensor(_StoreBackedSensor):
    _attr_name = "Listening time this year"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, data, entry, device_info) -> None:
        super().__init__(data, entry, device_info)
        self._attr_unique_id = f"{entry.entry_id}_listening_time_year"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        year = datetime.now(timezone.utc).strftime("%Y")
        store.recompute_yearly_stats(account_id, year)
        row = store.conn.execute(
            "SELECT total_ms, play_count, unique_tracks, unique_artists "
            "FROM yearly_stats WHERE account_id = ? AND year = ?",
            (account_id, year),
        ).fetchone()
        if row:
            self._value = round(row["total_ms"] / 60000, 1)
            self._attrs = {
                "play_count": row["play_count"],
                "unique_tracks": row["unique_tracks"],
                "unique_artists": row["unique_artists"],
                "year": year,
            }
        else:
            self._value = 0
            self._attrs = {"year": year}


class TopTrackSensor(_StoreBackedSensor):
    _attr_icon = "mdi:music-note"

    def __init__(self, data, entry, device_info, term: str) -> None:
        super().__init__(data, entry, device_info)
        self._term = term
        self._attr_name = f"Top track ({term.replace('_', ' ')})"
        self._attr_unique_id = f"{entry.entry_id}_top_track_{term}"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        row = store.conn.execute(
            """
            SELECT t.name AS track_name, t.id AS track_id
            FROM top_items_snapshots s
            JOIN tracks t ON t.id = s.item_id
            WHERE s.account_id = ? AND s.term = ? AND s.item_type = 'tracks'
            ORDER BY s.captured_at DESC, s.rank ASC LIMIT 1
            """,
            (account_id, self._term),
        ).fetchone()
        self._value = row["track_name"] if row else None
        self._attrs = {"term": self._term}


class TopArtistSensor(_StoreBackedSensor):
    _attr_icon = "mdi:account-music"

    def __init__(self, data, entry, device_info, term: str) -> None:
        super().__init__(data, entry, device_info)
        self._term = term
        self._attr_name = f"Top artist ({term.replace('_', ' ')})"
        self._attr_unique_id = f"{entry.entry_id}_top_artist_{term}"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        row = store.conn.execute(
            """
            SELECT a.name AS artist_name
            FROM top_items_snapshots s
            JOIN artists a ON a.id = s.item_id
            WHERE s.account_id = ? AND s.term = ? AND s.item_type = 'artists'
            ORDER BY s.captured_at DESC, s.rank ASC LIMIT 1
            """,
            (account_id, self._term),
        ).fetchone()
        self._value = row["artist_name"] if row else None
        self._attrs = {"term": self._term}


class RecentlyPlayedSyncSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor showing the recently-played reconciliation status."""

    _attr_has_entity_name = True
    _attr_name = "Recently played sync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator, entry: MusicInsightsConfigEntry, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_recently_played_sync"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("checked_at")

    @property
    def extra_state_attributes(self) -> dict:
        return {"last_imported_count": (self.coordinator.data or {}).get("imported", 0)}
