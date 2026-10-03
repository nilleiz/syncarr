#!/usr/bin/env python
"""Multi-job synchronization engine for Radarr, Sonarr, and Lidarr."""

import json
import logging
import re
import time

import requests


LOGGER = logging.getLogger('syncarr')
CONTENT_KEYS = {
    'radarr': 'tmdbId',
    'sonarr': 'tvdbId',
    'lidarr': 'foreignArtistId',
}
LOG_ID_KEYS = {
    'radarr': 'tmdb_id',
    'sonarr': 'tvdb_id',
    'lidarr': 'foreign_artist_id',
}
API_VERSIONS = {'radarr': 'v3', 'sonarr': 'v3', 'lidarr': 'v1'}
CONTENT_ROUTES = {'radarr': 'movie', 'sonarr': 'series', 'lidarr': 'artist'}


class SyncError(Exception):
    """A remote request or sync operation failed without exposing credentials."""


class ArrClient(object):
    def __init__(self, instance):
        self.instance = instance
        self.arr_type = instance['type']
        self.base_url = instance['url'].rstrip('/')
        self.api_version = API_VERSIONS[self.arr_type]
        self.content_route = CONTENT_ROUTES[self.arr_type]
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'X-Api-Key': instance['api_key']})
        self._profiles = None
        self._tags = None

    @property
    def identity(self):
        return self.arr_type, self.base_url

    def request(self, method, route, params=None, payload=None, expected=None):
        url = '{}/api/{}/{}'.format(self.base_url, self.api_version, route.lstrip('/'))
        try:
            response = self.session.request(method, url, params=params, json=payload, timeout=30)
        except requests.RequestException as error:
            raise SyncError('{} request failed ({})'.format(self.arr_type, error.__class__.__name__))
        if expected is not None and response.status_code not in expected:
            raise SyncError('{} API request failed with HTTP {}'.format(self.arr_type, response.status_code))
        if expected is None and not response.ok:
            raise SyncError('{} API request failed with HTTP {}'.format(self.arr_type, response.status_code))
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise SyncError('{} API returned invalid JSON'.format(self.arr_type))

    def list_content(self):
        result = self.request('GET', self.content_route, expected=(200,))
        if not isinstance(result, list):
            raise SyncError('{} content response was not a list'.format(self.arr_type))
        return result

    def profiles(self):
        if self._profiles is None:
            self._profiles = self.request('GET', 'qualityprofile', expected=(200,))
        return self._profiles

    def profile_id(self, name, explicit_id, setting_name):
        if explicit_id is not None:
            return explicit_id
        if not name:
            return None
        profile = next((item for item in self.profiles()
                       if str(item.get('name', '')).lower() == name.lower()), None)
        if profile is None:
            raise SyncError('Could not resolve {} on the configured instance'.format(setting_name))
        return profile.get('id')

    def tags(self):
        if self._tags is None:
            result = self.request('GET', 'tag', expected=(200,))
            if not isinstance(result, list):
                raise SyncError('{} tag response was not a list'.format(self.arr_type))
            self._tags = result
        return self._tags

    def tag_id(self, label, create=False):
        item = next((tag for tag in self.tags()
                     if str(tag.get('label', '')).lower() == label.lower()), None)
        if item is None and create:
            created = self.request('POST', 'tag', payload={'label': label}, expected=(200, 201))
            if isinstance(created, dict) and created.get('id') is not None:
                self._tags.append(created)
                return created['id']
            # Some *arr versions return only a status. Re-read to resolve the ID.
            self._tags = None
            return self.tag_id(label, create=False)
        return item.get('id') if item else None

    def list_movie_files(self, movie_id):
        result = self.request('GET', 'moviefile', params={'movieId': movie_id}, expected=(200,))
        if not isinstance(result, list):
            raise SyncError('Radarr movie-file response was not a list')
        return result

    def list_episodes(self, series_id):
        result = self.request('GET', 'episode',
                              params={'seriesId': series_id, 'includeEpisodeFile': 'true'},
                              expected=(200,))
        if not isinstance(result, list):
            raise SyncError('Sonarr episode response was not a list')
        return result

    def set_episodes_monitored(self, episode_ids, monitored):
        if not episode_ids:
            return
        self.request('PUT', 'episode/monitor',
                     payload={'episodeIds': list(episode_ids), 'monitored': bool(monitored)},
                     expected=(200, 202))

    def delete_episode_file(self, file_id, delete_files):
        self.request('DELETE', 'episodefile/{}'.format(file_id),
                     params={'deleteFiles': 'true' if delete_files else 'false'},
                     expected=(200, 202, 204))

    def delete_movie(self, content_id, delete_files):
        self.request('DELETE', 'movie/{}'.format(content_id),
                     params={'deleteFiles': 'true' if delete_files else 'false',
                             'addImportExclusion': 'false'},
                     expected=(200, 202, 204))

    def close(self):
        self.session.close()


