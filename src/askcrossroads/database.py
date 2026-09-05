"""Read-only DuckDB connection and Crossroads-UK convention validation (spec §1, §2, §5).

This module answers, in order, three questions about the file the user configured: can it
be opened at all, is it a Crossroads-UK build, and what built it. The first two are
implemented so far.
"""

import duckdb

from askcrossroads import config

# The ONLY structure AskCrossroads is permitted to assume (spec §2C): three table names,
# and the columns each must carry. Everything else about a build is discovered at
# runtime. Nothing else in this project may hardcode a table or column name.
CONVENTION_TABLES = {
    "codebook": ("variable", "code", "label", "is_missing"),
    "column_manifest": ("tbl", "col", "kind", "dtype"),
    "crossroads_meta": (
        "crossroads_version",
        "schema_version",
        "built_at_utc",
        "parameters",
    ),
}


class DatabaseError(Exception):
    """The configured file could not be opened as a DuckDB database.

    The message is written for a person reading a terminal: it names the path and gives
    the engine's own reason. It never contains a credential.
    """


class NotACrossroadsBuild(DatabaseError):
    """The file opened, but it is not a Crossroads-UK build.

    Carries the spec §1 message: what was missing, and the command that produces a valid
    file. It is a refusal, not a crash, and must never surface as a traceback.
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


def _in_english(names):
    """Join names the way a sentence does: 'a', 'a or b', 'a, b or c'."""
    names = list(names)
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def _convention_columns(connection):
    """{table name: {column names}} for whichever convention tables exist.

    Read from information_schema rather than by trying each table in turn, so a table
    that is absent and one that is misshapen are told apart without provoking an
    exception and reading it back out of an error message.
    """
    names = list(CONVENTION_TABLES)
    placeholders = ", ".join(["?"] * len(names))
    rows = connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        f"WHERE table_name IN ({placeholders})",
        names,
    ).fetchall()

    found = {}
    for table_name, column_name in rows:
        found.setdefault(table_name, set()).add(column_name)
    return found


def validate_conventions(connection):
    """Prove the open database is a Crossroads-UK build, by shape (spec §2D).

    Checks that each convention table exists, carries at least the columns listed in
    CONVENTION_TABLES, and can actually be read. Nothing is checked against a version
    number: structure is discovered at runtime, so a newer or older schema is not
    inherently unusable, and refusing on a version would reject working files.

    The column check is a SUBSET check on purpose. A future Crossroads-UK release that
    ADDS a column to one of these tables must keep working here; only a table that has
    LOST a column we rely on is a refusal.

    Returns None when the build is valid. Raises NotACrossroadsBuild, carrying the spec
    §1 message, when it is not.
    """
    present = _convention_columns(connection)
    problems = []

    absent = [name for name in CONVENTION_TABLES if name not in present]
    if absent:
        problems.append(
            f"no {_in_english([f'`{name}`' for name in absent])} table was found"
        )

    for name, required in CONVENTION_TABLES.items():
        if name in absent:
            continue
        missing = [column for column in required if column not in present[name]]
        if missing:
            problems.append(
                f"`{name}` has no "
                + _in_english([f"`{column}`" for column in missing])
                + " column"
            )

    # A table can be listed in the catalogue and still be unreadable. Reading one row
    # from each is the cheapest way to find that out now, with a clear message, rather
    # than halfway through building a prompt. SELECT * is deliberate: the columns were
    # confirmed above, so this asks only whether the table can be read at all.
    if not problems:
        for name in CONVENTION_TABLES:
            try:
                connection.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()
            except duckdb.Error as error:
                problems.append(f"`{name}` could not be read ({error})")

    if problems:
        raise NotACrossroadsBuild(
            "This file does not look like a Crossroads-UK database: "
            + "; ".join(problems)
            + ".\n\n"
            + config.HOW_TO_GET_A_DATABASE
        )
