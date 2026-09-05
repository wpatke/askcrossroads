"""FastAPI routes and the console entry point.

Nothing is served yet. ``main`` checks that the application is configured, then opens,
validates and introspects the configured database, and reports what it found. The HTTP
server is added as the application is built up.
"""

import sys

from askcrossroads.config import ConfigError, load_config
from askcrossroads.database import Database, DatabaseError


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
        database = Database.open(settings.database_path)
    except DatabaseError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # Held open only for as long as there is nothing to serve. Later work keeps it open
    # for the life of the process.
    with database:
        context = database.context
        provenance = context.provenance
        # The path is not a secret and is worth confirming back to the user. The API key
        # is never printed.
        print(f"Database: {context.path}")
        print(
            f"Built by Crossroads-UK {provenance.crossroads_version}, "
            f"schema {provenance.schema_version}, at {provenance.built_at_utc}"
        )
        print(
            f"{len(context.tables)} tables, {len(context.views)} views, "
            f"{len(context.codebook)} codebook rows, "
            f"{len(context.coded_column_names)} coded columns, "
            f"{len(context.temporal_ranges)} dated columns"
        )
        print("askcrossroads is configured. The application is not implemented yet.")
    return 0