def rule_tag(job):
    return 'syncarr-{}'.format(job['id']).lower()


def _path_key(path):
    original = str(path or '')
    normalized = original.replace('\\', '/')
    if normalized != '/':
        normalized = normalized.rstrip('/')
    if '\\' in original or re.match(r'^[A-Za-z]:/', normalized):
        return normalized.casefold()
    return normalized


def map_root_path(content_path, job):
    """Map the longest matching source prefix; return (root, error reason)."""
    mappings = job['root_mappings']
    if mappings:
        source_path = _path_key(content_path)
        if not source_path:
            return None, 'source path is missing'
        candidates = []
        for mapping in mappings:
            source_prefix = _path_key(mapping['source'])
            if not source_prefix:
                continue
            prefix_matches = (
                source_path == source_prefix or
                (source_prefix == '/' and source_path.startswith('/')) or
                source_path.startswith(source_prefix.rstrip('/') + '/')
            )
            if not prefix_matches:
                continue
            candidates.append((len(source_prefix), mapping['target']))
        if not candidates:
            return None, 'no root mapping matched the source path'
        unused_length, target_root = max(candidates, key=lambda value: value[0])
        return target_root, None

    if job['target_root_path']:
        return job['target_root_path'], None
    if not content_path:
        return None, 'source path is missing and no target root is configured'
    normalized = str(content_path).rstrip('/\\')
    parent = re.sub(r'[/\\][^/\\]+$', '', normalized)
    if not parent and (normalized.startswith('/') or re.match(r'^[A-Za-z]:[/\\]', normalized)):
        parent = normalized[:1] if normalized.startswith('/') else normalized[:3]
    return parent or None, None if parent else 'could not determine a source root'


def _content_key(content, arr_type):
    value = content.get(CONTENT_KEYS[arr_type])
    return str(value) if value is not None else None


def _log_entity(job, target_client, action, reason, content, entity_side='source',
                mode=None, level=logging.INFO, **details):
    """Emit a JSON entity event without serializing config or API payloads."""
    arr_type = target_client.arr_type
    content = content if isinstance(content, dict) else {}
    source = job.get('source') or {}
    target = job.get('target') or {}
    external_id = content.get(CONTENT_KEYS[arr_type])
    record = {
        'event': 'syncarr.entity',
        'mode': mode or ('dry_run' if job.get('test_run') else 'live'),
        'action': action,
        'reason': reason,
        'job_id': job.get('id'),
        'source_instance': job.get('source_instance_id') or source.get('id'),
        'target_instance': job.get('target_instance_id') or target.get('id'),
        'arr_type': arr_type,
        'entity_side': entity_side,
        'title': content.get('title') or content.get('artistName'),
        LOG_ID_KEYS[arr_type]: external_id,
        'arr_record_id': content.get('id'),
        'has_file': _has_file(content),
    }
    record.update(details)
    LOGGER.log(level, 'ENTITY %s', json.dumps(record, sort_keys=True, ensure_ascii=False))


def _path_for_content(content, job, target_client):
    root_path, reason = map_root_path(content.get('path'), job)
    if reason:
        _log_entity(job, target_client, 'skip', 'root_mapping_failed', content,
                    mapping_reason=reason, level=logging.WARNING)
    return root_path


def _resolve_source_filters(client, job):
    profile_filter_id = job['source_profile_filter_id']
    if profile_filter_id is None and job['source_profile_filter']:
        profile_filter_id = client.profile_id(job['source_profile_filter'], None, 'source profile filter')
    tag_filter_ids = list(job['source_tag_filter_id'])
    if job['source_tag_filter']:
        labels = {str(item.get('label', '')).lower(): item.get('id') for item in client.tags()}
        for tag_name in job['source_tag_filter']:
            tag_id = labels.get(tag_name.lower())
            if tag_id is None:
                raise SyncError('Could not resolve a source tag filter')
            tag_filter_ids.append(tag_id)
    return profile_filter_id, set(tag_filter_ids)


