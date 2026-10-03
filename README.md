# Syncarr

Syncs two Radarr/Sonarr/Lidarr servers through the web API. Useful for syncing a 4k Radarr/Sonarr instance to a 1080p Radarr/Sonarr instance.

* Supports Radarr, Sonarr, and Lidarr.
* Can sync by `profile` name or `profile_id`
* Filter what media gets synced by `profile` name or `profile_id`
* Supports Docker for multiple instances
* Can set interval for syncing
* Support two way sync (one way by default)
* Skip content with missing files
* Legacy Sonarr language-profile support
* Filter source file quality and custom formats for Radarr and Sonarr in multi-job mode
* Filter syncing by tags (Sonarr/Radarr)
* Allow for a test run using `test_run` flag (does everything but actually sync)
* Run multiple independent source-to-target jobs in one process
* Map source library roots to target library roots
* Optionally remove missing, Syncarr-managed Radarr movies and Sonarr episode files from a target

## Configuration

### Multi-job mode

Use multi-job mode when one process should run several source-to-target rules. The existing `config.conf` and single-pair environment variables continue to work when neither `SYNCARR_CONFIG` nor indexed multi-job variables are set.

Start from [`syncarr.example.yml`](syncarr.example.yml). Set `SYNCARR_CONFIG` to its mounted path. The YAML defines named instances and jobs; set the environment variables named by each instance's `url_env` and `api_key_env` fields to provide connection details. Keep API keys outside the YAML file.

The sample starts in `test_run` mode. Review the rules and logs, then set `test_run: false` when ready. Each job runs immediately and then follows its own `interval_seconds` value.

Set the optional top-level `reinitialize_b: true` (or `SYNCARR_REINITIALIZE_B=true` with indexed environment configuration) for a one-time run. Syncarr executes every configured job once in order, ignores intervals, performs the Radarr recovery pass, logs status `0` on success or `1` if any job or recovery step failed, then emits a red warning and stays idle until the container is stopped. Keeping the process alive prevents Docker restart policies from immediately repeating the run after completion. `test_run` remains global or per job. With all jobs in test mode, the run logs planned restorations, searches, and deletions without writing to Radarr; per-job live overrides keep their existing behavior.

### Entity action logs

Both modes write one JSON record per planned or attempted add or update. Multi-job mode also logs delete, monitor, unmonitor, movie-search, and episode-search actions. Entity log messages are prefixed with `ENTITY ` and contain `event: "syncarr.entity"`, `mode`, `action`, `reason`, `job_id`, source and target instance IDs, `arr_type`, `title`, a type-specific ID (`tmdb_id`, `tvdb_id`, or `foreign_artist_id`), `entity_side`, and `has_file`. Sonarr episode records also include season, episode, episode-file, and series IDs. Multi-job delete records include `delete_files` and the authorizing job IDs. `dry_run` records use `outcome: "would_apply"`; live records use `outcome: "attempted"`. Dry-run and live deletion records carry the same entity and decision fields, including `delete_files`.

The one-time Radarr recovery pass considers only target movie records that existed before the run and still have no file after normal jobs finish. It reactivates and searches a target movie if a source movie has a file and passes at least one incoming job's normal source filters and file filters. Source monitoring does not affect qualification. This recovery always sets the target movie to monitored and searches it, independent of `monitor_new_content` and `auto_search`. Newly added target records use normal add behavior and are not searched a second time by recovery. Logs use `update_movie_monitoring` and `search_movie` with reason `reinitialize_b_missing_file`.

Example (formatted here for readability; logs contain one JSON object per line):

```json
{"event":"syncarr.entity","mode":"dry_run","action":"delete_movie","reason":"missing_from_source","job_id":"movies","source_instance":"radarr-a","target_instance":"radarr-b","arr_type":"radarr","entity_side":"target","title":"Example Movie","tmdb_id":12345,"arr_record_id":42,"has_file":true,"source_has_file":false,"delete_files":false,"outcome":"would_apply"}
```

Filter skips and already-synced items are available at `DEBUG` level. Logs use instance IDs and selected entity fields; they do not serialize endpoint URLs, API keys, request headers, or API payloads.

