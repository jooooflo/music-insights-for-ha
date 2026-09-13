# Music Insights for Home Assistant (MI-HA)

A Home Assistant custom integration (`music_insights`) that builds a
**long-term, provider-agnostic listening history and statistics store**.
Spotify is the first supported provider.

> **Status: v0.1 — storage & architecture foundation.** Live playback
> tracking, recently-played reconciliation, top-items snapshots and a first
> set of sensors are implemented and functional. Statistics sensors,
> dashboards, and the second provider are follow-up work.

## Why a separate database?

Home Assistant's own recorder/history is short-lived and not designed for
year-over-year music statistics. MI-HA keeps its own SQLite database at:

```
/config/music_insights/music_insights.db
```

This is **outside** `custom_components/music_insights/`, so:

- Updating or reinstalling the integration never touches your data.
- Removing the integration from HA does **not** delete your history — you
  have to delete the folder yourself if you really want to.
- The database can be backed up, copied, or inspected independently of HA.

### Long-term data safety, concretely

- **Unlimited retention by default.** Nothing is ever auto-deleted.
- **Schema versioning.** Every database carries a `schema_migrations` /
  `meta.schema_version` record. Future integration versions migrate the
  schema forward automatically and idempotently.
- **Integrity checks.** `PRAGMA integrity_check` + `PRAGMA foreign_key_check`
  run on every startup and via the `music_insights.run_integrity_check`
  service.
