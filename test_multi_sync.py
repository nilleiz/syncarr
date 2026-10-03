import json
import unittest

from unittest.mock import patch

import index
from multi_sync import (ArrClient, SyncError, _apply_sonarr_episode_plan,
                        _items_for_deletion, _passes_file_filters, _passes_filters,
                        _reinitialize_radarr_target, _sonarr_episode_plan, _sync_items,
                        rule_tag, run_job, run_once)


class FakeSonarrClient(object):
    def __init__(self, identity, episodes=None, tag_ids=None):
        self.identity = identity
        self.arr_type = 'sonarr'
        self.url = identity[1]
        self.episodes = episodes or {}
        self.tag_ids = tag_ids or {}
        self.calls = []

    def list_episodes(self, series_id):
        return self.episodes[str(series_id)]

    def tag_id(self, label, create=False):
        return self.tag_ids.get(label)

    def request(self, method, route, params=None, payload=None, expected=None):
        self.calls.append((method, route, params, payload))
        return None

    def set_episodes_monitored(self, episode_ids, monitored):
        self.calls.append(('MONITOR', list(episode_ids), bool(monitored)))

    def delete_episode_file(self, file_id, delete_files):
        self.calls.append(('DELETE_FILE', file_id, delete_files))


class FakeSonarrAddTarget(object):
    def __init__(self):
        self.arr_type = 'sonarr'
        self.identity = ('sonarr', 'http://target')
        self.content_route = 'series'
        self.url = 'http://target'
        self.tag_ids = {}
        self.calls = []

    def request(self, method, route, params=None, payload=None, expected=None):
        self.calls.append((method, route, payload))
        return dict(payload, id=20, tvdbId=100) if method == 'POST' else payload


class FakeRadarrClient(object):
    def __init__(self, identity, items=None, error=None, movie_file_error=None):
        self.identity = identity
        self.arr_type = 'radarr'
        self.content_route = 'movie'
        self.url = identity[1]
        self.items = list(items or [])
        self.error = error
        self.movie_file_error = movie_file_error
        self.movie_files = {}
        self.movie_file_calls = []
        self.tag_ids = {}
        self.deleted_movies = []
        self.list_content_calls = 0
        self.calls = []

    def list_content(self):
        self.list_content_calls += 1
        if self.error:
            raise self.error
        return list(self.items)

    def list_movie_files(self, movie_id):
        self.movie_file_calls.append(movie_id)
        if self.movie_file_error:
            raise self.movie_file_error
        return list(self.movie_files.get(movie_id, []))

    def profile_id(self, name, explicit_id, setting_name):
        return explicit_id

    def tag_id(self, label, create=False):
        return self.tag_ids.get(label)

    def request(self, method, route, params=None, payload=None, expected=None):
        self.calls.append((method, route, params, payload))
        return None

    def delete_movie(self, content_id, delete_files):
        self.deleted_movies.append((content_id, delete_files))
        self.calls.append(('DELETE_MOVIE', content_id, delete_files))

    def update_movie(self, movie):
        self.calls.append(('UPDATE_MOVIE', movie['id'], dict(movie)))
        for current in self.items:
            if current.get('id') == movie['id']:
                current.update(movie)
                break

    def search_movies(self, movie_ids):
        self.calls.append(('SEARCH_MOVIES', list(movie_ids)))

    def close(self):
        pass


def _episode(number, file_id, monitored, quality, formats=None, score=0):
    return {
        'id': number + 50,
        'seasonNumber': 1,
        'episodeNumber': number,
        'episodeFileId': file_id,
        'hasFile': bool(file_id),
        'monitored': monitored,
        'episodeFile': ({
            'id': file_id,
            'quality': {'quality': {'name': quality}} if quality else None,
            'customFormats': formats or [],
            'customFormatScore': score,
        } if file_id else None),
    }


def _job(job_id, source, target, tag_id, **changes):
    job = {
        'id': job_id,
        'source_instance_id': source.identity[1].rsplit('/', 1)[-1],
        'target_instance_id': target.identity[1].rsplit('/', 1)[-1],
        'source': {'identity': source.identity},
        'target': {
            'identity': target.identity,
            'type': target.arr_type,
            'url': target.url,
            'delete_conflict_policy': 'keep_if_any_source',
        },
        'source_profile_filter_id': None,
        'source_profile_filter': None,
        'source_tag_filter_id': [],
        'source_tag_filter': [],
        'source_blacklist': [],
        'target_profile': None,
        'target_profile_id': None,
        'source_quality_match': '^Bluray',
        'source_custom_format_mode': 'any',
        'source_custom_format_names': ['Dolby Vision without fallback'],
        'source_custom_format_exclude_names': ['HDR10 fallback'],
        'source_custom_format_minimum_score': None,
        'has_file_filters': True,
        'delete_missing': False,
        'delete_if_filter_not_matching': False,
        'delete_scope': 'managed_only',
        'delete_files': False,
        'test_run': False,
        'auto_search': True,
        'monitor_new_content': True,
        'sync_monitor': False,
        'skip_missing': True,
    }
    job.update(changes)
    target.tag_ids[rule_tag(job)] = tag_id
    return job


def _entity_events(captured):
    return [json.loads(record.getMessage()[len('ENTITY '):])
            for record in captured.records
            if record.getMessage().startswith('ENTITY ')]


class FileFilterTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            'has_file_filters': True,
            'source_quality_match': None,
            'source_custom_format_mode': 'any',
            'source_custom_format_names': ['Dolby Vision without fallback'],
            'source_custom_format_exclude_names': ['HDR10 fallback'],
            'source_custom_format_minimum_score': None,
        }

    def test_custom_format_any_is_case_insensitive_and_exclusions_veto(self):
        file_record = {'customFormats': [{'name': 'DOLBY VISION WITHOUT FALLBACK'}]}
        self.assertTrue(_passes_file_filters(file_record, self.job))
        file_record['customFormats'].append({'name': 'HDR10 fallback'})
        self.assertFalse(_passes_file_filters(file_record, self.job))

    def test_custom_format_all_requires_every_name(self):
        self.job['source_custom_format_mode'] = 'all'
        self.job['source_custom_format_names'] = ['DV', 'HDR']
        self.assertFalse(_passes_file_filters({'customFormats': [{'name': 'DV'}]}, self.job))
        self.assertTrue(_passes_file_filters(
            {'customFormats': [{'name': 'dv'}, {'name': 'hdr'}]}, self.job))

    def test_score_threshold_is_inclusive_and_quality_is_combined(self):
        self.job.update({
            'source_quality_match': '^Bluray-2160p$',
            'source_custom_format_mode': 'score',
            'source_custom_format_names': [],
            'source_custom_format_exclude_names': [],
            'source_custom_format_minimum_score': 100,
        })
        record = {'quality': {'quality': {'name': 'Bluray-2160p'}}, 'customFormatScore': 100}
        self.assertTrue(_passes_file_filters(record, self.job))
        record['quality']['quality']['name'] = 'WEBDL-2160p'
        self.assertFalse(_passes_file_filters(record, self.job))

    def test_missing_file_data_does_not_pass_enabled_filter(self):
        self.assertFalse(_passes_file_filters(None, self.job))

    def test_arr_clients_use_file_and_episode_resources(self):
        radarr = ArrClient({'type': 'radarr', 'url': 'http://radarr', 'api_key': 'test'})
        with patch.object(radarr, 'request', return_value=[]) as request:
            radarr.list_movie_files(19)
        request.assert_called_once_with('GET', 'moviefile', params={'movieId': 19}, expected=(200,))
        with patch.object(radarr, 'request', return_value=[]) as request:
            radarr.search_movies([19])
        request.assert_called_once_with(
            'POST', 'command', payload={'name': 'MoviesSearch', 'movieIds': [19]},
            expected=(200, 201, 202))
        with patch.object(radarr, 'request', return_value=[]) as request:
            radarr.update_movie({'id': 19, 'monitored': True})
        request.assert_called_once_with(
            'PUT', 'movie/19', payload={'id': 19, 'monitored': True}, expected=(200, 202))
        radarr.close()

        sonarr = ArrClient({'type': 'sonarr', 'url': 'http://sonarr', 'api_key': 'test'})
        with patch.object(sonarr, 'request', return_value=[]) as request:
            sonarr.list_episodes(23)
        request.assert_called_once_with(
            'GET', 'episode', params={'seriesId': 23, 'includeEpisodeFile': 'true'}, expected=(200,))
        sonarr.close()


