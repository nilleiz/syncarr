#!/usr/bin/env python
"""Load and validate Syncarr's multi-job configuration."""

import os
import re

import yaml


class ConfigurationError(ValueError):
    """Raised when a multi-job configuration is invalid."""


SUPPORTED_TYPES = ('radarr', 'sonarr', 'lidarr')
CONFLICT_POLICIES = ('keep_if_any_source', 'source_rule_wins')
DELETE_SCOPES = ('managed_only', 'all_missing')
CUSTOM_FORMAT_MODES = ('any', 'all', 'score')


def multi_job_mode_requested(environ=None):
    env = os.environ if environ is None else environ
    return bool(env.get('SYNCARR_CONFIG') or env.get('SYNCARR_JOB_COUNT') or
                env.get('SYNCARR_PAIR_COUNT') or env.get('SYNCARR_INSTANCE_COUNT'))


def load_multi_job_config(environ=None):
    """Return normalized config, or None when legacy mode should be used."""
    env = os.environ if environ is None else environ
    config_path = env.get('SYNCARR_CONFIG')
    has_indexed_config = bool(env.get('SYNCARR_JOB_COUNT') or env.get('SYNCARR_PAIR_COUNT') or
                              env.get('SYNCARR_INSTANCE_COUNT'))

    if config_path and has_indexed_config:
        raise ConfigurationError('Set either SYNCARR_CONFIG or indexed SYNCARR_INSTANCE/JOB variables, not both')
    if not config_path and not has_indexed_config:
        if _boolean(env.get('SYNCARR_REINITIALIZE_B', '0'), 'SYNCARR_REINITIALIZE_B'):
            raise ConfigurationError('SYNCARR_REINITIALIZE_B requires multi-job configuration')
        return None
    if config_path:
        return _load_yaml_config(config_path, env)
    if has_indexed_config:
        return _load_environment_config(env)
    return None


def _load_yaml_config(path, env):
    try:
        with open(path, 'r') as config_file:
            data = yaml.safe_load(config_file) or {}
    except IOError as error:
        raise ConfigurationError('Could not read SYNCARR_CONFIG file ({})'.format(error.__class__.__name__))
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        location = ' at line {}'.format(mark.line + 1) if mark is not None else ''
        raise ConfigurationError('Could not parse SYNCARR_CONFIG YAML{}'.format(location))
    if not isinstance(data, dict):
        raise ConfigurationError('The YAML configuration must be a mapping')

    raw_instances = data.get('instances')
    raw_jobs = data.get('jobs', [])
    raw_pairs = data.get('pairs', [])
    if not isinstance(raw_instances, dict) or not raw_instances:
        raise ConfigurationError('The YAML configuration needs an instances mapping')
    if not isinstance(raw_jobs, list) or not isinstance(raw_pairs, list):
        raise ConfigurationError('The YAML jobs and pairs settings must be lists')
    if not raw_jobs and not raw_pairs:
        raise ConfigurationError('The YAML configuration needs at least one job or pair')

    instances = {}
    for instance_id, raw in raw_instances.items():
        if not isinstance(raw, dict):
            raise ConfigurationError('Instance {} must be a mapping'.format(instance_id))
        url_env = raw.get('url_env')
        key_env = raw.get('api_key_env')
        if not url_env or not key_env:
            raise ConfigurationError('Instance {} must use url_env and api_key_env'.format(instance_id))
        url = env.get(str(url_env))
        api_key = env.get(str(key_env))
        if not url:
            raise ConfigurationError('Missing required environment variable {}'.format(url_env))
        if not api_key:
            raise ConfigurationError('Missing required environment variable {}'.format(key_env))
        instances[str(instance_id)] = _normalize_instance(
            str(instance_id), raw.get('type'), url, api_key, raw.get('delete_conflict_policy'))

    global_test_run = _boolean(data.get('test_run', env.get('SYNCARR_TEST_RUN', '0')), 'test_run')
    reinitialize_b = _boolean(
        data.get('reinitialize_b', env.get('SYNCARR_REINITIALIZE_B', '0')), 'reinitialize_b')
    jobs = [_normalize_job(raw, index, instances, global_test_run)
            for index, raw in enumerate(raw_jobs, start=1)]
    pairs = [_normalize_pair(raw, index, instances, global_test_run)
             for index, raw in enumerate(raw_pairs, start=1)]
    return _finish_config(instances, jobs, global_test_run, reinitialize_b, pairs)


