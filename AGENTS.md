# Syncarr project notes

## Project structure

- `index.py` selects the multi-job engine when `SYNCARR_CONFIG` or indexed multi-job environment variables are set. Otherwise it starts the original single-pair implementation in `legacy_index.py`.
- The legacy configuration remains in `config.py` and `config.conf`.
- The multi-job configuration loader is `multi_config.py`; synchronization and scheduling are in `multi_sync.py`.
- The Docker image currently uses Python 3.6. Keep new code compatible with Python 3.6 unless the image runtime is deliberately upgraded.
- The multi-job mode uses YAML through PyYAML. Endpoint URLs and API keys are supplied through environment variables, not embedded in example configuration files or logs.

## Multi-job configuration decisions

- Jobs are directional source-to-target rules. Each job has its own stable ID and interval.
- `reinitialize_b` is an optional top-level multi-job setting, also available as `SYNCARR_REINITIALIZE_B` for indexed configuration. It defaults off and leaves continuous scheduling unchanged. When enabled, run all jobs once in configuration order, ignore intervals, perform a final Radarr recovery pass, log status 0 on success or 1 if any job/recovery step failed, then emit a red warning and keep the process idle until stopped. This prevents an exit-triggered Docker restart from immediately repeating the run; a new process start runs it again while the setting remains enabled.
- The one-time Radarr recovery pass only considers fileless target movies that existed before the run and remain after normal job decisions. A source movie qualifies when it has a file and passes that incoming job's regular source and file filters; source monitoring does not matter. If any incoming job qualifies it, set the target movie monitored and start one search, regardless of that job's `monitor_new_content` or `auto_search`. Normal add/update/delete rules remain unchanged, and newly added target movies are not searched again by recovery.
- A rule may map several source library root prefixes to target library roots. The longest matching prefix wins and path matching observes directory boundaries. If mappings are configured and none matches an item's source path, that item is skipped with a warning. Without mappings, `target_root_path` is used; if neither is configured, the legacy parent-directory fallback is used.
- A single target may receive content from multiple source instances. The default `keep_if_any_source` delete conflict policy protects an item while any configured source's effective presence rule says it is present. For Radarr, presence normally requires only `hasFile: true`. A delete-enabled job may opt into `delete_if_filter_not_matching`; then a movie with a file is present for that job only when at least one source movie file passes that job's quality and custom-format filters. Profile, tag, and blacklist filters do not affect file presence. `source_rule_wins` evaluates only each delete-enabled rule's own presence set.
- Aliases for the same *arr endpoint must use one API key and one destination conflict policy.
- The `managed_only` deletion scope is the default. Syncarr assigns target tags named `syncarr-<job-id>` to content it manages. Older untagged target items are not eligible under this scope unless manually tagged. `all_missing` is an explicit scope that does not require a Syncarr tag.
- Missing-content deletion is opt-in. Radarr jobs delete missing movies. Sonarr jobs can remove target episode-file entries when a source episode loses its file or no longer matches an enabled filter, then unmonitor that episode. `delete_files` defaults to false; shared media is removed only when every live rule authorizing deletion enables it.
- Deletion is skipped for a destination when any configured source inventory needed for its conflict policy cannot be read. Filter-aware Radarr deletion also requires complete movie-file inventories and the metadata fields used by each active file filter; missing or malformed required metadata fails closed.
- Optional multi-job file filters are `source_quality_match` and custom-format modes `any`, `all`, or `score`. Quality and custom-format checks are combined on the same source file. Sonarr evaluates episode files individually; only matching episodes are monitored. Multiple jobs targeting one Sonarr series combine matching episodes as a union. Unfiltered jobs retain their prior monitoring behavior.
- Sonarr file-filter jobs add a series only when at least one source episode file matches. Fileless and nonmatching episodes remain unmonitored. Episode matching uses season and episode numbers; newly monitored matching episodes are searched when `auto_search` is enabled.
- Multi-job mode does not expose the legacy Sonarr language-profile setting; use file-level custom-format filters when matching those formats.
- `test_run` may be set globally or per job. A test run performs read-only planning and does not add, update, tag, or delete content.
- In the one-time run, per-job `test_run` behavior is preserved. If every job is in test mode, restoration, search, and delete plans are logged without writes. A live job override remains live.
- Entity actions use `ENTITY`-prefixed JSON records with title, stable Arr ID, instance IDs, action, reason, and file state. One-time recovery uses `update_movie_monitoring` and `search_movie` actions with reason `reinitialize_b_missing_file`. Multi-job dry-run and live deletion records share the same decision fields. Serialize only allow-listed metadata; never log endpoint URLs, API keys, headers, or request payloads.

## Change hygiene

- Treat `syncarr/syncarr` as upstream. Work against it only when explicitly instructed, and send changes there exclusively through pull requests; never push directly to upstream branches.
- Keep credentials, live endpoint details, private media names, operational stack data, and other user-specific usage information out of tracked files.
- Update this file when project architecture or agreed multi-job behavior changes. Record only general implementation facts and decisions.


## Container publishing

- The container workflow runs the Python 3.6 unit suite and builds `linux/amd64` and `linux/arm64` on pull requests without publishing. Pushes to the default branch publish `latest`, and numeric version tags publish matching tags.
- GHCR packages are set to public after the first publish; public visibility cannot be reverted to private. Publishing uses the workflow's `GITHUB_TOKEN` with package-write permission and links images to the source repository.
- Keep local `.env` files and private runtime configuration out of Git and Docker build contexts. Compose examples use placeholders and mount runtime configuration read-only.
