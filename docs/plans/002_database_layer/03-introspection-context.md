# Stage 03 — Introspection & the Database Context

> Part of *Database Access, Convention Validation & Introspection*. You have the overview + this
> stage; implement **only** this stage. Read [00-overview.md](00-overview.md) first, in particular
> the Cross-Cutting Constraints and the Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 02 must be complete. Verify:

```bash
.venv/bin/python -c "from askcrossroads.database import validate_conventions, NotACrossroadsBuild; print('ok')" && .venv/bin/python -m pytest -q
```

Expected: prints `ok`, then a green run of the whole suite including the console-script refusal tests.

`src/askcrossroads/introspection.py` should currently contain only its docstring:

```bash
cat src/askcrossroads/introspection.py
```

If `validate_conventions` does not exist, stop — stage 02 is not finished.

---

## Objective

Discover everything about the open file at runtime — every table, view and column, the whole codebook
and manifest, the date range of every date-typed column, and the build's provenance — and bundle it
into one immutable object built once and never rebuilt.

---

## The rules that govern this whole stage

**1. Structure comes from the file, never from this code.** `introspection.py` must contain no table
name, no column name, no dataset name, and no code value. Everything comes from
`information_schema` and from the three convention tables. This is the rule the whole application
rests on ([spec.md §2C](../../spec.md)), and this is the module where breaking it would be easiest.

**2. Date columns are found by type, never by name.** [spec.md §6](../../spec.md). A column called
`date_raw` holding text is not a date column. A column called `valid_from` holding a `DATE` is. There
are tests for both directions, and step 1 of this stage adds the fixture columns that make those
tests able to fail.

**3. `introspection.py` never imports `database.py`.** The dependency runs one way: `database.py`
imports `introspection.py`. Reversing it anywhere creates an immediate import cycle.

**4. The context is immutable, deterministic, and built once.** Spec §3 requires the prompt prefix to
be byte-identical for a given database. That is only true if the context is fixed at load and every
collection on it has an explicit, stable order. Every field is a **tuple** — `@dataclass(frozen=True)`
only stops fields being reassigned; a list inside one is still mutable.

---

## Implementation Steps

### Step 1 — Extend the fixture factory in `tests/conftest.py`

Three additions, each mirroring a table that genuinely exists in a real Crossroads-UK build. Without
them, the central rule of this stage cannot be tested: a test of "find dates by type" would pass
whether the code were right or wrong.

**1a.** Change the datetime import at the top of the file:

```python
from datetime import datetime
```

to:

```python
from datetime import date, datetime
```

**1b.** Add a fourth dataset constant, next to the existing `STATS19` / `ERA5_WEATHER` / `AADF`:

```python
BANK_HOLIDAYS = "bank_holidays"
```

**1c.** Add the row data, after `AADF_ROWS`:

```python
# (month, day, title). The year comes from the build's `years` argument, like the
# collision rows.
#
# The bank_holidays table exists in the fixture for one reason: it carries the two traps
# that make "date columns are found by TYPE, never by name" (spec §6) a testable claim.
#
#   `date`      is a real DATE column. Code that only looks for TIMESTAMP misses it.
#   `date_raw`  is VARCHAR with a date-like name. Code that matches on names wrongly
#               treats it as a date.
#
# Both mirror upstream: a real build has bank_holidays.date DATE, and Crossroads-UK's
# keep-in-place model carries every cleansed field twice, the second verbatim as a string.
BANK_HOLIDAY_ROWS = [
    (1, 1, "New Year's Day"),
    (4, 7, "Good Friday"),
    (12, 25, "Christmas Day"),
]
```

**1d.** Inside `build_crossroads_db`, after the `if AADF in datasets:` block and **before** the
"Filtered views exist on every build" comment, add:

```python
        if BANK_HOLIDAYS in datasets:
            con.execute(
                "CREATE TABLE bank_holidays ("
                "source_row_key VARCHAR, date DATE, date_raw VARCHAR, "
                "division VARCHAR, title VARCHAR, bunting BOOLEAN)"
            )
            holidays = []
            for i, (month, day, title) in enumerate(BANK_HOLIDAY_ROWS):
                year = years[i % len(years)]
                on = date(year, month, day)
                holidays.append(
                    (f"bh{i}", on, on.isoformat(), "england-and-wales", title, True)
                )
            con.executemany(
                "INSERT INTO bank_holidays VALUES (?, ?, ?, ?, ?, ?)", holidays
            )
            # Upstream has no reject flag for this dimension, so the view is an
            # unfiltered passthrough kept for naming consistency.
            con.execute(
                "CREATE VIEW bank_holidays_clean AS SELECT * FROM bank_holidays"
            )

        # An always-present table that is always EMPTY, mirroring upstream's landing
        # place for lines that could not be parsed at all. Its TIMESTAMP column is how
        # the empty-table case gets tested: MIN and MAX both return NULL.
        con.execute(
            "CREATE TABLE quarantine_raw ("
            "source_id VARCHAR, raw_text VARCHAR, reason VARCHAR, "
            "ingested_at TIMESTAMP)"
        )
```

