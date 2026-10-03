#!/usr/bin/env python

"""Syncarr entry point. Multi-job mode is opt-in; legacy mode stays compatible."""

import logging
import sys
import threading

from multi_config import ConfigurationError, load_multi_job_config


def main():
    try:
        config = load_multi_job_config()
    except ConfigurationError as error:
        logging.error('Invalid multi-job configuration: %s', error)
        return 2

    if config is not None:
        from multi_sync import run, run_once
        if config.get('reinitialize_b', False):
            result = run_once(config)
            logging.warning(
                '\033[31mOne-time run finished with status %s; the container will stay idle to avoid '
                'restart-policy reruns. Stop the container when done.\033[0m', result)
            _wait_for_container_stop()
            return result
        run(config)
        return 0

    # Importing the legacy module starts its existing single-pair sync loop.
    import legacy_index  # noqa: F401
    return 0


def _wait_for_container_stop():
    """Keep Docker from restarting and repeating a completed one-time run."""
    threading.Event().wait()


if __name__ == '__main__':
    sys.exit(main())