def _load_environment_config(env):
    instance_count = _integer(env.get('SYNCARR_INSTANCE_COUNT'), 'SYNCARR_INSTANCE_COUNT', minimum=1)
    job_count = _integer(env.get('SYNCARR_JOB_COUNT', '0'), 'SYNCARR_JOB_COUNT', minimum=0)
    pair_count = _integer(env.get('SYNCARR_PAIR_COUNT', '0'), 'SYNCARR_PAIR_COUNT', minimum=0)
    if not job_count and not pair_count:
        raise ConfigurationError('Set SYNCARR_JOB_COUNT or SYNCARR_PAIR_COUNT to at least one')
    instances = {}
    for index in range(1, instance_count + 1):
        prefix = 'SYNCARR_INSTANCE_{}'.format(index)
        instance_id = _required(env, prefix + '_ID')
        if instance_id in instances:
            raise ConfigurationError('Duplicate instance id {}'.format(instance_id))
        instances[instance_id] = _normalize_instance(
            instance_id,
            _required(env, prefix + '_TYPE'),
            _required(env, prefix + '_URL'),
            _required(env, prefix + '_API_KEY'),
            env.get(prefix + '_DELETE_CONFLICT_POLICY'))

    global_test_run = _boolean(env.get('SYNCARR_TEST_RUN', '0'), 'SYNCARR_TEST_RUN')
    reinitialize_b = _boolean(env.get('SYNCARR_REINITIALIZE_B', '0'), 'SYNCARR_REINITIALIZE_B')
    jobs = []
    for index in range(1, job_count + 1):
        prefix = 'SYNCARR_JOB_{}'.format(index)
        if env.get(prefix + '_TARGET_LANGUAGE') or env.get(prefix + '_TARGET_LANGUAGE_ID'):
            raise ConfigurationError(
                '{} target language settings are legacy Sonarr v3 only'.format(prefix))
        raw = {
            'id': _required(env, prefix + '_ID'),
            'source': _required(env, prefix + '_SOURCE'),
            'target': _required(env, prefix + '_TARGET'),
            'interval_seconds': env.get(prefix + '_INTERVAL_SECONDS', env.get('SYNC_INTERVAL_SECONDS', 300)),
            'source_profile': env.get(prefix + '_SOURCE_PROFILE'),
            'source_profile_id': env.get(prefix + '_SOURCE_PROFILE_ID'),
            'source_profile_filter': env.get(prefix + '_SOURCE_PROFILE_FILTER'),
            'source_profile_filter_id': env.get(prefix + '_SOURCE_PROFILE_FILTER_ID'),
            'source_profile_filters': env.get(prefix + '_SOURCE_PROFILE_FILTERS'),
            'source_profile_filter_ids': env.get(prefix + '_SOURCE_PROFILE_FILTER_IDS'),
            'source_quality_match': env.get(prefix + '_SOURCE_QUALITY_MATCH'),
            'source_custom_format_mode': env.get(prefix + '_SOURCE_CUSTOM_FORMAT_MODE'),
            'source_custom_format_names': env.get(prefix + '_SOURCE_CUSTOM_FORMAT_NAMES'),
            'source_custom_format_exclude_names': env.get(prefix + '_SOURCE_CUSTOM_FORMAT_EXCLUDE_NAMES'),
            'source_custom_format_minimum_score': env.get(prefix + '_SOURCE_CUSTOM_FORMAT_MINIMUM_SCORE'),
            'source_tag_filter': env.get(prefix + '_SOURCE_TAG_FILTER'),
            'source_tag_filter_id': env.get(prefix + '_SOURCE_TAG_FILTER_ID'),
            'source_blacklist': env.get(prefix + '_SOURCE_BLACKLIST'),
            'target_profile': env.get(prefix + '_TARGET_PROFILE'),
            'target_profile_id': env.get(prefix + '_TARGET_PROFILE_ID'),
            'target_root_path': env.get(prefix + '_TARGET_ROOT_PATH'),
            'auto_search': env.get(prefix + '_AUTO_SEARCH', '1'),
            'skip_missing': env.get(prefix + '_SKIP_MISSING', '1'),
            'monitor_new_content': env.get(prefix + '_MONITOR_NEW_CONTENT', '1'),
            'sync_monitor': env.get(prefix + '_SYNC_MONITOR', '0'),
            'test_run': env.get(prefix + '_TEST_RUN', global_test_run),
            'delete_missing': env.get(prefix + '_DELETE_MISSING', '0'),
            'delete_if_filter_not_matching': env.get(prefix + '_DELETE_IF_FILTER_NOT_MATCHING', '0'),
            'delete_scope': env.get(prefix + '_DELETE_SCOPE', 'managed_only'),
            'delete_files': env.get(prefix + '_DELETE_FILES', '0'),
        }
        mapping_count_value = env.get(prefix + '_ROOT_MAPPING_COUNT', '0')
        mapping_count = _integer(mapping_count_value, prefix + '_ROOT_MAPPING_COUNT', minimum=0)
        mappings = []
        for mapping_index in range(1, mapping_count + 1):
            mapping_prefix = '{}_ROOT_MAPPING_{}'.format(prefix, mapping_index)
            mappings.append({
                'source': _required(env, mapping_prefix + '_SOURCE'),
                'target': _required(env, mapping_prefix + '_TARGET'),
            })
        raw['root_mappings'] = mappings
        jobs.append(_normalize_job(raw, index, instances, global_test_run))

    pairs = []
    for index in range(1, pair_count + 1):
        prefix = 'SYNCARR_PAIR_{}'.format(index)
        pair_id = _required(env, prefix + '_ID')
        source_id = _required(env, prefix + '_SOURCE')
        target_id = _required(env, prefix + '_TARGET')
        raw_pair = {
            'id': pair_id,
            'source': source_id,
            'target': target_id,
            'interval_seconds': env.get(prefix + '_INTERVAL_SECONDS', 300),
        }
        _read_indexed_settings(env, prefix, raw_pair)
        _read_indexed_mappings(env, prefix, raw_pair)
        profile_count = _integer(env.get(prefix + '_PROFILE_MAPPING_COUNT', '0'),
                                 prefix + '_PROFILE_MAPPING_COUNT', minimum=0)
        profile_mappings = []
        for mapping_index in range(1, profile_count + 1):
            mapping_prefix = '{}_PROFILE_MAPPING_{}'.format(prefix, mapping_index)
            profile_mappings.append({
                'source_profile': env.get(mapping_prefix + '_SOURCE_PROFILE'),
                'source_profile_id': env.get(mapping_prefix + '_SOURCE_PROFILE_ID'),
                'target_profile': env.get(mapping_prefix + '_TARGET_PROFILE'),
                'target_profile_id': env.get(mapping_prefix + '_TARGET_PROFILE_ID'),
            })
        raw_pair['profile_mappings'] = profile_mappings
        rule_count = _integer(env.get(prefix + '_RULE_COUNT', '0'),
                              prefix + '_RULE_COUNT', minimum=0)
        if rule_count < 1:
            raise ConfigurationError('{} needs at least one rule'.format(prefix))
        rules = []
        for rule_index in range(1, rule_count + 1):
            rule_prefix = '{}_RULE_{}'.format(prefix, rule_index)
            rule = {'id': _required(env, rule_prefix + '_ID')}
            _read_indexed_settings(env, rule_prefix, rule)
            _read_indexed_mappings(env, rule_prefix, rule)
            rules.append(rule)
        raw_pair['rules'] = rules
        pairs.append(_normalize_pair(raw_pair, index, instances, global_test_run))

    return _finish_config(instances, jobs, global_test_run, reinitialize_b, pairs)


