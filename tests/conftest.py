"""Synthetic Crossroads-UK databases for the offline test suite.

Real Crossroads-UK builds run to gigabytes. Spec §11.1 requires the default pytest run
to work for anyone who clones the repository — no API key, no network, no real build —
so the tests construct their own small databases carrying the same convention tables, in
the same shapes, as a real build.

This is a factory rather than a single fixture because spec §11.4 requires builds with
different dataset combinations, plus a build with no convention tables and one whose
convention tables have the wrong shape. Those are parameters of one builder, not five
hand-maintained files that drift apart.

The literal STATS19-shaped data below is test input. It is deliberately NOT mirrored
anywhere in src/ — spec §2C requires the application to discover all of this at runtime.
"""

import itertools
import json
from datetime import date, datetime

import duckdb
import pytest

# The classifications column_manifest uses. Spec §2C permits the application to know
# this vocabulary, so it is written once, here, and a future addition upstream is a
# one-line change rather than a search.
MANIFEST_KINDS = ("identity", "geo", "datetime", "coded", "numeric", "text")

# Datasets a build may contain. "stats19" is always present: it is what makes the file a
# road-safety database at all. The other two are optional, exactly as upstream.
STATS19 = "stats19"
ERA5_WEATHER = "era5_weather"
AADF = "aadf"
BANK_HOLIDAYS = "bank_holidays"

# A fixed build timestamp, so two builds with the same arguments are identical. Spec
# §11.4 requires a byte-stable prompt prefix, and that test is impossible if the
# fixture's provenance moves.
DEFAULT_BUILT_AT = datetime(2026, 1, 1, 12, 0, 0)

# --- codebook ---------------------------------------------------------------------
# (variable, code, label, is_missing). Codes are STRINGS, as upstream.
#
# Note `speed_limit`: it is in the codebook but the manifest classifies it `numeric`,
# so the labelled view gives it no label twin. That mirrors a real build and is what
# spec §3 means by "a speed limit stays a number".
#
# There is deliberately NO `enhanced_severity_collision` variable here. The manifest
# classifies that column `coded`, so the labelled view creates the twin anyway and it is
# NULL for every row — spec §3's documented exception, reproduced exactly.

CODEBOOK_ROWS = [
    ("collision_severity", "-1", "Data missing or out of range", True),
    ("collision_severity", "1", "Fatal", False),
    ("collision_severity", "2", "Serious", False),
    ("collision_severity", "3", "Slight", False),
    ("weather_conditions", "-1", "Data missing or out of range", True),
    ("weather_conditions", "1", "Fine no high winds", False),
    ("weather_conditions", "2", "Raining no high winds", False),
    ("weather_conditions", "3", "Snowing no high winds", False),
    ("weather_conditions", "9", "Unknown", True),
    ("vehicle_type", "-1", "Data missing or out of range", True),
    ("vehicle_type", "1", "Pedal cycle", False),
    ("vehicle_type", "9", "Car", False),
    ("speed_limit", "20", "20 mph", False),
    ("speed_limit", "30", "30 mph", False),
    ("speed_limit", "40", "40 mph", False),
    ("speed_limit", "70", "70 mph", False),
]

# --- column_manifest --------------------------------------------------------------
# (tbl, col, kind, dtype). `tbl` is the singular SOURCE name, never the table name.
# `dtype` is set only for coded and numeric.
#
# The `geo` and `datetime` entries name upstream columns that do NOT exist verbatim in
# the silver tables — the build derives `easting`/`northing` and `datetime_local` from
# them. A real build behaves the same way.

MANIFEST_ROWS = [
    ("collision", "accident_index", "identity", None),
    ("collision", "accident_year", "identity", None),
    ("collision", "location_easting_osgr", "geo", None),
    ("collision", "location_northing_osgr", "geo", None),
    ("collision", "date", "datetime", None),
    ("collision", "time", "datetime", None),
    ("collision", "collision_severity", "coded", "INTEGER"),
    ("collision", "weather_conditions", "coded", "INTEGER"),
    ("collision", "enhanced_severity_collision", "coded", "INTEGER"),
    ("collision", "speed_limit", "numeric", "INTEGER"),
    ("collision", "number_of_casualties", "numeric", "INTEGER"),
    ("collision", "longitude", "numeric", "DOUBLE"),
    ("collision", "local_authority_ons_district", "text", None),
    ("vehicle", "accident_index", "identity", None),
    ("vehicle", "vehicle_reference", "identity", None),
    ("vehicle", "vehicle_type", "coded", "INTEGER"),
    ("vehicle", "age_of_vehicle", "numeric", "INTEGER"),
    ("vehicle", "generic_make_model", "text", None),
]

