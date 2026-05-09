#!/usr/bin/env python
"""Django command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    """Run Django administrative commands.

    Raises:
        ImportError: If Django is not installed or unavailable on PYTHONPATH.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Install dependencies and activate the "
            "virtual environment before running management commands."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