INDEXED_SETTING_NAMES = (
    'source_profile_filter', 'source_profile_filter_id', 'source_profile_filters',
    'source_profile_filter_ids', 'source_quality_match',
    'source_custom_format_mode', 'source_custom_format_names',
    'source_custom_format_exclude_names', 'source_custom_format_minimum_score',
    'source_tag_filter', 'source_tag_filter_id', 'source_blacklist',
    'target_root_path', 'auto_search', 'skip_missing', 'monitor_new_content',
    'sync_monitor', 'test_run', 'delete_missing', 'delete_if_filter_not_matching',
    'delete_scope', 'delete_files',
)


def _read_indexed_settings(env, prefix, raw):
    for name in INDEXED_SETTING_NAMES:
        env_name = '{}_{}'.format(prefix, name.upper())
        if env_name not in env:
            continue
        value = env[env_name]
        if name in ('source_profile_filters', 'source_profile_filter_ids',
                    'source_custom_format_names', 'source_custom_format_exclude_names',
                    'source_tag_filter', 'source_tag_filter_id', 'source_blacklist'):
            value = _string_list(value)
        raw[name] = value


def _read_indexed_mappings(env, prefix, raw):
    count = _integer(env.get(prefix + '_ROOT_MAPPING_COUNT', '0'),
                     prefix + '_ROOT_MAPPING_COUNT', minimum=0)
    mappings = []
    for index in range(1, count + 1):
        mapping_prefix = '{}_ROOT_MAPPING_{}'.format(prefix, index)
        mappings.append({
            'source': _required(env, mapping_prefix + '_SOURCE'),
            'target': _required(env, mapping_prefix + '_TARGET'),
        })
    if count or prefix + '_ROOT_MAPPING_COUNT' in env:
        raw['root_mappings'] = mappings