`root_mappings` maps a source library root to a target library root. The longest matching source prefix is selected, with a directory boundary check. An item whose path matches none of a job's configured mappings is skipped and logged. With no mappings, `target_root_path` is used; if that is also absent, Syncarr uses the item's parent directory as the legacy fallback.

For several sources sharing one target, `keep_if_any_source` is the default conflict policy: an item is retained while any configured source's presence rule says it is present. For Radarr, the default rule counts a movie as present when its source record has `hasFile: true`, regardless of file filters. A job can opt into filter-aware deletion with `delete_if_filter_not_matching: true`; together with `delete_missing: true`, a movie with a file counts as present for that job only if at least one source movie file passes that job's `source_quality_match` and custom-format filters. This option is per job and defaults to `false`. A matching file from another source job still protects the target under `keep_if_any_source`; `source_rule_wins` evaluates only the deleting job's own source. Profile, tag, and blacklist filters do not affect this file-presence check.

Deleting missing content is disabled unless a job sets `delete_missing: true`. Filter-aware Radarr deletion fetches source movie-file inventories before planning target deletes. If a required inventory request fails or required filter metadata is missing or malformed, Syncarr skips deletions for that target in the current run. A source movie reporting `hasFile: true` alongside an empty movie-file inventory also fails closed. For Sonarr, deletion removes target episode-file records when a source episode has no file or no longer passes the configured file filters, and unmonitors that episode; the Sonarr series and episode metadata remain. By default, `delete_scope: managed_only` limits deletion to matching `syncarr-<job-id>` tags; older untagged content needs `all_missing` or a manually added rule tag. `delete_files` defaults to `false`, so the Arr entry is removed while the media file remains on disk. On a shared target, files are physically removed only if every live rule authorizing deletion sets `delete_files: true`.

When Radarr deletion is caused by a filter mismatch, the entity log uses `reason: "source_file_filter_mismatch"`, with `source_has_file: true` and `source_filter_mismatch: true`.

File filters are evaluated against each source movie file or Sonarr episode file. `source_quality_match` is a regular expression matched against the file quality name. Custom-format filters use one of three modes: `any` matches at least one configured name, `all` requires every configured name, and `score` requires a minimum `CustomFormatScore`. Optional excluded names veto matches in `any` and `all` modes. Quality and custom-format conditions are combined with AND on the same file. Names match case-insensitively and exactly. A Sonarr series is added only when at least one episode file matches; matching episodes are monitored, and unmatched or fileless episodes are unmonitored. If multiple jobs address the same target series, matching episode sets are combined so one job cannot unmonitor an episode another job needs.

You can configure multi-job mode entirely with indexed environment variables instead of YAML. Set `SYNCARR_INSTANCE_COUNT` and `SYNCARR_JOB_COUNT`, then provide `SYNCARR_INSTANCE_1_ID`, `_TYPE`, `_URL`, `_API_KEY` and corresponding numbered fields. Job fields use `SYNCARR_JOB_1_ID`, `_SOURCE`, `_TARGET`, `_INTERVAL_SECONDS`, `_ROOT_MAPPING_COUNT`, and `SYNCARR_JOB_1_ROOT_MAPPING_1_SOURCE` / `_TARGET`; optional job settings use the same names as the YAML keys in uppercase. Set `SYNCARR_REINITIALIZE_B=true` to select the one-time run. Do not set `SYNCARR_CONFIG` at the same time.

Optional multi-job file-filter fields are `source_quality_match`, `source_custom_format_mode`, `source_custom_format_names`, `source_custom_format_exclude_names`, and `source_custom_format_minimum_score`. For Radarr, `delete_if_filter_not_matching` enables the per-job filter-aware deletion behavior described above; indexed environment configuration uses `SYNCARR_JOB_N_DELETE_IF_FILTER_NOT_MATCHING`. In indexed environment configuration, use the matching `SYNCARR_JOB_N_SOURCE_*` names; custom-format name lists are comma-separated. For example:

```yaml
source_quality_match: '^Bluray-2160p$'
source_custom_format_mode: any
source_custom_format_names:
  - Dolby Vision without fallback
source_custom_format_exclude_names:
  - HDR10 fallback
```