class RadarrDeletionPresenceTests(unittest.TestCase):
    def make_fixture(self, source_movies, **changes):
        source = FakeRadarrClient(('radarr', 'http://source'), source_movies)
        target_item = {'id': 20, 'tmdbId': 100, 'tags': [7]}
        target = FakeRadarrClient(('radarr', 'http://target'), [target_item])
        options = {
            'delete_missing': True,
            'delete_scope': 'all_missing',
            'has_file_filters': False,
            'source_quality_match': None,
        }
        options.update(changes)
        job = _job('delete', source, target, 7, **options)
        return source, target, job, target_item

    def deletion_candidates(self, job, source_movies, target, target_item):
        snapshots = {job['source']['identity']: {'contents': source_movies}}
        return _items_for_deletion([target_item], [job], snapshots, target)

    def test_radarr_record_without_file_is_a_deletion_candidate(self):
        source, target, job, target_item = self.make_fixture(
            [{'tmdbId': 100, 'hasFile': False}])

        candidates = self.deletion_candidates(
            job, source.items, target, target_item)

        self.assertEqual([item['id'] for item, unused_authors in candidates], [20])

    def test_radarr_file_presence_transition_to_missing_allows_deletion(self):
        source, target, job, target_item = self.make_fixture(
            [{'tmdbId': 100, 'hasFile': True}])

        self.assertEqual(self.deletion_candidates(job, source.items, target, target_item), [])
        source.items[0]['hasFile'] = False

        candidates = self.deletion_candidates(job, source.items, target, target_item)

        self.assertEqual([item['id'] for item, unused_authors in candidates], [20])

    def test_file_presence_protects_target_even_when_quality_filters_do_not_match(self):
        source_a = FakeRadarrClient(('radarr', 'http://source-a'))
        source_b = FakeRadarrClient(('radarr', 'http://source-b'))
        target_item = {'id': 20, 'tmdbId': 100, 'tags': [8]}
        target = FakeRadarrClient(('radarr', 'http://target'), [target_item])
        job_a = _job('source-a', source_a, target, 7,
                     has_file_filters=True,
                     source_profile_filter_id=99,
                     source_quality_match='^Bluray-2160p$',
                     source_custom_format_mode=None,
                     source_custom_format_names=[],
                     source_custom_format_exclude_names=[])
        job_b = _job('source-b', source_b, target, 8,
                     delete_missing=True, delete_scope='all_missing',
                     has_file_filters=False, source_quality_match=None)
        source_movie = {'tmdbId': 100, 'hasFile': True, 'qualityProfileId': 1}
        nonmatching_file = {'quality': {'quality': {'name': 'WEBDL-1080p'}}}
        source_movies = [source_movie]
        snapshots = {
            source_a.identity: {'contents': source_movies},
            source_b.identity: {'contents': []},
        }

        candidates = _items_for_deletion(
            [target_item], [job_a, job_b], snapshots, target)

        self.assertFalse(_passes_filters(source_movie, source_a, job_a, 99, set()))
        self.assertFalse(_passes_file_filters(nonmatching_file, job_a))
        self.assertEqual(candidates, [])

    def test_enabled_job_treats_no_matching_file_as_missing(self):
        source, target, job, target_item = self.make_fixture(
            [{'id': 10, 'tmdbId': 100, 'hasFile': True}],
            delete_if_filter_not_matching=True,
            has_file_filters=True,
            source_quality_match='^Bluray-2160p$',
            source_custom_format_mode='any',
            source_custom_format_names=['DV'],
            source_custom_format_exclude_names=[])
        snapshots = {
            source.identity: {
                'contents': source.items,
                'movie_files': {10: [{
                    'id': 101,
                    'quality': {'quality': {'name': 'Bluray-2160p'}},
                    'customFormats': [],
                }]},
            },
        }

        candidates = _items_for_deletion([target_item], [job], snapshots, target)

        self.assertEqual([item['id'] for item, unused_authors in candidates], [20])

    def test_matching_file_protects_even_when_another_file_mismatches(self):
        source, target, job, target_item = self.make_fixture(
            [{'id': 10, 'tmdbId': 100, 'hasFile': True}],
            delete_if_filter_not_matching=True,
            has_file_filters=True,
            source_quality_match='^Bluray-2160p$',
            source_custom_format_mode='any',
            source_custom_format_names=['DV'],
            source_custom_format_exclude_names=[])
        snapshots = {
            source.identity: {
                'contents': source.items,
                'movie_files': {10: [
                    {'id': 101,
                     'quality': {'quality': {'name': 'WEBDL-1080p'}},
                     'customFormats': [{'name': 'DV'}]},
                    {'id': 102,
                     'quality': {'quality': {'name': 'Bluray-2160p'}},
                     'customFormats': [{'name': 'DV'}]},
                ]},
            },
        }

        candidates = _items_for_deletion([target_item], [job], snapshots, target)

        self.assertEqual(candidates, [])

    def test_quality_and_custom_format_must_match_on_the_same_file(self):
        source, target, job, target_item = self.make_fixture(
            [{'id': 10, 'tmdbId': 100, 'hasFile': True}],
            delete_if_filter_not_matching=True,
            has_file_filters=True,
            source_quality_match='^Bluray-2160p$',
            source_custom_format_mode='any',
            source_custom_format_names=['DV'],
            source_custom_format_exclude_names=[])
        snapshots = {
            source.identity: {
                'contents': source.items,
                'movie_files': {10: [
                    {'id': 101,
                     'quality': {'quality': {'name': 'Bluray-2160p'}},
                     'customFormats': []},
                    {'id': 102,
                     'quality': {'quality': {'name': 'WEBDL-1080p'}},
                     'customFormats': [{'name': 'DV'}]},
                ]},
            },
        }

        candidates = _items_for_deletion([target_item], [job], snapshots, target)

        self.assertEqual([item['id'] for item, unused_authors in candidates], [20])

    def test_keep_if_any_source_uses_each_jobs_filter_presence(self):
        source_a = FakeRadarrClient(('radarr', 'http://source-a'))
        source_b = FakeRadarrClient(('radarr', 'http://source-b'))
        target_item = {'id': 20, 'tmdbId': 100, 'tags': [7, 8]}
        target = FakeRadarrClient(('radarr', 'http://target'), [target_item])
        common = {
            'delete_missing': True,
            'delete_scope': 'all_missing',
            'delete_if_filter_not_matching': True,
            'has_file_filters': True,
            'source_quality_match': None,
            'source_custom_format_mode': 'any',
            'source_custom_format_names': ['DV'],
            'source_custom_format_exclude_names': [],
        }
        job_a = _job('source-a', source_a, target, 7, **common)
        job_b = _job('source-b', source_b, target, 8, **common)
        movie = {'id': 10, 'tmdbId': 100, 'hasFile': True}
        snapshots = {
            source_a.identity: {
                'contents': [movie],
                'movie_files': {10: [{'id': 101, 'customFormats': []}]},
            },
            source_b.identity: {
                'contents': [dict(movie, id=11)],
                'movie_files': {11: [{'id': 111, 'customFormats': [{'name': 'DV'}]}]},
            },
        }

        self.assertEqual(
            _items_for_deletion([target_item], [job_a, job_b], snapshots, target), [])

        snapshots[source_b.identity]['movie_files'][11] = [{'id': 111, 'customFormats': []}]
        candidates = _items_for_deletion([target_item], [job_a, job_b], snapshots, target)
        self.assertEqual([job['id'] for job in candidates[0][1]], ['source-a', 'source-b'])

    def test_filter_deletion_flag_is_inert_without_delete_missing(self):
        source_a = FakeRadarrClient(('radarr', 'http://source-a'))
        source_b = FakeRadarrClient(('radarr', 'http://source-b'))
        target_item = {'id': 20, 'tmdbId': 100, 'tags': [7]}
        target = FakeRadarrClient(('radarr', 'http://target'), [target_item])
        job_a = _job('source-a', source_a, target, 7,
                     delete_missing=False, delete_if_filter_not_matching=True,
                     has_file_filters=True, source_quality_match=None,
                     source_custom_format_mode='any', source_custom_format_names=['DV'],
                     source_custom_format_exclude_names=[])
        job_b = _job('source-b', source_b, target, 8,
                     delete_missing=True, delete_scope='all_missing',
                     has_file_filters=False, source_quality_match=None)
        snapshots = {
            source_a.identity: {'contents': [{'id': 10, 'tmdbId': 100, 'hasFile': True}]},
            source_b.identity: {'contents': []},
        }

        candidates = _items_for_deletion([target_item], [job_a, job_b], snapshots, target)

        self.assertEqual(candidates, [])

    def test_source_rule_wins_uses_only_the_deleting_job_filter_presence(self):
        source_a = FakeRadarrClient(('radarr', 'http://source-a'))
        source_b = FakeRadarrClient(('radarr', 'http://source-b'))
        target_item = {'id': 20, 'tmdbId': 100, 'tags': [7, 8]}
        target = FakeRadarrClient(('radarr', 'http://target'), [target_item])
        target_policy = 'source_rule_wins'
        job_a = _job('source-a', source_a, target, 7,
                     delete_missing=True, delete_scope='all_missing',
                     delete_if_filter_not_matching=True, has_file_filters=True,
                     source_quality_match=None, source_custom_format_mode='any',
                     source_custom_format_names=['DV'], source_custom_format_exclude_names=[])
        job_b = _job('source-b', source_b, target, 8,
                     delete_missing=True, delete_scope='all_missing',
                     delete_if_filter_not_matching=True, has_file_filters=True,
                     source_quality_match=None, source_custom_format_mode='any',
                     source_custom_format_names=['DV'], source_custom_format_exclude_names=[])
        job_a['target']['delete_conflict_policy'] = target_policy
        job_b['target']['delete_conflict_policy'] = target_policy
        movie = {'id': 10, 'tmdbId': 100, 'hasFile': True}
        snapshots = {
            source_a.identity: {
                'contents': [movie],
                'movie_files': {10: [{'id': 101, 'customFormats': []}]},
            },
            source_b.identity: {
                'contents': [dict(movie, id=11)],
                'movie_files': {11: [{'id': 111, 'customFormats': [{'name': 'DV'}]}]},
            },
        }

        candidates = _items_for_deletion([target_item], [job_a, job_b], snapshots, target)

        self.assertEqual(len(candidates), 1)
        self.assertEqual([job['id'] for job in candidates[0][1]], ['source-a'])

    def test_unknown_required_file_metadata_fails_closed(self):
        source, target, job, target_item = self.make_fixture(
            [{'id': 10, 'tmdbId': 100, 'hasFile': True}],
            delete_if_filter_not_matching=True,
            has_file_filters=True,
            source_quality_match=None,
            source_custom_format_mode='any',
            source_custom_format_names=['DV'],
            source_custom_format_exclude_names=[])
        snapshots = {
            source.identity: {
                'contents': source.items,
                'movie_files': {10: [{'id': 101}]},
            },
        }

        with self.assertRaisesRegex(SyncError, 'incomplete filter metadata'):
            _items_for_deletion([target_item], [job], snapshots, target)

        source.movie_files[10] = [{'id': 101}]
        clients = {source.identity: source, target.identity: target}
        run_job({'jobs': [job]}, job, clients)
        self.assertEqual(target.deleted_movies, [])

    def test_movie_file_inventory_error_prevents_radarr_deletion(self):
        source_a = FakeRadarrClient(('radarr', 'http://source-a'))
        source_b = FakeRadarrClient(
            ('radarr', 'http://source-b'),
            [{'id': 11, 'tmdbId': 100, 'hasFile': True}],
            movie_file_error=SyncError('inventory unavailable'))
        target = FakeRadarrClient(
            ('radarr', 'http://target'), [{'id': 20, 'tmdbId': 100, 'tags': [7]}])
        job_a = _job('source-a', source_a, target, 7,
                     delete_missing=True, delete_scope='all_missing',
                     has_file_filters=False, source_quality_match=None)
        job_b = _job('source-b', source_b, target, 8,
                     delete_missing=True, delete_scope='all_missing',
                     delete_if_filter_not_matching=True,
                     has_file_filters=True, source_quality_match=None,
                     source_custom_format_mode='any',
                     source_custom_format_names=['DV'],
                     source_custom_format_exclude_names=[])
        config = {'jobs': [job_a, job_b]}
        clients = {
            source_a.identity: source_a,
            source_b.identity: source_b,
            target.identity: target,
        }

        run_job(config, job_a, clients)

        self.assertEqual(source_b.movie_file_calls, [11])
        self.assertEqual(target.deleted_movies, [])

    def test_current_job_movie_file_error_stops_before_deletion(self):
        source = FakeRadarrClient(
            ('radarr', 'http://source'),
            [{'id': 10, 'tmdbId': 100, 'hasFile': True}],
            movie_file_error=SyncError('inventory unavailable'))
        target = FakeRadarrClient(
            ('radarr', 'http://target'), [{'id': 20, 'tmdbId': 100, 'tags': [7]}])
        job = _job('delete', source, target, 7,
                   delete_missing=True, delete_scope='all_missing',
                   delete_if_filter_not_matching=True, has_file_filters=True,
                   source_quality_match=None, source_custom_format_mode='any',
                   source_custom_format_names=['DV'], source_custom_format_exclude_names=[])
        config = {'jobs': [job]}
        clients = {source.identity: source, target.identity: target}

        run_job(config, job, clients)

        self.assertEqual(source.movie_file_calls, [10])
        self.assertEqual(target.deleted_movies, [])

    def test_filter_mismatch_delete_log_reports_existing_source_file(self):
        source = FakeRadarrClient(
            ('radarr', 'http://source'),
            [{'id': 10, 'tmdbId': 100, 'title': 'Filtered Movie', 'hasFile': True}])
        source.movie_files[10] = [{'id': 101, 'customFormats': []}]
        target_item = {'id': 20, 'tmdbId': 100, 'title': 'Filtered Movie', 'tags': [7]}
        target = FakeRadarrClient(('radarr', 'http://target'), [target_item])
        job = _job('delete', source, target, 7,
                   delete_missing=True, delete_scope='all_missing',
                   delete_if_filter_not_matching=True, has_file_filters=True,
                   source_quality_match=None, source_custom_format_mode='any',
                   source_custom_format_names=['DV'], source_custom_format_exclude_names=[])
        config = {'jobs': [job]}
        clients = {source.identity: source, target.identity: target}

        with self.assertLogs('syncarr', level='INFO') as captured:
            run_job(config, job, clients)

        event = next(item for item in _entity_events(captured)
                     if item['action'] == 'delete_movie')
        self.assertEqual(event['reason'], 'source_file_filter_mismatch')
        self.assertTrue(event['source_has_file'])
        self.assertTrue(event['source_filter_mismatch'])
        self.assertEqual(source.movie_file_calls, [10])
        self.assertEqual(target.deleted_movies, [(20, False)])

    def test_source_inventory_error_prevents_radarr_deletion(self):
        source_a = FakeRadarrClient(('radarr', 'http://source-a'))
        source_b = FakeRadarrClient(
            ('radarr', 'http://source-b'), error=SyncError('inventory unavailable'))
        target = FakeRadarrClient(
            ('radarr', 'http://target'), [{'id': 20, 'tmdbId': 100, 'tags': [7]}])
        job_a = _job('source-a', source_a, target, 7,
                     delete_missing=True, delete_scope='all_missing',
                     target_profile=None, target_profile_id=1)
        job_b = _job('source-b', source_b, target, 8,
                     delete_missing=False, target_profile=None, target_profile_id=1)
        config = {'jobs': [job_a, job_b]}
        clients = {
            source_a.identity: source_a,
            source_b.identity: source_b,
            target.identity: target,
        }

        run_job(config, job_a, clients)

        self.assertEqual(source_b.list_content_calls, 1)
        self.assertEqual(target.deleted_movies, [])


