"""Read-only DuckDB connection and Crossroads-UK convention validation (spec §1, §2, §5).

This module answers, in order, three questions about the file the user configured: can it
be opened at all, is it a Crossroads-UK build, and what built it. Only the first is
implemented so far.
"""

import duckdb


class DatabaseError(Exception):
    """The configured file could not be opened as a DuckDB database.

    The message is written for a person reading a terminal: it names the path and gives
    the engine's own reason. It never contains a credential.
    """


def open_database(path):
    """Open `path` read-only, with the database's own external access switched off.

    Two of the safety layers in spec §5 are established here, at connect time, so every
    later part of the application inherits them and no caller can opt out:

    - ``read_only=True`` makes the engine itself reject every write, whatever SQL
      reaches it. This is the backstop for the SQL validator that arrives later, not a
      duplicate of it: spec §11.4 requires a write attempt to fail as if the validator
      had been bypassed entirely.
    - ``enable_external_access=False`` stops a query reading or writing the filesystem
      or the network from inside the database. It can only be set when the connection is
      opened, which is exactly why it belongs here rather than at a call site.

    The caller is responsible for closing the returned connection.

    Returns an open DuckDB connection. Raises DatabaseError, naming the path, when the
    file cannot be opened.
    """
    try:
        return duckdb.connect(
            str(path),
            read_only=True,
            config={"enable_external_access": False},
        )
    except duckdb.Error as error:
        # duckdb.Error is the base class of every error the driver raises, so one except
        # covers a missing file, a directory, a file that is not a DuckDB database, and a
        # build written by a storage format this version cannot read. The user gets the
        # path and the engine's own reason rather than a traceback.
        raise DatabaseError(
            f"Could not open {path} as a DuckDB database.\n{error}"
        ) from error