# Which silver table each manifest `tbl` value corresponds to. The application must
# never need this mapping — it is here only so the fixture can build the labelled views.
SOURCE_TO_TABLE = {"collision": "collisions", "vehicle": "vehicles"}

# --- silver rows ------------------------------------------------------------------
# The date parts are (month, day, hour, minute); the year comes from the build's `years`
# argument, so a build for 2019 really does contain 2019 data and nothing else.
#
# Row c4 carries a NULL weather_conditions: upstream replaces missing/unknown sentinels
# with NULL before the file is written, so no is_missing code ever appears in a silver
# column. It also has geom_valid FALSE, so the spatial view drops it.

# (key, month, day, hour, minute, easting, northing, geom_valid, severity, weather,
#  enhanced, speed_limit, casualties, longitude, ons_district)
COLLISION_ROWS = [
    ("c1", 1, 15, 8, 30, 530000.0, 180000.0, True, 1, 1, 1, 30, 1, -0.1276, "E09000001"),
    ("c2", 2, 20, 17, 45, 531000.0, 181000.0, True, 2, 2, 2, 30, 2, -0.1200, "E09000001"),
    ("c3", 3, 5, 12, 0, 532000.0, 182000.0, True, 3, 1, 1, 40, 1, -0.1100, "E09000002"),
    ("c4", 6, 11, 22, 10, None, None, False, 3, None, 3, 20, 1, None, "E09000002"),
    ("c5", 9, 30, 7, 5, 533000.0, 183000.0, True, 2, 2, 2, 30, 3, -0.1000, "E09000003"),
    ("c6", 12, 25, 3, 20, 534000.0, 184000.0, True, 1, 3, 1, 70, 4, -0.0900, "E09000003"),
]

# (key, collision_key, vehicle_reference, vehicle_type, age_of_vehicle, make_model, link_valid)
VEHICLE_ROWS = [
    ("v1", "c1", 1, 9, 5, "FORD FOCUS", True),
    ("v2", "c1", 2, 1, None, "PEDAL CYCLE", True),
    ("v3", "c2", 1, 9, 12, "VAUXHALL CORSA", True),
    ("v4", "c5", 1, 9, 3, "TOYOTA YARIS", False),
]

# (month, day, temperature_c, precipitation_mm)
WEATHER_ROWS = [
    (1, 15, 4.2, 0.0),
    (2, 20, 7.8, 3.1),
    (6, 11, 18.4, 0.0),
    (12, 25, 1.1, 1.4),
]

# (count_point_id, road_name, all_motor_vehicles, easting, northing)
AADF_ROWS = [
    ("CP1001", "A1", 45120.0, 530500.0, 180500.0),
    ("CP1002", "A2", 18240.0, 531500.0, 181500.0),
    ("CP1003", "M25", 132400.0, 532500.0, 182500.0),
]

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


def _accident_index(key, year):
    """A stable, readable stand-in for an upstream collision reference."""
    return f"{year}{key.upper():0>13}"


def _coded_columns(source):
    """The coded columns the manifest lists for one source, in manifest order."""
    return [col for tbl, col, kind, _ in MANIFEST_ROWS if tbl == source and kind == "coded"]


def _labelled_view_sql(table, coded_columns):
    """Build a <table>_labelled view the same way a real Crossroads-UK build does.

    One LEFT JOIN to the codebook per coded column, matching on the variable name and on
    the code cast to VARCHAR. A coded column the codebook does not cover still gets its
    twin; that twin is simply NULL for every row.
    """
    selects = ["s.*"]
    joins = []
    for i, col in enumerate(coded_columns):
        alias = f"cb{i}"
        selects.append(f'{alias}."label" AS {col}_label')
        joins.append(
            f"LEFT JOIN codebook AS {alias} "
            f"ON {alias}.variable = '{col}' "
            f"AND {alias}.code = CAST(s.{col} AS VARCHAR)"
        )
    return (
        f"CREATE VIEW {table}_labelled AS SELECT "
        + ", ".join(selects)
        + f" FROM {table} AS s "
        + " ".join(joins)
    )