def _passes_filters(content, client, job, profile_filter_id, tag_filter_ids):
    if profile_filter_id is not None and content.get('qualityProfileId') != profile_filter_id:
        return False
    if tag_filter_ids and not (set(content.get('tags') or []) & tag_filter_ids):
        return False
    blacklist = set(job['source_blacklist'])
    if blacklist:
        slug = content.get('titleSlug') or content.get('foreignArtistId')
        if str(slug) in blacklist or str(content.get('id')) in blacklist:
            return False
    if client.arr_type == 'radarr' and job['skip_missing'] and not content.get('hasFile'):
        return False
    return True


def _has_file(record):
    if not isinstance(record, dict):
        return False
    file_record = record.get('episodeFile') or record.get('movieFile') or {}
    return bool(record.get('hasFile') or record.get('episodeFileId') or file_record.get('id'))


def _passes_file_filters(file_record, job):
    if not job['has_file_filters']:
        return True
    if not isinstance(file_record, dict):
        return False

    quality_match = job['source_quality_match']
    if quality_match:
        quality = file_record.get('quality') or {}
        quality_name = (quality.get('quality') or {}).get('name')
        if not quality_name or not re.match(quality_match, str(quality_name)):
            return False

    mode = job['source_custom_format_mode']
    if mode == 'score':
        try:
            score = int(file_record.get('customFormatScore'))
        except (TypeError, ValueError):
            return False
        if score < job['source_custom_format_minimum_score']:
            return False
    elif mode in ('any', 'all'):
        formats = file_record.get('customFormats') or []
        names = {str(item.get('name', '')).casefold() for item in formats if isinstance(item, dict)}
        required = {name.casefold() for name in job['source_custom_format_names']}
        excluded = {name.casefold() for name in job['source_custom_format_exclude_names']}
        if names & excluded:
            return False
        if mode == 'any' and not (names & required):
            return False
        if mode == 'all' and not required.issubset(names):
            return False
    return True


def _episode_key(external_id, episode):
    try:
        season_number = int(episode.get('seasonNumber'))
        episode_number = int(episode.get('episodeNumber'))
    except (TypeError, ValueError):
        return None
    return str(external_id), season_number, episode_number


def _episode_file_id(episode):
    return episode.get('episodeFileId') or (episode.get('episodeFile') or {}).get('id')


def _episode_log_fields(episode):
    return {
        'episode_id': episode.get('id'),
        'season_number': episode.get('seasonNumber'),
        'episode_number': episode.get('episodeNumber'),
        'episode_file_id': _episode_file_id(episode),
    }


def _source_episodes(client, content, episode_cache):
    series_id = content.get('id')
    if series_id is None:
        return []
    cache_key = (client.identity, str(series_id))
    if cache_key not in episode_cache:
        episode_cache[cache_key] = client.list_episodes(series_id)
    return episode_cache[cache_key]


def _matching_source_episodes(client, content, job, episode_cache):
    episodes = _source_episodes(client, content, episode_cache)
    return [episode for episode in episodes
            if _has_file(episode) and
            _passes_file_filters(episode.get('episodeFile'), job)]


def _image_payload(content, target_url):
    images = []
    for image in content.get('images') or []:
        entry = dict(image)
        image_url = entry.get('url')
        if image_url and not image_url.startswith(('http://', 'https://')):
            entry['url'] = target_url.rstrip('/') + '/' + image_url.lstrip('/')
        images.append(entry)
    return images


def _build_payload(content, job, client, root_path, tag_id, matching_episodes=None):
    target = job['target']
    profile_id = job['resolved_profile_id']
    monitored = job['monitor_new_content'] if job['monitor_new_content'] is not None else content.get('monitored', True)
    payload = {
        CONTENT_KEYS[target['type']]: content.get(CONTENT_KEYS[target['type']]),
        'qualityProfileId': profile_id or content.get('qualityProfileId'),
        'monitored': monitored,
        'rootFolderPath': root_path,
        'images': _image_payload(content, target['url']),
        'tags': [tag_id] if tag_id is not None else [],
    }
    add_options = dict(content.get('addOptions') or {})
    if target['type'] == 'radarr':
        payload.update({
            'title': content.get('title'),
            'year': content.get('year'),
            'tmdbId': content.get('tmdbId'),
            'titleSlug': content.get('titleSlug'),
            'addOptions': dict(add_options, searchForMovie=job['auto_search']),
        })
    elif target['type'] == 'sonarr':
        search_missing = job['auto_search']
        seasons = content.get('seasons')
        if job['has_file_filters']:
            # Let episode-level reconciliation decide what Sonarr monitors and searches.
            # A series-level add search here could grab episodes that failed the file filter.
            search_missing = False
            monitored = bool(matching_episodes)
            seasons = [dict(season, monitored=False) for season in (seasons or [])]
        payload.update({
            'title': content.get('title'),
            'titleSlug': content.get('titleSlug'),
            'seasons': seasons,
            'year': content.get('year'),
            'tvRageId': content.get('tvRageId'),
            'seasonFolder': content.get('seasonFolder'),
            'seriesType': content.get('seriesType'),
            'useSceneNumbering': content.get('useSceneNumbering'),
            'addOptions': dict(add_options, searchForMissingEpisodes=search_missing),
        })
        if job['has_file_filters']:
            payload['monitored'] = monitored
    else:
        payload.update({
            'artistName': content.get('artistName'),
            'foreignArtistId': content.get('foreignArtistId'),
            'albumFolder': content.get('albumFolder'),
            'metadataProfileId': content.get('metadataProfileId'),
            'addOptions': dict(add_options, searchForMissingAlbums=job['auto_search']),
        })
    return payload


