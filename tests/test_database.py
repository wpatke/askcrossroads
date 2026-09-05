"""The connection is read-only and cannot reach outside the database (spec §5, §11.4).

These tests deliberately run statements directly on the connection, going around
everything that will later sit above it. That is the point: spec §11.4 requires the
engine-level guard to hold as if the SQL validator had been bypassed entirely.
"""

import os
import subprocess
import sysconfig
from pathlib import Path

import duckdb
import pytest

from askcrossroads import config
from askcrossroads.database import (
    CONVENTION_TABLES,
    DatabaseError,
    NotACrossroadsBuild,
    open_database,
    validate_conventions,
)


@pytest.fixture
def con(crossroads_db):
    """An open, read-only connection to a valid synthetic build."""
    connection = open_database(crossroads_db)
    yield connection
    connection.close()


# --- The file opens and can be read -----------------------------------------------


def test_a_valid_build_opens_and_can_be_read(con):
    assert con.execute("SELECT COUNT(*) FROM collisions").fetchone()[0] > 0


def test_opening_does_not_modify_the_file(crossroads_db):
    """Read-only means the file on disk is untouched, not merely that writes error."""
    before = crossroads_db.stat()
    connection = open_database(crossroads_db)
    try:
        connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()
    after = crossroads_db.stat()
    assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)


# --- Layer 1: the engine rejects every write (spec §5, §11.4) ---------------------


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE intruder (i INTEGER)",
        "INSERT INTO collisions (source_row_key) VALUES ('x')",
        "UPDATE collisions SET speed_limit = 0",
        "DELETE FROM collisions",
        "DROP TABLE collisions",
        "CREATE VIEW intruder_view AS SELECT 1",
    ],
)
def test_the_engine_rejects_every_write(con, statement):
    """Run straight at the connection, with nothing in the way.

    If any of these ever succeeds, the whole safety model in spec §5 has lost its
    backstop, and no amount of validation above it makes up for that.
    """
    with pytest.raises(duckdb.Error):
        con.execute(statement)


# --- Layer 2: the query cannot reach the filesystem (spec §5) ---------------------


def test_the_connection_cannot_read_a_file_from_disk(con, tmp_path):
    """The CSV is written first, so a failure proves the engine refused.

    Pointing at a path that does not exist would fail either way and prove nothing.
    """
    csv = tmp_path / "outside.csv"
    csv.write_text("a,b\n1,2\n")
    assert csv.is_file()
    with pytest.raises(duckdb.Error):
        con.execute(f"SELECT * FROM read_csv_auto('{csv}')")


# --- Files that cannot be opened at all -------------------------------------------


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a database")
    with pytest.raises(DatabaseError) as raised:
        open_database(path)
    assert str(path) in str(raised.value)