def _write_convention_tables(con, *, convention, datasets, years, boundary_mode,
                             crossroads_version, schema_version, built_at_utc,
                             extra_meta_column=False, include_meta_row=True,
                             codebook_rows=CODEBOOK_ROWS, manifest_rows=MANIFEST_ROWS):
    """Write codebook, column_manifest and crossroads_meta in the requested state."""
    if convention == "missing":
        # A perfectly valid DuckDB file that is simply not a Crossroads-UK build.
        return

    if convention == "wrong_shape":
        # The names are right and the shapes are wrong. Spec §11.4 requires this case to
        # produce the documented refusal rather than a crash.
        con.execute("CREATE TABLE codebook (name VARCHAR, meaning VARCHAR)")
        con.execute("CREATE TABLE column_manifest (table_name VARCHAR, column_name VARCHAR)")
        con.execute("CREATE TABLE crossroads_meta (version VARCHAR)")
        return

    con.execute(
        "CREATE TABLE codebook ("
        "variable VARCHAR, code VARCHAR, label VARCHAR, is_missing BOOLEAN)"
    )
    con.executemany("INSERT INTO codebook VALUES (?, ?, ?, ?)", codebook_rows)

    con.execute(
        "CREATE TABLE column_manifest (tbl VARCHAR, col VARCHAR, kind VARCHAR, dtype VARCHAR)"
    )
    con.executemany("INSERT INTO column_manifest VALUES (?, ?, ?, ?)", manifest_rows)

    # `extra_meta_column` simulates a future Crossroads-UK release that adds a column.
    # A subset check must still accept the build; an exact-set check would refuse it.
    con.execute(
        "CREATE TABLE crossroads_meta ("
        "crossroads_version VARCHAR, schema_version INTEGER, "
        "built_at_utc TIMESTAMP, parameters VARCHAR"
        + (", build_host VARCHAR" if extra_meta_column else "")
        + ")"
    )
    # `parameters` is a JSON string upstream, with sorted keys so the same arguments
    # always serialise identically.
    parameters = json.dumps(
        {
            "boundary_mode": boundary_mode,
            "datasets": sorted(datasets),
            "years": sorted(years),
        },
        sort_keys=True,
    )
    # A real build always writes exactly one row. include_meta_row=False produces a
    # file whose shape is right but whose provenance is absent.
    if include_meta_row:
        con.execute(
            "INSERT INTO crossroads_meta "
            "(crossroads_version, schema_version, built_at_utc, parameters) "
            "VALUES (?, ?, ?, ?)",
            [crossroads_version, schema_version, built_at_utc, parameters],
        )


