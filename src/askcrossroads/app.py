"""FastAPI routes and the console entry point.

Nothing is served yet. ``main`` checks that the application is configured and reports
clearly when it is not; the HTTP server is added as the application is built up.
"""

import sys

from askcrossroads.config import ConfigError, load_config


def main():
    """Console entry point. Returns the process exit code.

    The console script wrapper calls ``sys.exit(main())``, so the value returned here
    becomes the process exit status. A configuration problem exits non-zero on the
    terminal rather than opening a browser onto an error page (spec §1).
    """
    try:
        config = load_config()
    except ConfigError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # The path is not a secret and is worth confirming back to the user. The API key is
    # never printed.
    print(f"Database: {config.database_path}")
    print("askcrossroads is configured. The application is not implemented yet.")
    return 0