def _normalize_instance(instance_id, arr_type, url, api_key, conflict_policy=None):
    arr_type = str(arr_type or '').strip().lower()
    if arr_type not in SUPPORTED_TYPES:
        raise ConfigurationError('Instance {} has unsupported type {}'.format(instance_id, arr_type or '<empty>'))
    policy = str(conflict_policy or 'keep_if_any_source').strip().lower()
    if policy not in CONFLICT_POLICIES:
        raise ConfigurationError('Instance {} has an invalid delete_conflict_policy'.format(instance_id))
    normalized_url = str(url).strip().rstrip('/')
    if not normalized_url:
        raise ConfigurationError('Instance {} has an empty URL'.format(instance_id))
    return {
        'id': instance_id,
        'type': arr_type,
        'url': normalized_url,
        'identity': (arr_type, normalized_url),
        'api_key': str(api_key),
        'delete_conflict_policy': policy,
    }


def _normalize_job(raw, index, instances, global_test_run, allow_profile_mapping=False):
    if not isinstance(raw, dict):
        raise ConfigurationError('Job {} must be a mapping'.format(index))
    job_id = str(raw.get('id') or '').strip().lower()
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]*$', job_id):
        raise ConfigurationError('Job {} needs a stable id using letters, digits, _ or -'.format(index))
    source_id = str(raw.get('source') or '').strip()
    target_id = str(raw.get('target') or '').strip()
    if source_id not in instances:
        raise ConfigurationError('Job {} references an unknown source instance'.format(job_id))
    if target_id not in instances:
        raise ConfigurationError('Job {} references an unknown target instance'.format(job_id))
    source = instances[source_id]
    target = instances[target_id]
    if source['identity'] == target['identity']:
        raise ConfigurationError('Job {} source and target must be different instances'.format(job_id))
    if source['type'] != target['type']:
        raise ConfigurationError('Job {} source and target must use the same *arr type'.format(job_id))

    interval = _integer(raw.get('interval_seconds', 300), 'job {} interval_seconds'.format(job_id), minimum=1)
    mappings = raw.get('root_mappings') or []
    if not isinstance(mappings, list):
        raise ConfigurationError('Job {} root_mappings must be a list'.format(job_id))
    root_mappings = []
    for mapping in mappings:
        if not isinstance(mapping, dict) or not mapping.get('source') or not mapping.get('target'):
            raise ConfigurationError('Job {} root mappings need source and target paths'.format(job_id))
        root_mappings.append({'source': str(mapping['source']), 'target': str(mapping['target'])})

    scope = str(raw.get('delete_scope') or 'managed_only').strip().lower()
    if scope not in DELETE_SCOPES:
        raise ConfigurationError('Job {} has an invalid delete_scope'.format(job_id))
    if source['type'] == 'lidarr' and _boolean(raw.get('delete_missing', False), 'delete_missing'):
        raise ConfigurationError('Deletion is currently supported for Radarr and Sonarr jobs only')

    job = {
        'id': job_id,
        'source': source,
        'target': target,
        'source_instance_id': source_id,
        'target_instance_id': target_id,
        'interval_seconds': interval,
        'source_profile': _optional_text(raw.get('source_profile')),
        'source_profile_id': _optional_int(raw.get('source_profile_id'), 'source_profile_id'),
        'source_profile_filter': _optional_text(raw.get('source_profile_filter')),
        'source_profile_filter_id': _optional_int(raw.get('source_profile_filter_id'), 'source_profile_filter_id'),
        'source_profile_filters': _string_list(raw.get('source_profile_filters')),
        'source_profile_filter_ids': _int_list(
            raw.get('source_profile_filter_ids'), 'source_profile_filter_ids'),
        'source_quality_match': _optional_text(raw.get('source_quality_match')),
        'source_custom_format_mode': _optional_lower_text(raw.get('source_custom_format_mode')),
        'source_custom_format_names': _string_list(raw.get('source_custom_format_names')),
        'source_custom_format_exclude_names': _string_list(raw.get('source_custom_format_exclude_names')),
        'source_custom_format_minimum_score': _optional_integer(
            raw.get('source_custom_format_minimum_score'),
            'source_custom_format_minimum_score', minimum=-2147483648),
        'source_tag_filter': _string_list(raw.get('source_tag_filter')),
        'source_tag_filter_id': _int_list(raw.get('source_tag_filter_id'), 'source_tag_filter_id'),
        'source_blacklist': _string_list(raw.get('source_blacklist')),
        'target_profile': _optional_text(raw.get('target_profile')),
        'target_profile_id': _optional_int(raw.get('target_profile_id'), 'target_profile_id'),
        'target_root_path': _optional_text(raw.get('target_root_path')),
        'root_mappings': root_mappings,
        'auto_search': _boolean(raw.get('auto_search', True), 'auto_search'),
        'skip_missing': _boolean(raw.get('skip_missing', True), 'skip_missing'),
        'monitor_new_content': _boolean(raw.get('monitor_new_content', True), 'monitor_new_content'),
        'sync_monitor': _boolean(raw.get('sync_monitor', False), 'sync_monitor'),
        'test_run': _boolean(raw.get('test_run', global_test_run), 'test_run'),
        'delete_missing': _boolean(raw.get('delete_missing', False), 'delete_missing'),
        'delete_if_filter_not_matching': _boolean(
            raw.get('delete_if_filter_not_matching', False), 'delete_if_filter_not_matching'),
        'delete_scope': scope,
        'delete_files': _boolean(raw.get('delete_files', False), 'delete_files'),
    }
    if ((job['source_profile_filter'] or job['source_profile_filter_id'] is not None) and
            (job['source_profile_filters'] or job['source_profile_filter_ids'])):
        raise ConfigurationError(
            'Job {} cannot combine singular and multiple source profile filters'.format(job_id))
    if ((job['source_profile_filters'] or job['source_profile_filter_ids']) and
            source['type'] not in ('radarr', 'sonarr')):
        raise ConfigurationError(
            'Job {} multiple source profile filters require Radarr or Sonarr'.format(job_id))
    if (not allow_profile_mapping and job['target_profile'] is None and
            job['target_profile_id'] is None):
        raise ConfigurationError('Job {} needs target_profile or target_profile_id'.format(job_id))
    if job['delete_if_filter_not_matching'] and source['type'] != 'radarr':
        raise ConfigurationError(
            'Job {} delete_if_filter_not_matching is supported for Radarr only'.format(job_id))
    if _optional_text(raw.get('target_language')) or _optional_text(raw.get('target_language_id')):
        raise ConfigurationError(
            'Job {} target language settings are legacy Sonarr v3 only'.format(job_id))
    if job['source_quality_match'] and source['type'] not in ('radarr', 'sonarr'):
        raise ConfigurationError('Job {} file quality filters require Radarr or Sonarr'.format(job_id))
    mode = job['source_custom_format_mode']
    if mode is None:
        if (job['source_custom_format_names'] or job['source_custom_format_exclude_names'] or
                job['source_custom_format_minimum_score'] is not None):
            raise ConfigurationError(
                'Job {} custom format settings require source_custom_format_mode'.format(job_id))
    else:
        if mode not in CUSTOM_FORMAT_MODES:
            raise ConfigurationError('Job {} has an invalid source_custom_format_mode'.format(job_id))
        if source['type'] not in ('radarr', 'sonarr'):
            raise ConfigurationError('Job {} custom format filters require Radarr or Sonarr'.format(job_id))
        if mode in ('any', 'all'):
            if not job['source_custom_format_names']:
                raise ConfigurationError(
                    'Job {} any/all custom format modes need source_custom_format_names'.format(job_id))
            if job['source_custom_format_minimum_score'] is not None:
                raise ConfigurationError(
                    'Job {} score thresholds can only be used with mode score'.format(job_id))
        elif job['source_custom_format_minimum_score'] is None:
            raise ConfigurationError(
                'Job {} score mode needs source_custom_format_minimum_score'.format(job_id))
        elif job['source_custom_format_names'] or job['source_custom_format_exclude_names']:
            raise ConfigurationError(
                'Job {} names and exclusions can only be used with mode any or all'.format(job_id))
    job['has_file_filters'] = bool(job['source_quality_match'] or mode)
    if job['source_quality_match']:
        try:
            re.compile(job['source_quality_match'])
        except re.error:
            raise ConfigurationError('Job {} has an invalid source_quality_match regular expression'.format(job_id))
    return job