class EntityLoggingTests(unittest.TestCase):
    def test_add_events_identify_movie_in_dry_run_and_live_without_secrets(self):
        source_item = {
            'id': 10, 'tmdbId': 100, 'title': 'Example Movie',
            'path': '/source/movies/Example Movie', 'hasFile': True,
        }
        records = []
        for test_run in (True, False):
            source = FakeRadarrClient(('radarr', 'https://source.invalid'), [source_item])
            target = FakeRadarrClient(('radarr', 'https://target.invalid'))
            job = _job('movies', source, target, 7,
                       test_run=test_run, has_file_filters=False,
                       source_quality_match=None, source_custom_format_mode=None,
                       root_mappings=[], target_root_path='/target/movies',
                       resolved_profile_id=1)
            job['source']['api_key'] = 'source-secret'
            job['target']['api_key'] = 'target-secret'
            job['target']['url'] = 'https://user:target-secret@target.invalid'
            with self.assertLogs('syncarr', level='INFO') as captured:
                _sync_items(job, source, target, 7, [source_item], [])
            event = next(item for item in _entity_events(captured) if item['action'] == 'add')
            records.append(event)

        for event in records:
            self.assertEqual(event['title'], 'Example Movie')
            self.assertEqual(event['tmdb_id'], 100)
            self.assertEqual(event['source_instance'], 'source.invalid')
            self.assertEqual(event['target_instance'], 'target.invalid')
            self.assertTrue(event['has_file'])
        self.assertEqual(records[0]['mode'], 'dry_run')
        self.assertEqual(records[1]['mode'], 'live')
        self.assertEqual(records[0]['outcome'], 'would_apply')
        self.assertEqual(records[1]['outcome'], 'attempted')
        comparable = [dict(event, mode=None, outcome=None) for event in records]
        self.assertEqual(comparable[0], comparable[1])
        self.assertNotIn('source-secret', str(records))
        self.assertNotIn('target-secret', str(records))
        self.assertNotIn('https://', str(records))

    def test_update_events_include_target_identity_and_change_reason(self):
        records = []
        for test_run in (True, False):
            source = FakeRadarrClient(('radarr', 'http://source'), [{
                'id': 10, 'tmdbId': 100, 'title': 'Existing Movie',
                'hasFile': True, 'monitored': False,
            }])
            target = FakeRadarrClient(('radarr', 'http://target'))
            job = _job('movies', source, target, 7,
                       test_run=test_run, has_file_filters=False,
                       source_quality_match=None, sync_monitor=True)
            target_item = {
                'id': 20, 'tmdbId': 100, 'title': 'Existing Movie',
                'hasFile': True, 'monitored': True, 'tags': [],
            }
            with self.assertLogs('syncarr', level='INFO') as captured:
                _sync_items(job, source, target, 7, source.items, [target_item])
            event = next(item for item in _entity_events(captured)
                         if item['action'] == 'update')
            records.append(event)

        for event in records:
            self.assertEqual(event['title'], 'Existing Movie')
            self.assertEqual(event['tmdb_id'], 100)
            self.assertEqual(event['arr_record_id'], 10)
            self.assertEqual(event['target_record_id'], 20)
            self.assertTrue(event['has_file'])
            self.assertTrue(event['target_has_file'])
            self.assertEqual(event['change_reasons'],
                             ['managed_tag_missing', 'monitored_state_differs'])
        self.assertEqual(records[0]['mode'], 'dry_run')
        self.assertEqual(records[1]['mode'], 'live')
        self.assertEqual(dict(records[0], mode=None, outcome=None),
                         dict(records[1], mode=None, outcome=None))

    def test_radarr_dry_run_delete_logs_same_details_as_live_delete(self):
        records = []
        targets = []
        for test_run in (True, False):
            source = FakeRadarrClient(('radarr', 'http://source'))
            target_item = {
                'id': 20, 'tmdbId': 100, 'title': 'Missing Movie',
                'hasFile': True, 'tags': [7],
            }
            target = FakeRadarrClient(('radarr', 'http://target'), [target_item])
            job = _job('delete', source, target, 7,
                       delete_missing=True, delete_scope='all_missing',
                       delete_files=True, has_file_filters=False,
                       source_quality_match=None, test_run=test_run)
            config = {'jobs': [job]}
            clients = {source.identity: source, target.identity: target}
            with self.assertLogs('syncarr', level='INFO') as captured:
                run_job(config, job, clients)
            event = next(item for item in _entity_events(captured)
                         if item['action'] == 'delete_movie')
            records.append(event)
            targets.append(target)

        dry_run, live = records
        for event in records:
            self.assertEqual(event['title'], 'Missing Movie')
            self.assertEqual(event['tmdb_id'], 100)
            self.assertEqual(event['arr_record_id'], 20)
            self.assertEqual(event['target_record_id'], 20)
            self.assertTrue(event['has_file'])
            self.assertFalse(event['source_has_file'])
            self.assertTrue(event['delete_files'])
            self.assertEqual(event['author_job_ids'], ['delete'])
        self.assertEqual(dry_run['mode'], 'dry_run')
        self.assertEqual(live['mode'], 'live')
        self.assertEqual(dry_run['outcome'], 'would_apply')
        self.assertEqual(live['outcome'], 'attempted')
        self.assertEqual(dict(dry_run, mode=None, outcome=None),
                         dict(live, mode=None, outcome=None))
        self.assertEqual(targets[0].deleted_movies, [])
        self.assertEqual(targets[1].deleted_movies, [(20, True)])