Radarr only, to let this filtered job delete a target movie when none of its source files pass those filters:

```yaml
delete_missing: true
delete_if_filter_not_matching: true
```

For score mode, set `source_custom_format_mode: score` and `source_custom_format_minimum_score`; do not set name or exclusion lists. When Sonarr episode filters are active, `auto_search` searches newly monitored matching episodes only. `delete_missing` and `delete_files` remain opt-in; use both as `true` to physically remove the target media file when its source episode disappears or stops matching.

### Legacy single-pair mode

 1. Edit the config.conf file and enter your servers URLs and API keys for each server.  
 2. Add the profile name (case insensitive) and movie path for the Radarr instance the movies will be synced to:

   ```ini
    [radarrA]
    url = https://4k.example.com:443
    key = XXXXX
    
    [radarrB]
    url = http://127.0.0.1:8080
    key = XXXXX
    profile = 1080p
    path = /data/Movies # if not given will use RadarrA path for each movie - may not be what you want!
    ```

 3. Or if you want to sync two Sonarr instances:

    ```ini
    [sonarrA]
    url = https://4k.example.com:443
    key = XXXXX
    
    [sonarrB]
    url = http://127.0.0.1:8080
    key = XXXXX
    profile = 1080p
    path = /data/Shows

 4. Or if you want to sync two Lidarr instances:
 5. 
    ```ini
    [lidarrA]
    url = https://lossless.example.com:443
    key = XXXXX
    
    [lidarrB]
    url = http://127.0.0.1:8080
    key = XXXXX
    profile = Standard
    path = /data/Music
    ```
    
    **Note:** Legacy single-pair mode supports one *arr type per configuration. Multi-job mode can define several jobs and instance pairs in one configuration.

 6. Optional Configuration
 
    ```ini
    [*arrA]
    url = http://127.0.0.1:8080
    key = XXXXX
    profile_filter = 1080p # add a filter to only sync contents belonging to this profile (can set by profile_filter_id as well)
    quality_match = HD- # (legacy Radarr only) regex against downloaded movie-file quality; multi-job source_quality_match also supports Sonarr episodes
    tag_filter = Horror # (Sonarr/Radarr) sync movies by tag name (seperate multiple tags by comma (no spaces) ie horror,comedy,action)
    tag_filter_id = 2 # (Sonarr/Radarr) sync movies by tag id (seperate multiple tags by comma (no spaces) ie 2,3,4)
    blacklist = movie-name-12,movie-name-43,432534,8e38819d-71be-9e7d-b41d-f1df91b01d3f # comma seperated list of content slugs OR IDs you want to never sync from A to B (no spaces)
         # the slug is the part of the URL after "/movies/" (for Radarr), "/series/" (for Sonarr), or "/artist/" (for Lidarr)

    [*arrB]
    url = http://127.0.0.1:8080
    key = XXXXX
    profile_id = 1 # Syncarr will try to find id from name but you can specify the id directly if you want
    language = Vietnamese # legacy Sonarr setting
    path = /data/Movies

    [general]
    sync_bidirectionally = 1 # sync from instance A to B **AND** instance B to A (default 0)
    auto_search = 0 # search is automatically started on new content - disable by setting to 0 (default 1)
    skip_missing = 1 # content with missing files are skipped on sync - disable by setting to 0 (default 1) (Radarr only)
    monitor_new_content = 0 # set to 0 to never monitor new content synced or to 1 to always monitor new content synced (default 1)
    test_run = 1 # enable test mode - will run through sync program but will not actually sync content (default 0)
    sync_monitor = 1 # if set to 1 will sync if the content is monitored or not to instance B (default 0)
    ```

    **Note** If `sync_bidirectionally` is set to `1`, then instance A will require either `profile_id` or `profile` AND `path` as well

---

## Requirements
 * Python 3.6 or greater
 * 2 Radarr, Sonarr, or Lidarr servers
  
---

## How to Run
 1. install the needed python modules (you'll need pip or you can install the modules manually inside the `requirements.txt` file):
    ```bash
    pip install -r requirements.txt
    ```
 2. run this script directly or through a Cron:
    ```bash
    python index.py
    ```

---
## Docker Compose
Copy the sample config and create a local `.env` with the values it references. Keep both files private; `.env` and `syncarr.yml` are ignored by Git and excluded from the image build.

```bash
cp syncarr.example.yml syncarr.yml
```

Example `compose.yaml`:

```yaml
services:
  syncarr:
    image: ${SYNCARR_IMAGE:-ghcr.io/your-namespace/syncarr:latest}
    container_name: syncarr
    restart: unless-stopped
    env_file: .env
    environment:
      SYNCARR_CONFIG: /config/syncarr.yml
    volumes:
      - ./syncarr.yml:/config/syncarr.yml:ro