**1e.** Add an `include_meta_row` option, so the "provenance table is empty" branch can be tested.
In `_write_convention_tables`, add `include_meta_row=True` to the keyword-only parameters, and wrap
the `INSERT INTO crossroads_meta` statement:

```python
    # A real build always writes exactly one row. include_meta_row=False produces a
    # file whose shape is right but whose provenance is absent.
    if include_meta_row:
        con.execute(
            "INSERT INTO crossroads_meta "
            "(crossroads_version, schema_version, built_at_utc, parameters) "
            "VALUES (?, ?, ?, ?)",
            [crossroads_version, schema_version, built_at_utc, parameters],
        )
```

Then add `include_meta_row=True` to `build_crossroads_db`'s keyword-only parameters, document it in
the docstring, and pass it into the `_write_convention_tables(...)` call.

**1f.** Update the default fixture so most tests get the new tables:

```python
@pytest.fixture
def crossroads_db(crossroads_db_factory):
    """A valid build with every dataset present — the default for most tests."""
    return crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER, AADF, BANK_HOLIDAYS))
```

**Expected result:** `.venv/bin/python -m pytest tests/test_fixtures.py -q` is still green. No
existing test asserts an exact set of table names, so adding two tables breaks nothing. If a test
does fail here, read it before changing it — it may have found a genuine mistake in 1d.

### Step 2 — Write `src/askcrossroads/introspection.py`

Replace the file's contents entirely with:

