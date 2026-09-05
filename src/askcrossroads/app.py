"""FastAPI routes and the console entry point.

Nothing is served yet. ``main`` checks that the application is configured, then opens and
validates the configured database, and reports clearly when either fails. The HTTP server
is added as the application is built up.
"""

import sys

from askcrossroads.config import ConfigError, load_config
from askcrossroads.database import DatabaseError, open_database, validate_conventions


def main():
    """Console entry point. Returns the process exit code.

    The console script wrapper calls ``sys.exit(main())``, so the value returned here
    becomes the process exit status. Both a configuration problem and an unusable
    database exit non-zero on the terminal rather than opening a browser onto an error
    page (spec §1).
    """
    try:
        settings = load_config()
    except ConfigError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # NotACrossroadsBuild subclasses DatabaseError, so one except covers both "the file
    # would not open" and "the file opened but is not a Crossroads-UK build".
    try:
        connection = open_database(settings.database_path)
        try:
            validate_conventions(connection)
        finally:
            # Nothing else uses the connection yet, so it is closed either way. Later
            # work holds it open for the life of the process.
            connection.close()
    except DatabaseError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # The path is not a secret and is worth confirming back to the user. The API key is
    # never printed.
    print(f"Database: {settings.database_path}")
    print("askcrossroads is configured. The application is not implemented yet.")
    return 0
