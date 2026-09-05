"""Read-only DuckDB connection, Crossroads-UK convention validation, and provenance
(spec §1, §2, §5).

This module answers, in order, three questions about the file the user configured: can it
be opened at all, is it a Crossroads-UK build, and what built it. It then bundles those
answers together with the runtime discovery in introspection.py into one immutable
DatabaseContext that the rest of the application reads and never rebuilds.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from askcrossroads import config, introspection

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


def _refusal(problems):
    """The spec §1 message: what was wrong, then how to get a file that is not."""
    return (
        "This file does not look like a Crossroads-UK database: "
        + "; ".join(problems)
        + ".\n\n"
        + config.HOW_TO_GET_A_DATABASE
    )


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
        raise NotACrossroadsBuild(_refusal(problems))


@dataclass(frozen=True)
class Provenance:
    """What built this file (spec §2D).

    Displayed to the user, never used to gate acceptance: because structure is discovered
    at runtime, a newer or older schema_version is not inherently unusable, and refusing
    on a version number would reject working files.

    The build parameters are held as the raw JSON string upstream wrote, and decoded on
    demand. Storing a decoded dict would put a mutable object inside a frozen dataclass,
    which would quietly defeat the immutability this object exists to provide.
    """

    crossroads_version: str
    schema_version: int
    built_at_utc: object
    parameters_json: str

    @property
    def parameters(self):
        """The build parameters — datasets, years, boundary mode — as a dict.

        Returns {} when the field is absent or is not a JSON object. Provenance is
        display material rather than a gate, so a build whose parameters field is
        unreadable is still perfectly usable and this must not raise.
        """
        try:
            decoded = json.loads(self.parameters_json or "")
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def as_dict(self):
        """A JSON-safe form, for the /api/database response (spec §9)."""
        return {
            "crossroads_version": self.crossroads_version,
            "schema_version": self.schema_version,
            "built_at_utc": (
                self.built_at_utc.isoformat() if self.built_at_utc is not None else None
            ),
            "parameters": self.parameters,
        }


def read_provenance(connection):
    """Read the crossroads_meta row.

    A Crossroads-UK build writes exactly one, replaced on each rebuild. An empty table is
    a refusal: the file has the right shape but no record of what produced it, so nothing
    downstream could report provenance to the user. More than one row is not an error —
    the most recent is taken.
    """
    row = connection.execute(
        "SELECT crossroads_version, schema_version, built_at_utc, parameters "
        "FROM crossroads_meta ORDER BY built_at_utc DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise NotACrossroadsBuild(
            _refusal(["`crossroads_meta` is empty, so nothing records what built this file"])
        )

    crossroads_version, schema_version, built_at_utc, parameters = row
    return Provenance(
        crossroads_version=crossroads_version,
        schema_version=schema_version,
        built_at_utc=built_at_utc,
        parameters_json=parameters,
    )


@dataclass(frozen=True)
class DatabaseContext:
    """Everything the rest of the application knows about the open file.

    Built once, when the database is opened, and never rebuilt: spec §3 requires the
    prompt prefix to be byte-identical for a given database, and re-reading the file per
    question would be both slower and a way for that to quietly stop being true.

    Every field is a TUPLE rather than a list, set, or dict. @dataclass(frozen=True) only
    stops the fields being reassigned; a list inside one is still mutable, and a caller
    that appended to it would change what every later question sees.
    """

    path: Path
    provenance: Provenance
    relations: tuple
    codebook: tuple
    column_manifest: tuple
    temporal_ranges: tuple

    @property
    def tables(self):
        """Stored tables only, in name order."""
        return tuple(r for r in self.relations if not r.is_view)

    @property
    def views(self):
        """Views only, in name order."""
        return tuple(r for r in self.relations if r.is_view)

    def relation(self, name):
        """The relation with this name, or None if the build does not have one.

        This is how a weatherless build is handled: there is no weather relation, so this
        returns None and nothing offers one to the prompt (spec §6).
        """
        for relation in self.relations:
            if relation.name == name:
                return relation
        return None

    @property
    def coded_columns(self):
        """Manifest entries classified `coded` — the columns carrying DfT codes.

        `coded` is the ONLY manifest kind anything branches on. Every other value is
        carried through verbatim, including one this project has never seen: structure
        comes from information_schema, so an unfamiliar classification loses no column.

        Remember ManifestEntry.tbl is the singular SOURCE name ('collision'), not a table
        name ('collisions'). Do not join it to a relation name.
        """
        return tuple(e for e in self.column_manifest if e.kind == "coded")

    @property
    def coded_column_names(self):
        """The distinct names of coded columns, sorted."""
        return tuple(sorted({e.col for e in self.coded_columns}))

    @property
    def manifest_kinds(self):
        """Every classification this build's manifest actually uses, sorted."""
        return tuple(sorted({e.kind for e in self.column_manifest if e.kind}))


class Database:
    """The one open connection, and the facts read from it.

    Spec §5 is explicit that v1 holds one connection and runs one query at a time,
    because interrupt() cancels the connection rather than a particular query. So this is
    created once at startup and shared, never one per request.
    """

    def __init__(self, connection, context):
        self.connection = connection
        self.context = context

    @classmethod
    def open(cls, path):
        """Open, validate and introspect `path`.

        Raises DatabaseError when the file cannot be opened, and NotACrossroadsBuild when
        it opens but is not a Crossroads-UK build. The connection is closed before either
        is raised, so a refusal leaves no file handle behind.
        """
        connection = open_database(path)
        try:
            validate_conventions(connection)
            relations = introspection.discover_relations(connection)
            context = DatabaseContext(
                path=Path(path),
                provenance=read_provenance(connection),
                relations=relations,
                codebook=introspection.load_codebook(connection),
                column_manifest=introspection.load_column_manifest(connection),
                temporal_ranges=introspection.temporal_ranges(connection, relations),
            )
        except Exception:
            # A refusal must not leave the file open.
            connection.close()
            raise
        return cls(connection, context)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
        return False