```python
"""Runtime schema discovery from the database's own catalogue (spec §2A).

Nothing in here knows a table name, a column name, a dataset name, or a code value.
Everything is read from whatever file the user supplied, which is what lets a build with
weather data and a build without it both work with no code change (spec §2C).

This module never imports database.py. The dependency runs one way only.
"""

from dataclasses import dataclass

import duckdb


def is_temporal(data_type):
    """True when a column's declared type is a point in calendar time.

    This is the whole of spec §6's "keyed on the column's type, never on its name". Note
    what it deliberately excludes:

    - INTERVAL is a duration, not a moment, so it has no date range.
    - TIME is a time of day with no calendar in it; asking whether 2019 is inside it is
      meaningless.

    The `== "DATE"` and the TIMESTAMP prefix are written out separately on purpose:
    "TIMESTAMP".startswith("TIME") is True, so a looser check would sweep TIME back in by
    accident. The prefix covers TIMESTAMP, TIMESTAMP WITH TIME ZONE, and the precision
    variants (TIMESTAMP_S, _MS, _NS).

    These are DuckDB's own type names, which is generic SQL knowledge rather than
    knowledge of Crossroads-UK, so spec §2C is not in play.
    """
    normalised = (data_type or "").strip().upper()
    return normalised == "DATE" or normalised.startswith("TIMESTAMP")


def quote_identifier(name):
    """Quote a table or column name for use in SQL.

    The names come from the database's own catalogue rather than from a user, so this
    guards against reserved words and unusual characters rather than against injection.
    """
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Column:
    """One column, with the type the database itself declares for it."""

    name: str
    data_type: str

    @property
    def is_temporal(self):
        return is_temporal(self.data_type)


@dataclass(frozen=True)
class Relation:
    """One table or view, with its columns in declaration order."""

    schema: str
    name: str
    is_view: bool
    columns: tuple

    @property
    def temporal_columns(self):
        return tuple(column for column in self.columns if column.is_temporal)


@dataclass(frozen=True)
class CodebookEntry:
    """One code and its DfT English label (spec §2B)."""

    variable: str
    code: str          # a STRING upstream: '1', '-1'. Never cast to an integer.
    label: str
    is_missing: bool   # True when the code is a missing/unknown sentinel


@dataclass(frozen=True)
class ManifestEntry:
    """One column's classification (spec §2B).

    `tbl` is the singular SOURCE name ('collision'), not the table name ('collisions').
    Do not join it to a relation name — it will match nothing.
    """

    tbl: str
    col: str
    kind: str
    dtype: str


@dataclass(frozen=True)
class TemporalRange:
    """The earliest and latest value of one date-typed column.

    Both are None when the table is empty, which is itself worth carrying: it says the
    column exists and holds nothing, rather than saying nothing at all.
    """

    relation: str
    column: str
    earliest: object
    latest: object


def discover_relations(connection):
    """Every table and view in the database, with every column and its declared type.

    This is spec §2A: the structure of the file is read from the file. Nothing is
    filtered out. Excluding relations by name pattern would be hardcoded knowledge of
    Crossroads-UK's naming, which spec §2C forbids, and it would break silently the day
    upstream renamed something. Deciding what a prompt should show is a separate question
    answered by later work — which can narrow this, but could never widen it.

    Returned sorted by (schema, name), with each relation's columns in declaration order,
    so the same database always produces the same result (spec §3).
    """
    column_rows = connection.execute(
        "SELECT table_schema, table_name, column_name, data_type "
        "FROM information_schema.columns "
        "ORDER BY table_schema, table_name, ordinal_position"
    ).fetchall()

    # information_schema.tables is what tells a stored table from a view. Standard SQL,
    # not Crossroads-UK knowledge.
    type_rows = connection.execute(
        "SELECT table_schema, table_name, table_type FROM information_schema.tables"
    ).fetchall()
    is_view = {
        (schema, name): (table_type or "").upper() != "BASE TABLE"
        for schema, name, table_type in type_rows
    }

    grouped = {}
    for schema, name, column_name, data_type in column_rows:
        grouped.setdefault((schema, name), []).append(Column(column_name, data_type))

    return tuple(
        Relation(
            schema=schema,
            name=name,
            is_view=is_view.get((schema, name), False),
            columns=tuple(columns),
        )
        for (schema, name), columns in sorted(grouped.items())
    )


def load_codebook(connection):
    """Every codebook row, sorted by (variable, code).

    Sorted here in Python with an explicit key rather than with ORDER BY, so the order
    cannot depend on the engine's collation. Spec §3 requires the prompt prefix to be
    byte-identical for a given database, and that is only true if this order is fixed.

    `code` is a string upstream ('1', '-1'), so this is a string sort: '10' sorts before
    '2'. Stability matters more than numeric prettiness, and nothing downstream reads
    meaning into the order.
    """
    rows = connection.execute(
        "SELECT variable, code, label, is_missing FROM codebook"
    ).fetchall()
    entries = [
        CodebookEntry(
            variable=variable,
            code=code,
            label=label,
            # NULL is not a missing-sentinel marker; it just is not one.
            is_missing=bool(is_missing),
        )
        for variable, code, label, is_missing in rows
    ]
    return tuple(sorted(entries, key=lambda e: (e.variable or "", e.code or "")))


def load_column_manifest(connection):
    """Every column_manifest row, sorted by (tbl, col).

    Every row is carried through verbatim, including any `kind` this project has never
    seen. Nothing is dropped and nothing is validated here: structure comes from
    information_schema, so an unfamiliar classification loses no column. `coded` is the
    only value anything ever branches on.
    """
    rows = connection.execute(
        "SELECT tbl, col, kind, dtype FROM column_manifest"
    ).fetchall()
    entries = [
        ManifestEntry(tbl=tbl, col=col, kind=kind, dtype=dtype)
        for tbl, col, kind, dtype in rows
    ]
    return tuple(sorted(entries, key=lambda e: (e.tbl or "", e.col or "")))


def temporal_ranges(connection, relations):
    """Earliest and latest value of every date-typed column, computed once at load.

    This is what lets the model say "this build contains 2023 only" instead of returning
    an empty table for a question about 2019 (spec §6).

    Two deliberate limits:

    - **Stored tables only; views are skipped.** A view is a saved filter over a table, so
      its range either repeats the table's or is narrower by a filter the model is not
      reasoning about. Ranging views as well roughly doubles the startup work for a
      difference that changes no answer.
    - **One query per table, not per column.** All of a table's date columns are asked
      about together, so a build with thirty such columns across ten tables costs ten
      round trips rather than thirty. DuckDB answers MIN/MAX from column statistics, so
      each is cheap — but this runs while the user waits for the application to start.

    A table that cannot be read contributes no range rather than stopping startup: one
    odd audit table must not prevent opening a file that is otherwise fine.

    Returned sorted by (relation, column).
    """
    ranges = []
    for relation in relations:
        if relation.is_view:
            continue
        columns = relation.temporal_columns
        if not columns:
            continue

        selects = ", ".join(
            f"MIN({quote_identifier(column.name)}), MAX({quote_identifier(column.name)})"
            for column in columns
        )
        sql = (
            f"SELECT {selects} FROM "
            f"{quote_identifier(relation.schema)}.{quote_identifier(relation.name)}"
        )
        try:
            row = connection.execute(sql).fetchone()
        except duckdb.Error:
            # Deliberately swallowed. See the docstring: a relation we cannot read simply
            # contributes no range.
            continue

        for i, column in enumerate(columns):
            ranges.append(
                TemporalRange(
                    relation=relation.name,
                    column=column.name,
                    earliest=row[i * 2],
                    latest=row[i * 2 + 1],
                )
            )

    return tuple(sorted(ranges, key=lambda r: (r.relation, r.column)))
```

