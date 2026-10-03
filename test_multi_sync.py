import unittest

from unittest.mock import patch

from multi_sync import (ArrClient, SyncError, _apply_sonarr_episode_plan,
                        _items_for_deletion, _passes_file_filters, _passes_filters,
                        _sonarr_episode_plan, _sync_items, rule_tag, run_job)


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
    def __init__(self, identity, items=None, error=None):
        self.identity = identity
        self.arr_type = 'radarr'
        self.url = identity[1]
        self.items = list(items or [])
        self.error = error
        self.tag_ids = {}
        self.deleted_movies = []
        self.list_content_calls = 0

    def list_content(self):
        self.list_content_calls += 1
        if self.error:
            raise self.error
        return list(self.items)

    def profile_id(self, name, explicit_id, setting_name):
        return explicit_id

    def tag_id(self, label, create=False):
        return self.tag_ids.get(label)

    def request(self, method, route, params=None, payload=None, expected=None):
        return None

    def delete_movie(self, content_id, delete_files):
        self.deleted_movies.append((content_id, delete_files))


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
        'source_quality_match': '^Bluray',
        'source_custom_format_mode': 'any',
        'source_custom_format_names': ['Dolby Vision without fallback'],
        'source_custom_format_exclude_names': ['HDR10 fallback'],
        'source_custom_format_minimum_score': None,
        'has_file_filters': True,
        'delete_missing': False,
        'delete_scope': 'managed_only',
        'delete_files': False,
        'test_run': False,
        'auto_search': True,
        'monitor_new_content': True,
        'skip_missing': True,
    }
    job.update(changes)
    target.tag_ids[rule_tag(job)] = tag_id
    return job


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
        job = _job('delete', source, target, 7,
                   delete_missing=True, delete_scope='all_missing',
                   has_file_filters=False, source_quality_match=None, **changes)
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
