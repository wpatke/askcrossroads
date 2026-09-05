"""The connection is read-only and cannot reach outside the database (spec §5, §11.4).

These tests deliberately run statements directly on the connection, going around
everything that will later sit above it. That is the point: spec §11.4 requires the
engine-level guard to hold as if the SQL validator had been bypassed entirely.
"""

import duckdb
import pytest

from askcrossroads.database import DatabaseError, open_database


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