```

Set `SYNCARR_IMAGE` to the GHCR image path. After its first publish, set the package visibility to Public. The local `.env` must define the endpoint URLs and API keys referenced in `syncarr.yml`; keep real values out of source control. The included sample config starts with `test_run: true`.

Example local `.env` values (replace placeholders locally; do not commit this file):

```dotenv
SYNCARR_IMAGE=ghcr.io/your-namespace/syncarr:latest
RADARR_A_URL=https://radarr-a.example.invalid
RADARR_A_KEY=replace-with-a-local-key
RADARR_B_URL=https://radarr-b.example.invalid
RADARR_B_KEY=replace-with-a-local-key
RADARR_TARGET_URL=https://radarr-target.example.invalid
RADARR_TARGET_KEY=replace-with-a-local-key
```

---

## Docker

For just plain docker (radarr example):

```bash
docker run -it --rm --name syncarr -e RADARR_A_URL=https://radarr-a.example.invalid -e RADARR_A_KEY=replace-me -e RADARR_B_URL=https://radarr-b.example.invalid -e RADARR_B_KEY=replace-me -e RADARR_B_PROFILE=1080p -e RADARR_B_PATH=/data/Movies -e SYNC_INTERVAL_SECONDS=300 ghcr.io/your-namespace/syncarr:latest
```

## Notes

* You can also specify the `PROFILE_ID` directly through the `*ARR_A_PROFILE_ID` and `*ARR_B_PROFILE_ID` ENV variables.
To filter by profile in docker use `*ARR_A_PROFILE_FILTER` or `*ARR_A_PROFILE_FILTER_ID` ENV variables. (same for `*arr_B` in bidirectional sync)
* Legacy single-pair mode accepts Sonarr language settings through `SONARR_B_LANGUAGE` or `SONARR_B_LANGUAGE_ID` (and `SONARR_A` for bidirectional sync)
* Set bidirectional sync with `SYNCARR_BIDIRECTIONAL_SYNC=1` (default 0)
* Set disable auto searching on new content with `SYNCARR_AUTO_SEARCH=0`  (default 1)
* Set if you want to NOT monitor new content with `SYNCARR_MONITOR_NEW_CONTENT=0`  (default 1)
* Legacy Radarr: match file quality with `*ARR_A_QUALITY_MATCH` or `*ARR_B_QUALITY_MATCH`; multi-job mode uses per-job `source_quality_match` for Radarr and Sonarr
* Filter by tag names or ids with `*ARR_A_TAG_FILTER` / `*ARR_B_TAG_FILTER` or `*ARR_A_TAG_FILTER_ID` / `*ARR_B_TAG_FILTER_ID`
* Enable test mode with `SYNCARR_TEST_RUN`
* add blacklist with `*ARR_A_BLACKLIST` and `**ARR_B_BLACKLIST`
* sync monitor settings with  `SYNCARR_SYNC_MONITOR`
  
---

## Troubleshooting

If you need to troubleshoot syncarr, then you can either set the log level through the config file:

```ini
[general]
log_level = 10
```

Or in docker, set the `LOG_LEVEL` ENV variable. Default is set to `20` (info only) but you can set to `10` to get debug info as well. When pasting debug logs online, **make sure to remove any apikeys and any other data you don't want others to see.**

---

## Disclaimer

Back up your instances before trying this out. I am not responsible for any lost data.
