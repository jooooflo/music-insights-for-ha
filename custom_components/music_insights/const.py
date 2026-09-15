"""Constants for Music Insights for Home Assistant (MI-HA)."""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "music_insights"
PLATFORMS: Final = ["sensor"]

# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
PROVIDER_SPOTIFY: Final = "spotify"
SUPPORTED_PROVIDERS: Final = [PROVIDER_SPOTIFY]

# --------------------------------------------------------------------------
# Spotify OAuth / Application Credentials
# --------------------------------------------------------------------------
SPOTIFY_AUTHORIZE_URL: Final = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL: Final = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE_URL: Final = "https://api.spotify.com/v1"

# Scopes strictly required for MI-HA. Do not widen without updating the
# privacy documentation in README.md.
SPOTIFY_SCOPES: Final = [
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-read-recently-played",
    "user-top-read",
]
SPOTIFY_SCOPE_STRING: Final = " ".join(SPOTIFY_SCOPES)

CONF_PROVIDER: Final = "provider"

# --------------------------------------------------------------------------
# Polling intervals
# --------------------------------------------------------------------------
# "Live" polling: something is currently playing on the account.
UPDATE_INTERVAL_LIVE: Final = timedelta(seconds=5)
# "Idle" polling: nothing is currently playing.
UPDATE_INTERVAL_IDLE: Final = timedelta(seconds=10)
# Recently-played history is a cheap catch-up call; it does not need to be
# fast because play_sessions are primarily built from live polling. It exists
# to catch sessions MI-HA missed (HA restart, network hiccup, etc.).
UPDATE_INTERVAL_RECENTLY_PLAYED: Final = timedelta(minutes=5)
# Top items (short/medium/long term) change slowly server-side.
UPDATE_INTERVAL_TOP_ITEMS: Final = timedelta(hours=6)

# --------------------------------------------------------------------------
# Play session classification
# --------------------------------------------------------------------------
RESULT_INSTANT_SKIP: Final = "instant_skip"
RESULT_SKIP: Final = "skip"
RESULT_PLAY: Final = "play"
RESULT_COMPLETE: Final = "complete"
RESULT_UNKNOWN: Final = "unknown"

SESSION_RESULTS: Final = [
    RESULT_INSTANT_SKIP,
    RESULT_SKIP,
    RESULT_PLAY,
    RESULT_COMPLETE,
    RESULT_UNKNOWN,
]

# A play under this many milliseconds counts as an "instant skip" (accidental
# tap / scrub-through), not a real listen.
INSTANT_SKIP_THRESHOLD_MS: Final = 3000
# A play is a "complete" listen once this fraction of the track was heard.
COMPLETE_THRESHOLD_PERCENT: Final = 0.90

# --------------------------------------------------------------------------
# Top items terms
# --------------------------------------------------------------------------
TERM_SHORT: Final = "short_term"
TERM_MEDIUM: Final = "medium_term"
TERM_LONG: Final = "long_term"
TOP_ITEM_TERMS: Final = [TERM_SHORT, TERM_MEDIUM, TERM_LONG]

TOP_ITEM_TYPE_TRACKS: Final = "tracks"
TOP_ITEM_TYPE_ARTISTS: Final = "artists"

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
# Long-term data lives outside the custom_components tree so it survives
# integration updates/removal. Path is relative to the HA config directory.
STORAGE_DIR_NAME: Final = "music_insights"
DB_FILE_NAME: Final = "music_insights.db"
BACKUP_DIR_NAME: Final = "backups"
EXPORT_DIR_NAME: Final = "exports"

# Current application-level schema version. Bump together with a migration
# function in storage.py's _MIGRATIONS list.
SCHEMA_VERSION: Final = 2

# Default retention: unlimited (None = keep forever).
DEFAULT_RETENTION_DAYS: Final = None

# Automatic snapshot cadence and how many snapshots to keep.
SNAPSHOT_INTERVAL: Final = timedelta(days=1)
SNAPSHOT_KEEP_COUNT: Final = 14

# Legacy prototype file that may exist from the shell-script prototype phase.
LEGACY_JSONL_FILENAME: Final = "spotify_history.jsonl"

# --------------------------------------------------------------------------
# Diagnostics / misc
# --------------------------------------------------------------------------
ATTR_ACCOUNT_ID: Final = "account_id"
SERVICE_EXPORT_DATA: Final = "export_data"
SERVICE_CREATE_SNAPSHOT: Final = "create_snapshot"
SERVICE_RUN_INTEGRITY_CHECK: Final = "run_integrity_check"
SERVICE_IMPORT_LEGACY_JSONL: Final = "import_legacy_jsonl"
