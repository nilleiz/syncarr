import unittest

from unittest.mock import patch

from multi_sync import (ArrClient, _apply_sonarr_episode_plan, _passes_file_filters,
                        _sonarr_episode_plan, _sync_items, rule_tag)


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
