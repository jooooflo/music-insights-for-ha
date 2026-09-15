"""Sensor entities for Music Insights (MI-HA) v0.1."""
from __future__ import annotations

import json
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
from .coordinator import pick_image_url

_ATTRIBUTION = "Data provided by Spotify"


def _image_url_from_metadata(metadata_json: str | None) -> str | None:
    if not metadata_json:
        return None
    try:
        return json.loads(metadata_json).get("image_url")
    except (json.JSONDecodeError, AttributeError):
        return None


def _tageszeit_label(hour: int | None) -> str | None:
    """Map a local hour-of-day (0-23) to a coarse German time-of-day label."""
    if hour is None:
        return None
    if 5 <= hour < 11:
        return "Vormittag"
    if 11 <= hour < 14:
        return "Mittag"
    if 14 <= hour < 18:
        return "Nachmittag"
    if 18 <= hour < 23:
        return "Abend"
    return "Nacht"


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
        MonthListeningTimeSensor(data, entry, device_info),
        YearListeningTimeSensor(data, entry, device_info),
        TopDeviceSensor(data, entry, device_info),
        TopDaySensor(data, entry, device_info),
        TopMonthSensor(data, entry, device_info),
        RecentlyPlayedSyncSensor(data.recently_played_coordinator, entry, device_info),
    ]
    for term in TOP_ITEM_TERMS:
        entities.append(TopTrackSensor(data, entry, device_info, term=term))
        entities.append(TopArtistSensor(data, entry, device_info, term=term))
    for period in ("today", "this_month"):
        entities.append(PeriodTopTrackSensor(data, entry, device_info, period=period))
        entities.append(PeriodTopArtistSensor(data, entry, device_info, period=period))
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

    def _handle_coordinator_update(self) -> None:
        # entity_picture must be set via _attr_entity_picture, not as an
        # overridden property - Entity.entity_picture is a cached_property
        # in current HA, and a plain @property override is silently not
        # picked up by the state-attribute writer.
        item = (self.coordinator.data or {}).get("item") or {}
        album = item.get("album") or {}
        self._attr_entity_picture = pick_image_url(album.get("images"))
        super()._handle_coordinator_update()


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
        store.recompute_daily_stats(account_id, today, self.hass.config.time_zone)
        row = store.fetchone(
            "SELECT total_ms, play_count, top_device, top_hour "
            "FROM daily_stats WHERE account_id = ? AND date = ?",
            (account_id, today),
        )
        if row:
            self._value = round(row["total_ms"] / 60000, 1)
            self._attrs = {
                "play_count": row["play_count"],
                "date": today,
                "top_device": row["top_device"],
                "top_hour": row["top_hour"],
                "tageszeit": _tageszeit_label(row["top_hour"]),
            }
        else:
            self._value = 0
            self._attrs = {"play_count": 0, "date": today}


