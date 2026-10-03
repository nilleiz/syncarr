import os
import tempfile
import unittest

from multi_config import ConfigurationError, _normalize_instance, _normalize_job, load_multi_job_config


class MultiJobConfigTests(unittest.TestCase):
    def setUp(self):
        self.instances = {
            'source': _normalize_instance('source', 'sonarr', 'http://source', 'key'),
            'target': _normalize_instance('target', 'sonarr', 'http://target', 'key'),
        }
        self.base_job = {
            'id': 'episodes',
            'source': 'source',
            'target': 'target',
            'target_profile': 'HD',
        }

    def normalize(self, values=None):
        raw = dict(self.base_job)
        raw.update(values or {})
        return _normalize_job(raw, 1, self.instances, False)

    def test_custom_format_any_mode_normalizes_names(self):
        job = self.normalize({
            'source_custom_format_mode': 'ANY',
            'source_custom_format_names': ['Dolby Vision without fallback'],
            'source_custom_format_exclude_names': ['HDR10 fallback'],
        })
        self.assertEqual(job['source_custom_format_mode'], 'any')
        self.assertEqual(job['source_custom_format_names'], ['Dolby Vision without fallback'])
        self.assertTrue(job['has_file_filters'])

    def test_custom_format_score_accepts_negative_threshold(self):
        job = self.normalize({
            'source_custom_format_mode': 'score',
            'source_custom_format_minimum_score': -10,
        })
        self.assertEqual(job['source_custom_format_minimum_score'], -10)

    def test_custom_format_any_requires_names(self):
        with self.assertRaises(ConfigurationError):
            self.normalize({'source_custom_format_mode': 'any'})

    def test_custom_format_score_rejects_names(self):
        with self.assertRaises(ConfigurationError):
            self.normalize({
                'source_custom_format_mode': 'score',
                'source_custom_format_minimum_score': 10,
                'source_custom_format_names': ['HDR'],
            })

    def test_sonarr_delete_is_supported_and_opt_in(self):
        job = self.normalize({'delete_missing': True, 'source_quality_match': '^Bluray'})
        self.assertTrue(job['delete_missing'])
        self.assertFalse(job['delete_files'])
        self.assertFalse(job['delete_if_filter_not_matching'])

    def test_filter_mismatch_deletion_is_radarr_only_and_per_job(self):
        radarr_instances = {
            'source': _normalize_instance('source', 'radarr', 'http://source', 'key'),
            'target': _normalize_instance('target', 'radarr', 'http://target', 'key'),
        }
        raw = dict(self.base_job, delete_if_filter_not_matching=True)
        job = _normalize_job(raw, 1, radarr_instances, False)
        self.assertTrue(job['delete_if_filter_not_matching'])
        self.assertFalse(job['delete_missing'])

        with self.assertRaisesRegex(ConfigurationError, 'Radarr only'):
            self.normalize({'delete_if_filter_not_matching': True})

    def test_multi_job_language_profiles_are_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, 'legacy Sonarr v3 only'):
            self.normalize({'target_language': 'English'})

    def test_indexed_custom_format_environment_settings(self):
        env = {
            'SYNCARR_INSTANCE_COUNT': '2',
            'SYNCARR_JOB_COUNT': '1',
            'SYNCARR_INSTANCE_1_ID': 'source',
            'SYNCARR_INSTANCE_1_TYPE': 'sonarr',
            'SYNCARR_INSTANCE_1_URL': 'http://source',
            'SYNCARR_INSTANCE_1_API_KEY': 'source-key',
            'SYNCARR_INSTANCE_2_ID': 'target',
            'SYNCARR_INSTANCE_2_TYPE': 'sonarr',
            'SYNCARR_INSTANCE_2_URL': 'http://target',
            'SYNCARR_INSTANCE_2_API_KEY': 'target-key',
            'SYNCARR_JOB_1_ID': 'episodes',
            'SYNCARR_JOB_1_SOURCE': 'source',
            'SYNCARR_JOB_1_TARGET': 'target',
            'SYNCARR_JOB_1_TARGET_PROFILE': 'HD',
            'SYNCARR_JOB_1_SOURCE_CUSTOM_FORMAT_MODE': 'all',
            'SYNCARR_JOB_1_SOURCE_CUSTOM_FORMAT_NAMES': 'DV, HDR',
            'SYNCARR_JOB_1_SOURCE_CUSTOM_FORMAT_EXCLUDE_NAMES': 'SDR fallback',
        }
        config = load_multi_job_config(env)
        job = config['jobs'][0]
        self.assertEqual(job['source_custom_format_names'], ['DV', 'HDR'])
        self.assertEqual(job['source_custom_format_exclude_names'], ['SDR fallback'])
        self.assertFalse(job['delete_if_filter_not_matching'])
        self.assertFalse(config['reinitialize_b'])

    def test_indexed_radarr_filter_mismatch_deletion_environment_setting(self):
        env = {
            'SYNCARR_INSTANCE_COUNT': '2',
            'SYNCARR_JOB_COUNT': '1',
            'SYNCARR_INSTANCE_1_ID': 'source',
            'SYNCARR_INSTANCE_1_TYPE': 'radarr',
            'SYNCARR_INSTANCE_1_URL': 'http://source',
            'SYNCARR_INSTANCE_1_API_KEY': 'source-key',
            'SYNCARR_INSTANCE_2_ID': 'target',
            'SYNCARR_INSTANCE_2_TYPE': 'radarr',
            'SYNCARR_INSTANCE_2_URL': 'http://target',
            'SYNCARR_INSTANCE_2_API_KEY': 'target-key',
            'SYNCARR_JOB_1_ID': 'movies',
            'SYNCARR_JOB_1_SOURCE': 'source',
            'SYNCARR_JOB_1_TARGET': 'target',
            'SYNCARR_JOB_1_TARGET_PROFILE': 'HD',
            'SYNCARR_JOB_1_DELETE_MISSING': '1',
            'SYNCARR_JOB_1_DELETE_IF_FILTER_NOT_MATCHING': '1',
        }
        job = load_multi_job_config(env)['jobs'][0]
        self.assertTrue(job['delete_missing'])
        self.assertTrue(job['delete_if_filter_not_matching'])

    def test_reinitialize_b_can_be_enabled_in_indexed_environment(self):
        env = {
            'SYNCARR_INSTANCE_COUNT': '2',
            'SYNCARR_JOB_COUNT': '1',
            'SYNCARR_INSTANCE_1_ID': 'source',
            'SYNCARR_INSTANCE_1_TYPE': 'radarr',
            'SYNCARR_INSTANCE_1_URL': 'http://source',
            'SYNCARR_INSTANCE_1_API_KEY': 'source-key',
            'SYNCARR_INSTANCE_2_ID': 'target',
            'SYNCARR_INSTANCE_2_TYPE': 'radarr',
            'SYNCARR_INSTANCE_2_URL': 'http://target',
            'SYNCARR_INSTANCE_2_API_KEY': 'target-key',
            'SYNCARR_JOB_1_ID': 'movies',
            'SYNCARR_JOB_1_SOURCE': 'source',
            'SYNCARR_JOB_1_TARGET': 'target',
            'SYNCARR_JOB_1_TARGET_PROFILE_ID': '1',
            'SYNCARR_REINITIALIZE_B': 'true',
        }
        config = load_multi_job_config(env)
        self.assertTrue(config['reinitialize_b'])

    def test_reinitialize_b_requires_multi_job_configuration(self):
        with self.assertRaisesRegex(ConfigurationError, 'requires multi-job configuration'):
            load_multi_job_config({'SYNCARR_REINITIALIZE_B': 'true'})

    def test_yaml_pair_inherits_defaults_and_allows_rule_overrides(self):
        yaml_config = """instances:
  source:
    type: radarr
    url_env: SOURCE_URL
    api_key_env: SOURCE_KEY
  target:
    type: radarr
    url_env: TARGET_URL
    api_key_env: TARGET_KEY
pairs:
  - id: movies
    source: source
    target: target
    interval_seconds: 47
    delete_missing: true
    profile_mappings:
      - source_profile_id: 1
        target_profile_id: 2
    rules:
      - id: quality
        source_quality_match: '^Bluray'
      - id: formats
        delete_missing: false
        auto_search: false
"""
        config = self.load_yaml(yaml_config, {
            'SOURCE_URL': 'http://source', 'SOURCE_KEY': 'source-key',
            'TARGET_URL': 'http://target', 'TARGET_KEY': 'target-key',
        })
        self.assertEqual(config['jobs'], [])
        self.assertEqual(len(config['pairs']), 1)
        self.assertEqual(len(config['all_jobs']), 2)
        self.assertEqual(config['units'][0]['key'], 'pair:movies')
        self.assertEqual(config['units'][0]['interval_seconds'], 47)
        quality, formats = config['pairs'][0]['jobs']
        self.assertEqual(quality['interval_seconds'], formats['interval_seconds'])
        self.assertTrue(quality['delete_missing'])
        self.assertFalse(formats['delete_missing'])
        self.assertFalse(formats['auto_search'])
        self.assertEqual(quality['profile_mappings'][0]['target_profile_id'], 2)

    def test_indexed_pair_configuration_matches_yaml_shape(self):
        env = {
            'SYNCARR_INSTANCE_COUNT': '2', 'SYNCARR_JOB_COUNT': '0',
            'SYNCARR_PAIR_COUNT': '1',
            'SYNCARR_INSTANCE_1_ID': 'source', 'SYNCARR_INSTANCE_1_TYPE': 'sonarr',
            'SYNCARR_INSTANCE_1_URL': 'http://source', 'SYNCARR_INSTANCE_1_API_KEY': 'source-key',
            'SYNCARR_INSTANCE_2_ID': 'target', 'SYNCARR_INSTANCE_2_TYPE': 'sonarr',
            'SYNCARR_INSTANCE_2_URL': 'http://target', 'SYNCARR_INSTANCE_2_API_KEY': 'target-key',
            'SYNCARR_PAIR_1_ID': 'shows', 'SYNCARR_PAIR_1_SOURCE': 'source',
            'SYNCARR_PAIR_1_TARGET': 'target', 'SYNCARR_PAIR_1_INTERVAL_SECONDS': '90',
            'SYNCARR_PAIR_1_DELETE_MISSING': 'true',
            'SYNCARR_PAIR_1_PROFILE_MAPPING_COUNT': '1',
            'SYNCARR_PAIR_1_PROFILE_MAPPING_1_SOURCE_PROFILE_ID': '1',
            'SYNCARR_PAIR_1_PROFILE_MAPPING_1_TARGET_PROFILE_ID': '2',
            'SYNCARR_PAIR_1_RULE_COUNT': '2',
            'SYNCARR_PAIR_1_RULE_1_ID': 'filtered',
            'SYNCARR_PAIR_1_RULE_1_SOURCE_QUALITY_MATCH': '^Bluray',
            'SYNCARR_PAIR_1_RULE_2_ID': 'remaining',
            'SYNCARR_PAIR_1_RULE_2_DELETE_MISSING': 'false',
        }
        config = load_multi_job_config(env)
        first, second = config['pairs'][0]['jobs']
        self.assertEqual(config['units'][0]['interval_seconds'], 90)
        self.assertEqual(first['source_quality_match'], '^Bluray')
        self.assertEqual(first['profile_mappings'][0]['source_profile_id'], 1)
        self.assertFalse(second['delete_missing'])
        self.assertTrue(first['delete_missing'])

    def test_pair_supports_many_rules_and_profile_mappings_without_fixed_cap(self):
        mappings = '\n'.join(
            '      - source_profile_id: {}\n        target_profile_id: {}'.format(i, i + 100)
            for i in range(1, 33))
        rules = '\n'.join(
            '      - id: rule_{}\n        source_quality_match: ".*"'.format(i)
            for i in range(1, 33))
        yaml_config = """instances:
  source:
    type: sonarr
    url_env: SOURCE_URL
    api_key_env: SOURCE_KEY
  target:
    type: sonarr
    url_env: TARGET_URL
    api_key_env: TARGET_KEY
pairs:
  - id: large
    source: source
    target: target
    profile_mappings:
{}
    rules:
{}
""".format(mappings, rules)
        config = self.load_yaml(yaml_config, {
            'SOURCE_URL': 'http://source', 'SOURCE_KEY': 'source-key',
            'TARGET_URL': 'http://target', 'TARGET_KEY': 'target-key',
        })
        self.assertEqual(len(config['pairs'][0]['profile_mappings']), 32)
        self.assertEqual(len(config['pairs'][0]['jobs']), 32)

    def test_legacy_job_stays_an_independent_unit_when_pairs_are_configured(self):
        yaml_config = """instances:
  source:
    type: sonarr
    url_env: SOURCE_URL
    api_key_env: SOURCE_KEY
  target:
    type: sonarr
    url_env: TARGET_URL
    api_key_env: TARGET_KEY
jobs:
  - id: legacy
    source: source
    target: target
    target_profile_id: 3
    interval_seconds: 17
pairs:
  - id: new
    source: source
    target: target
    profile_mappings:
      - source_profile_id: 1
        target_profile_id: 2
    rules:
      - id: rule
"""
        config = self.load_yaml(yaml_config, {
            'SOURCE_URL': 'http://source', 'SOURCE_KEY': 'source-key',
            'TARGET_URL': 'http://target', 'TARGET_KEY': 'target-key',
        })
        self.assertEqual(config['jobs'][0]['id'], 'legacy')
        self.assertEqual(config['units'][0]['key'], 'job:legacy')
        self.assertEqual(config['units'][0]['interval_seconds'], 17)
        self.assertEqual(config['units'][1]['key'], 'pair:new')

    def test_pair_rejects_rule_override_of_profile_mapping(self):
        yaml_config = """instances:
  source:
    type: sonarr
    url_env: SOURCE_URL
    api_key_env: SOURCE_KEY
  target:
    type: sonarr
    url_env: TARGET_URL
    api_key_env: TARGET_KEY
pairs:
  - id: shows
    source: source
    target: target
    profile_mappings:
      - source_profile_id: 1
        target_profile_id: 2
    rules:
      - id: rule
        target_profile_id: 3
"""
        with self.assertRaisesRegex(ConfigurationError, 'cannot override pair fields'):
            self.load_yaml(yaml_config, {
                'SOURCE_URL': 'http://source', 'SOURCE_KEY': 'source-key',
                'TARGET_URL': 'http://target', 'TARGET_KEY': 'target-key',
            })

    def load_yaml(self, contents, env):
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as config_file:
            config_file.write(contents)
            path = config_file.name
        try:
            config_env = dict(env, SYNCARR_CONFIG=path)
            return load_multi_job_config(config_env)
        finally:
            os.unlink(path)

    def test_reinitialize_b_defaults_off_and_yaml_can_enable_it(self):
        yaml_config = """instances:
  source:
    type: radarr
    url_env: SOURCE_URL
    api_key_env: SOURCE_KEY
  target:
    type: radarr
    url_env: TARGET_URL
    api_key_env: TARGET_KEY
jobs:
  - id: movies
    source: source
    target: target
    target_profile_id: 1
"""
        env = {
            'SOURCE_URL': 'http://source',
            'SOURCE_KEY': 'source-key',
            'TARGET_URL': 'http://target',
            'TARGET_KEY': 'target-key',
        }
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as config_file:
            config_file.write(yaml_config)
            config_path = config_file.name
        try:
            env['SYNCARR_CONFIG'] = config_path
            self.assertFalse(load_multi_job_config(env)['reinitialize_b'])

            with open(config_path, 'w') as config_file:
                config_file.write('reinitialize_b: true\n' + yaml_config)
            self.assertTrue(load_multi_job_config(env)['reinitialize_b'])

            with open(config_path, 'w') as config_file:
                config_file.write(yaml_config)
            env['SYNCARR_REINITIALIZE_B'] = '1'
            self.assertTrue(load_multi_job_config(env)['reinitialize_b'])
        finally:
            os.unlink(config_path)


if __name__ == '__main__':
    unittest.main()