**Expected result:**

```bash
.venv/bin/python -c "from askcrossroads.introspection import is_temporal; print(is_temporal('DATE'), is_temporal('TIMESTAMP WITH TIME ZONE'), is_temporal('VARCHAR'), is_temporal('TIME'), is_temporal('INTERVAL'))"
```

prints `True True False False False`.

### Step 3 — Complete `src/askcrossroads/database.py`

Replace the file's contents entirely with the following. Everything from stage 01 and 02 is carried
through unchanged; the new material is `Provenance`, `read_provenance`, `DatabaseContext` and
`Database`.

```python
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
      reaches it. This is the backstop for the SQL validator, not a duplicate of it:
      spec §11.4 requires a write attempt to fail as if the validator had been bypassed
      entirely.
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
        # build written by a storage format this version cannot read.
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

    Returns None when the build is valid. Raises NotACrossroadsBuild otherwise.
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
```

### Step 4 — Extend `main()` in `src/askcrossroads/app.py`

Replace the whole file with:

```python
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
```

### Step 5 — Create `tests/test_introspection.py`

```python
"""Structure, meaning and date ranges are discovered from the file (spec §2, §6).

The rule these tests exist to protect is spec §2C: nothing about the schema is assumed.
Where a test names a table or a column it is naming what the FIXTURE built, which is test
input — src/ still contains none of these names.
"""

import pytest

from askcrossroads.database import Database
from askcrossroads.introspection import is_temporal

from conftest import AADF, BANK_HOLIDAYS, ERA5_WEATHER, STATS19


@pytest.fixture
def context(crossroads_db):
    """The context for a valid build carrying every dataset."""
    with Database.open(crossroads_db) as database:
        yield database.context


def names(relations):
    return {relation.name for relation in relations}


# --- The type rule, in isolation (spec §6) ----------------------------------------


@pytest.mark.parametrize(
    "data_type, expected",
    [
        ("DATE", True),
        ("TIMESTAMP", True),
        ("TIMESTAMP WITH TIME ZONE", True),
        ("TIMESTAMP_MS", True),
        ("timestamp", True),      # normalised, not case-sensitive
        ("VARCHAR", False),
        ("INTEGER", False),
        ("TIME", False),          # a time of day carries no calendar
        ("INTERVAL", False),      # a duration, not a moment
        ("", False),
        (None, False),
    ],
)
def test_temporal_types_are_recognised_by_type_alone(data_type, expected):
    assert is_temporal(data_type) is expected


# --- Structure discovery (spec §2A) -----------------------------------------------


def test_every_relation_is_discovered(context):
    discovered = names(context.relations)
    assert {"collisions", "vehicles", "codebook", "column_manifest"} <= discovered
    assert {"collisions_labelled", "collisions_spatial"} <= discovered


def test_tables_and_views_are_told_apart(context):
    assert "collisions" in names(context.tables)
    assert "collisions" not in names(context.views)
    assert "collisions_labelled" in names(context.views)
    assert "collisions_labelled" not in names(context.tables)


def test_columns_carry_the_type_the_database_declares(context):
    collisions = context.relation("collisions")
    types = {column.name: column.data_type for column in collisions.columns}
    assert types["datetime_local"] == "TIMESTAMP"
    assert types["collision_severity"] == "INTEGER"
    assert types["local_authority_ons_district"] == "VARCHAR"


def test_columns_are_in_declaration_order(context):
    collisions = context.relation("collisions")
    assert collisions.columns[0].name == "source_row_key"


@pytest.mark.parametrize(
    "datasets, present, absent",
    [
        ((STATS19,), set(), {"weather", "aadf", "bank_holidays"}),
        ((STATS19, ERA5_WEATHER), {"weather"}, {"aadf", "bank_holidays"}),
        ((STATS19, AADF), {"aadf"}, {"weather", "bank_holidays"}),
        ((STATS19, BANK_HOLIDAYS), {"bank_holidays"}, {"weather", "aadf"}),
    ],
)
def test_absent_datasets_are_simply_absent(crossroads_db_factory, datasets, present, absent):
    """Spec §11.4: a weatherless build must be handled without error, and must not
    offer a table it does not have."""
    with Database.open(crossroads_db_factory(datasets=datasets)) as database:
        discovered = names(database.context.relations)
        assert present <= discovered
        assert not (absent & discovered)
        for missing in absent:
            assert database.context.relation(missing) is None


def test_nothing_is_filtered_out_of_discovery(context):
    """Everything information_schema reports is carried, audit tables included.

    Filtering by name would be hardcoded knowledge of Crossroads-UK's naming (spec §2C)
    and would break silently the day upstream renamed something.
    """
    assert "quarantine_raw" in names(context.relations)


def test_a_column_absent_from_the_manifest_is_still_discovered(context):
    """The manifest classifies STATS19 columns only, so this is the common case.

    Structure comes from information_schema, not from the manifest, which is why an
    unfamiliar or absent classification can never lose a column.
    """
    weather = context.relation("weather")
    assert "temperature_c" in {column.name for column in weather.columns}
    assert "temperature_c" not in {entry.col for entry in context.column_manifest}


# --- Date ranges, keyed on type and never on name (spec §6, §2C) ------------------


def ranges_by_key(context):
    return {(r.relation, r.column): r for r in context.temporal_ranges}


def test_a_timestamp_column_gets_a_range(context):
    found = ranges_by_key(context)[("collisions", "datetime_local")]
    assert found.earliest is not None
    assert found.latest is not None
    assert found.earliest <= found.latest


def test_a_date_column_gets_a_range(context):
    """The first trap: a DATE column, which code looking only for TIMESTAMP would miss."""
    found = ranges_by_key(context)[("bank_holidays", "date")]
    assert found.earliest is not None
    assert found.latest is not None


def test_a_text_column_with_a_date_like_name_gets_no_range(context):
    """The second trap: `date_raw` is VARCHAR. Matching on names would wrongly take it."""
    assert ("bank_holidays", "date_raw") not in ranges_by_key(context)


def test_no_range_is_computed_for_a_view(context):
    """Views are saved filters over tables; ranging them repeats work for no gain."""
    view_names = names(context.views)
    for temporal_range in context.temporal_ranges:
        assert temporal_range.relation not in view_names


def test_an_empty_table_yields_an_empty_range(context):
    """MIN and MAX of nothing are NULL, and that is a fact worth carrying: the column
    exists and holds nothing."""
    found = ranges_by_key(context)[("quarantine_raw", "ingested_at")]
    assert found.earliest is None
    assert found.latest is None


def test_ranges_reflect_the_data_actually_present(crossroads_db_factory):
    """The point of the whole mechanism: 2019 is recognisable as outside a 2023 build."""
    with Database.open(crossroads_db_factory(years=(2019,))) as database:
        found = ranges_by_key(database.context)[("collisions", "datetime_local")]
        assert found.earliest.year == 2019
        assert found.latest.year == 2019


def test_ranges_widen_with_the_years_in_the_build(crossroads_db_factory):
    with Database.open(crossroads_db_factory(years=(2019, 2023))) as database:
        found = ranges_by_key(database.context)[("collisions", "datetime_local")]
        assert found.earliest.year == 2019
        assert found.latest.year == 2023


# --- Codebook and manifest (spec §2B, §3) -----------------------------------------


def test_the_whole_codebook_is_loaded(context):
    assert len(context.codebook) > 0
    variables = {entry.variable for entry in context.codebook}
    assert "collision_severity" in variables


def test_codes_are_strings_and_missing_sentinels_are_flagged(context):
    """Spec §3: the model must know which codes were nulled during the build."""
    assert all(isinstance(entry.code, str) for entry in context.codebook)
    assert any(entry.is_missing for entry in context.codebook)
    assert all(isinstance(entry.is_missing, bool) for entry in context.codebook)


def test_the_codebook_is_sorted_explicitly(context):
    keys = [(entry.variable, entry.code) for entry in context.codebook]
    assert keys == sorted(keys)


def test_the_manifest_is_loaded_and_sorted(context):
    keys = [(entry.tbl, entry.col) for entry in context.column_manifest]
    assert keys == sorted(keys)
    assert len(context.column_manifest) > 0


def test_coded_columns_are_derived_from_the_manifest(context):
    assert "collision_severity" in context.coded_column_names
    # The manifest classifies this numeric, so it is not a coded column even though the
    # codebook carries it. A speed limit stays a number (spec §3).
    assert "speed_limit" not in context.coded_column_names


def test_manifest_tbl_is_the_source_name_not_a_relation_name(context):
    """A join from manifest.tbl to a relation name would match nothing."""
    sources = {entry.tbl for entry in context.column_manifest}
    assert not (sources & names(context.relations))


def test_every_kind_the_build_uses_is_carried_through(context, manifest_kinds):
    """Nothing is dropped for having an unfamiliar classification."""
    assert set(context.manifest_kinds) <= set(manifest_kinds)
    assert "coded" in context.manifest_kinds


# --- The context is immutable and deterministic (spec §3) -------------------------


def test_every_field_is_a_tuple(context):
    """A list inside a frozen dataclass is still mutable, which would defeat the point."""
    assert isinstance(context.relations, tuple)
    assert isinstance(context.codebook, tuple)
    assert isinstance(context.column_manifest, tuple)
    assert isinstance(context.temporal_ranges, tuple)
    assert isinstance(context.relation("collisions").columns, tuple)


def test_the_context_cannot_be_reassigned(context):
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.relations = ()


def test_two_opens_of_one_file_produce_identical_contexts(crossroads_db):
    """The foundation of spec §3's byte-stable prompt prefix.

    If this ever fails, the cache breaks silently and nothing else would catch it.
    """
    with Database.open(crossroads_db) as first, Database.open(crossroads_db) as second:
        assert first.context.relations == second.context.relations
        assert first.context.codebook == second.context.codebook
        assert first.context.column_manifest == second.context.column_manifest
        assert first.context.temporal_ranges == second.context.temporal_ranges
        assert first.context.provenance == second.context.provenance
```