def test_a_path_that_does_not_exist_is_refused(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(DatabaseError) as raised:
        open_database(missing)
    assert str(missing) in str(raised.value)


def test_a_directory_is_refused(tmp_path):
    with pytest.raises(DatabaseError) as raised:
        open_database(tmp_path)
    assert str(tmp_path) in str(raised.value)


def test_no_raw_duckdb_error_escapes(tmp_path):
    """Callers handle DatabaseError. They must never have to know duckdb's own types."""
    path = tmp_path / "notes.txt"
    path.write_text("nope")
    try:
        open_database(path)
    except DatabaseError:
        pass
    except duckdb.Error as error:
        pytest.fail(f"a raw duckdb error escaped open_database: {error!r}")
    else:
        pytest.fail("open_database accepted a file that is not a database")


# --- Convention validation (spec §1, §2D, §11.4) ----------------------------------


def validate(path):
    """Open a fixture and validate it, closing the connection either way."""
    connection = open_database(path)
    try:
        validate_conventions(connection)
    finally:
        connection.close()


def refusal_for(path):
    """The refusal message for a build that is not a Crossroads-UK database."""
    with pytest.raises(NotACrossroadsBuild) as raised:
        validate(path)
    return str(raised.value)


def test_a_valid_build_is_accepted(crossroads_db):
    validate(crossroads_db)  # must not raise


@pytest.mark.parametrize(
    "datasets",
    [("stats19",), ("stats19", "era5_weather"), ("stats19", "aadf")],
)
def test_every_dataset_combination_is_accepted(crossroads_db_factory, datasets):
    """Which optional datasets a build carries has nothing to do with whether it is one."""
    validate(crossroads_db_factory(datasets=datasets))


def test_a_build_with_no_convention_tables_is_refused(crossroads_db_factory):
    message = refusal_for(crossroads_db_factory(convention="missing"))
    assert "does not look like a Crossroads-UK database" in message
    for name in CONVENTION_TABLES:
        assert name in message


def test_a_build_with_the_wrong_shape_is_refused(crossroads_db_factory):
    """Spec §11.4: wrong shape must produce the §1 message, not a crash.

    The fixture's wrong-shape build has all three tables by name with entirely different
    columns, so the message must name the columns rather than claim the tables are absent.
    """
    message = refusal_for(crossroads_db_factory(convention="wrong_shape"))
    assert "does not look like a Crossroads-UK database" in message
    assert "was found" not in message  # the tables exist; only their shape is wrong
    for column in CONVENTION_TABLES["codebook"]:
        assert column in message


@pytest.mark.parametrize("convention", ["missing", "wrong_shape"])
def test_the_refusal_says_how_to_get_a_valid_database(crossroads_db_factory, convention):
    """Refusal is not a dead end (spec §1): it names the command that produces one."""
    message = refusal_for(crossroads_db_factory(convention=convention))
    assert "pip install crossroads-uk" in message
    assert config.HOW_TO_GET_A_DATABASE in message


@pytest.mark.parametrize("convention", ["missing", "wrong_shape"])
def test_the_refusal_is_never_a_raw_duckdb_error(crossroads_db_factory, convention):
    path = crossroads_db_factory(convention=convention)
    connection = open_database(path)
    try:
        validate_conventions(connection)
    except NotACrossroadsBuild:
        pass
    except duckdb.Error as error:
        pytest.fail(f"a raw duckdb error escaped validation: {error!r}")
    else:
        pytest.fail("an invalid build was accepted")
    finally:
        connection.close()


def test_an_extra_column_does_not_make_a_build_invalid(crossroads_db_factory):
    """Spec §2D: validation is by shape, and tolerant of a build newer than this code.

    An exact-set column check would refuse this, turning every upstream addition into an
    outage. The check is a subset check for exactly this reason.
    """
    validate(crossroads_db_factory(extra_meta_column=True))


def test_an_unfamiliar_schema_version_is_still_accepted(crossroads_db_factory):
    """Spec §2D: schema_version is displayed, never used to gate acceptance."""
    validate(crossroads_db_factory(schema_version=999))
    validate(crossroads_db_factory(schema_version=1))


# --- The refusal reaches the terminal (spec §1) -----------------------------------


def run_console_script(database_path, working_directory):
    """Run the installed `askcrossroads` command against one database path.

    A subprocess rather than a call to main(), because spec §1's promise is about what
    happens when a person types the command — an exit code and a message on stderr.
    """
    script = Path(sysconfig.get_path("scripts")) / "askcrossroads"
    assert script.exists(), f"console script not found at {script}; is the package installed?"
    return subprocess.run(
        [str(script)],
        cwd=working_directory,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ.get("PATH", ""),
            "ANTHROPIC_API_KEY": "test-key-not-a-real-credential",
            "ASKCROSSROADS_DB": str(database_path),
        },
    )


@pytest.mark.parametrize("convention", ["missing", "wrong_shape"])
def test_the_command_refuses_a_non_crossroads_file(crossroads_db_factory, tmp_path, convention):
    result = run_console_script(crossroads_db_factory(convention=convention), tmp_path)
    assert result.returncode != 0
    assert "does not look like a Crossroads-UK database" in result.stderr
    assert "pip install crossroads-uk" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_command_refuses_a_file_that_is_not_a_database(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a database")
    result = run_console_script(path, tmp_path)
    assert result.returncode != 0
    assert str(path) in result.stderr
    assert "Traceback" not in result.stderr


def test_the_command_accepts_a_valid_build(crossroads_db, tmp_path):
    result = run_console_script(crossroads_db, tmp_path)
    assert result.returncode == 0, result.stderr
    assert str(crossroads_db) in result.stdout
