"""Long-term SQLite storage engine for Music Insights (MI-HA).

Design goals (see project README for the full rationale):

- The database lives at ``/config/music_insights/music_insights.db`` -
  outside the ``custom_components`` tree - so it survives integration
  updates, reinstalls and even removal of the integration itself.
- Unlimited retention by default. Nothing is ever deleted automatically.
- Schema is versioned independently of the integration version. Migrations
  are plain, ordered, idempotent functions applied inside a transaction.
- Every write path is deduplicated via a stable hash so re-ingesting the
  same data (recently-played overlap, restarts, imports) never creates
  duplicate rows.
- All blocking SQLite calls run through this module only; callers (the
  coordinator, config flow, services) must invoke it via
  ``hass.async_add_executor_job``. This module contains no HA imports and
  no asyncio, which also makes it independently unit-testable.
- Home Assistant's executor is a *pool* of worker threads, so several
  entities/services can call into this store at the same moment. A single
  ``sqlite3.Connection`` is not safe for concurrent use across threads
  (even with ``check_same_thread=False``, which only disables the identity
  check - it does not add locking), so every public method below acquires
  ``self._lock`` to fully serialise access.
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .const import (
    COMPLETE_THRESHOLD_PERCENT,
    INSTANT_SKIP_THRESHOLD_MS,
    RESULT_COMPLETE,
    RESULT_INSTANT_SKIP,
    RESULT_PLAY,
    RESULT_SKIP,
    RESULT_UNKNOWN,
    SCHEMA_VERSION,
    SNAPSHOT_KEEP_COUNT,
)

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Data classes used by callers to pass data into the store without them
# needing to know column order / SQL details.
# --------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class TrackData:
    provider: str
    external_id: str
    name: str
    duration_ms: int | None = None
    album_external_id: str | None = None
    album_name: str | None = None
    album_release_date: str | None = None
    album_image_url: str | None = None
    artist_external_ids: Sequence[str] = ()
    artist_names: Sequence[str] = ()
    metadata: dict[str, Any] | None = None


@dataclasses.dataclass(slots=True)
class PlaySessionData:
    provider: str
    account_external_id: str
    track: TrackData
    started_at: str  # ISO 8601 UTC
    ended_at: str | None
    duration_ms: int | None
    listened_ms: int
    device: str | None
    source: str | None
    spotify_played_at: str | None
    context: dict[str, Any] | None = None
    result: str | None = None  # computed automatically if omitted


@dataclasses.dataclass(slots=True)
class TopItemEntry:
    external_id: str
    rank: int


def classify_result(listened_ms: int, duration_ms: int | None) -> str:
    """Classify a play into instant_skip / skip / play / complete / unknown."""
    if listened_ms <= 0:
        return RESULT_UNKNOWN
    if listened_ms < INSTANT_SKIP_THRESHOLD_MS:
        return RESULT_INSTANT_SKIP
    if not duration_ms or duration_ms <= 0:
        return RESULT_UNKNOWN
    fraction = listened_ms / duration_ms
    if fraction >= COMPLETE_THRESHOLD_PERCENT:
        return RESULT_COMPLETE
    if fraction < 0.5:
        return RESULT_SKIP
    return RESULT_PLAY


def _dedup_hash(account_id: int, track_id: int, started_at: str) -> str:
    raw = f"{account_id}:{track_id}:{started_at}".encode()
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES providers(id),
    external_id TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, external_id)
);

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES providers(id),
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, external_id)
);

CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES providers(id),
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    release_date TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, external_id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES providers(id),
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    album_id INTEGER REFERENCES albums(id),
    duration_ms INTEGER,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, external_id)
);

CREATE TABLE IF NOT EXISTS track_artists (
    track_id INTEGER NOT NULL REFERENCES tracks(id),
    artist_id INTEGER NOT NULL REFERENCES artists(id),
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (track_id, artist_id)
);

CREATE TABLE IF NOT EXISTS play_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    track_id INTEGER NOT NULL REFERENCES tracks(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    listened_ms INTEGER NOT NULL DEFAULT 0,
    completion_percent REAL,
    result TEXT NOT NULL CHECK (
        result IN ('instant_skip', 'skip', 'play', 'complete', 'unknown')
    ),
    device TEXT,
    source TEXT,
    spotify_played_at TEXT,
    context_json TEXT,
    dedup_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_play_sessions_account_started
    ON play_sessions(account_id, started_at);
CREATE INDEX IF NOT EXISTS idx_play_sessions_track
    ON play_sessions(track_id);

CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    total_ms INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    unique_tracks INTEGER NOT NULL DEFAULT 0,
    unique_artists INTEGER NOT NULL DEFAULT 0,
    top_track_id INTEGER REFERENCES tracks(id),
    top_artist_id INTEGER REFERENCES artists(id),
    computed_at TEXT NOT NULL,
    UNIQUE(account_id, date)
);

CREATE TABLE IF NOT EXISTS yearly_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    year TEXT NOT NULL,
    total_ms INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    unique_tracks INTEGER NOT NULL DEFAULT 0,
    unique_artists INTEGER NOT NULL DEFAULT 0,
    top_track_id INTEGER REFERENCES tracks(id),
    top_artist_id INTEGER REFERENCES artists(id),
    computed_at TEXT NOT NULL,
    UNIQUE(account_id, year)
);

CREATE TABLE IF NOT EXISTS top_items_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    term TEXT NOT NULL CHECK (term IN ('short_term', 'medium_term', 'long_term')),
    item_type TEXT NOT NULL CHECK (item_type IN ('tracks', 'artists')),
    rank INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_top_items_lookup
    ON top_items_snapshots(account_id, term, item_type, captured_at);
"""