def build_crossroads_db(
    path,
    *,
    datasets=(STATS19,),
    years=(2023,),
    boundary_mode="snapshot",
    crossroads_version="0.9.0",
    schema_version=4,
    built_at_utc=DEFAULT_BUILT_AT,
    convention="full",
    extra_meta_column=False,
    include_meta_row=True,
):
    """Build a small synthetic Crossroads-UK database at `path` and return the path.

    datasets    which optional datasets are present. "stats19" is always built; add
                ERA5_WEATHER for a `weather` table, AADF for an `aadf` table, and
                BANK_HOLIDAYS for a `bank_holidays` table.
    years       the years the collision rows fall in, cycled across the rows.
    convention  "full" (a valid build), "missing" (no convention tables at all), or
                "wrong_shape" (the three tables exist by name with the wrong columns).
    extra_meta_column  add a column to `crossroads_meta` beyond what AskCrossroads
                requires, simulating a future Crossroads-UK release (a subset check
                must still accept the build).
    include_meta_row  a real build always writes exactly one crossroads_meta row.
                False produces a file whose shape is right but whose provenance is
                absent.

    The connection is closed before returning, so the file is complete and can then be
    opened read-only.
    """
    if convention not in ("full", "missing", "wrong_shape"):
        raise ValueError(f"unknown convention state: {convention!r}")
    years = tuple(years)
    if not years:
        raise ValueError("years must not be empty")
    datasets = tuple(datasets)

    con = duckdb.connect(str(path))
    try:
        _write_convention_tables(
            con,
            convention=convention,
            datasets=(STATS19,) + tuple(d for d in datasets if d != STATS19),
            years=years,
            boundary_mode=boundary_mode,
            crossroads_version=crossroads_version,
            schema_version=schema_version,
            built_at_utc=built_at_utc,
            extra_meta_column=extra_meta_column,
            include_meta_row=include_meta_row,
        )

        con.execute(
            "CREATE TABLE collisions ("
            "source_row_key VARCHAR, accident_index VARCHAR, accident_year VARCHAR, "
            "easting DOUBLE, northing DOUBLE, geom_valid BOOLEAN, "
            "datetime_local TIMESTAMP, "
            "collision_severity INTEGER, weather_conditions INTEGER, "
            "enhanced_severity_collision INTEGER, "
            "speed_limit INTEGER, number_of_casualties INTEGER, longitude DOUBLE, "
            "local_authority_ons_district VARCHAR)"
        )
        collisions = []
        collision_year = {}
        for i, row in enumerate(COLLISION_ROWS):
            (key, month, day, hour, minute, easting, northing, geom_valid,
             severity, weather, enhanced, speed, casualties, longitude, district) = row
            year = years[i % len(years)]
            collision_year[key] = year
            collisions.append(
                (key, _accident_index(key, year), str(year), easting, northing,
                 geom_valid, datetime(year, month, day, hour, minute),
                 severity, weather, enhanced, speed, casualties, longitude, district)
            )
        con.executemany(
            "INSERT INTO collisions VALUES (" + ", ".join(["?"] * 14) + ")", collisions
        )

        con.execute(
            "CREATE TABLE vehicles ("
            "source_row_key VARCHAR, accident_index VARCHAR, vehicle_reference INTEGER, "
            "vehicle_type INTEGER, age_of_vehicle INTEGER, generic_make_model VARCHAR, "
            "link_valid BOOLEAN)"
        )
        vehicles = [
            (key, _accident_index(collision_key, collision_year[collision_key]),
             reference, vehicle_type, age, make_model, link_valid)
            for key, collision_key, reference, vehicle_type, age, make_model, link_valid
            in VEHICLE_ROWS
        ]
        con.executemany(
            "INSERT INTO vehicles VALUES (?, ?, ?, ?, ?, ?, ?)", vehicles
        )

        if ERA5_WEATHER in datasets:
            # Weather columns are NOT in column_manifest, exactly as upstream: the
            # manifest classifies STATS19 columns only.
            con.execute(
                "CREATE TABLE weather ("
                "source_row_key VARCHAR, datetime_utc TIMESTAMP, "
                "latitude DOUBLE, longitude DOUBLE, "
                "temperature_c DOUBLE, precipitation_mm DOUBLE, geom_valid BOOLEAN)"
            )
            weather = [
                (f"w{i}", datetime(years[i % len(years)], month, day, 12, 0),
                 51.5 + i * 0.01, -0.12 + i * 0.01, temperature, precipitation, True)
                for i, (month, day, temperature, precipitation) in enumerate(WEATHER_ROWS)
            ]
            con.executemany(
                "INSERT INTO weather VALUES (?, ?, ?, ?, ?, ?, ?)", weather
            )
            con.execute("CREATE VIEW weather_clean AS SELECT * FROM weather WHERE geom_valid")

        if AADF in datasets:
            con.execute(
                "CREATE TABLE aadf ("
                "count_point_id VARCHAR, year INTEGER, road_name VARCHAR, "
                "all_motor_vehicles DOUBLE, easting DOUBLE, northing DOUBLE, "
                "geom_valid BOOLEAN, count_valid BOOLEAN)"
            )
            aadf = [
                (count_point, years[i % len(years)], road, flow, easting, northing, True, True)
                for i, (count_point, road, flow, easting, northing) in enumerate(AADF_ROWS)
            ]
            con.executemany(
                "INSERT INTO aadf VALUES (?, ?, ?, ?, ?, ?, ?, ?)", aadf
            )
            con.execute(
                "CREATE VIEW aadf_clean AS "
                "SELECT * FROM aadf WHERE geom_valid AND count_valid"
            )

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

        # Filtered views exist on every build.
        con.execute("CREATE VIEW collisions_spatial AS SELECT * FROM collisions WHERE geom_valid")
        con.execute("CREATE VIEW vehicles_clean AS SELECT * FROM vehicles WHERE link_valid")

        # Labelled views need the codebook, so they exist only on a valid build.
        if convention == "full":
            for source, table in SOURCE_TO_TABLE.items():
                con.execute(_labelled_view_sql(table, _coded_columns(source)))
    finally:
        con.close()

    return path


# A build that is entirely valid and looks nothing like a real one. Spec §2C's test is "if
# Crossroads-UK added a dataset tomorrow, could AskCrossroads answer questions about it with
# no code change?" — this fixture is how that question gets asked of the prompt. Nothing here
# shares a single name with the standard fixture, so a prompt built from it that mentions
# `collisions` could only have got the name from hardcoded source.

UNCONVENTIONAL_CODEBOOK_ROWS = [
    ("grade", "-1", "Data missing or out of range", True),
    ("grade", "1", "Top", False),
    ("grade", "2", "Middle", False),
    ("grade", "3", "Bottom", False),
]