PAIR_SETTING_FIELDS = (
    'source_profile_filter', 'source_profile_filter_id', 'source_profile_filters',
    'source_profile_filter_ids', 'source_quality_match',
    'source_custom_format_mode', 'source_custom_format_names',
    'source_custom_format_exclude_names', 'source_custom_format_minimum_score',
    'source_tag_filter', 'source_tag_filter_id', 'source_blacklist',
    'target_root_path', 'root_mappings', 'auto_search', 'skip_missing',
    'monitor_new_content', 'sync_monitor', 'test_run', 'delete_missing',
    'delete_if_filter_not_matching', 'delete_scope', 'delete_files',
)


def _normalize_profile_mappings(raw_mappings, pair_id):
    if not isinstance(raw_mappings, list) or not raw_mappings:
        raise ConfigurationError('Pair {} needs a non-empty profile_mappings list'.format(pair_id))
    mappings = []
    source_selectors = set()
    for index, mapping in enumerate(raw_mappings, start=1):
        if not isinstance(mapping, dict):
            raise ConfigurationError('Pair {} profile mapping {} must be a mapping'.format(pair_id, index))
        source_name = _optional_text(mapping.get('source_profile'))
        source_id = _optional_int(mapping.get('source_profile_id'), 'source_profile_id')
        target_name = _optional_text(mapping.get('target_profile'))
        target_id = _optional_int(mapping.get('target_profile_id'), 'target_profile_id')
        if (source_name is None) == (source_id is None):
            raise ConfigurationError(
                'Pair {} profile mapping {} needs exactly one source profile name or ID'.format(pair_id, index))
        if (target_name is None) == (target_id is None):
            raise ConfigurationError(
                'Pair {} profile mapping {} needs exactly one target profile name or ID'.format(pair_id, index))
        selector = ('id', source_id) if source_id is not None else ('name', source_name.casefold())
        if selector in source_selectors:
            raise ConfigurationError('Pair {} maps one source profile more than once'.format(pair_id))
        source_selectors.add(selector)
        mappings.append({
            'source_profile': source_name,
            'source_profile_id': source_id,
            'target_profile': target_name,
            'target_profile_id': target_id,
        })
    return mappings


