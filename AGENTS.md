# Syncarr project notes

## Project structure

- `index.py` selects the multi-job engine when `SYNCARR_CONFIG` or indexed multi-job environment variables are set. Otherwise it starts the original single-pair implementation in `legacy_index.py`.
- The legacy configuration remains in `config.py` and `config.conf`.
- The multi-job configuration loader is `multi_config.py`; synchronization and scheduling are in `multi_sync.py`.
- The Docker image currently uses Python 3.6. Keep new code compatible with Python 3.6 unless the image runtime is deliberately upgraded.
- The multi-job mode uses YAML through PyYAML. Endpoint URLs and API keys are supplied through environment variables, not embedded in example configuration files or logs.

## Multi-job configuration decisions

- Jobs are directional source-to-target rules. Each job has its own stable ID and interval.
- A rule may map several source library root prefixes to target library roots. The longest matching prefix wins and path matching observes directory boundaries. If mappings are configured and none matches an item's source path, that item is skipped with a warning. Without mappings, `target_root_path` is used; if neither is configured, the legacy parent-directory fallback is used.
- A single target may receive content from multiple source instances. The default `keep_if_any_source` delete conflict policy protects an item while any configured source still contains it. `source_rule_wins` is an explicit alternative that evaluates each delete-enabled rule independently.
- Aliases for the same *arr endpoint must use one API key and one destination conflict policy.
- The `managed_only` deletion scope is the default. Syncarr assigns target tags named `syncarr-<job-id>` to content it manages. Older untagged target items are not eligible under this scope unless manually tagged. `all_missing` is an explicit scope that does not require a Syncarr tag.
- Missing-content deletion is opt-in. Radarr jobs delete missing movies. Sonarr jobs can remove target episode-file entries when a source episode loses its file or no longer matches an enabled filter, then unmonitor that episode. `delete_files` defaults to false; shared media is removed only when every live rule authorizing deletion enables it.
- Deletion is skipped for a destination when any configured source inventory needed for its conflict policy cannot be read.
- Optional multi-job file filters are `source_quality_match` and custom-format modes `any`, `all`, or `score`. Quality and custom-format checks are combined on the same source file. Sonarr evaluates episode files individually; only matching episodes are monitored. Multiple jobs targeting one Sonarr series combine matching episodes as a union. Unfiltered jobs retain their prior monitoring behavior.
- Sonarr file-filter jobs add a series only when at least one source episode file matches. Fileless and nonmatching episodes remain unmonitored. Episode matching uses season and episode numbers; newly monitored matching episodes are searched when `auto_search` is enabled.
- Multi-job mode does not expose the legacy Sonarr language-profile setting; use file-level custom-format filters when matching those formats.
- `test_run` may be set globally or per job. A test run performs read-only planning and does not add, update, tag, or delete content.

## Change hygiene

- Keep credentials, live endpoint details, private media names, operational stack data, and other user-specific usage information out of tracked files.
- Update this file when project architecture or agreed multi-job behavior changes. Record only general implementation facts and decisions.


## Container publishing

- The container workflow builds `linux/amd64` and `linux/arm64`. Pull requests build without publishing; pushes to the default branch publish `latest`, and numeric version tags publish matching tags.
- GHCR publishing uses the workflow's `GITHUB_TOKEN` with package-write permission. Link images to the source repository with the OCI source label.
- Keep local `.env` files and private runtime configuration out of Git and Docker build contexts. Compose examples use placeholders and mount runtime configuration read-only.
