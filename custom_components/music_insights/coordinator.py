"""Data update coordinators for Music Insights (MI-HA).

Three coordinators run per config entry:

- ``MusicInsightsPlaybackCoordinator``: polls the currently-playing endpoint.
  Dynamically switches its own interval between ``UPDATE_INTERVAL_LIVE``
  (something is playing) and ``UPDATE_INTERVAL_IDLE`` (nothing is playing),
  and is the primary source that builds ``play_sessions`` rows.
- ``MusicInsightsRecentlyPlayedCoordinator``: periodically reconciles against
  Spotify's recently-played history to catch anything the live poller missed
  (HA restarts, network drops). Deduplicated by storage.py, so overlap with
  the live poller is safe and expected.
- ``MusicInsightsTopItemsCoordinator``: periodically snapshots Spotify's
  short/medium/long-term top tracks and artists.

All three delegate every blocking DB call to the shared ``MusicInsightsStore``
via ``hass.async_add_executor_job``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SpotifyApiError, SpotifyClient, SpotifyRateLimitedError
from .const import (
    DOMAIN,
    TOP_ITEM_TERMS,
    TOP_ITEM_TYPE_ARTISTS,
    TOP_ITEM_TYPE_TRACKS,
    UPDATE_INTERVAL_IDLE,
    UPDATE_INTERVAL_LIVE,
    UPDATE_INTERVAL_RECENTLY_PLAYED,
    UPDATE_INTERVAL_TOP_ITEMS,
)
from .storage import MusicInsightsStore, PlaySessionData, TopItemEntry, TrackData

_LOGGER = logging.getLogger(__name__)


def pick_image_url(images: list[dict] | None) -> str | None:
    """Pick a reasonably-sized cover image from Spotify's images array.

    Spotify returns images largest-first (typically 640/300/64px). A
    dashboard thumbnail doesn't need full resolution, so prefer something
    close to 300px; fall back to whatever is available.
    """
    if not images:
        return None
    for image in images:
        if image.get("width") and 200 <= image["width"] <= 400:
            return image.get("url")
    return images[0].get("url")


def _track_from_spotify_item(item: dict) -> TrackData:
    album = item.get("album") or {}
    artists = item.get("artists") or []
    return TrackData(
        provider="spotify",
        external_id=item["id"],
        name=item.get("name", item["id"]),
        duration_ms=item.get("duration_ms"),
        album_external_id=album.get("id"),
        album_name=album.get("name"),
        album_release_date=album.get("release_date"),
        album_image_url=pick_image_url(album.get("images")),
        artist_external_ids=[a["id"] for a in artists if a.get("id")],
        artist_names=[a.get("name", "") for a in artists],
        metadata={"popularity": item.get("popularity")} if "popularity" in item else None,
    )


class MusicInsightsPlaybackCoordinator(DataUpdateCoordinator[dict]):
    """Polls current playback and records play_sessions in real time."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: SpotifyClient,
        store: MusicInsightsStore,
        account_external_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_playback",
            update_interval=UPDATE_INTERVAL_IDLE,
        )
        self._client = client
        self._store = store
        self._account_external_id = account_external_id
        # Tracks the in-progress session so we can close it out once the
        # track changes or playback stops (accumulating listened_ms across
        # pause/resume cycles rather than trusting a single progress sample).
        self._active_track_external_id: str | None = None
        self._active_started_at: str | None = None
        self._active_listened_ms: int = 0
        self._last_progress_ms: int | None = None
        self._last_poll_monotonic: float | None = None
        self._pending_item: dict | None = None
        self._pending_device: str | None = None
        self._pending_source: str | None = None

    async def _async_update_data(self) -> dict:
        try:
            playback = await self._client.async_get_playback_state()
        except SpotifyRateLimitedError as err:
            # Back off politely; keep current interval, just skip this cycle.
            _LOGGER.debug("Music Insights: rate limited, retry in %.1fs", err.retry_after)
            return self.data or {"is_playing": False}
        except SpotifyApiError as err:
            raise UpdateFailed(str(err)) from err

        now_monotonic = time.monotonic()
        is_playing = bool(playback and playback.get("is_playing"))
        item = (playback or {}).get("item")

        if is_playing and item and item.get("type") == "track":
            await self._handle_playing(item, playback, now_monotonic)
        else:
            await self._handle_stopped()

        self._last_poll_monotonic = now_monotonic
        self.update_interval = UPDATE_INTERVAL_LIVE if is_playing else UPDATE_INTERVAL_IDLE

        return {
            "is_playing": is_playing,
            "item": item,
            "device": (playback or {}).get("device"),
            "progress_ms": (playback or {}).get("progress_ms"),
        }

    async def _handle_playing(self, item: dict, playback: dict, now_monotonic: float) -> None:
        track_external_id = item["id"]
        progress_ms = playback.get("progress_ms") or 0
        device = (playback.get("device") or {}).get("name")

        if track_external_id != self._active_track_external_id:
            # Track changed (or first observation): close the previous
            # session, if any, and open a new one.
            if self._active_track_external_id is not None:
                await self._flush_active_session()
            self._active_track_external_id = track_external_id
            self._active_started_at = _now_iso()
            self._active_listened_ms = 0
            self._last_progress_ms = progress_ms
            self._pending_item = item
            self._pending_device = device
            self._pending_source = playback.get("context", {}).get("type") if playback.get("context") else "unknown"
            return

        # Same track still playing: accumulate real elapsed time, but cap it
        # to the poll interval so a resumed/seeked track can't inflate
        # listened_ms beyond wall-clock reality.
        if self._last_poll_monotonic is not None:
            elapsed = now_monotonic - self._last_poll_monotonic
            self._active_listened_ms += max(0, int(elapsed * 1000))
        self._last_progress_ms = progress_ms
        self._pending_item = item
        self._pending_device = device

    async def _handle_stopped(self) -> None:
        if self._active_track_external_id is not None:
            await self._flush_active_session(ended=True)
        self._active_track_external_id = None
        self._active_started_at = None
        self._active_listened_ms = 0
        self._last_progress_ms = None

    async def _flush_active_session(self, ended: bool = False) -> None:
        if not self._pending_item:
            return
        track = _track_from_spotify_item(self._pending_item)
        session = PlaySessionData(
            provider="spotify",
            account_external_id=self._account_external_id,
            track=track,
            started_at=self._active_started_at or _now_iso(),
            ended_at=_now_iso() if ended else None,
            duration_ms=self._pending_item.get("duration_ms"),
            listened_ms=self._active_listened_ms,
            device=self._pending_device,
            source=self._pending_source or "unknown",
            spotify_played_at=None,
        )

        def _write() -> None:
            self._store.record_play_session(session)
            account_id = self._store.upsert_account("spotify", self._account_external_id, None)
            today = self._active_started_at[:10] if self._active_started_at else _now_iso()[:10]
            self._store.recompute_daily_stats(account_id, today)
            self._store.recompute_yearly_stats(account_id, today[:4])

        await self.hass.async_add_executor_job(_write)