### Step 6 — Add the provenance and `Database` tests to `tests/test_database.py`

Append to the existing file. Add `Database` and `NotACrossroadsBuild` to the imports from
`askcrossroads.database`, then append:

```python
# --- Provenance (spec §2D) --------------------------------------------------------


def test_provenance_reports_what_built_the_file(crossroads_db_factory):
    path = crossroads_db_factory(crossroads_version="1.2.3", schema_version=7)
    with Database.open(path) as database:
        provenance = database.context.provenance
        assert provenance.crossroads_version == "1.2.3"
        assert provenance.schema_version == 7
        assert provenance.built_at_utc is not None


def test_provenance_decodes_the_build_parameters(crossroads_db_factory):
    path = crossroads_db_factory(
        datasets=("stats19", "era5_weather"), years=(2021, 2022), boundary_mode="as_at"
    )
    with Database.open(path) as database:
        parameters = database.context.provenance.parameters
        assert parameters["boundary_mode"] == "as_at"
        assert parameters["years"] == [2021, 2022]
        assert "era5_weather" in parameters["datasets"]


def test_provenance_is_json_safe_for_the_api(crossroads_db):
    """The /api/database response carries this straight out (spec §9)."""
    import json as _json

    with Database.open(crossroads_db) as database:
        payload = database.context.provenance.as_dict()
        assert _json.dumps(payload)
        assert isinstance(payload["built_at_utc"], str)


def test_an_empty_provenance_table_is_refused(crossroads_db_factory):
    """The shape is right, but nothing records what built the file."""
    path = crossroads_db_factory(include_meta_row=False)
    with pytest.raises(NotACrossroadsBuild) as raised:
        Database.open(path)
    assert "crossroads_meta" in str(raised.value)
    assert "pip install crossroads-uk" in str(raised.value)


# --- The Database object ----------------------------------------------------------


def test_open_returns_a_usable_connection_and_a_context(crossroads_db):
    with Database.open(crossroads_db) as database:
        assert database.connection.execute("SELECT 1").fetchone() == (1,)
        assert database.context.path == crossroads_db
        assert len(database.context.relations) > 0


def test_the_connection_is_still_read_only(crossroads_db):
    """The stage 01 guarantee survives everything built on top of it."""
    with Database.open(crossroads_db) as database:
        with pytest.raises(duckdb.Error):
            database.connection.execute("CREATE TABLE intruder (i INTEGER)")


@pytest.mark.parametrize("convention", ["missing", "wrong_shape"])
def test_a_refusal_leaves_no_file_handle_behind(crossroads_db_factory, convention):
    """Proved by opening the same file again straight afterwards."""
    path = crossroads_db_factory(convention=convention)
    with pytest.raises(NotACrossroadsBuild):
        Database.open(path)
    connection = open_database(path)
    connection.close()


def test_close_releases_the_connection(crossroads_db):
    database = Database.open(crossroads_db)
    database.close()
    with pytest.raises(Exception):
        database.connection.execute("SELECT 1")


def test_the_command_reports_provenance_for_a_valid_build(crossroads_db, tmp_path):
    """The end-to-end success path, as a user sees it."""
    result = run_console_script(crossroads_db, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Built by Crossroads-UK" in result.stdout
    assert "codebook rows" in result.stdout
```

