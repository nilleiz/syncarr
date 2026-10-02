#!/usr/bin/env python
"""Multi-job synchronization engine for Radarr, Sonarr, and Lidarr."""

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
        self._languages = None

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

    def language_profile_id(self, name, explicit_id):
        if explicit_id is not None:
            return explicit_id
        if not name:
            return None
        if self.arr_type != 'sonarr':
            raise SyncError('Language profiles can only be resolved for Sonarr')
        if self._languages is None:
            self._languages = self.request('GET', 'languageprofile', expected=(200,))
        match = next((profile for profile in self._languages
                      if str(profile.get('name', '')).lower() == name.lower()), None)
        if match is None:
            match = next((profile for profile in self._languages
                          if any(str(item.get('language', {}).get('name', '')).lower() == name.lower()
                                 for item in profile.get('languages', []))), None)
        if match is None:
            raise SyncError('Could not resolve target language on the configured Sonarr instance')
        return match.get('id')

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


def _path_for_content(content, job):
    root_path, reason = map_root_path(content.get('path'), job)
    if reason:
        LOGGER.warning('Job %s skipped an item: %s', job['id'], reason)
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
    if job['source_quality_match']:
        movie_file = content.get('movieFile') or {}
        quality = movie_file.get('quality') or {}
        quality_name = (quality.get('quality') or {}).get('name', '')
        if quality_name and not re.match(job['source_quality_match'], quality_name):
            return False
    blacklist = set(job['source_blacklist'])
    if blacklist:
        slug = content.get('titleSlug') or content.get('foreignArtistId')
        if str(slug) in blacklist or str(content.get('id')) in blacklist:
            return False
    if client.arr_type == 'radarr' and job['skip_missing'] and not content.get('hasFile'):
        return False
    return True


def _image_payload(content, target_url):
    images = []
    for image in content.get('images') or []:
        entry = dict(image)
        image_url = entry.get('url')
        if image_url and not image_url.startswith(('http://', 'https://')):
            entry['url'] = target_url.rstrip('/') + '/' + image_url.lstrip('/')
        images.append(entry)
    return images


def _build_payload(content, job, client, root_path, tag_id):
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
        payload.update({
            'title': content.get('title'),
            'titleSlug': content.get('titleSlug'),
            'seasons': content.get('seasons'),
            'year': content.get('year'),
            'tvRageId': content.get('tvRageId'),
            'seasonFolder': content.get('seasonFolder'),
            'languageProfileId': job.get('resolved_language_profile_id') or content.get('languageProfileId'),
            'seriesType': content.get('seriesType'),
            'useSceneNumbering': content.get('useSceneNumbering'),
            'addOptions': dict(add_options, searchForMissingEpisodes=job['auto_search']),
        })
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
    job['resolved_language_profile_id'] = target_client.language_profile_id(
        job['target_language'], job['target_language_id'])
    tag_id = target_client.tag_id(rule_tag(job), create=create_tags)
    return source_client, target_client, tag_id


def _sync_items(job, source_client, target_client, tag_id, source_items, target_items):
    profile_filter_id, tag_filter_ids = _resolve_source_filters(source_client, job)
    arr_type = source_client.arr_type
    target_by_key = {}
    for item in target_items:
        key = _content_key(item, arr_type)
        if key is not None:
            target_by_key.setdefault(key, []).append(item)

    for content in source_items:
        if not _passes_filters(content, source_client, job, profile_filter_id, tag_filter_ids):
            continue
        key = _content_key(content, arr_type)
        if key is None:
            LOGGER.warning('Job %s skipped an item without a stable external ID', job['id'])
            continue
        matches = target_by_key.get(key, [])
        if not matches:
            root_path = _path_for_content(content, job)
            if not root_path:
                continue
            if job['test_run']:
                LOGGER.info('Job %s would add one item (test run)', job['id'])
                continue
            payload = _build_payload(content, job, target_client, root_path, tag_id)
            result = target_client.request('POST', target_client.content_route,
                                           payload=payload, expected=(200, 201))
            if isinstance(result, dict):
                target_items.append(result)
                target_by_key.setdefault(key, []).append(result)
            LOGGER.info('Job %s added one item', job['id'])
            continue

        if len(matches) > 1:
            LOGGER.warning('Job %s found duplicate target IDs; skipped an update', job['id'])
            continue
        current = matches[0]
        current_tags = list(current.get('tags') or [])
        changed = False
        if tag_id is not None and tag_id not in current_tags:
            current_tags.append(tag_id)
            current['tags'] = current_tags
            changed = True
        if job['sync_monitor'] and current.get('monitored') != content.get('monitored'):
            current['monitored'] = content.get('monitored')
            changed = True
        if changed and not job['test_run']:
            target_client.request('PUT', '{}/{}'.format(target_client.content_route, current['id']),
                                  payload=current, expected=(200, 202))
            LOGGER.info('Job %s updated one item', job['id'])


def _items_for_deletion(target_items, incoming_jobs, source_snapshots, target_client):
    deleting_jobs = [job for job in incoming_jobs if job['delete_missing']]
    if not deleting_jobs:
        return []
    policy = deleting_jobs[0]['target']['delete_conflict_policy']
    arr_type = target_client.arr_type
    source_keys = {}
    for job in incoming_jobs:
        contents = source_snapshots[job['source']['identity']]['contents']
        source_keys[job['id']] = set(key for key in (_content_key(item, arr_type) for item in contents) if key is not None)

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


def run_job(config, job, clients):
    source_client, target_client, tag_id = _prepare_job(job, clients, create_tags=not job['test_run'])
    source_items = source_client.list_content()
    target_items = target_client.list_content()
    _sync_items(job, source_client, target_client, tag_id, source_items, target_items)

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
        live_authors = [author for author in authors if not author['test_run']]
        if not live_authors:
            LOGGER.info('Job %s would delete one missing Radarr item (test run)', job['id'])
            continue
        if job not in live_authors:
            continue
        delete_files = all(author['delete_files'] for author in live_authors)
        target_client.delete_movie(item['id'], delete_files)
        LOGGER.info('Job %s deleted one missing Radarr item (delete_files=%s)', job['id'], delete_files)


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
