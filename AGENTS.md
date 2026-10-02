# Syncarr project notes

## Project structure

- `index.py` selects the multi-job engine when `SYNCARR_CONFIG` or indexed multi-job environment variables are set. Otherwise it starts the original single-pair implementation in `legacy_index.py`.
- The legacy configuration remains in `config.py` and `config.conf`.
- The multi-job configuration loader is `multi_config.py`; synchronization and scheduling are in `multi_sync.py`.
- The Docker image currently uses Python 3.6. Keep new code compatible with Python 3.6 unless the image runtime is deliberately upgraded.
- The multi-job mode uses YAML through PyYAML. Endpoint URLs and API keys are supplied through environment variables, not embedded in example configuration files.

## Multi-job configuration decisions

- Jobs are directional source-to-target rules. Each job has its own stable ID and interval.
- A rule may map several source library root prefixes to target library roots. The longest matching prefix wins and path matching observes directory boundaries. If mappings are configured and none matches an item's source path, that item is skipped with a warning. Without mappings, `target_root_path` is used; if neither is configured, the legacy parent-directory fallback is used.
- A single target may receive content from multiple source instances. The default `keep_if_any_source` delete conflict policy protects an item while any configured source still contains it. `source_rule_wins` is an explicit alternative that evaluates each delete-enabled rule independently.
- Aliases for the same *arr endpoint must use one API key and one destination conflict policy.
- The `managed_only` deletion scope is the default. Syncarr assigns target tags named `syncarr-<job-id>` to content it manages. Older untagged target items are not eligible under this scope unless manually tagged. `all_missing` is an explicit scope that does not require a Syncarr tag.
- Missing-content deletion is opt-in, supported for Radarr jobs, and uses Radarr's `DELETE /api/v3/movie/{id}` endpoint. `delete_files` defaults to false. For a shared target, media files are removed only when every rule authorizing the deletion enables `delete_files`.
- Deletion is skipped for a destination when any configured source inventory needed for its conflict policy cannot be read.
- `test_run` may be set globally or per job. A test run performs read-only planning and does not add, update, tag, or delete content.

## *arr API notes

- Radarr and Sonarr use API v3 routes; Lidarr uses API v1 routes in this codebase.
- The external IDs used for matching are `tmdbId` (Radarr), `tvdbId` (Sonarr), and `foreignArtistId` (Lidarr).
- API keys are sent in the `X-Api-Key` request header by the multi-job engine and must not be written to logs, documentation, or committed configuration.

## Change hygiene

- Keep credentials, live endpoint details, private media names, operational stack data, and other user-specific usage information out of tracked files.
- Update this file when project architecture or agreed multi-job behavior changes. Record only general implementation facts and decisions.