---

## Testing & Verification

### Primary: the whole suite, offline

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -v
```

**Expected result:** green, including `tests/test_introspection.py`. Nothing that passed before this
stage may now fail — in particular `tests/test_fixtures.py`, which the step 1 changes touch.

### Just this stage

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_introspection.py tests/test_database.py -v
```

### The success path, end to end, exactly as a user would see it

Build a synthetic database outside the test suite and open it with the real command:

```bash
cd /tmp && /Users/will/Documents/Code/AskCrossroads/.venv/bin/python -c "
import sys; sys.path.insert(0, '/Users/will/Documents/Code/AskCrossroads/tests')
from conftest import build_crossroads_db, STATS19, ERA5_WEATHER, AADF, BANK_HOLIDAYS
build_crossroads_db('/tmp/demo-crossroads.db', datasets=(STATS19, ERA5_WEATHER, AADF, BANK_HOLIDAYS), years=(2021, 2023))
print('built')"
```

```bash
cd /tmp && ANTHROPIC_API_KEY=test-key-not-a-real-credential ASKCROSSROADS_DB=/tmp/demo-crossroads.db /Users/will/Documents/Code/AskCrossroads/.venv/bin/askcrossroads; echo "exit=$?"
```

**Expected result:** four lines — the path, a `Built by Crossroads-UK 0.9.0, schema 4, at ...` line, a
counts line naming tables, views, codebook rows, coded columns and dated columns, and the
not-implemented line — then `exit=0`. Then clean up: `rm -f /tmp/demo-crossroads.db`.

