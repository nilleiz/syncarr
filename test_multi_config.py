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


if __name__ == '__main__':
    unittest.main()