- **Automatic snapshots.** A consistent on-disk copy (via SQLite's own
  `backup()` API, so it's safe even while the DB is being written) is taken
  daily to `/config/music_insights/backups/` and on-demand via the
  `music_insights.create_snapshot` service. The 14 most recent snapshots are
  kept.
- **Deduplication.** Every play session gets a stable hash
  (`account + track + start time`). Re-importing overlapping data (recently
  played catch-up, HA restarts, manual imports) never creates duplicates —
  it merges into the existing row instead.
- **Export prepared.** `music_insights.export_data` dumps all sessions and
  aggregates for an account to JSON under `/config/music_insights/exports/`.
  Import of that format, and of other providers' exports, is a planned
  follow-up once the format has proven itself.

## Data model (v0.1)

| Table | Purpose |
|---|---|
| `providers`, `accounts` | One row per music service / connected account |
| `artists`, `albums`, `tracks`, `track_artists` | Deduplicated catalogue, keyed by provider + external id |
| `play_sessions` | One row per listen: `started_at`, `ended_at`, `duration_ms`, `listened_ms`, `completion_percent`, `result` (`instant_skip`/`skip`/`play`/`complete`/`unknown`), `device`, `source`, `spotify_played_at` |
| `daily_stats`, `yearly_stats` | Recomputed aggregates per account/day/year |
| `top_items_snapshots` | Spotify's short/medium/long-term top tracks & artists, captured over time |

`result` classification: a play under 3s counts as `instant_skip`; under
50% of the track's duration is a `skip`; 90%+ is `complete`; otherwise
`play`. Thresholds live in `const.py` and are meant to be tuned.

## Authentication

MI-HA uses Home Assistant's **Application Credentials** + OAuth2, exactly
like the official Spotify integration. There are no token files, no shell
scripts, and nothing to run outside HA.

Scopes requested (and only these):

- `user-read-currently-playing`
- `user-read-playback-state`
- `user-read-recently-played`
- `user-top-read`

## Polling

| Coordinator | Interval | Purpose |
|---|---|---|
| Playback | 5s while something is playing, 10s while idle | Builds `play_sessions` in near-real-time |
| Recently played | every 5 min | Catches sessions the live poller missed (restarts, network drops); safe due to dedup |
| Top items | every 6 h | Snapshots short/medium/long-term top tracks & artists |

## Installation (manual, pre-HACS)

1. Copy `custom_components/music_insights/` into your Home Assistant
   `config/custom_components/` folder, so you end up with
   `config/custom_components/music_insights/manifest.json`.
2. Restart Home Assistant.
3. Create a Spotify app at the
   [Spotify Developer Dashboard](https://developer.spotify.com/dashboard):
   - Add Redirect URI: `https://my.home-assistant.io/redirect/oauth`
     (or your own instance's `https://<your-ha-url>/auth/external/callback`
     if you don't use My Home Assistant).
   - Note the **Client ID** and **Client Secret**.
4. In HA: **Settings → Devices & Services → Application Credentials → Add
   Application Credential**, select the `music_insights` domain, and enter
   the Client ID/Secret from step 3.
5. **Settings → Devices & Services → Add Integration → Music Insights**,
   then complete the Spotify login/consent screen.
6. Verify: **Settings → Devices & Services → Music Insights** should show a
   device with sensors (Currently playing, Listening time today, ...).

## Installation via HACS (once published)

1. HACS → Integrations → ⋮ → Custom repositories → add this repo URL,
   category "Integration".
2. Install "Music Insights (MI-HA)", restart HA, then continue from step 3
   above.

Repository: https://github.com/jooooflo/music-insights-for-ha (MIT licensed).

Still open before submitting to the HACS default store:

- Tag a release (`v0.1.0`) — HACS installs from GitHub releases/tags.
- Add a repo description/topics and a few screenshots for the HACS listing.

## Testing checklist

- [ ] Config flow completes and creates a config entry named "Spotify (<display name>)".
- [ ] `Currently playing` sensor updates within ~5s of starting/skipping a track on Spotify.
- [ ] Stopping playback closes the session; check `play_sessions` in the DB (see below).
- [ ] `Listening time today` increases after a completed track.
- [ ] Restart HA — no duplicate `play_sessions` rows appear (dedup check).
- [ ] Call `music_insights.run_integrity_check` from Developer Tools → Actions; check the log for `"ok": true`.
- [ ] Call `music_insights.create_snapshot`; confirm a new file appears in `/config/music_insights/backups/`.
- [ ] Call `music_insights.export_data`; confirm a JSON file appears in `/config/music_insights/exports/`.

Inspecting the database directly (stop HA first, or use a read-only copy):

```bash
sqlite3 /config/music_insights/music_insights.db \
  "SELECT started_at, listened_ms, result FROM play_sessions ORDER BY started_at DESC LIMIT 20;"
```

## Importing prototype data

If you have an existing `/config/spotify_history.jsonl` from the shell-script
prototype, call the `music_insights.import_legacy_jsonl` service (optionally
passing a custom `path`). Each line is a JSON object; recognised fields:
`track_id`, `track_name`, `duration_ms`, `album_id`, `album_name`,
`artist_ids`, `artist_names`, `started_at`, `ended_at`, `listened_ms`,
`device`, `spotify_played_at`. Unrecognised/missing fields are handled
gracefully; malformed lines are skipped and logged.

## Architecture

```
custom_components/music_insights/
  __init__.py               entry setup/unload, services, runtime wiring
  const.py                  domain, scopes, intervals, thresholds
  application_credentials.py  Spotify OAuth2 authorization server
  config_flow.py            OAuth2 config flow, entry title/unique_id
  api.py                    async Spotify Web API client (uses OAuth2Session)
  coordinator.py            playback / recently-played / top-items coordinators
  storage.py                SQLite engine: schema, migrations, dedup, snapshots
  backup.py                 periodic snapshot scheduling
  sensor.py                 sensor entities
  services.yaml, strings.json, translations/{en,de}.json
```

`storage.py` has no Home Assistant imports and no asyncio — it's a plain
synchronous module, always invoked through `hass.async_add_executor_job`,
which keeps it independently testable and keeps blocking SQLite calls off
the event loop.

## Roadmap

- Native Home Assistant Backup integration hooks (`async_pre_backup` /
  `async_post_backup`) instead of (or in addition to) the current
  self-scheduled snapshots.
- Import for the JSON export format (cross-instance migration).
- More statistics sensors (streaks, listening-by-hour, genre breakdown).
- A second provider (structure in `storage.py`/`const.py` is already
  provider-agnostic: every table is keyed by `provider_id`).