### Nothing about the schema leaked into `src/`

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -rniE "collision|weather|aadf|stats19|vehicle|casualty|bank_holiday|quarantine" src/
```

**Expected result:** no output. This is the single most important check in this stage. A match means
knowledge that belongs in the file has been written into the code.

### The only hardcoded structure is the one dictionary

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -rn "codebook\|column_manifest\|crossroads_meta" src/
```

**Expected result:** matches only inside `CONVENTION_TABLES`, inside the two SQL statements in
`introspection.py` that read those tables, inside `read_provenance`, and in docstrings. Nothing else.

### The integration guard still works

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest -m integration -q
```

**Expected result:** one test collected, skipped, no unknown-marker warning.

### No credential anywhere

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .
```

**Expected result:** no output.

### Nothing was committed

```bash
git status --short
```

**Expected result:** modified and untracked files listed, **nothing staged**.

### Stage ship-readiness checklist

- [ ] `introspection.py` contains no table name, column name, dataset name, or code value.
- [ ] `introspection.py` does not import `database.py`.
- [ ] `is_temporal` accepts `DATE` and every `TIMESTAMP` variant, and rejects `TIME` and `INTERVAL`.
- [ ] `discover_relations` filters nothing out and marks views correctly.
- [ ] Codebook and manifest are sorted in Python on an explicit key.
- [ ] `temporal_ranges` skips views, asks one query per table, and survives an unreadable table.
- [ ] `Provenance` holds the raw JSON string and decodes on demand; `as_dict()` is JSON-safe.
- [ ] An empty `crossroads_meta` raises `NotACrossroadsBuild`.
- [ ] `DatabaseContext` is frozen and every field is a tuple.
- [ ] `Database.open` closes the connection before raising.
- [ ] Two opens of one file produce identical contexts.
- [ ] `tests/conftest.py` has `bank_holidays` (with the `DATE` and `date_raw` traps), an always-empty `quarantine_raw`, and `include_meta_row`.
- [ ] `tests/test_fixtures.py` is still green with no edits.
- [ ] The full offline suite is green.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage:**