class RadarrReinitializationTests(unittest.TestCase):
    def make_fixture(self):
        source = FakeRadarrClient(('radarr', 'http://source'), [{
            'id': 10,
            'tmdbId': 100,
            'title': 'Example Movie',
            'hasFile': True,
            'monitored': False,
        }])
        source.movie_files[10] = [{
            'id': 101,
            'quality': {'quality': {'name': 'Bluray-2160p'}},
            'customFormats': [{'name': 'Dolby Vision without fallback'}],
        }]
        target_movie = {
            'id': 20,
            'tmdbId': 100,
            'title': 'Example Movie',
            'hasFile': False,
            'monitored': False,
        }
        target = FakeRadarrClient(('radarr', 'http://target'), [target_movie])
        job = _job('movies', source, target, 7)
        return source, target, job, target_movie

    def make_run_context(self, jobs, clients):
        context = {
            'source_contents_by_job': {},
            'resolved_source_filters': {},
            'movie_file_cache': {},
        }
        for job in jobs:
            source = clients[job['source']['identity']]
            context['source_contents_by_job'][job['id']] = source.items
            context['resolved_source_filters'][job['id']] = (None, set())
            if job['has_file_filters']:
                for movie in source.items:
                    if movie.get('hasFile') is True:
                        context['movie_file_cache'][(source.identity, movie['id'])] = (
                            source.movie_files[movie['id']])
        return context

    def test_reactivates_fileless_target_from_unmonitored_source_and_searches(self):
        source, target, job, target_movie = self.make_fixture()

        success = _reinitialize_radarr_target(
            [job], target, {20}, {source.identity: source, target.identity: target},
            self.make_run_context([job], {source.identity: source}))

        self.assertTrue(success)
        self.assertTrue(target_movie['monitored'])
        self.assertEqual([call[0] for call in target.calls],
                         ['UPDATE_MOVIE', 'SEARCH_MOVIES'])
        self.assertEqual(target.calls[-1], ('SEARCH_MOVIES', [20]))

    def test_file_filter_mismatch_does_not_reactivate_target(self):
        source, target, job, unused_target_movie = self.make_fixture()
        source.movie_files[10][0]['quality']['quality']['name'] = 'WEBDL-1080p'

        success = _reinitialize_radarr_target(
            [job], target, {20}, {source.identity: source, target.identity: target},
            self.make_run_context([job], {source.identity: source}))

        self.assertTrue(success)
        self.assertEqual(target.calls, [])

    def test_target_with_file_is_not_reinitialized(self):
        source, target, job, target_movie = self.make_fixture()
        target_movie['hasFile'] = True

        success = _reinitialize_radarr_target(
            [job], target, {20}, {source.identity: source, target.identity: target},
            self.make_run_context([job], {source.identity: source}))

        self.assertTrue(success)
        self.assertEqual(target.calls, [])

    def test_only_preexisting_target_records_are_reinitialized(self):
        source, target, job, unused_target_movie = self.make_fixture()

        success = _reinitialize_radarr_target(
            [job], target, set(), {source.identity: source, target.identity: target},
            self.make_run_context([job], {source.identity: source}))

        self.assertTrue(success)
        self.assertEqual(target.calls, [])
        self.assertEqual(source.list_content_calls, 0)

    def test_overlapping_jobs_reinitialize_and_search_movie_once(self):
        source_a, target, job_a, unused_target_movie = self.make_fixture()
        source_b = FakeRadarrClient(('radarr', 'http://source-b'), [{
            'id': 11,
            'tmdbId': 100,
            'title': 'Example Movie',
            'hasFile': True,
            'monitored': False,
        }])
        source_b.movie_files[11] = list(source_a.movie_files[10])
        job_b = _job('movies-b', source_b, target, 8)
        target.calls = []

        success = _reinitialize_radarr_target(
            [job_a, job_b], target, {20},
            {source_a.identity: source_a, source_b.identity: source_b,
             target.identity: target},
            self.make_run_context([job_a, job_b],
                                  {source_a.identity: source_a,
                                   source_b.identity: source_b}))

        self.assertTrue(success)
        self.assertEqual([call[0] for call in target.calls],
                         ['UPDATE_MOVIE', 'SEARCH_MOVIES'])

    def test_one_matching_job_is_enough_when_another_job_does_not_match(self):
        source_a, target, job_a, unused_target_movie = self.make_fixture()
        source_b = FakeRadarrClient(('radarr', 'http://source-b'), [{
            'id': 11,
            'tmdbId': 100,
            'title': 'Example Movie',
            'hasFile': True,
            'monitored': False,
        }])
        source_b.movie_files[11] = [{
            'quality': {'quality': {'name': 'Bluray-2160p'}},
            'customFormats': [{'name': 'HDR10 fallback'}],
        }]
        job_b = _job('movies-b', source_b, target, 8)

        with self.assertLogs('syncarr', level='INFO') as captured:
            success = _reinitialize_radarr_target(
                [job_a, job_b], target, {20},
                {source_a.identity: source_a, source_b.identity: source_b,
                 target.identity: target},
                self.make_run_context([job_a, job_b],
                                      {source_a.identity: source_a,
                                       source_b.identity: source_b}))

        events = _entity_events(captured)
        self.assertTrue(success)
        self.assertEqual([call[0] for call in target.calls],
                         ['UPDATE_MOVIE', 'SEARCH_MOVIES'])
        self.assertTrue(all(event['qualifying_job_ids'] == ['movies'] for event in events))

    def test_one_time_runner_snapshots_before_jobs_then_runs_recovery(self):
        source, target, job, unused_target_movie = self.make_fixture()
        config = {'instances': {}, 'jobs': [job]}
        clients = {source.identity: source, target.identity: target}

        with patch('multi_sync._create_clients', return_value=clients), \
                patch('multi_sync.run_job', wraps=run_job) as job_runner:
            result = run_once(config)

        self.assertEqual(result, 0)
        job_runner.assert_called_once()
        self.assertEqual([call[0] for call in target.calls[-2:]],
                         ['UPDATE_MOVIE', 'SEARCH_MOVIES'])
        self.assertEqual(source.list_content_calls, 1)
        self.assertEqual(source.movie_file_calls, [10])

    def test_one_time_dry_run_logs_recovery_and_delete_without_writes(self):
        source, target, job, target_movie = self.make_fixture()
        target_movie['tags'] = [7]
        target.items.append({
            'id': 22,
            'tmdbId': 101,
            'title': 'Missing Movie',
            'hasFile': True,
            'monitored': True,
            'tags': [7],
        })
        job['test_run'] = True
        job['delete_missing'] = True
        job['delete_scope'] = 'all_missing'
        config = {'instances': {}, 'jobs': [job]}
        clients = {source.identity: source, target.identity: target}

        with self.assertLogs('syncarr', level='INFO') as captured, \
                patch('multi_sync._create_clients', return_value=clients):
            result = run_once(config)

        events = _entity_events(captured)
        actions = [event['action'] for event in events]
        self.assertEqual(result, 0)
        self.assertIn('delete_movie', actions)
        self.assertIn('update_movie_monitoring', actions)
        self.assertIn('search_movie', actions)
        self.assertTrue(all(event['outcome'] == 'would_apply' for event in events))
        self.assertEqual(target.calls, [])
        self.assertEqual(target.deleted_movies, [])

    def test_dry_run_logs_reactivation_and_search_without_radarr_writes(self):
        source, target, job, unused_target_movie = self.make_fixture()
        job['test_run'] = True

        with self.assertLogs('syncarr', level='INFO') as captured:
            success = _reinitialize_radarr_target(
                [job], target, {20}, {source.identity: source, target.identity: target},
                self.make_run_context([job], {source.identity: source}))

        events = _entity_events(captured)
        self.assertTrue(success)
        self.assertEqual([event['action'] for event in events],
                         ['update_movie_monitoring', 'search_movie'])
        self.assertTrue(all(event['outcome'] == 'would_apply' for event in events))
        self.assertEqual(target.calls, [])