def _prepare_job(job, clients, create_tags):
    source_client = clients[job['source']['identity']]
    target_client = clients[job['target']['identity']]
    job['resolved_profile_id'] = target_client.profile_id(
        job['target_profile'], job['target_profile_id'], 'target profile')
    tag_id = target_client.tag_id(rule_tag(job), create=create_tags)
    return source_client, target_client, tag_id


def _sync_items(job, source_client, target_client, tag_id, source_items, target_items, episode_cache=None):
    episode_cache = {} if episode_cache is None else episode_cache
    profile_filter_id, tag_filter_ids = _resolve_source_filters(source_client, job)
    arr_type = source_client.arr_type
    target_by_key = {}
    for item in target_items:
        key = _content_key(item, arr_type)
        if key is not None:
            target_by_key.setdefault(key, []).append(item)

    for content in source_items:
        if not _passes_filters(content, source_client, job, profile_filter_id, tag_filter_ids):
            _log_entity(job, target_client, 'skip', 'source_filters', content,
                        level=logging.DEBUG)
            continue
        matching_episodes = None
        if source_client.arr_type == 'radarr' and job['has_file_filters']:
            movie_id = content.get('id')
            files = source_client.list_movie_files(movie_id) if movie_id is not None else []
            if not any(_passes_file_filters(file_record, job) for file_record in files):
                _log_entity(job, target_client, 'skip', 'source_file_filters', content,
                            level=logging.DEBUG)
                continue
        elif source_client.arr_type == 'sonarr' and job['has_file_filters']:
            matching_episodes = _matching_source_episodes(
                source_client, content, job, episode_cache)
            if not matching_episodes:
                _log_entity(job, target_client, 'skip', 'source_episode_file_filters', content,
                            level=logging.DEBUG)
                continue
        key = _content_key(content, arr_type)
        if key is None:
            _log_entity(job, target_client, 'skip', 'missing_external_id', content,
                        level=logging.WARNING)
            continue
        matches = target_by_key.get(key, [])
        if not matches:
            root_path = _path_for_content(content, job, target_client)
            if not root_path:
                continue
            if job['test_run']:
                _log_entity(job, target_client, 'add', 'missing_on_target', content,
                            outcome='would_apply')
                continue
            payload = _build_payload(content, job, target_client, root_path, tag_id,
                                     matching_episodes=matching_episodes)
            _log_entity(job, target_client, 'add', 'missing_on_target', content,
                        outcome='attempted')
            result = target_client.request('POST', target_client.content_route,
                                           payload=payload, expected=(200, 201))
            if isinstance(result, dict):
                target_items.append(result)
                target_by_key.setdefault(key, []).append(result)
            continue

        if len(matches) > 1:
            _log_entity(job, target_client, 'skip', 'duplicate_target_matches', content,
                        target_record_ids=[item.get('id') for item in matches],
                        level=logging.WARNING)
            continue
        current = matches[0]
        current_tags = list(current.get('tags') or [])
        changed = False
        change_reasons = []
        if tag_id is not None and tag_id not in current_tags:
            current_tags.append(tag_id)
            current['tags'] = current_tags
            changed = True
            change_reasons.append('managed_tag_missing')
        if (job['sync_monitor'] and not (arr_type == 'sonarr' and job['has_file_filters']) and
                current.get('monitored') != content.get('monitored')):
            current['monitored'] = content.get('monitored')
            changed = True
            change_reasons.append('monitored_state_differs')
        if changed:
            details = {
                'target_record_id': current.get('id'),
                'target_has_file': _has_file(current),
                'change_reasons': change_reasons,
                'outcome': 'would_apply' if job['test_run'] else 'attempted',
            }
            _log_entity(job, target_client, 'update', 'target_state_differs', content,
                        **details)
            if not job['test_run']:
                target_client.request('PUT', '{}/{}'.format(target_client.content_route, current['id']),
                                      payload=current, expected=(200, 202))
        else:
            _log_entity(job, target_client, 'skip', 'target_already_in_sync', content,
                        target_record_id=current.get('id'), level=logging.DEBUG)


