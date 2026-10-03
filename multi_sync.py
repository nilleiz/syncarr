#!/usr/bin/env python
"""Multi-job synchronization engine for Radarr, Sonarr, and Lidarr."""

import copy
import json
import logging
import ntpath
import posixpath
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


class MovieFileInventoryError(SyncError):
    """A required Radarr movie-file inventory could not be read."""


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
            if not isinstance(self._profiles, list) or any(
                    not isinstance(item, dict) or item.get('id') is None
                    for item in self._profiles):
                raise SyncError('{} quality-profile response was incomplete'.format(self.arr_type))
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

    def update_movie(self, movie):
        self.request('PUT', 'movie/{}'.format(movie['id']), payload=movie,
                     expected=(200, 202))

    def search_movies(self, movie_ids):
        self.request('POST', 'command',
                     payload={'name': 'MoviesSearch', 'movieIds': list(movie_ids)},
                     expected=(200, 201, 202))

    def close(self):
        self.session.close()


def rule_tag(job):
    if job.get('is_pair_rule'):
        pair_id = job['pair_id']
        rule_id = job['rule_id']
        return 'syncarr-pair-{}-{}-rule-{}-{}'.format(
            len(pair_id), pair_id, len(rule_id), rule_id).lower()
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
    mappings = job.get('root_mappings') or []
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

    if job.get('target_root_path'):
        return job['target_root_path'], None
    if not content_path:
        return None, 'source path is missing and no target root is configured'
    normalized = str(content_path).rstrip('/\\')
    parent = re.sub(r'[/\\][^/\\]+$', '', normalized)
    if not parent and (normalized.startswith('/') or re.match(r'^[A-Za-z]:[/\\]', normalized)):
        parent = normalized[:1] if normalized.startswith('/') else normalized[:3]
    return parent or None, None if parent else 'could not determine a source root'


def _movie_path_in_root(root_path, current_path):
    """Return the existing movie folder under a new root, preserving its leaf name."""
    if not isinstance(root_path, str) or not root_path.strip():
        return None
    if not isinstance(current_path, str) or not current_path.strip():
        return None

    windows_path = ('\\' in root_path or
                    bool(re.match(r'^[A-Za-z]:[/\\]', root_path)))
    path_module = ntpath if windows_path else posixpath
    folder_name = path_module.basename(current_path.rstrip('/\\'))
    if not folder_name or folder_name in ('.', '..'):
        return None
    return path_module.normpath(path_module.join(root_path, folder_name))


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
        'source_instance': job.get('source_instance_id') or source.get('id'),
        'target_instance': job.get('target_instance_id') or target.get('id'),
        'arr_type': arr_type,
        'entity_side': entity_side,
        'title': content.get('title') or content.get('artistName'),
        LOG_ID_KEYS[arr_type]: external_id,
        'arr_record_id': content.get('id'),
        'has_file': _has_file(content),
    }
    if job.get('is_pair_rule'):
        record['pair_id'] = job.get('pair_id')
        record['rule_id'] = job.get('rule_id')
    else:
        record['job_id'] = job.get('id')
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


def _filter_aware_deletion_enabled(job):
    return bool(job['delete_missing'] and job.get('delete_if_filter_not_matching') and
                job['has_file_filters'])


def _deletion_file_filter_match(file_record, job):
    """Return True/False for known metadata, or None when a required field is unknown."""
    if not isinstance(file_record, dict):
        return None

    if job['source_quality_match']:
        quality = file_record.get('quality')
        nested_quality = quality.get('quality') if isinstance(quality, dict) else None
        quality_name = nested_quality.get('name') if isinstance(nested_quality, dict) else None
        if not isinstance(quality_name, str) or not quality_name.strip():
            return None

    mode = job['source_custom_format_mode']
    if mode == 'score':
        if 'customFormatScore' not in file_record:
            return None
        raw_score = file_record.get('customFormatScore')
        if isinstance(raw_score, bool) or raw_score is None:
            return None
        try:
            parsed_score = int(raw_score)
        except (TypeError, ValueError):
            return None
        if isinstance(raw_score, float) and not raw_score.is_integer():
            return None
        if isinstance(raw_score, str) and str(parsed_score) != raw_score.strip():
            return None
    elif mode in ('any', 'all'):
        if 'customFormats' not in file_record or not isinstance(file_record['customFormats'], list):
            return None
        for custom_format in file_record['customFormats']:
            if (not isinstance(custom_format, dict) or
                    not isinstance(custom_format.get('name'), str) or
                    not custom_format['name'].strip()):
                return None

    return _passes_file_filters(file_record, job)


def _movie_files_match_filters(file_records, job):
    """Fail closed if no known match exists and any required metadata is incomplete."""
    if not isinstance(file_records, list) or not file_records:
        raise SyncError('Radarr movie-file inventory was empty or invalid for a movie with hasFile true')
    saw_unknown = False
    for file_record in file_records:
        match = _deletion_file_filter_match(file_record, job)
        if match is True:
            return True
        if match is None:
            saw_unknown = True
    if saw_unknown:
        raise SyncError('Radarr movie-file inventory has incomplete filter metadata')
    return False


def _cached_movie_files(source_client, movie_id, cache):
    cache_key = (source_client.identity, movie_id)
    if cache_key not in cache:
        try:
            cache[cache_key] = source_client.list_movie_files(movie_id)
        except SyncError as error:
            raise MovieFileInventoryError(
                'Radarr movie-file inventory request failed ({})'.format(error))
    return cache[cache_key]


def _load_radarr_movie_file_inventories(incoming_jobs, source_snapshots, clients, cache):
    """Read every file inventory needed by deletion before planning any deletes."""
    for job in incoming_jobs:
        if not _filter_aware_deletion_enabled(job):
            continue
        source_identity = job['source']['identity']
        snapshot = source_snapshots[source_identity]
        movie_files = snapshot.setdefault('movie_files', {})
        source_client = clients[source_identity]
        for item in snapshot['contents']:
            if not isinstance(item, dict):
                raise SyncError('Radarr source inventory contained an invalid movie record')
            if item.get('hasFile') is not True or _content_key(item, 'radarr') is None:
                continue
            movie_id = item.get('id')
            if movie_id is None:
                raise SyncError('Radarr source movie has no ID for its movie-file inventory')
            if movie_id not in movie_files:
                records = _cached_movie_files(source_client, movie_id, cache)
                if not isinstance(records, list):
                    raise SyncError('Radarr movie-file response was not a list')
                if not records:
                    raise SyncError('Radarr source movie reports hasFile true but has no movie-file records')
                movie_files[movie_id] = records


def _episode_key(external_id, episode):
    try:
        season_number = int(episode.get('seasonNumber'))
        episode_number = int(episode.get('episodeNumber'))
    except (TypeError, ValueError):
        return None
    return str(external_id), season_number, episode_number


