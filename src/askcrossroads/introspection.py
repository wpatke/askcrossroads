"""Runtime schema discovery from the database's own catalogue (spec §2A).

Nothing in here knows a table name, a column name, a dataset name, or a code value.
Everything is read from whatever file the user supplied, which is what lets a build with
an optional dataset and a build without it both work with no code change (spec §2C).

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

    `tbl` is the singular SOURCE name, not the plural name of the table built from it.
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