def _items_for_deletion(target_items, incoming_jobs, source_snapshots, target_client):
    deleting_jobs = [job for job in incoming_jobs if job['delete_missing']]
    if not deleting_jobs:
        return []
    policy = deleting_jobs[0]['target']['delete_conflict_policy']
    arr_type = target_client.arr_type
    source_keys = {}
    for job in incoming_jobs:
        contents = source_snapshots[job['source']['identity']]['contents']
        keys = set()
        for item in contents:
            # Radarr deletion presence is based on an actual source file, but is
            # intentionally independent of profile and file-quality sync filters.
            if arr_type == 'radarr' and item.get('hasFile') is not True:
                continue
            key = _content_key(item, arr_type)
            if key is not None:
                keys.add(key)
        source_keys[job['id']] = keys

    managed_tag_ids = {}
    for job in deleting_jobs:
        tag_id = target_client.tag_id(rule_tag(job), create=False)
        if tag_id is not None:
            managed_tag_ids[job['id']] = tag_id

    candidates = []
    all_source_keys = set().union(*source_keys.values()) if source_keys else set()
    for item in target_items:
        key = _content_key(item, arr_type)
        if key is None:
            continue
        authors = []
        if policy == 'keep_if_any_source':
            if key in all_source_keys:
                continue
            item_tags = set(item.get('tags') or [])
            authors = [job for job in deleting_jobs
                       if job['delete_scope'] == 'all_missing' or
                       managed_tag_ids.get(job['id']) in item_tags]
        else:  # Explicit source_rule_wins policy.
            item_tags = set(item.get('tags') or [])
            for job in deleting_jobs:
                if key in source_keys.get(job['id'], set()):
                    continue
                if job['delete_scope'] == 'all_missing' or managed_tag_ids.get(job['id']) in item_tags:
                    authors.append(job)
        if authors:
            # Shared targets use the conservative file policy: every rule that
            # authorizes this deletion must also allow removal of the media file.
            candidates.append((item, authors))
    return candidates


def _target_group(config, target_identity):
    return [job for job in config['jobs'] if job['target']['identity'] == target_identity]


def _series_episode_keys(external_id, episodes, job):
    keys = set()
    for episode in episodes:
        if not _has_file(episode):
            continue
        if job['has_file_filters'] and not _passes_file_filters(episode.get('episodeFile'), job):
            continue
        key = _episode_key(external_id, episode)
        if key is not None:
            keys.add(key)
    return keys