UNCONVENTIONAL_MANIFEST_ROWS = [
    ("widget", "widget_id", "identity", None),
    ("widget", "observed_on", "datetime", None),
    ("widget", "grade", "coded", "INTEGER"),
    ("widget", "mass_kg", "numeric", "DOUBLE"),
]


def build_unconventional_db(path):
    """A valid Crossroads-UK build whose tables are named nothing like a real one.

    The three convention tables are present and correctly shaped — spec §2C permits exactly
    those names to be assumed, and a file without them would be refused before a prompt was
    ever built. Everything else is deliberately unfamiliar.
    """
    con = duckdb.connect(str(path))
    try:
        _write_convention_tables(
            con,
            convention="full",
            datasets=(STATS19,),
            years=(2023,),
            boundary_mode="snapshot",
            crossroads_version="0.9.0",
            schema_version=4,
            built_at_utc=DEFAULT_BUILT_AT,
            codebook_rows=UNCONVENTIONAL_CODEBOOK_ROWS,
            manifest_rows=UNCONVENTIONAL_MANIFEST_ROWS,
        )
        con.execute(
            "CREATE TABLE widgets ("
            "widget_id VARCHAR, observed_on DATE, grade INTEGER, mass_kg DOUBLE)"
        )
        con.executemany(
            "INSERT INTO widgets VALUES (?, ?, ?, ?)",
            [
                ("w1", date(2023, 3, 1), 1, 4.5),
                ("w2", date(2023, 7, 14), 2, 9.25),
                ("w3", date(2023, 11, 2), 3, 1.0),
            ],
        )
        con.execute(
            "CREATE TABLE gadget_log ("
            "gadget_id VARCHAR, noted_at TIMESTAMP, note VARCHAR)"
        )
        con.execute(
            "CREATE VIEW widget_summary AS "
            "SELECT grade, COUNT(*) AS n FROM widgets GROUP BY grade"
        )
    finally:
        con.close()
    return path


@pytest.fixture
def crossroads_db_factory(tmp_path):
    """Build synthetic Crossroads-UK databases on demand, one file per call.

    Takes the same keyword arguments as build_crossroads_db and returns the path:

        db = crossroads_db_factory(datasets=("stats19", "era5_weather"))
        db = crossroads_db_factory(convention="missing")
    """
    counter = itertools.count()

    def make(**kwargs):
        path = tmp_path / f"crossroads_{next(counter)}.db"
        return build_crossroads_db(path, **kwargs)

    return make


@pytest.fixture
def crossroads_db(crossroads_db_factory):
    """A valid build with every dataset present — the default for most tests."""
    return crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER, AADF, BANK_HOLIDAYS))


@pytest.fixture
def unconventional_db(tmp_path):
    """A valid build with no familiar names in it at all."""
    return build_unconventional_db(tmp_path / "unconventional.db")


@pytest.fixture
def manifest_kinds():
    """The classifications column_manifest uses.

    A fixture rather than a plain import so that tests in subdirectories can reach it.
    pytest puts a test file's OWN directory on the import path, so `from conftest import
    MANIFEST_KINDS` does not resolve from tests/integration/ — but fixtures defined here
    are available to every test beneath tests/.
    """
    return MANIFEST_KINDS


# --- A scripted stand-in for the model ---------------------------------------------


class FakeModel:
    """A scripted stand-in for a real model client (spec §11.1).

    The default suite must be offline and deterministic, and the repair loops in a later plan
    can only be tested against replies chosen in advance. This satisfies llm.ModelClient and
    returns whatever was queued, in order.

    Queue an Exception instead of a response and it is raised — which is how a test exercises
    a malformed reply or a provider failure without a network.

    `calls` records every call in order, so a test can assert that the chart call was never
    made before the SQL succeeded (spec §11.4).
    """

    def __init__(self, sql_responses=(), chart_responses=()):
        self.sql_responses = list(sql_responses)
        self.chart_responses = list(chart_responses)
        self.calls = []

    def generate_sql(self, context, question):
        self.calls.append(("generate_sql", question))
        return self._next(self.sql_responses, "generate_sql")

    def describe_chart(self, question, sql, shape):
        self.calls.append(("describe_chart", question, sql, shape))
        return self._next(self.chart_responses, "describe_chart")

    @staticmethod
    def _next(queue, name):
        if not queue:
            raise AssertionError(
                f"{name} was called more times than this test queued replies for"
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_model():
    """A FakeModel factory: fake_model(sql_responses=[...], chart_responses=[...])."""
    return FakeModel
