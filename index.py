#!/usr/bin/env python

"""Syncarr entry point. Multi-job mode is opt-in; legacy mode stays compatible."""

import logging
import sys

from multi_config import ConfigurationError, load_multi_job_config


def main():
    try:
        config = load_multi_job_config()
    except ConfigurationError as error:
        logging.error('Invalid multi-job configuration: %s', error)
        return 2

    if config is not None:
        from multi_sync import run
        run(config)
        return 0

    # Importing the legacy module starts its existing single-pair sync loop.
    import legacy_index  # noqa: F401
    return 0


if __name__ == '__main__':
    sys.exit(main())