def _sonarr_episode_plan(incoming_jobs, clients, target_client, target_items,
                         source_snapshots, episode_cache):
    """Build all Sonarr monitor and file-deletion actions before performing writes."""
    target_by_key = {}
    for item in target_items:
        key = _content_key(item, 'sonarr')
        if key is not None:
            target_by_key.setdefault(key, []).append(item)

    tag_ids = {}
    resolved_filters = {}
    source_content_by_job = {}
    for job in incoming_jobs:
        source_client = clients[job['source']['identity']]
        resolved_filters[job['id']] = _resolve_source_filters(source_client, job)
        contents = source_snapshots[job['source']['identity']]
        content_by_key = {}
        for content in contents:
            key = _content_key(content, 'sonarr')
            if key is not None:
                content_by_key.setdefault(key, content)
        source_content_by_job[job['id']] = content_by_key
        tag_ids[job['id']] = target_client.tag_id(rule_tag(job), create=False)

    source_keys_by_job = dict((job['id'], set()) for job in incoming_jobs)
    deleting_jobs = [job for job in incoming_jobs if job['delete_missing']]
    all_missing_deletion = any(job['delete_scope'] == 'all_missing' for job in deleting_jobs)
    plans = []
    duplicate_target_keys = {key for key, items in target_by_key.items() if len(items) > 1}

    for external_id, target_matches in target_by_key.items():
        if external_id in duplicate_target_keys:
            LOGGER.warning('Sonarr episode reconciliation skipped a duplicate target series ID')
            continue
        target_series = target_matches[0]
        target_tags = set(target_series.get('tags') or [])
        applicable_filter_jobs = []
        unfiltered_applicable = False
        desired_source_keys = set()
        auto_search_source_keys = set()

        for job in incoming_jobs:
            source_client = clients[job['source']['identity']]
            content = source_content_by_job[job['id']].get(external_id)
            profile_filter_id, tag_filter_ids = resolved_filters[job['id']]
            passes = bool(content and _passes_filters(
                content, source_client, job, profile_filter_id, tag_filter_ids))
            tagged = (tag_ids[job['id']] is not None and tag_ids[job['id']] in target_tags)

            if content is not None and passes:
                source_episodes = _source_episodes(source_client, content, episode_cache)
                source_keys_by_job[job['id']].update(
                    _series_episode_keys(external_id, source_episodes, job))
            else:
                source_episodes = []

            if job['has_file_filters'] or job['delete_missing']:
                # A previously managed series stays in this rule's episode set even if
                # its source series disappeared or stopped passing a series-level filter.
                if (passes or tagged) and not job['test_run']:
                    applicable_filter_jobs.append(job)
                    if passes:
                        matched = _series_episode_keys(external_id, source_episodes, job)
                        desired_source_keys.update(matched)
                        if job['auto_search']:
                            auto_search_source_keys.update(matched)
            elif passes and not job['test_run']:
                # Preserve the existing whole-series monitoring behavior for an
                # overlapping job that does not participate in episode filtering.
                unfiltered_applicable = True

        if not applicable_filter_jobs and not all_missing_deletion:
            continue

        target_episodes = target_client.list_episodes(target_series.get('id'))
        target_by_episode_key = {}
        for episode in target_episodes:
            key = _episode_key(external_id, episode)
            if key is not None:
                target_by_episode_key[key] = episode

        selected_ids = set()
        newly_monitored_ids = set()
        unmonitored_ids = set()
        for key, episode in target_by_episode_key.items():
            selected = key in desired_source_keys
            episode_id = episode.get('id')
            if episode_id is None:
                continue
            if selected:
                selected_ids.add(episode_id)
                if not episode.get('monitored', False):
                    newly_monitored_ids.add(episode_id)
            elif applicable_filter_jobs and not unfiltered_applicable and episode.get('monitored', False):
                unmonitored_ids.add(episode_id)

        series_monitored = None
        if applicable_filter_jobs and not unfiltered_applicable:
            series_monitored = bool(selected_ids)
        elif selected_ids and not target_series.get('monitored', False):
            series_monitored = True

        deletions = []
        if deleting_jobs:
            policy = deleting_jobs[0]['target']['delete_conflict_policy']
            any_source_episode_keys = set().union(*source_keys_by_job.values()) if source_keys_by_job else set()
            target_files = {}
            episode_authors = {}
            for key, episode in target_by_episode_key.items():
                if not _has_file(episode):
                    continue
                file_id = _episode_file_id(episode)
                if file_id:
                    target_files.setdefault(file_id, []).append((key, episode))
                authors = []
                if policy == 'keep_if_any_source':
                    if key in any_source_episode_keys:
                        continue
                    for author in deleting_jobs:
                        tag_id = tag_ids[author['id']]
                        if author['delete_scope'] == 'all_missing' or (
                                tag_id is not None and tag_id in target_tags):
                            authors.append(author)
                else:
                    for author in deleting_jobs:
                        tag_id = tag_ids[author['id']]
                        if key in source_keys_by_job[author['id']]:
                            continue
                        if author['delete_scope'] == 'all_missing' or (
                                tag_id is not None and tag_id in target_tags):
                            authors.append(author)
                if authors:
                    episode_authors[key] = authors

            # A Sonarr file may cover multiple episodes. Delete it only when every
            # episode attached to that file is eligible and shares an authorizing rule.
            jobs_by_id = {job['id']: job for job in deleting_jobs}
            for file_id, file_episodes in target_files.items():
                author_sets = []
                for episode_key, unused_episode in file_episodes:
                    authors = episode_authors.get(episode_key)
                    if not authors:
                        author_sets = []
                        break
                    author_sets.append(set(author['id'] for author in authors))
                if not author_sets:
                    continue
                common_author_ids = set.intersection(*author_sets)
                if common_author_ids:
                    representative = file_episodes[0][1]
                    deletions.append((representative,
                                      [jobs_by_id[author_id] for author_id in common_author_ids]))

        if applicable_filter_jobs or deletions:
            plans.append({
                'series': target_series,
                'episodes': target_episodes,
                'monitor_true': selected_ids,
                'monitor_false': unmonitored_ids,
                'newly_monitored': newly_monitored_ids & selected_ids,
                'auto_search_keys': auto_search_source_keys,
                'series_monitored': series_monitored,
                'deletions': deletions,
                'jobs': applicable_filter_jobs,
            })

    return plans