# Ordered list of (version, sql | callable) migrations. Version 1 is the
# baseline schema above and is applied by _ensure_schema before this list
# runs, so the list only needs entries for version >= 2 going forward.
_MIGRATIONS: list[tuple[int, str]] = [
    # (2, "ALTER TABLE ... "),  # example for the next schema change
]


def _locked(method):
    """Serialise a method call through the store's instance lock.

    Home Assistant dispatches executor jobs onto a thread *pool*, so
    multiple entities/coordinators can end up calling into the same
    ``MusicInsightsStore`` at once. ``RLock`` (not a plain ``Lock``) is
    required because several locked methods call other locked methods
    internally (e.g. ``record_play_session`` -> ``upsert_track`` ->
    ``upsert_album``) from the same thread.
    """

    @functools.wraps(method)
    def wrapper(self: "MusicInsightsStore", *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class MusicInsightsStore:
    """Synchronous SQLite storage engine. Must be called from an executor."""

    def __init__(self, db_path: Path, backup_dir: Path) -> None:
        self._db_path = db_path
        self._backup_dir = backup_dir
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    @_locked
    def open(self) -> None:
        """Open the database, creating it and applying migrations if needed."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        self._conn = conn

        self._ensure_schema()
        self._seed_providers()

    @_locked
    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("MusicInsightsStore.open() was not called")
        return self._conn

    @_locked
    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """Run an ad-hoc, lock-protected SELECT for callers outside this module.

        Prefer a dedicated method on this class for anything reused; this
        exists so simple, one-off read queries (e.g. sensor state lookups)
        don't need to reach into ``self.conn`` directly and bypass locking.
        """
        return self.conn.execute(sql, params).fetchone()

    # -- schema / migrations -------------------------------------------

    def _ensure_schema(self) -> None:
        conn = self.conn
        with contextlib.closing(conn.cursor()) as cur:
            cur.executescript(_SCHEMA_SQL)

        current = self._get_schema_version()
        if current == 0:
            self._set_schema_version(SCHEMA_VERSION)
            current = SCHEMA_VERSION
            _LOGGER.info(
                "Music Insights: initialised new database at schema version %s",
                current,
            )

        for version, sql in _MIGRATIONS:
            if version <= current:
                continue
            _LOGGER.info("Music Insights: applying migration to schema v%s", version)
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) "
                    "VALUES (?, datetime('now'))",
                    (version,),
                )
            self._set_schema_version(version)
            current = version

    def _get_schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _set_schema_version(self, version: int) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, datetime('now'))",
            (version,),
        )

    def _seed_providers(self) -> None:
        from .const import SUPPORTED_PROVIDERS

        with self.conn:
            for name in SUPPORTED_PROVIDERS:
                self.conn.execute(
                    "INSERT OR IGNORE INTO providers (name) VALUES (?)", (name,)
                )

    # -- integrity / maintenance ----------------------------------------

    @_locked
    def run_integrity_check(self) -> dict[str, Any]:
        """Run SQLite's built-in integrity + foreign-key checks."""
        cur = self.conn.execute("PRAGMA integrity_check")
        integrity_rows = [r[0] for r in cur.fetchall()]
        fk_rows = self.conn.execute("PRAGMA foreign_key_check").fetchall()
        ok = integrity_rows == ["ok"] and not fk_rows
        result = {
            "ok": ok,
            "integrity_check": integrity_rows,
            "foreign_key_violations": len(fk_rows),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if not ok:
            _LOGGER.error("Music Insights: database integrity check FAILED: %s", result)
        return result

    @_locked
    def create_snapshot(self) -> Path:
        """Create a consistent on-disk snapshot using SQLite's backup API."""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = self._backup_dir / f"music_insights_{timestamp}.db"

        dest_conn = sqlite3.connect(target)
        try:
            with dest_conn:
                self.conn.backup(dest_conn)
        finally:
            dest_conn.close()

        self._prune_snapshots()
        _LOGGER.info("Music Insights: created snapshot %s", target.name)
        return target

    def _prune_snapshots(self, keep: int = SNAPSHOT_KEEP_COUNT) -> None:
        snapshots = sorted(self._backup_dir.glob("music_insights_*.db"))
        excess = len(snapshots) - keep
        for path in snapshots[:max(excess, 0)]:
            with contextlib.suppress(OSError):
                path.unlink()

    @_locked
    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    # -- provider / account -----------------------------------------------

    @_locked
    def get_provider_id(self, provider: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM providers WHERE name = ?", (provider,)
        ).fetchone()
        if row is None:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO providers (name) VALUES (?)", (provider,)
                )
            return cur.lastrowid
        return row["id"]

    @_locked
    def upsert_account(
        self, provider: str, external_id: str, display_name: str | None
    ) -> int:
        provider_id = self.get_provider_id(provider)
        now = _now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO accounts (provider_id, external_id, display_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, external_id) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, accounts.display_name),
                    updated_at = excluded.updated_at
                """,
                (provider_id, external_id, display_name, now, now),
            )
        row = self.conn.execute(
            "SELECT id FROM accounts WHERE provider_id = ? AND external_id = ?",
            (provider_id, external_id),
        ).fetchone()
        return row["id"]

    # -- catalogue (artists / albums / tracks) -----------------------------

    @_locked
    def upsert_artist(self, provider: str, external_id: str, name: str,
                       metadata: dict[str, Any] | None = None) -> int:
        provider_id = self.get_provider_id(provider)
        now = _now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO artists (provider_id, external_id, name, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, external_id) DO UPDATE SET
                    name = excluded.name,
                    metadata_json = COALESCE(excluded.metadata_json, artists.metadata_json),
                    updated_at = excluded.updated_at
                """,
                (provider_id, external_id, name, _dump_json(metadata), now, now),
            )
        return self.conn.execute(
            "SELECT id FROM artists WHERE provider_id = ? AND external_id = ?",
            (provider_id, external_id),
        ).fetchone()["id"]

    @_locked
    def upsert_album(self, provider: str, external_id: str, name: str,
                      release_date: str | None,
                      metadata: dict[str, Any] | None = None) -> int:
        provider_id = self.get_provider_id(provider)
        now = _now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO albums (provider_id, external_id, name, release_date, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, external_id) DO UPDATE SET
                    name = excluded.name,
                    release_date = excluded.release_date,
                    metadata_json = COALESCE(excluded.metadata_json, albums.metadata_json),
                    updated_at = excluded.updated_at
                """,
                (provider_id, external_id, name, release_date, _dump_json(metadata), now, now),
            )
        return self.conn.execute(
            "SELECT id FROM albums WHERE provider_id = ? AND external_id = ?",
            (provider_id, external_id),
        ).fetchone()["id"]

    @_locked
    def upsert_track(self, track: TrackData) -> int:
        provider_id = self.get_provider_id(track.provider)
        now = _now_iso()

        album_id = None
        if track.album_external_id:
            album_metadata = (
                {"image_url": track.album_image_url} if track.album_image_url else None
            )
            album_id = self.upsert_album(
                track.provider,
                track.album_external_id,
                track.album_name or track.album_external_id,
                track.album_release_date,
                album_metadata,
            )

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tracks (provider_id, external_id, name, album_id, duration_ms, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, external_id) DO UPDATE SET
                    name = excluded.name,
                    album_id = COALESCE(excluded.album_id, tracks.album_id),
                    duration_ms = COALESCE(excluded.duration_ms, tracks.duration_ms),
                    metadata_json = COALESCE(excluded.metadata_json, tracks.metadata_json),
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    track.external_id,
                    track.name,
                    album_id,
                    track.duration_ms,
                    _dump_json(track.metadata),
                    now,
                    now,
                ),
            )
        track_id = self.conn.execute(
            "SELECT id FROM tracks WHERE provider_id = ? AND external_id = ?",
            (provider_id, track.external_id),
        ).fetchone()["id"]

        if track.artist_external_ids:
            names = list(track.artist_names) or list(track.artist_external_ids)
            with self.conn:
                self.conn.execute(
                    "DELETE FROM track_artists WHERE track_id = ?", (track_id,)
                )
                for position, (artist_external_id, artist_name) in enumerate(
                    zip(track.artist_external_ids, names)
                ):
                    artist_id = self.upsert_artist(
                        track.provider, artist_external_id, artist_name
                    )
                    self.conn.execute(
                        "INSERT OR IGNORE INTO track_artists (track_id, artist_id, position) "
                        "VALUES (?, ?, ?)",
                        (track_id, artist_id, position),
                    )

        return track_id

    # -- play sessions -------------------------------------------------

    @_locked
    def record_play_session(self, session: PlaySessionData) -> tuple[int, bool]:
        """Insert a play session. Returns (id, created). Deduplicated."""
        account_id = self.upsert_account(
            session.provider, session.account_external_id, None
        )
        track_id = self.upsert_track(session.track)

        result = session.result or classify_result(
            session.listened_ms, session.duration_ms or session.track.duration_ms
        )
        duration_ms = session.duration_ms or session.track.duration_ms
        completion_percent = None
        if duration_ms:
            completion_percent = round(
                min(session.listened_ms / duration_ms, 1.0) * 100, 2
            )

        dedup_hash = _dedup_hash(account_id, track_id, session.started_at)
        now = _now_iso()

        existing = self.conn.execute(
            "SELECT id FROM play_sessions WHERE dedup_hash = ?", (dedup_hash,)
        ).fetchone()
        if existing:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE play_sessions SET
                        ended_at = COALESCE(?, ended_at),
                        listened_ms = MAX(listened_ms, ?),
                        completion_percent = COALESCE(?, completion_percent),
                        result = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        session.ended_at,
                        session.listened_ms,
                        completion_percent,
                        result,
                        now,
                        existing["id"],
                    ),
                )
            return existing["id"], False

        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO play_sessions (
                    account_id, track_id, started_at, ended_at, duration_ms,
                    listened_ms, completion_percent, result, device, source,
                    spotify_played_at, context_json, dedup_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    track_id,
                    session.started_at,
                    session.ended_at,
                    duration_ms,
                    session.listened_ms,
                    completion_percent,
                    result,
                    session.device,
                    session.source,
                    session.spotify_played_at,
                    _dump_json(session.context),
                    dedup_hash,
                    now,
                    now,
                ),
            )
        return cur.lastrowid, True

    # -- top items -------------------------------------------------------

    @_locked
    def replace_top_items(
        self,
        provider: str,
        account_external_id: str,
        term: str,
        item_type: str,
        items: Iterable[TopItemEntry],
        resolved_item_ids: Sequence[int],
    ) -> None:
        account_id = self.upsert_account(provider, account_external_id, None)
        now = _now_iso()
        with self.conn:
            for entry, item_id in zip(items, resolved_item_ids):
                self.conn.execute(
                    """
                    INSERT INTO top_items_snapshots
                        (account_id, term, item_type, rank, item_id, captured_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (account_id, term, item_type, entry.rank, item_id, now),
                )

    # -- aggregate stats ---------------------------------------------------

    @_locked
    def recompute_daily_stats(self, account_id: int, date: str) -> None:
        """Recompute daily_stats for one account/date from play_sessions."""
        row = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(listened_ms), 0) AS total_ms,
                COUNT(*) AS play_count,
                COUNT(DISTINCT track_id) AS unique_tracks
            FROM play_sessions
            WHERE account_id = ? AND substr(started_at, 1, 10) = ?
              AND result != 'instant_skip'
            """,
            (account_id, date),
        ).fetchone()

        unique_artists_row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT ta.artist_id) AS unique_artists
            FROM play_sessions ps
            JOIN track_artists ta ON ta.track_id = ps.track_id
            WHERE ps.account_id = ? AND substr(ps.started_at, 1, 10) = ?
              AND ps.result != 'instant_skip'
            """,
            (account_id, date),
        ).fetchone()

        top_track_row = self.conn.execute(
            """
            SELECT track_id, SUM(listened_ms) AS ms
            FROM play_sessions
            WHERE account_id = ? AND substr(started_at, 1, 10) = ?
              AND result != 'instant_skip'
            GROUP BY track_id ORDER BY ms DESC LIMIT 1
            """,
            (account_id, date),
        ).fetchone()

        top_artist_row = self.conn.execute(
            """
            SELECT ta.artist_id AS artist_id, SUM(ps.listened_ms) AS ms
            FROM play_sessions ps
            JOIN track_artists ta ON ta.track_id = ps.track_id
            WHERE ps.account_id = ? AND substr(ps.started_at, 1, 10) = ?
              AND ps.result != 'instant_skip'
            GROUP BY ta.artist_id ORDER BY ms DESC LIMIT 1
            """,
            (account_id, date),
        ).fetchone()

        now = _now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO daily_stats (
                    account_id, date, total_ms, play_count, unique_tracks,
                    unique_artists, top_track_id, top_artist_id, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, date) DO UPDATE SET
                    total_ms = excluded.total_ms,
                    play_count = excluded.play_count,
                    unique_tracks = excluded.unique_tracks,
                    unique_artists = excluded.unique_artists,
                    top_track_id = excluded.top_track_id,
                    top_artist_id = excluded.top_artist_id,
                    computed_at = excluded.computed_at
                """,
                (
                    account_id,
                    date,
                    row["total_ms"],
                    row["play_count"],
                    row["unique_tracks"],
                    unique_artists_row["unique_artists"],
                    top_track_row["track_id"] if top_track_row else None,
                    top_artist_row["artist_id"] if top_artist_row else None,
                    now,
                ),
            )

    @_locked
    def recompute_yearly_stats(self, account_id: int, year: str) -> None:
        row = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(total_ms), 0) AS total_ms,
                COALESCE(SUM(play_count), 0) AS play_count
            FROM daily_stats
            WHERE account_id = ? AND substr(date, 1, 4) = ?
            """,
            (account_id, year),
        ).fetchone()

        unique_tracks_row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT track_id) AS unique_tracks
            FROM play_sessions
            WHERE account_id = ? AND substr(started_at, 1, 4) = ?
              AND result != 'instant_skip'
            """,
            (account_id, year),
        ).fetchone()

        unique_artists_row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT ta.artist_id) AS unique_artists
            FROM play_sessions ps
            JOIN track_artists ta ON ta.track_id = ps.track_id
            WHERE ps.account_id = ? AND substr(ps.started_at, 1, 4) = ?
              AND ps.result != 'instant_skip'
            """,
            (account_id, year),
        ).fetchone()

        top_track_row = self.conn.execute(
            """
            SELECT track_id, SUM(listened_ms) AS ms
            FROM play_sessions
            WHERE account_id = ? AND substr(started_at, 1, 4) = ?
              AND result != 'instant_skip'
            GROUP BY track_id ORDER BY ms DESC LIMIT 1
            """,
            (account_id, year),
        ).fetchone()

        top_artist_row = self.conn.execute(
            """
            SELECT ta.artist_id AS artist_id, SUM(ps.listened_ms) AS ms
            FROM play_sessions ps
            JOIN track_artists ta ON ta.track_id = ps.track_id
            WHERE ps.account_id = ? AND substr(ps.started_at, 1, 4) = ?
              AND ps.result != 'instant_skip'
            GROUP BY ta.artist_id ORDER BY ms DESC LIMIT 1
            """,
            (account_id, year),
        ).fetchone()

        now = _now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO yearly_stats (
                    account_id, year, total_ms, play_count, unique_tracks,
                    unique_artists, top_track_id, top_artist_id, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, year) DO UPDATE SET
                    total_ms = excluded.total_ms,
                    play_count = excluded.play_count,
                    unique_tracks = excluded.unique_tracks,
                    unique_artists = excluded.unique_artists,
                    top_track_id = excluded.top_track_id,
                    top_artist_id = excluded.top_artist_id,
                    computed_at = excluded.computed_at
                """,
                (
                    account_id,
                    year,
                    row["total_ms"],
                    row["play_count"],
                    unique_tracks_row["unique_tracks"],
                    unique_artists_row["unique_artists"],
                    top_track_row["track_id"] if top_track_row else None,
                    top_artist_row["artist_id"] if top_artist_row else None,
                    now,
                ),
            )

    # -- export / import -----------------------------------------------

    @_locked
    def export_account_json(self, account_id: int) -> dict[str, Any]:
        """Export all data for one account as a JSON-serialisable dict."""
        sessions = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM play_sessions WHERE account_id = ? ORDER BY started_at",
                (account_id,),
            ).fetchall()
        ]
        daily = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM daily_stats WHERE account_id = ? ORDER BY date",
                (account_id,),
            ).fetchall()
        ]
        yearly = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM yearly_stats WHERE account_id = ? ORDER BY year",
                (account_id,),
            ).fetchall()
        ]
        return {
            "schema_version": self._get_schema_version(),
            "exported_at": _now_iso(),
            "account_id": account_id,
            "play_sessions": sessions,
            "daily_stats": daily,
            "yearly_stats": yearly,
        }

    @_locked
    def import_legacy_jsonl(
        self, provider: str, account_external_id: str, lines: Iterable[str]
    ) -> dict[str, int]:
        """Import the prototype's line-delimited JSON history file.

        Each line is expected to be a JSON object describing one play event.
        Unknown fields are ignored; missing fields are handled gracefully so
        this stays compatible with the shell-script prototype's evolving
        format. Returns counts of imported/skipped/failed lines.
        """
        imported = skipped = failed = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                track = TrackData(
                    provider=provider,
                    external_id=payload["track_id"],
                    name=payload.get("track_name", payload["track_id"]),
                    duration_ms=payload.get("duration_ms"),
                    album_external_id=payload.get("album_id"),
                    album_name=payload.get("album_name"),
                    artist_external_ids=payload.get("artist_ids", []),
                    artist_names=payload.get("artist_names", []),
                )
                session = PlaySessionData(
                    provider=provider,
                    account_external_id=account_external_id,
                    track=track,
                    started_at=payload["started_at"],
                    ended_at=payload.get("ended_at"),
                    duration_ms=payload.get("duration_ms"),
                    listened_ms=payload.get("listened_ms", 0),
                    device=payload.get("device"),
                    source=payload.get("source", "legacy_import"),
                    spotify_played_at=payload.get("spotify_played_at"),
                )
                _, created = self.record_play_session(session)
                imported += 1 if created else 0
                skipped += 0 if created else 1
            except (KeyError, json.JSONDecodeError, TypeError) as err:
                _LOGGER.warning("Music Insights: skipping malformed legacy line: %s", err)
                failed += 1
        return {"imported": imported, "skipped": skipped, "failed": failed}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dump_json(data: dict[str, Any] | None) -> str | None:
    return json.dumps(data, ensure_ascii=False) if data else None