def _normalize_pair(raw, index, instances, global_test_run):
    if not isinstance(raw, dict):
        raise ConfigurationError('Pair {} must be a mapping'.format(index))
    pair_id = str(raw.get('id') or '').strip().lower()
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]*$', pair_id):
        raise ConfigurationError('Pair {} needs a stable id using letters, digits, _ or -'.format(index))
    source_id = str(raw.get('source') or '').strip()
    target_id = str(raw.get('target') or '').strip()
    if source_id not in instances or target_id not in instances:
        raise ConfigurationError('Pair {} references an unknown source or target instance'.format(pair_id))
    source = instances[source_id]
    target = instances[target_id]
    if source['identity'] == target['identity']:
        raise ConfigurationError('Pair {} source and target must be different instances'.format(pair_id))
    if source['type'] != target['type']:
        raise ConfigurationError('Pair {} source and target must use the same *arr type'.format(pair_id))
    if source['type'] not in ('radarr', 'sonarr'):
        raise ConfigurationError('Pair {} supports Radarr and Sonarr only'.format(pair_id))

    profile_mappings = _normalize_profile_mappings(raw.get('profile_mappings'), pair_id)
    interval = _integer(raw.get('interval_seconds', 300),
                        'pair {} interval_seconds'.format(pair_id), minimum=1)
    raw_rules = raw.get('rules')
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigurationError('Pair {} needs a non-empty rules list'.format(pair_id))

    common = {name: raw[name] for name in PAIR_SETTING_FIELDS if name in raw}
    jobs = []
    rule_ids = set()
    for rule_index, rule in enumerate(raw_rules, start=1):
        if not isinstance(rule, dict):
            raise ConfigurationError('Pair {} rule {} must be a mapping'.format(pair_id, rule_index))
        rule_id = str(rule.get('id') or '').strip().lower()
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]*$', rule_id):
            raise ConfigurationError(
                'Pair {} rule {} needs a stable id using letters, digits, _ or -'.format(pair_id, rule_index))
        if rule_id in rule_ids:
            raise ConfigurationError('Pair {} rule ids must be unique'.format(pair_id))
        rule_ids.add(rule_id)
        forbidden = {'source', 'target', 'interval_seconds', 'profile_mappings',
                     'target_profile', 'target_profile_id'} & set(rule)
        if forbidden:
            raise ConfigurationError(
                'Pair {} rule {} cannot override pair fields: {}'.format(
                    pair_id, rule_id, ', '.join(sorted(forbidden))))
        effective = dict(common)
        singular_profile_filter_fields = ('source_profile_filter', 'source_profile_filter_id')
        plural_profile_filter_fields = ('source_profile_filters', 'source_profile_filter_ids')
        if set(singular_profile_filter_fields) & set(rule):
            for field in plural_profile_filter_fields:
                effective.pop(field, None)
        if set(plural_profile_filter_fields) & set(rule):
            for field in singular_profile_filter_fields:
                effective.pop(field, None)
        effective.update({key: value for key, value in rule.items() if key != 'id'})
        internal_id = 'pair{}_{}_rule{}_{}'.format(
            len(pair_id), pair_id, len(rule_id), rule_id)
        effective.update({
            'id': internal_id,
            'source': source_id,
            'target': target_id,
            'interval_seconds': interval,
        })
        job = _normalize_job(effective, rule_index, instances, global_test_run,
                             allow_profile_mapping=True)
        job.update({
            'pair_id': pair_id,
            'rule_id': rule_id,
            'profile_mappings': profile_mappings,
            'is_pair_rule': True,
        })
        jobs.append(job)
    return {
        'id': pair_id,
        'source_instance_id': source_id,
        'target_instance_id': target_id,
        'source': source,
        'target': target,
        'interval_seconds': interval,
        'profile_mappings': profile_mappings,
        'jobs': jobs,
    }