class RunOnceTests(unittest.TestCase):
    def make_config(self, results_count=3):
        jobs = []
        for index_value in range(results_count):
            jobs.append({
                'id': 'job-{}'.format(index_value + 1),
                'interval_seconds': 1,
                'target': {'type': 'sonarr', 'identity': ('sonarr', 'http://target')},
            })
        return {'instances': {}, 'jobs': jobs}

    def test_jobs_run_once_in_order_and_failures_do_not_stop_later_jobs(self):
        config = self.make_config()
        called = []

        def execute_job(unused_config, job, unused_clients, run_context=None):
            called.append(job['id'])
            return job['id'] != 'job-2'

        with patch('multi_sync._create_clients', return_value={}), \
                patch('multi_sync.run_job', side_effect=execute_job):
            result = run_once(config)

        self.assertEqual(result, 1)
        self.assertEqual(called, ['job-1', 'job-2', 'job-3'])

    def test_all_successful_jobs_return_zero(self):
        config = self.make_config(results_count=2)
        called = []

        def execute_job(unused_config, job, unused_clients, run_context=None):
            called.append(job['id'])
            return True

        with patch('multi_sync._create_clients', return_value={}), \
                patch('multi_sync.run_job', side_effect=execute_job):
            result = run_once(config)

        self.assertEqual(result, 0)
        self.assertEqual(called, ['job-1', 'job-2'])

    def test_entrypoint_selects_once_mode_and_preserves_default_runner(self):
        with patch('index.load_multi_job_config', return_value={'reinitialize_b': True}), \
                patch('multi_sync.run_once', return_value=1) as one_time, \
                patch('multi_sync.run') as continuous, \
                patch('index._wait_for_container_stop') as idle:
            with self.assertLogs(level='WARNING') as logs:
                self.assertEqual(index.main(), 1)
        one_time.assert_called_once()
        idle.assert_called_once()
        continuous.assert_not_called()
        self.assertIn('One-time run finished with status 1', logs.output[0])
        self.assertIn('\x1b[31m', logs.output[0])

        with patch('index.load_multi_job_config', return_value={'reinitialize_b': False}), \
                patch('multi_sync.run_once') as one_time, \
                patch('multi_sync.run') as continuous:
            self.assertEqual(index.main(), 0)
        one_time.assert_not_called()
        continuous.assert_called_once()