- `src/askcrossroads/introspection.py` holds `is_temporal`, `quote_identifier`, `Column`,
  `Relation`, `CodebookEntry`, `ManifestEntry`, `TemporalRange`, `discover_relations`,
  `load_codebook`, `load_column_manifest`, and `temporal_ranges`.
- `src/askcrossroads/database.py` additionally holds `Provenance`, `read_provenance`,
  `DatabaseContext`, and `Database`.
- `src/askcrossroads/app.py`'s `main()` opens the database and prints provenance and counts.
- `tests/test_introspection.py` exists and passes; `tests/test_database.py` covers provenance and
  the `Database` object.
- `tests/conftest.py` carries the two traps, the empty table, and `include_meta_row`.
- Nothing is staged or committed.

**What the next stage of the project may assume:**

- `Database.open(path)` gives one connection and one frozen `DatabaseContext`, built once. Nothing
  downstream re-reads the file to learn about it, and nothing rebuilds the context per question.
- The context answers every question the prompt needs: which relations and columns exist
  (`relations`, `tables`, `views`, `relation(name)`), what the codes mean (`codebook`), which columns
  are coded (`coded_columns`, `coded_column_names`), what years the build covers
  (`temporal_ranges`), and what built it (`provenance`, and `provenance.parameters`).
- Every collection on the context is a tuple in a stable, explicit order, so serialising it twice
  produces identical bytes. That is the precondition for the cached prompt prefix in spec §3.
- The connection is read-only with external access disabled, and one query runs at a time on it.
- Prompt assembly decides what of this to *show*. Introspection has already decided what to *know*,
  which is everything.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `ImportError: cannot import name 'introspection'` | A circular import — `introspection.py` imported `database.py` | `introspection.py` must import nothing from this package. Remove the import |
| `test_a_date_column_gets_a_range` fails with `KeyError` | Step 1d was skipped, or `bank_holidays` was not added to the `crossroads_db` fixture's datasets in 1f | Do both. The trap is the only thing making this test able to fail |
| `test_a_text_column_with_a_date_like_name_gets_no_range` fails | Date columns were matched on name somewhere | Every decision must go through `is_temporal(column.data_type)`. No column name appears in that path |
| `test_no_range_is_computed_for_a_view` fails | The `if relation.is_view: continue` guard is missing, or `table_type` was compared case-sensitively | DuckDB reports `BASE TABLE`; the comparison uppercases before testing |
| `test_two_opens_of_one_file_produce_identical_contexts` fails | Something is unsorted, or a set leaked into a field | Every collection must be built with an explicit `sorted(...)` or preserve `ordinal_position`. A set has no order and must never be a field |
| `test_every_field_is_a_tuple` fails | A `list(...)` was returned instead of `tuple(...)` | Wrap it. A frozen dataclass does not make a list immutable |
| `duckdb.BinderException` from a temporal range query | An identifier was not quoted, and collided with a reserved word | Every discovered identifier goes through `quote_identifier` |
| `test_an_empty_provenance_table_is_refused` fails | Step 1e was skipped, or the `if include_meta_row:` guard wraps the wrong statement | It wraps the `INSERT` only, never the `CREATE TABLE` |
| `tests/test_fixtures.py` fails after step 1 | A new table broke an assertion that expected an exact set | Read the failing assertion before changing it. No existing test asserts an exact table set, so a failure here probably means a genuine mistake in 1d |
| Startup is noticeably slow against a real build | More date columns than expected, or the per-table batching was written per-column | Confirm one query per table. If it is still slow, narrow which relations are ranged — do not make the work lazy, which would break the once-only guarantee in spec §3 |

**Rollback:** restore `src/askcrossroads/introspection.py` to its docstring-only contents, remove
everything after `validate_conventions` in `database.py`, revert `app.py` to its stage-02 contents,
revert the step 1 changes to `tests/conftest.py`, and delete `tests/test_introspection.py` and the
step 6 additions to `tests/test_database.py`. Stage 02's suite then passes unchanged.