def _finish_config(instances, jobs, test_run, reinitialize_b=False, pairs=None):
    pairs = [] if pairs is None else pairs
    ids = [job['id'] for job in jobs]
    if len(ids) != len(set(ids)):
        raise ConfigurationError('Job ids must be unique')
    if not jobs and not pairs:
        raise ConfigurationError('At least one job or pair is required')
    pair_ids = [pair['id'] for pair in pairs]
    if len(pair_ids) != len(set(pair_ids)):
        raise ConfigurationError('Pair ids must be unique')
    all_jobs = list(jobs)
    units = []
    for job in jobs:
        units.append({'kind': 'job', 'id': job['id'], 'key': 'job:{}'.format(job['id']),
                      'interval_seconds': job['interval_seconds'], 'jobs': [job]})
    for pair in pairs:
        all_jobs.extend(pair['jobs'])
        units.append({'kind': 'pair', 'id': pair['id'], 'key': 'pair:{}'.format(pair['id']),
                      'interval_seconds': pair['interval_seconds'], 'jobs': pair['jobs']})
    ids = [job['id'] for job in all_jobs]
    if len(ids) != len(set(ids)):
        raise ConfigurationError('Job and pair rule identifiers must be unique')
    policies_by_target = {}
    for instance in instances.values():
        identity = (instance['type'], instance['url'])
        current = policies_by_target.get(identity)
        policy_and_key = (instance['delete_conflict_policy'], instance['api_key'])
        if current and current != policy_and_key:
            raise ConfigurationError('Aliases for the same instance must use one API key and delete_conflict_policy')
        policies_by_target[identity] = policy_and_key
    return {
        'instances': instances,
        'jobs': jobs,
        'all_jobs': all_jobs,
        'pairs': pairs,
        'units': units,
        'test_run': test_run,
        'reinitialize_b': reinitialize_b,
    }