class SonarrEpisodePlanTests(unittest.TestCase):
    def make_fixture(self, second_source=False):
        target_identity = ('sonarr', 'http://target')
        target = FakeSonarrClient(target_identity)
        source_a = FakeSonarrClient(
            ('sonarr', 'http://source-a'),
            {'10': [_episode(1, 0, True, None), _episode(2, 0, True, None)]})
        source_a.episodes['10'] = [
            _episode(1, 0, True, None),
            _episode(2, 0, True, None),
        ]
        series = {'id': 10, 'tvdbId': 100, 'qualityProfileId': 1}
        target_series = {
            'id': 20, 'tvdbId': 100, 'monitored': True, 'tags': [7],
        }
        target_episodes = [
            _episode(1, 0, False, None),
            _episode(2, 500, True, 'WEBDL-2160p', [{'name': 'HDR10 fallback'}]),
        ]
        target.episodes['20'] = target_episodes
        source_a.episodes['10'] = [
            dict(_episode(1, 101, True, 'Bluray-2160p',
                          [{'name': 'Dolby Vision without fallback'}]),
                 id=1),
            dict(_episode(2, 102, True, 'WEB-2160p', [{'name': 'HDR10 fallback'}]),
                 id=2),
        ]
        source_a.tag_ids[rule_tag({'id': 'episodes'})] = 7
        job = _job('episodes', source_a, target, 7,
                   delete_missing=True, delete_files=True, delete_scope='all_missing')
        clients = {source_a.identity: source_a, target.identity: target}
        snapshots = {source_a.identity: [series]}

        if not second_source:
            return target, source_a, job, clients, snapshots, [target_series]

        source_b = FakeSonarrClient(
            ('sonarr', 'http://source-b'),
            {'11': [dict(_episode(2, 201, True, 'Bluray-2160p',
                                  [{'name': 'Dolby Vision without fallback'}]), id=3)]})
        series_b = {'id': 11, 'tvdbId': 100, 'qualityProfileId': 1}
        job_b = _job('episodes_b', source_b, target, 8)
        target_series['tags'].append(8)
        clients[source_b.identity] = source_b
        snapshots[source_b.identity] = [series_b]
        return target, source_a, job, clients, snapshots, [target_series], job_b

    def test_file_filter_monitors_only_matching_episode_and_deletes_stale_file(self):
        target, unused_source, job, clients, snapshots, target_items = self.make_fixture()
        plans = _sonarr_episode_plan([job], clients, target, target_items, snapshots, {})
        self.assertEqual(plans[0]['monitor_true'], {51})
        self.assertEqual(plans[0]['monitor_false'], {52})
        self.assertEqual(len(plans[0]['deletions']), 1)
        self.assertEqual(plans[0]['deletions'][0][0]['episodeFileId'], 500)

        _apply_sonarr_episode_plan(plans, job, target)
        self.assertIn(('MONITOR', [51], True), target.calls)
        self.assertIn(('MONITOR', [52], False), target.calls)
        self.assertIn(('DELETE_FILE', 500, True), target.calls)
        self.assertTrue(any(call[0] == 'POST' and call[1] == 'command' for call in target.calls))

    def test_overlapping_jobs_union_episode_monitor_sets(self):
        target, unused_source, job_a, clients, snapshots, target_items, job_b = self.make_fixture(
            second_source=True)
        source_b = clients[job_b['source']['identity']]
        # Job B selects episode 2, while job A selects episode 1.
        job_b['source_quality_match'] = '^Bluray'
        job_b['source_custom_format_names'] = ['Dolby Vision without fallback']
        target.episodes['20'][0]['monitored'] = False
        target.episodes['20'][1]['monitored'] = False
        plans = _sonarr_episode_plan([job_a, job_b], clients, target, target_items, snapshots, {})
        self.assertEqual(plans[0]['monitor_true'], {51, 52})
        self.assertEqual(plans[0]['monitor_false'], set())

    def test_shared_target_file_is_kept_if_any_episode_in_it_still_matches(self):
        target, unused_source, job, clients, snapshots, target_items = self.make_fixture()
        target.episodes['20'][0].update({
            'episodeFileId': 500,
            'hasFile': True,
            'episodeFile': {'id': 500},
        })
        plans = _sonarr_episode_plan([job], clients, target, target_items, snapshots, {})
        self.assertEqual(plans[0]['deletions'], [])

    def test_sonarr_test_run_does_not_change_monitoring_or_files(self):
        target, unused_source, job, clients, snapshots, target_items = self.make_fixture()
        plans = _sonarr_episode_plan([job], clients, target, target_items, snapshots, {})
        job['test_run'] = True
        _apply_sonarr_episode_plan(plans, job, target)
        self.assertEqual(target.calls, [])

    def test_sonarr_episode_delete_dry_run_has_same_details_as_live(self):
        records = []
        targets = []
        for test_run in (True, False):
            target, unused_source, job, clients, snapshots, target_items = self.make_fixture()
            target_items[0]['title'] = 'Example Series'
            job['test_run'] = test_run
            plans = _sonarr_episode_plan([job], clients, target, target_items, snapshots, {})
            with self.assertLogs('syncarr', level='INFO') as captured:
                _apply_sonarr_episode_plan(plans, job, target)
            event = next(item for item in _entity_events(captured)
                         if item['action'] == 'delete_episode_file')
            records.append(event)
            targets.append(target)

        dry_run, live = records
        for event in records:
            self.assertEqual(event['title'], 'Example Series')
            self.assertEqual(event['tvdb_id'], 100)
            self.assertEqual(event['episode_id'], 52)
            self.assertEqual(event['season_number'], 1)
            self.assertEqual(event['episode_number'], 2)
            self.assertEqual(event['episode_file_id'], 500)
            self.assertTrue(event['has_file'])
            self.assertFalse(event['source_has_file'])
            self.assertTrue(event['delete_files'])
        self.assertEqual(dry_run['mode'], 'dry_run')
        self.assertEqual(live['mode'], 'live')
        self.assertEqual(dict(dry_run, mode=None, outcome=None),
                         dict(live, mode=None, outcome=None))
        self.assertFalse(any(call[0] == 'DELETE_FILE' for call in targets[0].calls))
        self.assertIn(('DELETE_FILE', 500, True), targets[1].calls)

    def test_filtered_series_is_added_only_for_matching_episode_files(self):
        source = FakeSonarrClient(('sonarr', 'http://source'), {
            '10': [dict(_episode(1, 101, True, 'Bluray-2160p',
                           [{'name': 'Dolby Vision without fallback'}]), id=1),
                   dict(_episode(2, 102, True, 'WEB-2160p', [{'name': 'HDR10 fallback'}]), id=2)]
        })
        target = FakeSonarrAddTarget()
        job = _job('episodes', source, target, 7)
        job.update({
            'resolved_profile_id': 1,
            'target_root_path': '/target/shows',
            'root_mappings': [],
            'test_run': False,
        })
        content = {
            'id': 10, 'tvdbId': 100, 'title': 'Series', 'path': '/source/shows/Series',
            'seasons': [{'seasonNumber': 1, 'monitored': True}],
            'images': [],
        }
        _sync_items(job, source, target, 7, [content], [])
        self.assertEqual(len(target.calls), 1)
        payload = target.calls[0][2]
        self.assertFalse(payload['addOptions']['searchForMissingEpisodes'])
        self.assertFalse(payload['seasons'][0]['monitored'])
        self.assertTrue(payload['monitored'])

        source.episodes['10'] = [dict(_episode(1, 101, True, 'WEB-2160p', []), id=1)]
        empty_target = FakeSonarrAddTarget()
        _sync_items(job, source, empty_target, 7, [content], [])
        self.assertEqual(empty_target.calls, [])


if __name__ == '__main__':
    unittest.main()
