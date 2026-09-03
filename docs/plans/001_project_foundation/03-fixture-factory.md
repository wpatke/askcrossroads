# Stage 03 — Synthetic Fixture Factory & Test Split

> Part of *Project Foundation & Synthetic Fixtures*. You have the overview + this stage; implement
> **only** this stage. Read [00-overview.md](00-overview.md) first — in particular *The shape of a
> real Crossroads-UK build*, which this stage reproduces in miniature and which you must not
> paraphrase from memory.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stages 01 and 02 must be complete. Verify:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -q
```

Expected: green, covering `tests/test_package.py` and `tests/test_config.py`.

```bash
.venv/bin/python -c "import duckdb; print(duckdb.__version__)"
```

Expected: `1.5` or newer.

`tests/conftest.py` does not exist yet. If it does, read it before overwriting.

---

## Objective

Build a factory in `tests/conftest.py` that constructs small synthetic Crossroads-UK DuckDB
databases on demand — with a chosen dataset combination and a chosen convention-table state — and a
self-test proving the databases it produces have the shapes [spec.md §2](../../spec.md) describes.

Every offline test written after this stage depends on this factory. It gets its own self-test
because if it is subtly wrong, every later test is quietly testing the wrong thing.

This stage also adds the project's first `integration`-marked test: a guard that reads a real
Crossroads-UK build and fails if its `column_manifest` uses a classification this project does not
know about. See *The one thing that fails silently* below for why that one gets a check when the
rest of the fidelity question does not.

---

## The one thing that fails silently

Almost everything about a Crossroads-UK build is discovered at runtime (spec §2C), so an upstream
change that adds a dataset, a table, a column or a code needs no change here — that is the whole
point of the design, and it is why this stage does **not** carry a general "does the fixture still
match a real build?" test.

The `kind` vocabulary is the exception, and it is worth being precise about why.

`kind` is one of the few things spec §2C permits the application to know: `identity`, `geo`,
`datetime`, `coded`, `numeric`, `text`. Anything that branches on it — deciding which columns need
label twins, which are numbers, which are free text — is choosing from that fixed list. If
Crossroads-UK adds a seventh classification, nothing breaks. There is no error, no exception, no
failing test. Columns of the new kind are simply not recognised, and quietly drop out of whatever
the application does with them.

A clean run that silently omits data is precisely the failure mode spec §6 exists to prevent, and it
is the one kind of drift no other test in the project would notice. So it gets a check.

The check has to live in the integration suite, because answering "what does a real build use?"
requires a real build. Under spec §11.2 that means it **skips, never fails**, when no build is
available — a contributor without one gets a clean skip, not a red suite.

## What the factory must reproduce

The overview lists the properties of a real build. These are the ones the fixture exists to carry
into every later test, each with the reason it matters:

| Property | Why a later test needs it |
|---|---|
| `codebook.code` is `VARCHAR`, holding `'1'` not `1` | Any join to a coded column casts the column to `VARCHAR`. A fixture with integer codes would let a wrong join pass. |
| `codebook` contains rows with `is_missing = TRUE` | Spec §3: those codes are nulled in the silver tables, and the prompt must carry that fact or the model writes filters that match nothing. |
| A code marked `is_missing` never appears in a silver column | That is what a real build does. A fixture that violated it would make a later "NULL renders as *Not recorded*" test meaningless. |
| `column_manifest.tbl` is the singular source name (`collision`), not the table name (`collisions`) | Code that joins the manifest against `information_schema` must be caught now, not on a real build. |
| The manifest covers STATS19 columns only — weather and AADF columns are absent from it | Anything assuming the manifest describes every column is wrong, and only a fixture with an unmanifested table exposes it. |
| Only `coded`, `identity`, `numeric` and `text` manifest columns appear verbatim in the silver table; `datetime` and `geo` do not | Verified on a real build. A fixture where every manifest column resolves would hide a whole class of bug. |
| `dtype` is `NULL` except for `coded` and `numeric` | Structural fidelity; cheap to get right and confusing to get wrong. |
| One `coded` column has **no** matching codebook variable, so its `_label` twin is `NULL` for every row | Spec §3's documented exception. Nothing may repair it by fuzzy name matching. |
| A codebook variable classified `numeric` in the manifest gets **no** `_label` twin | Spec §3: a speed limit stays a number. |
| The dataset combination controls which tables exist | Spec §11.4 requires builds with no weather tables and with no AADF tables. |
| The same arguments produce byte-identical convention tables | Spec §11.4 requires a byte-stable prompt prefix. That test is impossible if the fixture is not deterministic. |

---

## Implementation Steps

### Step 1 — Write `tests/conftest.py`

Create the file with the content below. It is long because it is data; the logic is three short
functions.

```python
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
from datetime import datetime

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
                             crossroads_version, schema_version, built_at_utc):
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
    con.executemany("INSERT INTO codebook VALUES (?, ?, ?, ?)", CODEBOOK_ROWS)

    con.execute(
        "CREATE TABLE column_manifest (tbl VARCHAR, col VARCHAR, kind VARCHAR, dtype VARCHAR)"
    )
    con.executemany("INSERT INTO column_manifest VALUES (?, ?, ?, ?)", MANIFEST_ROWS)

    con.execute(
        "CREATE TABLE crossroads_meta ("
        "crossroads_version VARCHAR, schema_version INTEGER, "
        "built_at_utc TIMESTAMP, parameters VARCHAR)"
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
    con.execute(
        "INSERT INTO crossroads_meta VALUES (?, ?, ?, ?)",
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
):
    """Build a small synthetic Crossroads-UK database at `path` and return the path.

    datasets    which optional datasets are present. "stats19" is always built; add
                ERA5_WEATHER for a `weather` table and AADF for an `aadf` table.
    years       the years the collision rows fall in, cycled across the rows.
    convention  "full" (a valid build), "missing" (no convention tables at all), or
                "wrong_shape" (the three tables exist by name with the wrong columns).

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
    return crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER, AADF))


@pytest.fixture
def manifest_kinds():
    """The classifications column_manifest uses.

    A fixture rather than a plain import so that tests in subdirectories can reach it.
    pytest puts a test file's OWN directory on the import path, so `from conftest import
    MANIFEST_KINDS` does not resolve from tests/integration/ — but fixtures defined here
    are available to every test beneath tests/.
    """
    return MANIFEST_KINDS
```

**Expected result:** `.venv/bin/python -m pytest -q` still passes (nothing uses the fixture yet, and
a broken `conftest.py` would fail collection immediately, so a green run already proves the module
imports).

### Step 2 — Write `tests/test_fixtures.py`

This is the self-test: it proves the factory produces what spec §2 describes.

```python
"""The fixture factory produces databases shaped like a real Crossroads-UK build.

Every offline test in this project builds on that factory, so if it is subtly wrong,
every later test is quietly testing the wrong thing. These tests are the guard.
"""

import json

import duckdb
import pytest

from conftest import (
    AADF,
    ERA5_WEATHER,
    MANIFEST_KINDS,
    MANIFEST_ROWS,
    STATS19,
)

CONVENTION_TABLES = ("codebook", "column_manifest", "crossroads_meta")


def open_db(path):
    """Open a fixture database the way the application will (spec §5)."""
    return duckdb.connect(
        str(path), read_only=True, config={"enable_external_access": False}
    )


def columns_of(con, table):
    """{column_name: data_type} for one table or view."""
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return dict(rows)


def table_names(con):
    return {
        name
        for (name,) in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }


# --- The file opens the way the application will ----------------------------------


def test_the_database_opens_read_only_with_external_access_disabled(crossroads_db):
    con = open_db(crossroads_db)
    try:
        assert con.execute("SELECT COUNT(*) FROM collisions").fetchone()[0] > 0
    finally:
        con.close()


# --- The three convention tables (spec §2B) ---------------------------------------


def test_a_full_build_has_the_three_convention_tables(crossroads_db):
    con = open_db(crossroads_db)
    try:
        assert set(CONVENTION_TABLES).issubset(table_names(con))
    finally:
        con.close()


def test_codebook_has_the_upstream_shape(crossroads_db):
    con = open_db(crossroads_db)
    try:
        assert columns_of(con, "codebook") == {
            "variable": "VARCHAR",
            "code": "VARCHAR",   # a STRING, not an integer
            "label": "VARCHAR",
            "is_missing": "BOOLEAN",
        }
    finally:
        con.close()


def test_column_manifest_has_the_upstream_shape(crossroads_db):
    con = open_db(crossroads_db)
    try:
        assert columns_of(con, "column_manifest") == {
            "tbl": "VARCHAR",
            "col": "VARCHAR",
            "kind": "VARCHAR",
            "dtype": "VARCHAR",
        }
    finally:
        con.close()


def test_crossroads_meta_has_the_upstream_shape_and_one_row(crossroads_db):
    con = open_db(crossroads_db)
    try:
        assert columns_of(con, "crossroads_meta") == {
            "crossroads_version": "VARCHAR",
            "schema_version": "INTEGER",
            "built_at_utc": "TIMESTAMP",
            "parameters": "VARCHAR",
        }
        assert con.execute("SELECT COUNT(*) FROM crossroads_meta").fetchone()[0] == 1
    finally:
        con.close()


def test_provenance_reflects_the_build_arguments(crossroads_db_factory):
    path = crossroads_db_factory(
        datasets=(STATS19, ERA5_WEATHER),
        years=(2021, 2022),
        boundary_mode="as_at",
        crossroads_version="1.2.3",
        schema_version=7,
    )
    con = open_db(path)
    try:
        version, schema_version, _built_at, parameters = con.execute(
            "SELECT crossroads_version, schema_version, built_at_utc, parameters "
            "FROM crossroads_meta"
        ).fetchone()
        assert version == "1.2.3"
        assert schema_version == 7
        decoded = json.loads(parameters)
        assert decoded["boundary_mode"] == "as_at"
        assert decoded["datasets"] == [ERA5_WEATHER, STATS19]
        assert decoded["years"] == [2021, 2022]
    finally:
        con.close()


# --- Codebook content -------------------------------------------------------------


def test_codebook_carries_missing_sentinels(crossroads_db):
    """Spec §3: the model must know which codes were nulled during the build."""
    con = open_db(crossroads_db)
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM codebook WHERE is_missing"
        ).fetchone()[0]
        assert count >= 1
    finally:
        con.close()


def test_codes_are_stored_as_strings(crossroads_db):
    con = open_db(crossroads_db)
    try:
        codes = {
            code
            for (code,) in con.execute("SELECT DISTINCT code FROM codebook").fetchall()
        }
        assert "-1" in codes
        assert all(isinstance(code, str) for code in codes)
    finally:
        con.close()


def test_no_silver_row_carries_a_missing_sentinel(crossroads_db):
    """Upstream nulls those codes before writing the silver tables."""
    con = open_db(crossroads_db)
    try:
        leaked = con.execute(
            "SELECT COUNT(*) FROM collisions c JOIN codebook cb "
            "ON cb.variable = 'weather_conditions' "
            "AND cb.code = CAST(c.weather_conditions AS VARCHAR) "
            "WHERE cb.is_missing"
        ).fetchone()[0]
        assert leaked == 0
    finally:
        con.close()


# --- Manifest content -------------------------------------------------------------


def test_the_manifest_covers_every_kind(crossroads_db):
    con = open_db(crossroads_db)
    try:
        kinds = {
            kind
            for (kind,) in con.execute(
                "SELECT DISTINCT kind FROM column_manifest"
            ).fetchall()
        }
        assert kinds == set(MANIFEST_KINDS)
    finally:
        con.close()


def test_dtype_is_set_only_for_coded_and_numeric(crossroads_db):
    con = open_db(crossroads_db)
    try:
        rows = con.execute("SELECT kind, dtype FROM column_manifest").fetchall()
        for kind, dtype in rows:
            if kind in ("coded", "numeric"):
                assert dtype in ("INTEGER", "DOUBLE"), (kind, dtype)
            else:
                assert dtype is None, (kind, dtype)
    finally:
        con.close()


def test_manifest_tbl_is_the_source_name_not_the_table_name(crossroads_db):
    """A join from column_manifest.tbl to information_schema must match nothing."""
    con = open_db(crossroads_db)
    try:
        sources = {
            tbl for (tbl,) in con.execute("SELECT DISTINCT tbl FROM column_manifest").fetchall()
        }
        assert sources == {"collision", "vehicle"}
        assert not (sources & table_names(con))
    finally:
        con.close()


@pytest.mark.parametrize("kind", ["coded", "identity", "numeric", "text"])
def test_these_manifest_columns_appear_verbatim_in_the_silver_table(crossroads_db, kind):
    con = open_db(crossroads_db)
    try:
        for tbl, col, row_kind, _dtype in MANIFEST_ROWS:
            if row_kind != kind:
                continue
            table = {"collision": "collisions", "vehicle": "vehicles"}[tbl]
            assert col in columns_of(con, table), f"{col} missing from {table}"
    finally:
        con.close()


@pytest.mark.parametrize("kind", ["geo", "datetime"])
def test_these_manifest_columns_do_not_appear_verbatim(crossroads_db, kind):
    """Verified on a real build: the silver table carries derived columns instead."""
    con = open_db(crossroads_db)
    try:
        collision_columns = columns_of(con, "collisions")
        for tbl, col, row_kind, _dtype in MANIFEST_ROWS:
            if tbl == "collision" and row_kind == kind:
                assert col not in collision_columns, f"{col} should be derived, not verbatim"
    finally:
        con.close()


def test_the_manifest_does_not_cover_optional_datasets(crossroads_db):
    """Upstream classifies STATS19 columns only; weather and AADF are absent from it."""
    con = open_db(crossroads_db)
    try:
        manifest_columns = {
            col for (col,) in con.execute("SELECT DISTINCT col FROM column_manifest").fetchall()
        }
        assert "temperature_c" not in manifest_columns
        assert "all_motor_vehicles" not in manifest_columns
    finally:
        con.close()


# --- The labelled views (spec §3) -------------------------------------------------


def test_every_coded_column_gets_a_label_twin(crossroads_db):
    con = open_db(crossroads_db)
    try:
        labelled = columns_of(con, "collisions_labelled")
        for tbl, col, kind, _dtype in MANIFEST_ROWS:
            if tbl == "collision" and kind == "coded":
                assert f"{col}_label" in labelled
    finally:
        con.close()


def test_a_covered_coded_column_gets_real_labels(crossroads_db):
    con = open_db(crossroads_db)
    try:
        labels = con.execute(
            "SELECT DISTINCT collision_severity_label FROM collisions_labelled "
            "WHERE collision_severity_label IS NOT NULL ORDER BY 1"
        ).fetchall()
        assert ("Fatal",) in labels
        assert ("Serious",) in labels
    finally:
        con.close()


def test_an_uncovered_coded_column_has_a_null_label_for_every_row(crossroads_db):
    """Spec §3's documented exception, reproduced exactly.

    The manifest classifies enhanced_severity_collision as coded, but the codebook has no
    variable of that name, so the twin exists and is NULL throughout. Nothing repairs this
    by guessing which variable it "probably" means.
    """
    con = open_db(crossroads_db)
    try:
        total, nulls = con.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE enhanced_severity_collision_label IS NULL) "
            "FROM collisions_labelled"
        ).fetchone()
        assert total > 0
        assert nulls == total
    finally:
        con.close()


def test_a_numeric_column_gets_no_label_twin(crossroads_db):
    """The codebook has speed_limit, but the manifest says numeric, so it stays a number."""
    con = open_db(crossroads_db)
    try:
        assert "speed_limit_label" not in columns_of(con, "collisions_labelled")
    finally:
        con.close()


def test_a_coded_column_can_be_null_in_the_silver_table(crossroads_db):
    """Row c4 carries a NULL weather_conditions, so a later 'Not recorded' test has data."""
    con = open_db(crossroads_db)
    try:
        nulls = con.execute(
            "SELECT COUNT(*) FROM collisions_labelled WHERE weather_conditions_label IS NULL"
        ).fetchone()[0]
        assert nulls >= 1
    finally:
        con.close()


# --- Dataset combinations (spec §11.4) --------------------------------------------


@pytest.mark.parametrize(
    "datasets, expected_present, expected_absent",
    [
        ((STATS19,), set(), {"weather", "aadf"}),
        ((STATS19, ERA5_WEATHER), {"weather"}, {"aadf"}),
        ((STATS19, AADF), {"aadf"}, {"weather"}),
        ((STATS19, ERA5_WEATHER, AADF), {"weather", "aadf"}, set()),
    ],
)
def test_dataset_combinations_control_which_tables_exist(
    crossroads_db_factory, datasets, expected_present, expected_absent
):
    con = open_db(crossroads_db_factory(datasets=datasets))
    try:
        names = table_names(con)
        assert expected_present <= names
        assert not (expected_absent & names)
        # STATS19 is always there.
        assert {"collisions", "vehicles"} <= names
    finally:
        con.close()


def test_years_control_the_data_present(crossroads_db_factory):
    con = open_db(crossroads_db_factory(years=(2019,)))
    try:
        years = {
            year
            for (year,) in con.execute(
                "SELECT DISTINCT YEAR(datetime_local) FROM collisions"
            ).fetchall()
        }
        assert years == {2019}
    finally:
        con.close()


def test_a_temporal_column_is_discoverable_by_type(crossroads_db):
    """Spec §6: date ranges are keyed on a column's TYPE, never on its name."""
    con = open_db(crossroads_db)
    try:
        found = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE data_type = 'TIMESTAMP' AND table_name = 'collisions'"
        ).fetchall()
        assert found
    finally:
        con.close()


# --- The invalid builds (spec §11.4) ----------------------------------------------


def test_a_build_with_no_convention_tables(crossroads_db_factory):
    con = open_db(crossroads_db_factory(convention="missing"))
    try:
        names = table_names(con)
        assert not (set(CONVENTION_TABLES) & names)
        # Still a perfectly readable DuckDB file — it is simply not a Crossroads-UK build.
        assert "collisions" in names
    finally:
        con.close()


def test_a_build_whose_convention_tables_have_the_wrong_shape(crossroads_db_factory):
    con = open_db(crossroads_db_factory(convention="wrong_shape"))
    try:
        assert set(CONVENTION_TABLES).issubset(table_names(con))
        assert columns_of(con, "codebook") == {"name": "VARCHAR", "meaning": "VARCHAR"}
        assert "variable" not in columns_of(con, "codebook")
    finally:
        con.close()


def test_an_unknown_convention_state_is_rejected(crossroads_db_factory):
    with pytest.raises(ValueError):
        crossroads_db_factory(convention="nonsense")


# --- Determinism ------------------------------------------------------------------


def test_the_same_arguments_produce_identical_convention_tables(crossroads_db_factory):
    """Spec §11.4 requires a byte-stable prompt prefix; that starts here."""

    def snapshot(path):
        con = open_db(path)
        try:
            return (
                con.execute("SELECT * FROM codebook ORDER BY variable, code").fetchall(),
                con.execute("SELECT * FROM column_manifest ORDER BY tbl, col").fetchall(),
                con.execute("SELECT * FROM crossroads_meta").fetchall(),
            )
        finally:
            con.close()

    first = crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER))
    second = crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER))
    assert first != second          # two different files
    assert snapshot(first) == snapshot(second)   # with identical convention content
```

**Note on `from conftest import ...`:** pytest inserts the `tests/` directory onto `sys.path` when
there is no `tests/__init__.py`, so importing the conftest module by name works. If it does not
resolve in your environment, import the names through the fixture instead of at module level, and
record the deviation.

### Step 3 — Write `tests/integration/test_conventions.py`

Create the directory `tests/integration/` and the file below. Do **not** create
`tests/integration/__init__.py`.

This is the project's first `integration`-marked test. It needs a real Crossroads-UK build, which it
finds through `ASKCROSSROADS_TEST_DB`. That variable is **test-only** and has nothing to do with
`ASKCROSSROADS_DB` from stage 02: one points at a build to check against, the other points at the
database the application serves. Do not add `ASKCROSSROADS_TEST_DB` to `config.py`.

```python
"""The `kind` vocabulary still matches what a real Crossroads-UK build uses.

Spec §2C permits the application to know the classifications column_manifest uses, and
tests/conftest.py writes that list down once as MANIFEST_KINDS. If Crossroads-UK adds a
seventh classification upstream, nothing else in this project would notice: columns of the
new kind would simply be ignored, with no error anywhere. This test is the guard.

Marked `integration` because answering "what does a real build use?" needs a real build.
Per spec §11.2 it skips — never fails — when one is not available, so a contributor
without a build gets a clean skip rather than a red suite.
"""

import os
from pathlib import Path

import duckdb
import pytest

# Everything in this module needs a real build.
pytestmark = pytest.mark.integration

ENV_TEST_DB = "ASKCROSSROADS_TEST_DB"


@pytest.fixture
def real_build():
    """A read-only connection to a real Crossroads-UK build, or a clean skip.

    Opened the same way the application opens a database (spec §5), so this test cannot
    pass through a route the application would not use.
    """
    raw = (os.environ.get(ENV_TEST_DB) or "").strip()
    if not raw:
        pytest.skip(f"{ENV_TEST_DB} is not set; no real build to check against")

    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"{ENV_TEST_DB} is {path}, which is not a file")

    con = duckdb.connect(
        str(path), read_only=True, config={"enable_external_access": False}
    )
    try:
        has_manifest = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'column_manifest'"
        ).fetchone()[0]
        if not has_manifest:
            pytest.skip(f"{path} has no column_manifest; it is not a Crossroads-UK build")
        yield con
    finally:
        con.close()


def test_no_unknown_manifest_kind_upstream(real_build, manifest_kinds):
    """Every classification the real build uses is one this project knows about.

    Deliberately a subset check, not an equality check. A build that happens to contain no
    geo columns is entirely valid and must not fail here; what matters is that nothing NEW
    has appeared.
    """
    found = {
        kind
        for (kind,) in real_build.execute(
            "SELECT DISTINCT kind FROM column_manifest ORDER BY 1"
        ).fetchall()
    }
    unknown = found - set(manifest_kinds)
    assert not unknown, (
        f"column_manifest uses {sorted(unknown)}, which this project does not know about.\n"
        f"Known kinds: {sorted(manifest_kinds)}.\n"
        "Columns of an unrecognised kind are currently ignored without any error, so this "
        "is a silent data loss rather than a crash. Add the new kind to MANIFEST_KINDS in "
        "tests/conftest.py, then review every place that branches on kind to decide what "
        "the new classification should actually do."
    )
```

Note that the test takes `manifest_kinds` as a fixture rather than importing `MANIFEST_KINDS`
directly. pytest puts a test file's **own** directory on the import path, so `from conftest import ...`
resolves from `tests/` but not from `tests/integration/`. Fixtures declared in `tests/conftest.py`
are available to everything beneath it, which sidesteps the problem without touching `sys.path`.

**Expected result:** with no `ASKCROSSROADS_TEST_DB` set, `pytest -m integration` collects this one
test and skips it. With the variable pointing at a real build, it passes.

---

## Testing & Verification

### Primary: the self-test suite

```bash
cd /Users/will/Documents/Code/AskCrossroads && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest tests/test_fixtures.py -v
```

**Expected result:** every test passes. Read the names in the output — they are the list of
properties a real build has, and a green run is the claim that the fixture has them too.

### The whole suite, offline

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -v
```

**Expected result:** `tests/test_package.py`, `tests/test_config.py` and `tests/test_fixtures.py`
all green. No network, no key, no real build.

### The guard is excluded from the default run

The marker is what keeps the new test out of the offline suite, so prove it rather than assume it:

```bash
.venv/bin/python -m pytest --collect-only -q | grep -c "test_conventions"
```

**Expected result:** `0`. If this prints `1`, the `integration` marker is missing from
`tests/integration/test_conventions.py` and the default suite is no longer offline.

### The guard skips cleanly with no real build

```bash
env -u ASKCROSSROADS_TEST_DB .venv/bin/python -m pytest -m integration -v
```

**Expected result:** exactly one test collected, reported as `SKIPPED`, and the run passes. The skip
reason should read `ASKCROSSROADS_TEST_DB is not set; no real build to check against`.

A contributor with no Crossroads-UK build must see this, not a failure (spec §11.2). If it errors
instead of skipping, the fixture is doing work before its `pytest.skip` call.

### The guard actually runs against a real build

A real build is available on this machine, so run the test for real at least once — a guard that has
never executed is not a guard:

```bash
ASKCROSSROADS_TEST_DB=/Users/will/Documents/Code/Crossroads/stats19_test.db .venv/bin/python -m pytest -m integration -v
```

**Expected result:** one test, passed. That build's `column_manifest` uses exactly the six kinds in
`MANIFEST_KINDS`, so the subset check holds.

If that file is not present, point the variable at any Crossroads-UK build you have. If you have
none, record in your deviations note that the guard was verified as skipping but not as passing.

### The guard fails when it should

Prove the assertion can fail, or you have only tested that it does nothing. Temporarily shorten the
tuple in `tests/conftest.py`:

```python
MANIFEST_KINDS = ("identity", "geo", "datetime", "coded", "numeric")   # "text" removed
```

Re-run the command above. **Expected result:** one failure, whose message names `['text']` and
explains what to do. **Then put `"text"` back** and re-run to confirm it passes again.

Do not skip this. A test that cannot fail is worse than no test, because it reads like coverage.

### Inspect a fixture by hand once

Do this at least once rather than trusting the tests alone — you are checking that the thing looks
like a database a person would recognise:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'tests')
import duckdb, tempfile, pathlib
from conftest import build_crossroads_db, STATS19, ERA5_WEATHER, AADF
path = pathlib.Path(tempfile.mkdtemp()) / 'demo.db'
build_crossroads_db(path, datasets=(STATS19, ERA5_WEATHER, AADF))
con = duckdb.connect(str(path), read_only=True)
print(con.execute('SELECT table_name, table_type FROM information_schema.tables ORDER BY 2, 1').fetchdf())
print(con.execute('SELECT collision_severity, collision_severity_label, weather_conditions_label, enhanced_severity_collision_label FROM collisions_labelled ORDER BY 1').fetchdf())
print(con.execute('SELECT * FROM crossroads_meta').fetchdf())
"
```

**Expected result:** a table listing showing `collisions`, `vehicles`, `weather`, `aadf`, the three
convention tables and the views; a labelled result where `collision_severity_label` reads
*Fatal* / *Serious* / *Slight*, one `weather_conditions_label` is `None`, and
`enhanced_severity_collision_label` is `None` on every row; and a single provenance row.

### No leftover database files in the repository

`tmp_path` cleans up after itself, but confirm nothing was written into the working tree:

```bash
git status --short && ls *.db *.duckdb *.wal 2>/dev/null
```

**Expected result:** `git status` shows only the intended new and modified files; the `ls` finds
nothing. (`.gitignore` would hide a stray `.db` from git, which is exactly why this is checked
directly.)

### Nothing was committed

```bash
git status --short
```

**Expected result:** untracked and modified files listed, **nothing staged**.

### Stage ship-readiness checklist

- [ ] `tests/conftest.py` defines `build_crossroads_db`, `crossroads_db_factory` and `crossroads_db`.
- [ ] `MANIFEST_KINDS` is defined once and contains all six kinds.
- [ ] The factory writes a real file on disk and closes its connection before returning.
- [ ] `datasets` controls whether `weather` and `aadf` exist.
- [ ] `convention` supports `"full"`, `"missing"` and `"wrong_shape"`, and rejects anything else.
- [ ] `codebook.code` is `VARCHAR` and contains at least one `is_missing = TRUE` row.
- [ ] No silver row carries a code marked `is_missing`.
- [ ] `column_manifest.tbl` holds source names that match no table name in the database.
- [ ] One coded column has no codebook variable, and its `_label` twin is `NULL` for every row.
- [ ] `speed_limit` is in the codebook, is `numeric` in the manifest, and has no `_label` twin.
- [ ] Weather and AADF columns are absent from the manifest.
- [ ] Two builds with the same arguments have identical convention-table contents.
- [ ] `tests/conftest.py` exposes `MANIFEST_KINDS` through a `manifest_kinds` fixture.
- [ ] `tests/integration/test_conventions.py` exists, carries `pytestmark = pytest.mark.integration`,
      and has no `__init__.py` beside it.
- [ ] `pytest --collect-only -q` does not list the guard — it is excluded from the default run.
- [ ] With `ASKCROSSROADS_TEST_DB` unset, `pytest -m integration` reports one SKIPPED test and passes.
- [ ] With `ASKCROSSROADS_TEST_DB` pointed at a real build, the guard runs and passes.
- [ ] The guard was seen to fail with a kind removed from `MANIFEST_KINDS`, and the tuple was restored.
- [ ] `ASKCROSSROADS_TEST_DB` does **not** appear in `src/askcrossroads/config.py`.
- [ ] `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
- [ ] No table, column or code name from `tests/` appears anywhere in `src/` — check with
      `grep -rn "collision_severity\|codebook\|collisions" src/`, which must return nothing.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage:**

- `tests/conftest.py` provides `build_crossroads_db(path, **kwargs)` and the two fixtures
  `crossroads_db_factory` and `crossroads_db`.
- `tests/test_fixtures.py` proves the factory's output matches the shapes spec §2 describes.
- `tests/integration/test_conventions.py` guards the `kind` vocabulary against upstream drift. It is
  the project's first `integration`-marked test.
- The default `pytest` run is green, fully offline, and needs no API key and no real build.
- `pytest -m integration` collects exactly one test, which skips cleanly without a real build and
  passes against one.
- `src/` still contains no schema knowledge of any kind.
- Nothing is staged or committed.

**What later work may assume:**

- A test can obtain a real Crossroads-UK-shaped `.db` file on disk in one line:

  ```python
  def test_something(crossroads_db):
      ...

  def test_something_else(crossroads_db_factory):
      weatherless = crossroads_db_factory(datasets=("stats19",))
      broken = crossroads_db_factory(convention="missing")
      wrong = crossroads_db_factory(convention="wrong_shape")
  ```

- Those files open with `duckdb.connect(path, read_only=True, config={"enable_external_access": False})`
  — the exact mode spec §5 requires — so the safety layer can be tested against them.
- The fixtures cover, without further work: a build with no weather tables, a build with no AADF
  tables, a coded column with a `NULL` value, a coded column the codebook does not cover, a codebook
  variable that is numeric in the manifest, `is_missing` sentinel codes, a table absent from the
  manifest, temporal columns discoverable by type, and two invalid builds.
- The factory is deterministic for a given set of arguments, which is the precondition for the
  byte-stable prompt-prefix test spec §11.4 requires.
- `tests/integration/` exists and the `integration` marker is proven end to end — collected under
  `-m integration`, excluded from the default run, and skipping rather than failing when its
  prerequisite is absent. Later integration tests can follow the same pattern with no new setup.
- `MANIFEST_KINDS` is reachable from any test through the `manifest_kinds` fixture, including from
  `tests/integration/`.
- The `kind` vocabulary is checked against reality, but **only when someone runs
  `pytest -m integration`**. Work that branches on `kind` still has to decide what an unrecognised
  classification should do at runtime; the guard reports drift to a developer, it does not handle it
  in the application.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `ImportError: cannot import name 'build_crossroads_db' from 'conftest'` | `tests/__init__.py` exists, so `tests/` is a package and the bare name does not resolve | Delete `tests/__init__.py`; pytest does not need it |
| `duckdb.IOException: Could not set lock on file` | A previous connection was not closed | The `finally: con.close()` in `build_crossroads_db` is required, not optional — confirm it is there |
| `Catalog Error: Table with name codebook does not exist` when building a labelled view | Labelled views were built for a `missing` or `wrong_shape` build | Guard the view creation with `if convention == "full"` |
| `test_the_manifest_covers_every_kind` fails | A kind was dropped from `MANIFEST_ROWS`, or `MANIFEST_KINDS` was edited | Both must agree; the manifest rows are the source of truth for the fixture |
| `test_an_uncovered_coded_column_has_a_null_label_for_every_row` fails | An `enhanced_severity_collision` variable was added to `CODEBOOK_ROWS` | Remove it. Its absence is the point — it reproduces spec §3's documented exception |
| `test_no_silver_row_carries_a_missing_sentinel` fails | A silver row was given a code whose `is_missing` is TRUE | Change the row's value or the code's flag; a real build never writes those codes |
| Determinism test fails | Something time-varying reached the convention tables | `built_at_utc` must default to the fixed `DEFAULT_BUILT_AT`, and `parameters` must be serialised with `sort_keys=True` |
| Stray `.db` files appear in the repository root | A test called `build_crossroads_db` with a relative path instead of using the fixture | Always go through `crossroads_db_factory`, which writes into pytest's `tmp_path` |
| The guard runs in the default suite | `pytestmark = pytest.mark.integration` missing from `tests/integration/test_conventions.py` | Add it; the offline guarantee in spec §11.1 depends on it |
| `fixture 'manifest_kinds' not found` | The fixture was added to the wrong file, or to `tests/integration/` instead of `tests/conftest.py` | It must live in `tests/conftest.py`, which is visible to every test beneath `tests/` |
| `ModuleNotFoundError: conftest` from `tests/integration/` | The guard imports `MANIFEST_KINDS` directly instead of taking the `manifest_kinds` fixture | pytest only puts a test file's own directory on the import path; use the fixture |
| The guard fails on a valid build that lacks a kind | The assertion was written as equality rather than a subset check | A build with no geo columns is valid; only a **new** kind is a problem |
| The guard errors instead of skipping when `ASKCROSSROADS_TEST_DB` is unset | The fixture opens the database before calling `pytest.skip` | Check the variable and the path first, connect last |
| `ASKCROSSROADS_TEST_DB` ends up in `config.py` | It was mistaken for application configuration | It is test-only; `config.py` knows about `ASKCROSSROADS_DB` and nothing else |

**Rollback:** delete `tests/conftest.py`, `tests/test_fixtures.py` and `tests/integration/`.
Stages 01 and 02 are untouched and their suites still pass. Nothing in `src/` was modified in this
stage.