def _apply_sonarr_episode_plan(plans, current_job, target_client):
    for plan in plans:
        target_series = plan['series']
        episode_by_id = {episode.get('id'): episode for episode in plan['episodes']}
        new_ids = plan['newly_monitored']
        search_ids = [episode_id for episode_id in new_ids
                      if _episode_key(_content_key(target_series, 'sonarr'), episode_by_id[episode_id])
                      in plan['auto_search_keys'] and not _has_file(episode_by_id[episode_id])]

        active_authors = set(author['id'] for unused_episode, authors in plan['deletions']
                             for author in authors if not author['test_run'])
        mutating_jobs = set(job['id'] for job in plan['jobs'] if not job['test_run']) | active_authors
        mode = 'live' if not current_job['test_run'] and mutating_jobs else 'dry_run'

        if (plan['series_monitored'] is not None and
                target_series.get('monitored') != plan['series_monitored']):
            _log_entity(current_job, target_client, 'update_series_monitoring',
                        'episode_filter_selection', target_series, entity_side='target',
                        mode=mode, target_record_id=target_series.get('id'),
                        monitored=plan['series_monitored'],
                        outcome='attempted' if mode == 'live' else 'would_apply')

        for episode_id in plan['monitor_true']:
            episode = episode_by_id.get(episode_id)
            if episode is not None:
                _log_entity(current_job, target_client, 'monitor_episode',
                            'matches_source_file_filters', target_series,
                            entity_side='target_episode', mode=mode,
                            has_file=_has_file(episode), **_episode_log_fields(episode),
                            source_has_file=True,
                            outcome='attempted' if mode == 'live' else 'would_apply')
        for episode_id in plan['monitor_false']:
            episode = episode_by_id.get(episode_id)
            if episode is not None:
                _log_entity(current_job, target_client, 'unmonitor_episode',
                            'source_episode_missing_or_filtered', target_series,
                            entity_side='target_episode', mode=mode,
                            has_file=_has_file(episode), **_episode_log_fields(episode),
                            source_has_file=False,
                            outcome='attempted' if mode == 'live' else 'would_apply')

        deletion_actions = []
        for episode, authors in plan['deletions']:
            if current_job not in authors:
                continue
            file_id = _episode_file_id(episode)
            if not file_id:
                continue
            live_authors = [author for author in authors if not author['test_run']]
            effective_authors = live_authors or authors
            delete_files = all(author['delete_files'] for author in effective_authors)
            details = _episode_log_fields(episode)
            details.update({
                'target_record_id': target_series.get('id'),
                'delete_files': delete_files,
                'author_job_ids': sorted(author['id'] for author in authors),
                'author_source_instances': sorted(set(
                    author.get('source_instance_id') or author['source'].get('id')
                    for author in authors)),
                'source_has_file': False,
                'outcome': 'attempted' if mode == 'live' else 'would_apply',
            })
            _log_entity(current_job, target_client, 'delete_episode_file',
                        'source_episode_missing_or_filtered', target_series,
                        entity_side='target_episode', mode=mode,
                        has_file=_has_file(episode), **details)
            if mode == 'live' and current_job in live_authors:
                deletion_actions.append((file_id, delete_files))

        if mode != 'live':
            continue

        if (plan['series_monitored'] is not None and
                target_series.get('monitored') != plan['series_monitored']):
            target_series['monitored'] = plan['series_monitored']
            target_client.request('PUT', 'series/{}'.format(target_series['id']),
                                  payload=target_series, expected=(200, 202))

        target_client.set_episodes_monitored(list(plan['monitor_true']), True)
        target_client.set_episodes_monitored(list(plan['monitor_false']), False)

        for file_id, delete_files in deletion_actions:
            target_client.delete_episode_file(file_id, delete_files)

        if search_ids and any(job['auto_search'] and not job['test_run'] for job in plan['jobs']):
            for episode_id in search_ids:
                episode = episode_by_id[episode_id]
                _log_entity(current_job, target_client, 'search_episode',
                            'newly_monitored_source_match', target_series,
                            entity_side='target_episode', mode='live',
                            has_file=_has_file(episode), **_episode_log_fields(episode),
                            source_has_file=True, outcome='attempted')
            target_client.request('POST', 'command',
                                  payload={'name': 'EpisodeSearch', 'episodeIds': search_ids},
                                  expected=(200, 201, 202))