def _required(env, name):
    value = env.get(name)
    if value is None or str(value).strip() == '':
        raise ConfigurationError('Missing required setting {}'.format(name))
    return str(value).strip()


def _integer(value, name, minimum):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ConfigurationError('{} must be an integer'.format(name))
    if result < minimum:
        raise ConfigurationError('{} must be at least {}'.format(name, minimum))
    return result


def _optional_int(value, name):
    if value is None or str(value).strip() == '':
        return None
    return _integer(value, name, minimum=1)


def _optional_integer(value, name, minimum):
    if value is None or str(value).strip() == '':
        return None
    return _integer(value, name, minimum=minimum)


def _int_list(value, name):
    values = _string_list(value)
    return [_integer(item, name, minimum=1) for item in values]


def _string_list(value):
    if value is None or value == '':
        return []
    if isinstance(value, str):
        values = value.split(',')
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ConfigurationError('List settings must be a comma-separated string or YAML list')
    return [str(item).strip() for item in values if str(item).strip()]


def _optional_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


def _optional_lower_text(value):
    value = _optional_text(value)
    return value.lower() if value else None


def _boolean(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in ('1', 'true', 'yes', 'on'):
        return True
    if normalized in ('0', 'false', 'no', 'off', ''):
        return False
    raise ConfigurationError('{} must be a boolean'.format(name))
