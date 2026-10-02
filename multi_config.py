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


def multi_job_mode_requested(environ=None):
    env = os.environ if environ is None else environ
    return bool(env.get('SYNCARR_CONFIG') or env.get('SYNCARR_JOB_COUNT') or env.get('SYNCARR_INSTANCE_COUNT'))


def load_multi_job_config(environ=None):
    """Return normalized config, or None when legacy mode should be used."""
    env = os.environ if environ is None else environ
    config_path = env.get('SYNCARR_CONFIG')
    has_indexed_config = bool(env.get('SYNCARR_JOB_COUNT') or env.get('SYNCARR_INSTANCE_COUNT'))

    if config_path and has_indexed_config:
        raise ConfigurationError('Set either SYNCARR_CONFIG or indexed SYNCARR_INSTANCE/JOB variables, not both')
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
    raw_jobs = data.get('jobs')
    if not isinstance(raw_instances, dict) or not raw_instances:
        raise ConfigurationError('The YAML configuration needs an instances mapping')
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ConfigurationError('The YAML configuration needs a non-empty jobs list')

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
    jobs = [_normalize_job(raw, index, instances, global_test_run)
            for index, raw in enumerate(raw_jobs, start=1)]
    return _finish_config(instances, jobs, global_test_run)


def _load_environment_config(env):
    instance_count = _integer(env.get('SYNCARR_INSTANCE_COUNT'), 'SYNCARR_INSTANCE_COUNT', minimum=1)
    job_count = _integer(env.get('SYNCARR_JOB_COUNT'), 'SYNCARR_JOB_COUNT', minimum=1)
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
    jobs = []
    for index in range(1, job_count + 1):
        prefix = 'SYNCARR_JOB_{}'.format(index)
        raw = {
            'id': _required(env, prefix + '_ID'),
            'source': _required(env, prefix + '_SOURCE'),
            'target': _required(env, prefix + '_TARGET'),
            'interval_seconds': env.get(prefix + '_INTERVAL_SECONDS', env.get('SYNC_INTERVAL_SECONDS', 300)),
            'source_profile': env.get(prefix + '_SOURCE_PROFILE'),
            'source_profile_id': env.get(prefix + '_SOURCE_PROFILE_ID'),
            'source_profile_filter': env.get(prefix + '_SOURCE_PROFILE_FILTER'),
            'source_profile_filter_id': env.get(prefix + '_SOURCE_PROFILE_FILTER_ID'),
            'source_quality_match': env.get(prefix + '_SOURCE_QUALITY_MATCH'),
            'source_tag_filter': env.get(prefix + '_SOURCE_TAG_FILTER'),
            'source_tag_filter_id': env.get(prefix + '_SOURCE_TAG_FILTER_ID'),
            'source_blacklist': env.get(prefix + '_SOURCE_BLACKLIST'),
            'target_profile': env.get(prefix + '_TARGET_PROFILE'),
            'target_profile_id': env.get(prefix + '_TARGET_PROFILE_ID'),
            'target_language': env.get(prefix + '_TARGET_LANGUAGE'),
            'target_language_id': env.get(prefix + '_TARGET_LANGUAGE_ID'),
            'target_root_path': env.get(prefix + '_TARGET_ROOT_PATH'),
            'auto_search': env.get(prefix + '_AUTO_SEARCH', '1'),
            'skip_missing': env.get(prefix + '_SKIP_MISSING', '1'),
            'monitor_new_content': env.get(prefix + '_MONITOR_NEW_CONTENT', '1'),
            'sync_monitor': env.get(prefix + '_SYNC_MONITOR', '0'),
            'test_run': env.get(prefix + '_TEST_RUN', global_test_run),
            'delete_missing': env.get(prefix + '_DELETE_MISSING', '0'),
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

    return _finish_config(instances, jobs, global_test_run)


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


def _normalize_job(raw, index, instances, global_test_run):
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
    if source['type'] != 'radarr' and _boolean(raw.get('delete_missing', False), 'delete_missing'):
        raise ConfigurationError('Deletion is currently supported for Radarr jobs only')

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
        'source_quality_match': _optional_text(raw.get('source_quality_match')),
        'source_tag_filter': _string_list(raw.get('source_tag_filter')),
        'source_tag_filter_id': _int_list(raw.get('source_tag_filter_id'), 'source_tag_filter_id'),
        'source_blacklist': _string_list(raw.get('source_blacklist')),
        'target_profile': _optional_text(raw.get('target_profile')),
        'target_profile_id': _optional_int(raw.get('target_profile_id'), 'target_profile_id'),
        'target_language': _optional_text(raw.get('target_language')),
        'target_language_id': _optional_int(raw.get('target_language_id'), 'target_language_id'),
        'target_root_path': _optional_text(raw.get('target_root_path')),
        'root_mappings': root_mappings,
        'auto_search': _boolean(raw.get('auto_search', True), 'auto_search'),
        'skip_missing': _boolean(raw.get('skip_missing', True), 'skip_missing'),
        'monitor_new_content': _boolean(raw.get('monitor_new_content', True), 'monitor_new_content'),
        'sync_monitor': _boolean(raw.get('sync_monitor', False), 'sync_monitor'),
        'test_run': _boolean(raw.get('test_run', global_test_run), 'test_run'),
        'delete_missing': _boolean(raw.get('delete_missing', False), 'delete_missing'),
        'delete_scope': scope,
        'delete_files': _boolean(raw.get('delete_files', False), 'delete_files'),
    }
    if job['target_profile'] is None and job['target_profile_id'] is None:
        raise ConfigurationError('Job {} needs target_profile or target_profile_id'.format(job_id))
    if job['source_quality_match']:
        try:
            re.compile(job['source_quality_match'])
        except re.error:
            raise ConfigurationError('Job {} has an invalid source_quality_match regular expression'.format(job_id))
    return job


def _finish_config(instances, jobs, test_run):
    ids = [job['id'] for job in jobs]
    if len(ids) != len(set(ids)):
        raise ConfigurationError('Job ids must be unique')
    if not jobs:
        raise ConfigurationError('At least one job is required')
    policies_by_target = {}
    for instance in instances.values():
        identity = (instance['type'], instance['url'])
        current = policies_by_target.get(identity)
        policy_and_key = (instance['delete_conflict_policy'], instance['api_key'])
        if current and current != policy_and_key:
            raise ConfigurationError('Aliases for the same instance must use one API key and delete_conflict_policy')
        policies_by_target[identity] = policy_and_key
    return {'instances': instances, 'jobs': jobs, 'test_run': test_run}


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