def run_job(config, job, clients):
    source_client, target_client, tag_id = _prepare_job(job, clients, create_tags=not job['test_run'])
    source_items = source_client.list_content()
    target_items = target_client.list_content()
    episode_cache = {}
    _sync_items(job, source_client, target_client, tag_id, source_items, target_items,
                episode_cache=episode_cache)

    if target_client.arr_type == 'sonarr':
        incoming_jobs = _target_group(config, target_client.identity)
        if any(incoming_job['has_file_filters'] or incoming_job['delete_missing']
               for incoming_job in incoming_jobs):
            source_snapshots = {}
            try:
                for incoming_job in incoming_jobs:
                    source_identity = incoming_job['source']['identity']
                    if source_identity not in source_snapshots:
                        incoming_client = clients[source_identity]
                        contents = (source_items if incoming_client.identity == source_client.identity
                                    else incoming_client.list_content())
                        source_snapshots[source_identity] = contents
                # The just-added series and its episode IDs must be visible before we
                # reconcile individual episode monitoring and file deletion.
                target_items = target_client.list_content()
                plans = _sonarr_episode_plan(incoming_jobs, clients, target_client,
                                             target_items, source_snapshots, episode_cache)
            except SyncError as error:
                LOGGER.error('Job %s will not reconcile Sonarr episode state because an inventory failed (%s)',
                             job['id'], error)
                return
            _apply_sonarr_episode_plan(plans, job, target_client)
        return

    if not job['delete_missing']:
        return
    incoming_jobs = _target_group(config, target_client.identity)
    source_snapshots = {}
    safe_to_delete = True
    for incoming_job in incoming_jobs:
        source_key = incoming_job['source']['identity']
        if source_key in source_snapshots:
            continue
        incoming_client = clients[source_key]
        try:
            contents = source_items if incoming_client.identity == source_client.identity else incoming_client.list_content()
            source_snapshots[source_key] = {'client': incoming_client, 'contents': contents}
        except SyncError as error:
            LOGGER.error('Job %s will not delete from its target because a source inventory failed (%s)',
                         job['id'], error)
            safe_to_delete = False
    if not safe_to_delete:
        return

    candidates = _items_for_deletion(target_items, incoming_jobs, source_snapshots, target_client)
    for item, authors in candidates:
        if job not in authors:
            continue
        live_authors = [author for author in authors if not author['test_run']]
        effective_authors = live_authors or authors
        delete_files = all(author['delete_files'] for author in effective_authors)
        _log_entity(job, target_client, 'delete_movie', 'missing_from_source', item,
                    entity_side='target', target_record_id=item.get('id'),
                    source_has_file=False, delete_files=delete_files,
                    author_job_ids=sorted(author['id'] for author in authors),
                    author_source_instances=sorted(set(
                        author.get('source_instance_id') or author['source'].get('id')
                        for author in authors)),
                    outcome='would_apply' if job['test_run'] else 'attempted')
        if not live_authors:
            continue
        if job not in live_authors:
            continue
        target_client.delete_movie(item['id'], delete_files)


def run(config):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    clients = {}
    for instance in config['instances'].values():
        key = (instance['type'], instance['url'])
        if key not in clients:
            clients[key] = ArrClient(instance)

    next_runs = dict((job['id'], 0) for job in config['jobs'])
    LOGGER.info('Starting multi-job mode with %d configured jobs', len(config['jobs']))
    try:
        while True:
            now = time.time()
            for job in config['jobs']:
                if next_runs[job['id']] > now:
                    continue
                try:
                    run_job(config, job, clients)
                except SyncError as error:
                    LOGGER.error('Job %s failed: %s', job['id'], error)
                except Exception as error:
                    LOGGER.exception('Job %s failed unexpectedly (%s)', job['id'], error.__class__.__name__)
                next_runs[job['id']] = time.time() + job['interval_seconds']
            wait_seconds = max(0.2, min(next_runs.values()) - time.time())
            time.sleep(min(wait_seconds, 5))
    except KeyboardInterrupt:
        LOGGER.info('Stopping multi-job mode')
    finally:
        for client in clients.values():
            client.close()