class MonthListeningTimeSensor(_StoreBackedSensor):
    _attr_name = "Listening time this month"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar-month"

    def __init__(self, data, entry, device_info) -> None:
        super().__init__(data, entry, device_info)
        self._attr_unique_id = f"{entry.entry_id}_listening_time_month"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        store.recompute_monthly_stats(account_id, month, self.hass.config.time_zone)
        row = store.fetchone(
            "SELECT total_ms, play_count, unique_tracks, unique_artists, top_device, top_hour "
            "FROM monthly_stats WHERE account_id = ? AND month = ?",
            (account_id, month),
        )
        if row:
            self._value = round(row["total_ms"] / 60000, 1)
            self._attrs = {
                "play_count": row["play_count"],
                "unique_tracks": row["unique_tracks"],
                "unique_artists": row["unique_artists"],
                "month": month,
                "top_device": row["top_device"],
                "top_hour": row["top_hour"],
                "tageszeit": _tageszeit_label(row["top_hour"]),
            }
        else:
            self._value = 0
            self._attrs = {"month": month}


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
        row = store.fetchone(
            "SELECT total_ms, play_count, unique_tracks, unique_artists, top_device "
            "FROM yearly_stats WHERE account_id = ? AND year = ?",
            (account_id, year),
        )
        if row:
            self._value = round(row["total_ms"] / 60000, 1)
            self._attrs = {
                "play_count": row["play_count"],
                "unique_tracks": row["unique_tracks"],
                "unique_artists": row["unique_artists"],
                "year": year,
                "top_device": row["top_device"],
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
        row = store.fetchone(
            """
            SELECT t.name AS track_name, al.metadata_json AS album_metadata_json
            FROM top_items_snapshots s
            JOIN tracks t ON t.id = s.item_id
            LEFT JOIN albums al ON al.id = t.album_id
            WHERE s.account_id = ? AND s.term = ? AND s.item_type = 'tracks'
            ORDER BY s.captured_at DESC, s.rank ASC LIMIT 1
            """,
            (account_id, self._term),
        )
        self._value = row["track_name"] if row else None
        self._attrs = {"term": self._term}
        self._attr_entity_picture = _image_url_from_metadata(row["album_metadata_json"] if row else None)


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
        row = store.fetchone(
            """
            SELECT a.name AS artist_name, a.metadata_json AS artist_metadata_json
            FROM top_items_snapshots s
            JOIN artists a ON a.id = s.item_id
            WHERE s.account_id = ? AND s.term = ? AND s.item_type = 'artists'
            ORDER BY s.captured_at DESC, s.rank ASC LIMIT 1
            """,
            (account_id, self._term),
        )
        self._value = row["artist_name"] if row else None
        self._attrs = {"term": self._term}
        self._attr_entity_picture = _image_url_from_metadata(row["artist_metadata_json"] if row else None)


_PERIOD_NAMES = {"today": "heute", "this_month": "diesen Monat"}


class PeriodTopTrackSensor(_StoreBackedSensor):
    """Most-listened track for a rolling period, from our own play_sessions

    (as opposed to TopTrackSensor, which mirrors Spotify's own short/medium/
    long-term top-items algorithm from their API).
    """

    _attr_icon = "mdi:music-note"

    def __init__(self, data, entry, device_info, period: str) -> None:
        super().__init__(data, entry, device_info)
        self._period = period
        self._attr_name = f"Top track ({_PERIOD_NAMES[period]})"
        self._attr_unique_id = f"{entry.entry_id}_top_track_period_{period}"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        tz_name = self.hass.config.time_zone
        if self._period == "today":
            key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            store.recompute_daily_stats(account_id, key, tz_name)
            table, column = "daily_stats", "date"
        else:
            key = datetime.now(timezone.utc).strftime("%Y-%m")
            store.recompute_monthly_stats(account_id, key, tz_name)
            table, column = "monthly_stats", "month"

        row = store.fetchone(
            f"""
            SELECT t.name AS track_name, al.metadata_json AS album_metadata_json
            FROM {table} p
            JOIN tracks t ON t.id = p.top_track_id
            LEFT JOIN albums al ON al.id = t.album_id
            WHERE p.account_id = ? AND p.{column} = ?
            """,
            (account_id, key),
        )
        self._value = row["track_name"] if row else None
        self._attrs = {"period": self._period}
        self._attr_entity_picture = _image_url_from_metadata(row["album_metadata_json"] if row else None)


class PeriodTopArtistSensor(_StoreBackedSensor):
    """Most-listened artist for a rolling period, from our own play_sessions."""

    _attr_icon = "mdi:account-music"

    def __init__(self, data, entry, device_info, period: str) -> None:
        super().__init__(data, entry, device_info)
        self._period = period
        self._attr_name = f"Top artist ({_PERIOD_NAMES[period]})"
        self._attr_unique_id = f"{entry.entry_id}_top_artist_period_{period}"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        tz_name = self.hass.config.time_zone
        if self._period == "today":
            key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            store.recompute_daily_stats(account_id, key, tz_name)
            table, column = "daily_stats", "date"
        else:
            key = datetime.now(timezone.utc).strftime("%Y-%m")
            store.recompute_monthly_stats(account_id, key, tz_name)
            table, column = "monthly_stats", "month"

        row = store.fetchone(
            f"""
            SELECT a.name AS artist_name, a.metadata_json AS artist_metadata_json
            FROM {table} p
            JOIN artists a ON a.id = p.top_artist_id
            WHERE p.account_id = ? AND p.{column} = ?
            """,
            (account_id, key),
        )
        self._value = row["artist_name"] if row else None
        self._attrs = {"period": self._period}
        self._attr_entity_picture = _image_url_from_metadata(row["artist_metadata_json"] if row else None)


class TopDeviceSensor(_StoreBackedSensor):
    """The device with the most listened minutes across all history."""

    _attr_name = "Top device (all time)"
    _attr_icon = "mdi:cellphone-link"

    def __init__(self, data, entry, device_info) -> None:
        super().__init__(data, entry, device_info)
        self._attr_unique_id = f"{entry.entry_id}_top_device_all_time"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        self._value = store.get_all_time_top_device(account_id)


class TopDaySensor(_StoreBackedSensor):
    """The single best day ever, by listening minutes."""

    _attr_name = "Top day"
    _attr_icon = "mdi:calendar-star"

    def __init__(self, data, entry, device_info) -> None:
        super().__init__(data, entry, device_info)
        self._attr_unique_id = f"{entry.entry_id}_top_day"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        row = store.get_top_day(account_id)
        self._value = row["date"] if row else None
        self._attrs = (
            {"minutes": round(row["total_ms"] / 60000, 1), "play_count": row["play_count"]}
            if row
            else {}
        )


class TopMonthSensor(_StoreBackedSensor):
    """The single best month ever, by listening minutes."""

    _attr_name = "Top month"
    _attr_icon = "mdi:calendar-star"

    def __init__(self, data, entry, device_info) -> None:
        super().__init__(data, entry, device_info)
        self._attr_unique_id = f"{entry.entry_id}_top_month"

    def _refresh(self) -> None:
        store = self._data.store
        account_id = store.upsert_account("spotify", self._data.account_external_id, None)
        row = store.get_top_month(account_id)
        self._value = row["month"] if row else None
        self._attrs = (
            {"minutes": round(row["total_ms"] / 60000, 1), "play_count": row["play_count"]}
            if row
            else {}
        )


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
