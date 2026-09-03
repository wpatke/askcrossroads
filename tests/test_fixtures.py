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