def _episode_file_id(episode):
    return episode.get('episodeFileId') or (episode.get('episodeFile') or {}).get('id')


def _quality_profile_key(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _sync_items(job, source_client, target_client, tag_id, source_items, target_items,
                episode_cache=None, movie_file_cache=None, run_context=None):
    episode_cache = {} if episode_cache is None else episode_cache
    movie_file_cache = {} if movie_file_cache is None else movie_file_cache
    try:
        profile_filter_id, tag_filter_ids = _resolve_source_filters(source_client, job)
    except Exception:
        if run_context is not None:
            run_context.setdefault('resolved_source_filters', {})[job['id']] = None
        raise
    if run_context is not None:
        run_context.setdefault('resolved_source_filters', {})[job['id']] = (
            profile_filter_id, tag_filter_ids)
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
            files = (_cached_movie_files(source_client, movie_id, movie_file_cache)
                     if movie_id is not None else [])
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
        if (arr_type == 'radarr' and not _has_file(current) and
                content.get('path') and current.get('path')):
            root_path = _path_for_content(content, job, target_client)
            movie_path = _movie_path_in_root(root_path, current.get('path'))
            if movie_path and _path_key(movie_path) != _path_key(current.get('path')):
                current['path'] = movie_path
                current['rootFolderPath'] = root_path
                changed = True
                change_reasons.append('target_root_changed')
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
                params = {'moveFiles': 'false'} if 'target_root_changed' in change_reasons else None
                target_client.request('PUT', '{}/{}'.format(target_client.content_route, current['id']),
                                      params=params, payload=current, expected=(200, 202))
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
        snapshot = source_snapshots[job['source']['identity']]
        contents = snapshot['contents']
        keys = set()
        for item in contents:
            if arr_type == 'radarr' and not isinstance(item, dict):
                raise SyncError('Radarr source inventory contained an invalid movie record')
            if arr_type == 'radarr' and item.get('hasFile') is not True:
                continue
            key = _content_key(item, arr_type)
            if key is not None:
                if arr_type == 'radarr' and _filter_aware_deletion_enabled(job):
                    movie_id = item.get('id')
                    movie_files = snapshot.get('movie_files', {})
                    if movie_id is None or movie_id not in movie_files:
                        raise SyncError('Required Radarr movie-file inventory is unavailable')
                    if not _movie_files_match_filters(movie_files[movie_id], job):
                        continue
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


def _source_movie_has_file(job, target_item, source_snapshots):
    key = _content_key(target_item, 'radarr')
    contents = source_snapshots[job['source']['identity']]['contents']
    return any(item.get('hasFile') is True and _content_key(item, 'radarr') == key
               for item in contents)


def _target_group(config, target_identity):
    return [job for job in config.get('all_jobs', config['jobs'])
            if job['target']['identity'] == target_identity]


def _series_episode_keys(external_id, episodes, job):
    keys = set()
    for episode in episodes:
        if not _has_file(episode):
            continue
        if job['has_file_filters']:
            match = _deletion_file_filter_match(episode.get('episodeFile'), job)
            if match is None:
                raise SyncError('Sonarr episode file has incomplete filter metadata')
            if not match:
                continue
        key = _episode_key(external_id, episode)
        if key is not None:
            keys.add(key)
    return keys


def _sonarr_episode_plan(incoming_jobs, clients, target_client, target_items,
                         source_snapshots, episode_cache, due_job_ids=None,
                         profile_maps=None):
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
    due_job_ids = ({job['id'] for job in incoming_jobs}
                   if due_job_ids is None else set(due_job_ids))
    profile_maps = {} if profile_maps is None else profile_maps
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
            profile_mapped = (not job.get('is_pair_rule') or
                              _quality_profile_key(content.get('qualityProfileId')) in
                              profile_maps.get(job.get('pair_id'), {})) if content else False
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
                if (passes or tagged) and not job['test_run'] and job['id'] in due_job_ids:
                    applicable_filter_jobs.append(job)
                    if passes:
                        matched = _series_episode_keys(external_id, source_episodes, job)
                        if profile_mapped:
                            desired_source_keys.update(matched)
                            if job['auto_search']:
                                auto_search_source_keys.update(matched)
            elif passes and not job['test_run'] and job['id'] in due_job_ids:
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
                'due_job_ids': due_job_ids,
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

        due_job_ids = plan.get('due_job_ids')
        if due_job_ids is None:
            due_job_ids = {job['id'] for job in plan['jobs']}
            due_job_ids.update(author['id'] for unused_episode, authors in plan['deletions']
                               for author in authors)
        active_authors = set(author['id'] for unused_episode, authors in plan['deletions']
                             for author in authors
                             if not author['test_run'] and author['id'] in due_job_ids)
        mutating_jobs = set(job['id'] for job in plan['jobs'] if not job['test_run']) | active_authors
        logging_job = current_job
        if logging_job is None:
            logging_job = (plan['jobs'][0] if plan['jobs'] else
                           next((author for unused_episode, authors in plan['deletions']
                                 for author in authors), None))
        if logging_job is None:
            continue
        mode = 'live' if mutating_jobs and (current_job is None or
                                             not current_job['test_run']) else 'dry_run'

        if (plan['series_monitored'] is not None and
                target_series.get('monitored') != plan['series_monitored']):
            _log_entity(logging_job, target_client, 'update_series_monitoring',
                        'episode_filter_selection', target_series, entity_side='target',
                        mode=mode, target_record_id=target_series.get('id'),
                        monitored=plan['series_monitored'],
                        outcome='attempted' if mode == 'live' else 'would_apply')

        for episode_id in plan['monitor_true']:
            episode = episode_by_id.get(episode_id)
            if episode is not None:
                _log_entity(logging_job, target_client, 'monitor_episode',
                            'matches_source_file_filters', target_series,
                            entity_side='target_episode', mode=mode,
                            has_file=_has_file(episode), **_episode_log_fields(episode),
                            source_has_file=True,
                            outcome='attempted' if mode == 'live' else 'would_apply')
        for episode_id in plan['monitor_false']:
            episode = episode_by_id.get(episode_id)
            if episode is not None:
                _log_entity(logging_job, target_client, 'unmonitor_episode',
                            'source_episode_missing_or_filtered', target_series,
                            entity_side='target_episode', mode=mode,
                            has_file=_has_file(episode), **_episode_log_fields(episode),
                            source_has_file=False,
                            outcome='attempted' if mode == 'live' else 'would_apply')

        deletion_actions = []
        for episode, authors in plan['deletions']:
            if current_job is not None and current_job not in authors:
                continue
            eligible_authors = [author for author in authors
                                if author['id'] in due_job_ids]
            if current_job is not None:
                eligible_authors = [current_job] if current_job in eligible_authors else []
            if not eligible_authors:
                continue
            file_id = _episode_file_id(episode)
            if not file_id:
                continue
            live_authors = [author for author in eligible_authors if not author['test_run']]
            policy_authors = [author for author in authors if not author['test_run']] or authors
            delete_files = all(author['delete_files'] for author in policy_authors)
            action_mode = 'live' if live_authors else 'dry_run'
            details = _episode_log_fields(episode)
            details.update({
                'target_record_id': target_series.get('id'),
                'delete_files': delete_files,
                'author_job_ids': sorted(author['id'] for author in authors),
                'author_source_instances': sorted(set(
                    author.get('source_instance_id') or author['source'].get('id')
                    for author in authors)),
                'source_has_file': False,
                'outcome': 'attempted' if action_mode == 'live' else 'would_apply',
            })
            _log_entity(logging_job, target_client, 'delete_episode_file',
                        'source_episode_missing_or_filtered', target_series,
                        entity_side='target_episode', mode=action_mode,
                        has_file=_has_file(episode), **details)
            if live_authors and (current_job is None or current_job in live_authors):
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
                _log_entity(logging_job, target_client, 'search_episode',
                            'newly_monitored_source_match', target_series,
                            entity_side='target_episode', mode='live',
                            has_file=_has_file(episode), **_episode_log_fields(episode),
                            source_has_file=True, outcome='attempted')
            target_client.request('POST', 'command',
                                  payload={'name': 'EpisodeSearch', 'episodeIds': search_ids},
                                  expected=(200, 201, 202))


def _list_job_source_items(source_client, job, run_context):
    try:
        source_items = source_client.list_content()
    except Exception:
        if run_context is not None:
            run_context.setdefault('source_contents_by_job', {})[job['id']] = None
        raise
    if run_context is not None:
        run_context.setdefault('source_contents_by_job', {})[job['id']] = source_items
    return source_items


def run_job(config, job, clients, run_context=None):
    source_client, target_client, tag_id = _prepare_job(job, clients, create_tags=not job['test_run'])
    source_items = _list_job_source_items(source_client, job, run_context)
    target_items = target_client.list_content()
    episode_cache = {}
    movie_file_cache = (run_context.setdefault('movie_file_cache', {})
                        if run_context is not None else {})
    try:
        _sync_items(job, source_client, target_client, tag_id, source_items, target_items,
                    episode_cache=episode_cache, movie_file_cache=movie_file_cache,
                    run_context=run_context)
    except MovieFileInventoryError as error:
        LOGGER.error('Job %s will not continue because its source movie-file inventory failed (%s)',
                     job['id'], error)
        return False

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
                                    else _list_job_source_items(incoming_client, incoming_job,
                                                                run_context))
                        source_snapshots[source_identity] = contents
                # The just-added series and its episode IDs must be visible before we
                # reconcile individual episode monitoring and file deletion.
                target_items = target_client.list_content()
                plans = _sonarr_episode_plan(incoming_jobs, clients, target_client,
                                             target_items, source_snapshots, episode_cache)
            except SyncError as error:
                LOGGER.error('Job %s will not reconcile Sonarr episode state because an inventory failed (%s)',
                             job['id'], error)
                return False
            _apply_sonarr_episode_plan(plans, job, target_client)
        return True

    if not job['delete_missing']:
        return True
    incoming_jobs = _target_group(config, target_client.identity)
    source_snapshots = {}
    safe_to_delete = True
    for incoming_job in incoming_jobs:
        source_key = incoming_job['source']['identity']
        if source_key in source_snapshots:
            continue
        incoming_client = clients[source_key]
        try:
            contents = (source_items if incoming_client.identity == source_client.identity
                        else _list_job_source_items(incoming_client, incoming_job, run_context))
            source_snapshots[source_key] = {'client': incoming_client, 'contents': contents}
        except SyncError as error:
            LOGGER.error('Job %s will not delete from its target because a source inventory failed (%s)',
                         job['id'], error)
            safe_to_delete = False
    if not safe_to_delete:
        return False

    try:
        _load_radarr_movie_file_inventories(
            incoming_jobs, source_snapshots, clients, movie_file_cache)
    except SyncError as error:
        LOGGER.error('Job %s will not delete from its target because a required source movie-file inventory was unavailable or incomplete (%s)',
                     job['id'], error)
        return False
    try:
        candidates = _items_for_deletion(
            target_items, incoming_jobs, source_snapshots, target_client)
    except SyncError as error:
        LOGGER.error('Job %s will not delete from its target because deletion planning data was unavailable or incomplete (%s)',
                     job['id'], error)
        return False

    for item, authors in candidates:
        if job not in authors:
            continue
        source_has_file = _source_movie_has_file(job, item, source_snapshots)
        source_filter_mismatch = bool(source_has_file and _filter_aware_deletion_enabled(job))
        reason = 'source_file_filter_mismatch' if source_filter_mismatch else 'missing_from_source'
        live_authors = [author for author in authors if not author['test_run']]
        effective_authors = live_authors or authors
        delete_files = all(author['delete_files'] for author in effective_authors)
        _log_entity(job, target_client, 'delete_movie', reason, item,
                    entity_side='target', target_record_id=item.get('id'),
                    source_has_file=source_has_file,
                    source_filter_mismatch=source_filter_mismatch,
                    delete_files=delete_files,
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

    return True


def _radarr_target_groups(config):
    groups = {}
    for job in config.get('all_jobs', config['jobs']):
        if job['target']['type'] == 'radarr':
            groups.setdefault(job['target']['identity'], []).append(job)
    return groups


def _reinitialize_radarr_target(incoming_jobs, target_client, initial_target_ids,
                                clients, run_context, target_items=None):
    """Reactivate pre-existing fileless Radarr movies that pass any incoming job."""
    if target_items is None:
        try:
            target_items = target_client.list_content()
        except Exception as error:
            LOGGER.error('Could not inspect Radarr target for reinitialization (%s)',
                         error.__class__.__name__)
            return False

    target_by_key = {}
    for item in target_items:
        if item.get('id') not in initial_target_ids or _has_file(item):
            continue
        key = _content_key(item, 'radarr')
        if key is not None:
            target_by_key.setdefault(key, []).append(item)

    duplicates = {key for key, items in target_by_key.items() if len(items) > 1}
    success = True
    for key in duplicates:
        LOGGER.error('Radarr reinitialization skipped a duplicate target movie ID')
        target_by_key.pop(key, None)
        success = False
    if not target_by_key:
        return success

    source_by_job = {}
    resolved_filters = run_context.get('resolved_source_filters', {})
    source_contents = run_context.get('source_contents_by_job', {})
    target_profile_maps = run_context.get('profile_maps_by_target', {}).get(
        target_client.identity, {})
    for job in incoming_jobs:
        identity = job['source']['identity']
        contents = source_contents.get(job['id'])
        if contents is None:
            LOGGER.error('Job %s source inventory is unavailable for the Radarr recovery pass',
                         job['id'])
            success = False
            continue
        if job['id'] not in resolved_filters or resolved_filters[job['id']] is None:
            LOGGER.error('Job %s source filters are unavailable for the Radarr recovery pass',
                         job['id'])
            success = False
            continue
        source_by_job[job['id']] = {}
        for content in contents:
            content_key = _content_key(content, 'radarr')
            if content_key is not None:
                source_by_job[job['id']].setdefault(content_key, content)

    movie_file_cache = run_context.get('movie_file_cache', {})
    for key, target_movie_matches in target_by_key.items():
        target_movie = target_movie_matches[0]
        qualifying_jobs = []
        for job in incoming_jobs:
            identity = job['source']['identity']
            if job['id'] not in source_by_job or job['id'] not in resolved_filters:
                continue
            source_movie = source_by_job[job['id']].get(key)
            if not isinstance(source_movie, dict) or source_movie.get('hasFile') is not True:
                continue
            profile_filter_id, tag_filter_ids = resolved_filters[job['id']]
            source_client = clients[identity]
            if not _passes_filters(source_movie, source_client, job,
                                   profile_filter_id, tag_filter_ids):
                continue

            if job.get('is_pair_rule'):
                source_profile_id = _quality_profile_key(source_movie.get('qualityProfileId'))
                if source_profile_id not in target_profile_maps.get(job['pair_id'], {}):
                    continue

            root_path = _path_for_content(source_movie, job, target_client)
            if not root_path:
                continue

            if job['has_file_filters']:
                movie_id = source_movie.get('id')
                if movie_id is None:
                    LOGGER.error('Job %s source movie has no ID for its file-filter inventory',
                                 job['id'])
                    success = False
                    continue
                cache_key = (source_client.identity, movie_id)
                if cache_key not in movie_file_cache:
                    LOGGER.error('Job %s source movie-file inventory is unavailable for the Radarr recovery pass',
                                 job['id'])
                    success = False
                    continue
                movie_files = movie_file_cache[cache_key]
                if not isinstance(movie_files, list) or not movie_files:
                    LOGGER.error('Job %s source movie-file inventory is empty for the Radarr recovery pass',
                                 job['id'])
                    success = False
                    continue
                file_matches = [_deletion_file_filter_match(file_record, job)
                                for file_record in movie_files]
                if any(match is True for match in file_matches):
                    pass
                elif any(match is None for match in file_matches):
                    LOGGER.error('Job %s source movie-file metadata is incomplete for the Radarr recovery pass',
                                 job['id'])
                    success = False
                    continue
                else:
                    continue
            qualifying_jobs.append((job, root_path, source_movie))

        if not qualifying_jobs:
            continue

        root_demands = {_path_key(root_path) for unused_job, root_path, unused_movie in qualifying_jobs}
        profile_demands = {
            target_profile_id
            for job, unused_root, source_movie in qualifying_jobs
            if job.get('is_pair_rule')
            for target_profile_id in [target_profile_maps.get(job['pair_id'], {}).get(
                _quality_profile_key(source_movie.get('qualityProfileId')))]
            if target_profile_id is not None
        }
        if len(root_demands) > 1 or len(profile_demands) > 1:
            LOGGER.warning('Radarr recovery skipped a conflicting target movie %s under rules %s',
                           key, ','.join(sorted(_rule_label(job)
                                                for job, unused_root, unused_movie in qualifying_jobs)))
            success = False
            continue

        live_jobs = [job for job, unused_root, unused_movie in qualifying_jobs if not job['test_run']]
        action_job = live_jobs[0] if live_jobs else qualifying_jobs[0][0]
        mode = 'live' if live_jobs else 'dry_run'
        outcome = 'attempted' if live_jobs else 'would_apply'
        details = {
            'target_record_id': target_movie.get('id'),
            'target_has_file': False,
            'source_has_file': True,
            'qualifying_job_ids': sorted(
                job['id'] for job, unused_root, unused_movie in qualifying_jobs
                if not job.get('is_pair_rule')),
            'qualifying_rules': sorted(_rule_label(job)
                                       for job, unused_root, unused_movie in qualifying_jobs),
        }

        if not target_movie.get('monitored', False):
            _log_entity(action_job, target_client, 'update_movie_monitoring',
                        'reinitialize_b_missing_file', target_movie,
                        entity_side='target', mode=mode,
                        change_reasons=['reinitialize_b_missing_file'],
                        monitored=True, outcome=outcome, **details)
            if live_jobs:
                updated_movie = dict(target_movie)
                updated_movie['monitored'] = True
                try:
                    target_client.update_movie(updated_movie)
                except Exception as error:
                    LOGGER.error('Radarr movie %s could not be reactivated (%s)',
                                 key, error.__class__.__name__)
                    success = False
                    continue

        _log_entity(action_job, target_client, 'search_movie',
                    'reinitialize_b_missing_file', target_movie,
                    entity_side='target', mode=mode, outcome=outcome, **details)
        if live_jobs:
            try:
                target_client.search_movies([target_movie['id']])
            except Exception as error:
                LOGGER.error('Radarr movie %s search could not be started (%s)',
                             key, error.__class__.__name__)
                success = False

    return success


def _create_clients(config):
    clients = {}
    for instance in config['instances'].values():
        key = (instance['type'], instance['url'])
        if key not in clients:
            clients[key] = ArrClient(instance)
    return clients


def _resolve_profile_selector(client, name, profile_id, label):
    profiles = client.profiles()
    matches = ([item for item in profiles if item.get('id') == profile_id]
               if profile_id is not None else
               [item for item in profiles
                if str(item.get('name', '')).casefold() == name.casefold()])
    if len(matches) != 1:
        instance_id = getattr(client, 'instance', {}).get('id', 'unknown')
        raise SyncError('Configured {} is missing or ambiguous on instance {}'.format(
            label, instance_id))
    return matches[0]['id']


def _instance_label(config, identity):
    for instance_id, instance in config['instances'].items():
        if instance['identity'] == identity:
            return instance_id
    return 'unknown'


def _rule_label(job):
    if job.get('is_pair_rule'):
        return '{}/{}'.format(job['pair_id'], job['rule_id'])
    return job['id']


def _resolve_pair_profile_maps(config, target_identity, clients):
    maps = {}
    for pair in config.get('pairs', []):
        if pair['target']['identity'] != target_identity:
            continue
        source_client = clients[pair['source']['identity']]
        target_client = clients[target_identity]
        resolved = {}
        for mapping in pair['profile_mappings']:
            source_profile_id = _resolve_profile_selector(
                source_client, mapping['source_profile'], mapping['source_profile_id'],
                'source profile in pair {}'.format(pair['id']))
            target_profile_id = _resolve_profile_selector(
                target_client, mapping['target_profile'], mapping['target_profile_id'],
                'target profile in pair {}'.format(pair['id']))
            previous = resolved.get(source_profile_id)
            if previous is not None and previous != target_profile_id:
                raise SyncError('Pair {} maps one source profile to conflicting target profiles'.format(
                    pair['id']))
            resolved[source_profile_id] = target_profile_id
        maps[pair['id']] = resolved
    return maps


def _candidate_for_rule(job, content, source_client, target_client, source_filters,
                        profile_maps, episode_cache, movie_file_cache):
    profile_filter_id, tag_filter_ids = source_filters
    if not _passes_filters(content, source_client, job, profile_filter_id, tag_filter_ids):
        _log_entity(job, target_client, 'skip', 'source_filters', content,
                    level=logging.DEBUG)
        return None
    matching_episodes = None
    if source_client.arr_type == 'radarr' and job['has_file_filters']:
        movie_id = content.get('id')
        if movie_id is None:
            raise SyncError('Radarr source movie has no ID for file-filter inventory')
        files = _cached_movie_files(source_client, movie_id, movie_file_cache)
        if not any(_passes_file_filters(record, job) for record in files):
            _log_entity(job, target_client, 'skip', 'source_file_filters', content,
                        level=logging.DEBUG)
            return None
    elif source_client.arr_type == 'sonarr' and job['has_file_filters']:
        matching_episodes = _matching_source_episodes(source_client, content, job, episode_cache)
        if not matching_episodes:
            _log_entity(job, target_client, 'skip', 'source_episode_file_filters', content,
                        level=logging.DEBUG)
            return None

    profile_id = None
    if job.get('is_pair_rule'):
        source_profile_id = _quality_profile_key(content.get('qualityProfileId'))
        profile_id = profile_maps.get(job['pair_id'], {}).get(source_profile_id)
        if profile_id is None:
            _log_entity(job, target_client, 'skip', 'source_profile_unmapped', content,
                        source_profile=content.get('qualityProfileId'), level=logging.DEBUG)
            return None
    elif job.get('resolved_profile_id') is not None:
        profile_id = job['resolved_profile_id']

    root_path, root_error = map_root_path(content.get('path'), job)
    if not root_path:
        _log_entity(job, target_client, 'skip', 'root_mapping_failed', content,
                    mapping_reason=root_error or 'could not determine a target root',
                    level=logging.WARNING)
        return None
    return {
        'job': job,
        'content': content,
        'profile_id': profile_id,
        'root_path': root_path,
        'matching_episodes': matching_episodes,
    }


def _run_planned_cycle(config, due_jobs, clients, run_context=None, initial_target_ids=None,
                       initial_target_contents=None):
    """Plan all rules for due targets together, then apply non-conflicting actions."""
    all_jobs = config.get('all_jobs', config['jobs'])
    success = True
    for client in clients.values():
        if hasattr(client, '_profiles'):
            client._profiles = None
        if hasattr(client, '_tags'):
            client._tags = None
    due_ids = {job['id'] for job in due_jobs}
    target_identities = {job['target']['identity'] for job in due_jobs}
    relevant_jobs = [job for job in all_jobs if job['target']['identity'] in target_identities]
    source_identities = {job['source']['identity'] for job in relevant_jobs}
    source_contents = {}
    target_contents = {}
    target_failed = set()
    inventory_ids = source_identities | target_identities
    inventory_snapshots = {}
    for identity in inventory_ids:
        if initial_target_contents is not None and identity in initial_target_contents:
            inventory_snapshots[identity] = initial_target_contents[identity]
            continue
        try:
            inventory_snapshots[identity] = clients[identity].list_content()
        except Exception as error:
            inventory_snapshots[identity] = None
            success = False
            LOGGER.error('Source inventory for instance %s is unavailable (%s)',
                         _instance_label(config, identity), error.__class__.__name__)
    for identity in source_identities:
        contents = inventory_snapshots.get(identity)
        source_contents[identity] = copy.deepcopy(contents) if contents is not None else None
    for identity in target_identities:
        contents = inventory_snapshots.get(identity)
        target_contents[identity] = copy.deepcopy(contents) if contents is not None else None
        if target_contents[identity] is None:
            target_failed.add(identity)
            success = False
            LOGGER.error('Target inventory for instance %s is unavailable',
                         _instance_label(config, identity))

    for job in relevant_jobs:
        if not job.get('is_pair_rule'):
            try:
                job['resolved_profile_id'] = clients[job['target']['identity']].profile_id(
                    job['target_profile'], job['target_profile_id'], 'target profile')
            except Exception as error:
                target_failed.add(job['target']['identity'])
                success = False
                LOGGER.error('Job %s target profile could not be resolved (%s)',
                             job['id'], error.__class__.__name__)

    profile_maps = {}
    for identity in target_identities - target_failed:
        try:
            profile_maps[identity] = _resolve_pair_profile_maps(config, identity, clients)
            if run_context is not None:
                run_context.setdefault('profile_maps_by_target', {})[identity] = profile_maps[identity]
        except Exception as error:
            target_failed.add(identity)
            success = False
            LOGGER.error('Profile mappings for target %s are invalid or unavailable (%s)',
                         _instance_label(config, identity), error.__class__.__name__)

    filters = {}
    filter_failures = set()
    for job in relevant_jobs:
        try:
            filters[job['id']] = _resolve_source_filters(
                clients[job['source']['identity']], job)
        except Exception as error:
            filter_failures.add(job['id'])
            LOGGER.error('Source filters for rule %s could not be resolved (%s)',
                         job.get('rule_id', job['id']), error.__class__.__name__)
    if filter_failures:
        success = False
    if run_context is not None:
        run_context['blocked_targets'] = set(target_failed)

    episode_cache = (run_context.setdefault('episode_cache', {})
                     if run_context is not None else {})
    movie_file_cache = (run_context.setdefault('movie_file_cache', {})
                        if run_context is not None else {})

    for identity in target_identities:
        if identity in target_failed or identity not in target_contents:
            continue
        incoming = [job for job in all_jobs if job['target']['identity'] == identity]
        target_client = clients[identity]
        target_items = target_contents[identity]
        if any(job['id'] in filter_failures for job in incoming if job['delete_missing']):
            deletion_safe = False
        else:
            deletion_safe = True
        source_snapshots = {}
        for job in incoming:
            source_identity = job['source']['identity']
            if source_identity not in source_snapshots:
                contents = source_contents.get(source_identity)
                source_snapshots[source_identity] = (
                    {'contents': contents, 'client': clients[source_identity], 'movie_files': {}}
                    if contents is not None else None)
        protected_keys = set()
        plans_by_key = {}
        target_by_key = {}
        for item in target_items:
            key = _content_key(item, target_client.arr_type)
            if key is not None:
                target_by_key.setdefault(key, []).append(item)
        protected_keys.update(key for key, matches in target_by_key.items()
                              if len(matches) > 1)

        for job in incoming:
            contents = source_contents.get(job['source']['identity'])
            if run_context is not None:
                run_context.setdefault('source_contents_by_job', {})[job['id']] = contents
                run_context.setdefault('resolved_source_filters', {})[job['id']] = (
                    filters.get(job['id']) if job['id'] not in filter_failures else None)
            if contents is None or job['id'] in filter_failures:
                if job['delete_missing']:
                    deletion_safe = False
                continue
            source_client = clients[job['source']['identity']]
            by_key = {}
            for content in contents:
                if not isinstance(content, dict):
                    if job['delete_missing']:
                        deletion_safe = False
                    continue
                try:
                    candidate = _candidate_for_rule(
                        job, content, source_client, target_client, filters[job['id']],
                        profile_maps.get(identity, {}), episode_cache, movie_file_cache)
                except SyncError as error:
                    success = False
                    LOGGER.error('Rule %s filter inventory failed (%s)',
                                 job.get('rule_id', job['id']), error)
                    if job['delete_missing']:
                        deletion_safe = False
                    continue
                key = _content_key(content, target_client.arr_type)
                if key is None:
                    continue
                by_key.setdefault(key, []).append(candidate)
            for key, candidates in by_key.items():
                if any(candidate is not None for candidate in candidates):
                    plans_by_key.setdefault(key, []).extend(
                        candidate for candidate in candidates if candidate is not None)

        # Include all profile/root/monitor demands, including rules not due yet,
        # before deciding whether any due rule may write this item.
        for key, candidates in plans_by_key.items():
            target_matches = target_by_key.get(key, [])
            if len(target_matches) > 1:
                protected_keys.add(key)
                continue
            target_item = target_matches[0] if target_matches else None
            profile_demands = {candidate['profile_id'] for candidate in candidates
                               if candidate['profile_id'] is not None and
                               (candidate['job'].get('is_pair_rule') or target_item is None)}
            root_demands = set()
            if target_item is None or (target_client.arr_type == 'radarr' and not _has_file(target_item)):
                root_demands = {_path_key(candidate['root_path']) for candidate in candidates}
            if target_item is None:
                monitor_demands = {bool(candidate['job']['monitor_new_content'])
                                   for candidate in candidates
                                   if not (target_client.arr_type == 'sonarr' and
                                           candidate['job']['has_file_filters'])}
            else:
                monitor_demands = {bool(candidate['content'].get('monitored'))
                                   for candidate in candidates
                                   if candidate['job']['sync_monitor'] and
                                   not (target_client.arr_type == 'sonarr' and
                                        candidate['job']['has_file_filters'])}
            conflicts = (len(profile_demands) > 1 or len(root_demands) > 1 or
                         len(monitor_demands) > 1)
            if conflicts:
                protected_keys.add(key)
                self_jobs = sorted({_rule_label(candidate['job']) for candidate in candidates})
                target_id = candidates[0]['job'].get('target_instance_id', 'unknown')
                title = next((candidate['content'].get('title') for candidate in candidates
                              if candidate['content'].get('title')), key)
                LOGGER.warning('Skipping conflicting target entity %s (%s) on instance %s; rules=%s',
                               title, key, target_id, ','.join(self_jobs))
                continue

        # Build Sonarr's complete episode plan before applying any writes.
        sonarr_plans = []
        sonarr_blocked_keys = set(protected_keys)
        if (target_client.arr_type == 'sonarr' and
                any(job['has_file_filters'] or job['delete_missing'] for job in incoming)):
            if any(snapshot is None for snapshot in source_snapshots.values()):
                deletion_safe = False
            else:
                sonarr_source_snapshots = {
                    source_identity: snapshot['contents']
                    for source_identity, snapshot in source_snapshots.items()}
                safe_target_items = [item for item in target_items
                                     if _content_key(item, 'sonarr') not in sonarr_blocked_keys]
                try:
                    sonarr_plans = _sonarr_episode_plan(
                        incoming, clients, target_client, safe_target_items,
                        sonarr_source_snapshots, episode_cache, due_ids,
                        profile_maps.get(identity, {}))
                except Exception as error:
                    deletion_safe = False
                    success = False
                    LOGGER.error('Sonarr episode plan was incomplete; dependent deletions skipped (%s)',
                                 error.__class__.__name__)

        # Resolve existing management tags and all deletion candidates before writes.
        try:
            for job in incoming:
                target_client.tag_id(rule_tag(job), create=False)
        except Exception as error:
            deletion_safe = False
            success = False
            LOGGER.error('Could not inspect target management tags; deletions skipped (%s)',
                         error.__class__.__name__)

        deletion_candidates = []
        if deletion_safe and any(job['delete_missing'] for job in incoming):
            if any(snapshot is None for snapshot in source_snapshots.values()):
                deletion_safe = False
            elif target_client.arr_type == 'radarr':
                try:
                    _load_radarr_movie_file_inventories(
                        incoming, source_snapshots, clients, movie_file_cache)
                    deletion_candidates = _items_for_deletion(
                        target_items, incoming, source_snapshots, target_client)
                except Exception as error:
                    deletion_safe = False
                    success = False
                    LOGGER.error('Radarr deletion plan incomplete; target deletions skipped (%s)',
                                 error.__class__.__name__)
            # Sonarr deletions are already in sonarr_plans.

        # Apply adds and updates once per external content ID.
        for key, candidates in plans_by_key.items():
            target_matches = target_by_key.get(key, [])
            if key in protected_keys or len(target_matches) > 1:
                continue
            active = [candidate for candidate in candidates
                      if candidate['job']['id'] in due_ids]
            live = [candidate for candidate in active if not candidate['job']['test_run']]
            effective = live or active
            if not effective:
                continue
            target_item = target_matches[0] if target_matches else None
            if target_client.arr_type == 'radarr' and target_item is not None and _has_file(target_item):
                root = target_item.get('rootFolderPath')
            else:
                root = effective[0]['root_path']
            profile_ids = {candidate['profile_id'] for candidate in candidates
                           if candidate['profile_id'] is not None and
                           candidate['job']['id'] in due_ids and
                           (candidate['job'].get('is_pair_rule') or target_item is None)}
            selected_profile = next(iter(profile_ids)) if profile_ids else None
            representative = effective[0]
            job = representative['job']
            if target_item is None:
                live_jobs = [candidate['job'] for candidate in live]
                test_run = not live_jobs
                root = representative['root_path']
                profile_for_add = selected_profile or job.get('resolved_profile_id')
                if profile_for_add is None:
                    protected_keys.add(key)
                    LOGGER.error('Skipping target entity without an explicit target profile mapping')
                    continue
                add_job = dict(job)
                add_job['resolved_profile_id'] = profile_for_add
                add_job['auto_search'] = any(item['job']['auto_search'] for item in effective)
                add_job['monitor_new_content'] = any(
                    item['job']['monitor_new_content'] for item in effective)
                filtered_sonarr = (target_client.arr_type == 'sonarr' and
                                   any(item['job']['has_file_filters'] for item in effective))
                if filtered_sonarr:
                    add_job['has_file_filters'] = True
                    selected_episodes = {}
                    for item in effective:
                        for episode in item['matching_episodes'] or []:
                            episode_key = _episode_key(key, episode)
                            if episode_key is not None:
                                selected_episodes[episode_key] = episode
                    matching_episodes = list(selected_episodes.values())
                else:
                    matching_episodes = representative['matching_episodes']
                if test_run:
                    _log_entity(job, target_client, 'add', 'missing_on_target',
                                representative['content'], mode='dry_run',
                                source_profile=representative['content'].get('qualityProfileId'),
                                target_profile=profile_for_add,
                                contributing_rules=sorted(_rule_label(item['job']) for item in effective),
                                outcome='would_apply')
                    continue
                payload = _build_payload(
                    representative['content'], add_job, target_client, root, None,
                    matching_episodes=matching_episodes)
                tag_values = []
                try:
                    for candidate in live:
                        tag = target_client.tag_id(rule_tag(candidate['job']), create=True)
                        if tag is not None and tag not in tag_values:
                            tag_values.append(tag)
                except Exception as error:
                    success = False
                    LOGGER.error('Target tags for a new entity could not be prepared (%s)',
                                 error.__class__.__name__)
                    continue
                payload['tags'] = tag_values
                payload['qualityProfileId'] = profile_for_add
                _log_entity(job, target_client, 'add', 'missing_on_target',
                            representative['content'], mode='live',
                            source_profile=representative['content'].get('qualityProfileId'),
                            target_profile=profile_for_add,
                            contributing_rules=sorted(_rule_label(item['job']) for item in live),
                            outcome='attempted')
                try:
                    result = target_client.request('POST', target_client.content_route,
                                                   payload=payload, expected=(200, 201))
                except Exception as error:
                    success = False
                    LOGGER.error('Target add failed for entity %s (%s)', key,
                                 error.__class__.__name__)
                    continue
                if isinstance(result, dict):
                    target_items.append(result)
                    target_by_key.setdefault(key, []).append(result)
                    if target_client.arr_type == 'sonarr' and any(
                            candidate['job']['has_file_filters'] for candidate in live):
                        try:
                            _monitor_new_sonarr_series(
                                target_client, result, live, key, episode_cache)
                        except Exception as error:
                            success = False
                            LOGGER.error('New Sonarr series episode plan failed (%s)',
                                         error.__class__.__name__)
                continue

            current = target_item
            changed = []
            update = dict(current)
            try:
                for candidate in live:
                    tag_id = target_client.tag_id(rule_tag(candidate['job']), create=True)
                    tags = list(update.get('tags') or [])
                    if tag_id is not None and tag_id not in tags:
                        tags.append(tag_id)
                        update['tags'] = tags
                        if 'managed_tag_missing' not in changed:
                            changed.append('managed_tag_missing')
            except Exception as error:
                success = False
                LOGGER.error('Target tags for entity %s could not be prepared (%s)',
                             key, error.__class__.__name__)
                continue
            if selected_profile is not None and update.get('qualityProfileId') != selected_profile:
                update['qualityProfileId'] = selected_profile
                changed.append('quality_profile_changed')
            if target_client.arr_type == 'radarr' and not _has_file(current):
                mapped_root = effective[0]['root_path']
                movie_path = _movie_path_in_root(mapped_root, current.get('path'))
                if movie_path and _path_key(movie_path) != _path_key(current.get('path')):
                    update['path'] = movie_path
                    update['rootFolderPath'] = mapped_root
                    changed.append('target_root_changed')
            monitor_values = {bool(candidate['content'].get('monitored'))
                              for candidate in live
                              if candidate['job']['sync_monitor'] and
                              not (target_client.arr_type == 'sonarr' and
                                   candidate['job']['has_file_filters'])}
            if monitor_values:
                desired_monitor = next(iter(monitor_values))
                if update.get('monitored') != desired_monitor:
                    update['monitored'] = desired_monitor
                    changed.append('monitored_state_differs')
            if not changed:
                continue
            if not live:
                _log_entity(job, target_client, 'update', 'target_state_differs',
                            representative['content'], target_record_id=current.get('id'),
                            target_has_file=_has_file(current), change_reasons=changed,
                            source_profile=representative['content'].get('qualityProfileId'),
                            target_profile=selected_profile,
                            contributing_rules=sorted(_rule_label(item['job']) for item in active),
                            mode='dry_run', outcome='would_apply')
                continue
            _log_entity(job, target_client, 'update', 'target_state_differs',
                        representative['content'], target_record_id=current.get('id'),
                        target_has_file=_has_file(current), change_reasons=changed,
                        source_profile=representative['content'].get('qualityProfileId'),
                        target_profile=selected_profile,
                        contributing_rules=sorted(_rule_label(item['job']) for item in live),
                        outcome='attempted')
            params = {'moveFiles': 'false'} if 'target_root_changed' in changed else None
            try:
                target_client.request('PUT', '{}/{}'.format(target_client.content_route, current['id']),
                                      params=params, payload=update, expected=(200, 202))
            except Exception as error:
                success = False
                LOGGER.error('Target update failed for entity %s (%s)', key,
                             error.__class__.__name__)
                continue
            current.update(update)

        if target_client.arr_type == 'sonarr' and sonarr_plans:
            try:
                _apply_sonarr_episode_plan(sonarr_plans, None, target_client)
            except Exception as error:
                success = False
                LOGGER.error('Sonarr episode actions failed (%s)', error.__class__.__name__)

        for item, authors in deletion_candidates:
            key = _content_key(item, target_client.arr_type)
            if key in protected_keys or key not in target_by_key:
                continue
            active_authors = [job for job in authors
                              if job['id'] in due_ids and not job['test_run']]
            dry_authors = [job for job in authors
                           if job['id'] in due_ids and job['test_run']]
            if not active_authors and not dry_authors:
                continue
            effective_authors = active_authors or dry_authors
            policy_authors = [job for job in authors if not job['test_run']] or authors
            delete_files = all(job['delete_files'] for job in policy_authors)
            _log_entity(effective_authors[0], target_client, 'delete_movie',
                        'missing_from_source', item, entity_side='target',
                        target_record_id=item.get('id'), delete_files=delete_files,
                        author_job_ids=sorted(job['id'] for job in authors),
                        outcome='attempted' if active_authors else 'would_apply')
            if active_authors:
                try:
                    target_client.delete_movie(item['id'], delete_files)
                    target_items[:] = [target_item for target_item in target_items
                                       if target_item.get('id') != item.get('id')]
                except Exception as error:
                    success = False
                    LOGGER.error('Radarr delete failed for target entity %s (%s)',
                                 key, error.__class__.__name__)

        if run_context is not None:
            run_context.setdefault('target_contents_after_cycle', {})[identity] = target_items

    return success and not target_failed


def _monitor_new_sonarr_series(target_client, series, candidates, external_id, episode_cache):
    """Apply filtered episode monitoring/search after Sonarr creates the series record."""
    series_id = series.get('id')
    if series_id is None:
        return
    episodes = target_client.list_episodes(series_id)
    desired = set()
    auto_search = set()
    for candidate in candidates:
        job = candidate['job']
        for source_episode in candidate['matching_episodes'] or []:
            key = _episode_key(external_id, source_episode)
            if key is not None:
                desired.add(key)
                if job['auto_search']:
                    auto_search.add(key)
    target_ids = []
    search_ids = []
    source_by_key = {}
    for episode in episodes:
        key = _episode_key(external_id, episode)
        if key not in desired or episode.get('id') is None:
            continue
        target_ids.append(episode['id'])
        source_by_key[key] = next(
            candidate for candidate in candidates
            if any(_episode_key(external_id, source_episode) == key
                   for source_episode in (candidate['matching_episodes'] or [])))
        if key in auto_search and not _has_file(episode):
            search_ids.append(episode['id'])
        candidate = source_by_key[key]
        _log_entity(candidate['job'], target_client, 'monitor_episode',
                    'matches_source_file_filters', series, entity_side='target_episode',
                    has_file=_has_file(episode), **_episode_log_fields(episode),
                    source_has_file=True, outcome='attempted')
    if target_ids:
        target_client.set_episodes_monitored(target_ids, True)
    if search_ids:
        for episode_id in search_ids:
            episode = next(item for item in episodes if item.get('id') == episode_id)
            key = _episode_key(external_id, episode)
            candidate = source_by_key[key]
            _log_entity(candidate['job'], target_client, 'search_episode',
                        'newly_monitored_source_match', series,
                        entity_side='target_episode', has_file=_has_file(episode),
                        **_episode_log_fields(episode), source_has_file=True,
                        outcome='attempted')
        target_client.request('POST', 'command',
                              payload={'name': 'EpisodeSearch', 'episodeIds': search_ids},
                              expected=(200, 201, 202))


def run_once(config):
    """Execute every configured job once, then perform Radarr B recovery."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    clients = _create_clients(config)
    success = True
    target_groups = _radarr_target_groups(config)
    initial_target_ids = {}
    run_context = {
        'source_contents_by_job': {},
        'resolved_source_filters': {},
        'movie_file_cache': {},
    }

    try:
        if config.get('pairs'):
            initial_target_contents = {}
            for identity in target_groups:
                try:
                    items = clients[identity].list_content()
                    initial_target_contents[identity] = items
                    initial_target_ids[identity] = {
                        item.get('id') for item in items
                        if isinstance(item, dict) and item.get('id') is not None
                    }
                except Exception as error:
                    initial_target_contents[identity] = None
                    LOGGER.error('Could not snapshot a Radarr target before the one-time run (%s)',
                                 error.__class__.__name__)
                    initial_target_ids[identity] = None
                    success = False
            LOGGER.info('Starting one-time run with %d rules across %d units',
                        len(config['all_jobs']), len(config['units']))
            cycle_completed = True
            try:
                success = _run_planned_cycle(
                    config, config['all_jobs'], clients, run_context=run_context,
                    initial_target_contents=initial_target_contents) and success
            except Exception as error:
                LOGGER.exception('One-time pair cycle failed unexpectedly (%s)',
                                 error.__class__.__name__)
                success = False
                cycle_completed = False
            if cycle_completed:
                for identity, incoming_jobs in target_groups.items():
                    baseline = initial_target_ids.get(identity)
                    if baseline is None or identity in run_context.get('blocked_targets', set()):
                        continue
                    try:
                        if not _reinitialize_radarr_target(
                                incoming_jobs, clients[identity], baseline, clients, run_context,
                                run_context.get('target_contents_after_cycle', {}).get(identity)):
                            success = False
                    except Exception as error:
                        LOGGER.exception('Radarr reinitialization failed unexpectedly (%s)',
                                         error.__class__.__name__)
                        success = False
            LOGGER.info('One-time run completed with status %s', 0 if success else 1)
            return 0 if success else 1

        for identity in target_groups:
            try:
                target_items = clients[identity].list_content()
                initial_target_ids[identity] = {
                    item.get('id') for item in target_items
                    if isinstance(item, dict) and item.get('id') is not None
                }
            except Exception as error:
                LOGGER.error('Could not snapshot a Radarr target before the one-time run (%s)',
                             error.__class__.__name__)
                initial_target_ids[identity] = None
                success = False

        LOGGER.info('Starting one-time run with %d configured jobs', len(config['jobs']))
        for job in config['jobs']:
            try:
                if run_job(config, job, clients, run_context=run_context) is not True:
                    LOGGER.error('Job %s reported an incomplete run', job['id'])
                    success = False
            except SyncError as error:
                LOGGER.error('Job %s failed: %s', job['id'], error)
                success = False
            except Exception as error:
                LOGGER.exception('Job %s failed unexpectedly (%s)',
                                 job['id'], error.__class__.__name__)
                success = False

        for identity, incoming_jobs in target_groups.items():
            baseline = initial_target_ids.get(identity)
            if baseline is None:
                continue
            try:
                target_succeeded = _reinitialize_radarr_target(
                    incoming_jobs, clients[identity], baseline, clients, run_context)
            except Exception as error:
                LOGGER.exception('Radarr reinitialization failed unexpectedly (%s)',
                                 error.__class__.__name__)
                target_succeeded = False
            if not target_succeeded:
                success = False

        LOGGER.info('One-time run completed with status %s', 0 if success else 1)
        return 0 if success else 1
    finally:
        for client in clients.values():
            client.close()


def run(config):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    clients = _create_clients(config)

    if config.get('pairs'):
        units = config['units']
        next_runs = {unit['key']: 0 for unit in units}
        LOGGER.info('Starting multi-job mode with %d rules across %d units',
                    len(config['all_jobs']), len(units))
        try:
            while True:
                now = time.time()
                due_units = [unit for unit in units if next_runs[unit['key']] <= now]
                if due_units:
                    due_jobs = [job for unit in due_units for job in unit['jobs']]
                    try:
                        if not _run_planned_cycle(config, due_jobs, clients):
                            LOGGER.error('One or more target plans were incomplete')
                    except Exception as error:
                        LOGGER.exception('Pair cycle failed unexpectedly (%s)',
                                         error.__class__.__name__)
                    completed_at = time.time()
                    for unit in due_units:
                        next_runs[unit['key']] = completed_at + unit['interval_seconds']
                wait_seconds = max(0.2, min(next_runs.values()) - time.time())
                time.sleep(min(wait_seconds, 5))
        except KeyboardInterrupt:
            LOGGER.info('Stopping multi-job mode')
        finally:
            for client in clients.values():
                client.close()
        return

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