class MusicInsightsRecentlyPlayedCoordinator(DataUpdateCoordinator[dict]):
    """Periodically reconciles against Spotify's recently-played history."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: SpotifyClient,
        store: MusicInsightsStore,
        account_external_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_recently_played",
            update_interval=UPDATE_INTERVAL_RECENTLY_PLAYED,
        )
        self._client = client
        self._store = store
        self._account_external_id = account_external_id

    async def _async_update_data(self) -> dict:
        try:
            data = await self._client.async_get_recently_played(limit=50)
        except SpotifyApiError as err:
            raise UpdateFailed(str(err)) from err

        items = data.get("items", [])

        def _write() -> int:
            imported = 0
            for entry in items:
                track_payload = entry.get("track")
                played_at = entry.get("played_at")
                if not track_payload or not played_at:
                    continue
                track = _track_from_spotify_item(track_payload)
                session = PlaySessionData(
                    provider="spotify",
                    account_external_id=self._account_external_id,
                    track=track,
                    started_at=played_at,
                    ended_at=None,
                    duration_ms=track_payload.get("duration_ms"),
                    listened_ms=track_payload.get("duration_ms") or 0,
                    device=None,
                    source="recently_played",
                    spotify_played_at=played_at,
                    result="complete",
                )
                _, created = self._store.record_play_session(session)
                imported += 1 if created else 0

            if imported:
                account_id = self._store.upsert_account(
                    "spotify", self._account_external_id, None
                )
                affected_dates = {e["played_at"][:10] for e in items if e.get("played_at")}
                for date in affected_dates:
                    self._store.recompute_daily_stats(account_id, date)
                    self._store.recompute_yearly_stats(account_id, date[:4])
            return imported

        imported_count = await self.hass.async_add_executor_job(_write)
        return {"imported": imported_count, "checked_at": _now_iso()}


class MusicInsightsTopItemsCoordinator(DataUpdateCoordinator[dict]):
    """Periodically snapshots Spotify's short/medium/long-term top items."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: SpotifyClient,
        store: MusicInsightsStore,
        account_external_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_top_items",
            update_interval=UPDATE_INTERVAL_TOP_ITEMS,
        )
        self._client = client
        self._store = store
        self._account_external_id = account_external_id

    async def _async_update_data(self) -> dict:
        results: dict[str, dict] = {}
        try:
            for term in TOP_ITEM_TERMS:
                tracks = await self._client.async_get_top_items(
                    TOP_ITEM_TYPE_TRACKS, term
                )
                artists = await self._client.async_get_top_items(
                    TOP_ITEM_TYPE_ARTISTS, term
                )
                results[term] = {"tracks": tracks, "artists": artists}
        except SpotifyApiError as err:
            raise UpdateFailed(str(err)) from err

        def _write() -> None:
            for term, payload in results.items():
                track_ids = []
                entries = []
                for rank, item in enumerate(payload["tracks"].get("items", []), start=1):
                    track_id = self._store.upsert_track(_track_from_spotify_item(item))
                    track_ids.append(track_id)
                    entries.append(TopItemEntry(external_id=item["id"], rank=rank))
                self._store.replace_top_items(
                    "spotify",
                    self._account_external_id,
                    term,
                    TOP_ITEM_TYPE_TRACKS,
                    entries,
                    track_ids,
                )

                artist_ids = []
                artist_entries = []
                for rank, item in enumerate(payload["artists"].get("items", []), start=1):
                    image_url = pick_image_url(item.get("images"))
                    artist_id = self._store.upsert_artist(
                        "spotify",
                        item["id"],
                        item.get("name", item["id"]),
                        {"image_url": image_url} if image_url else None,
                    )
                    artist_ids.append(artist_id)
                    artist_entries.append(TopItemEntry(external_id=item["id"], rank=rank))
                self._store.replace_top_items(
                    "spotify",
                    self._account_external_id,
                    term,
                    TOP_ITEM_TYPE_ARTISTS,
                    artist_entries,
                    artist_ids,
                )

        await self.hass.async_add_executor_job(_write)
        return {"captured_at": _now_iso(), "terms": list(results.keys())}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
