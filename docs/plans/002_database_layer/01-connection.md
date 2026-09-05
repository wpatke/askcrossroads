# Stage 01 — Sandboxed Connection

> Part of *Database Access, Convention Validation & Introspection*. You have the overview + this
> stage; implement **only** this stage. Read [00-overview.md](00-overview.md) first, in particular
> the Cross-Cutting Constraints and the Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

`docs/plans/001_project_foundation/` must be complete. Verify:

```bash
.venv/bin/python -c "import askcrossroads; print(askcrossroads.__version__)" && .venv/bin/python -m pytest -q
```

Expected: prints a version string, then a green run of `tests/test_package.py`,
`tests/test_config.py` and `tests/test_fixtures.py`.

`src/askcrossroads/database.py` should currently contain only its docstring:

```bash
cat src/askcrossroads/database.py
```

If `.venv/` does not exist, or `tests/conftest.py` has no `crossroads_db_factory` fixture, stop —
the previous plan is not finished.

---

## Objective

Give `database.py` a single function that opens a DuckDB file read-only with the database's own
external access switched off, and turns every failure the driver can raise into one clear
`DatabaseError` naming the path.

---

## The rule that governs this whole stage

Two of the six safety layers in [spec.md §5](../../spec.md) are established at connect time and
nowhere else:

| Layer | Mechanism | Stops |
|---|---|---|
| Read-only connection | `read_only=True` | All writes, at the engine level, regardless of what SQL reaches it. |
| No external access | `config={"enable_external_access": False}` | Any read or write of the filesystem or network from inside a query. |

Both must be set **when the connection is opened**, because `enable_external_access` cannot be
re-enabled afterwards — that is the point of setting it there. Because they are set in one function,
no caller anywhere in the project can opt out of them.

These layers are **not** duplicates of the SQL validator that arrives in later work. Spec §11.4 is
explicit: a write attempted directly on this connection must fail *as if the validator had been
bypassed entirely*. This stage is that backstop, and its tests deliberately go around everything
that will later sit above it.

---

## Implementation Steps

### Step 1 — Replace `src/askcrossroads/database.py`

Replace the file's contents entirely with:

```python
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
```

**Expected result:**

```bash
.venv/bin/python -c "from askcrossroads.database import open_database, DatabaseError; print('ok')"
```

prints `ok`.

Note what is deliberately **not** here: no check that the path exists, and no check that it is
readable. `load_config()` already guarantees both, and re-checking would be duplicate logic that can
drift. Catching the open failure covers the same ground and also covers the narrow window in which
the file disappears between configuration loading and connecting.

### Step 2 — Create `tests/test_database.py`

This file is new. Later stages add to it; write exactly this now:

```python
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
```

**A note on the write list.** `CREATE TABLE`, `INSERT`, `UPDATE`, `DELETE` and `DROP TABLE` are the
cases spec §11.4 requires and are not negotiable. `CREATE VIEW` is included because a view is a
catalogue change and a read-only connection should refuse it. If the engine turns out to permit any
one of these, **that is a finding, not a test to soften**: record it prominently in your report,
remove only that case, and say so. Do not weaken the five required cases.

---

## Testing & Verification

### Primary: the whole suite, offline

```bash
cd /Users/will/Documents/Code/AskCrossroads && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -v
```

**Expected result:** every test in `tests/test_package.py`, `tests/test_config.py`,
`tests/test_fixtures.py` and the new `tests/test_database.py` passes. Nothing that passed before this
stage may now fail.

### Just this stage's tests, with the write cases visible

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_database.py -v
```

**Expected result:** green, including six separately named `test_the_engine_rejects_every_write`
cases.

### Nothing about the schema leaked into `src/`

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -rniE "collision|weather|aadf|stats19|vehicle|casualty|codebook|manifest" src/
```

**Expected result:** matches only in module **docstrings** (`database.py` and `introspection.py`
mention the convention tables by name in prose). No match inside a function body, a constant, or a
SQL string. This stage adds no table name to `src/` at all.

### The integration suite is untouched

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest -m integration -q
```

**Expected result:** one test collected, skipped, no unknown-marker warning.

### Nothing was committed

```bash
git status --short
```

**Expected result:** `src/askcrossroads/database.py` modified and `tests/test_database.py` untracked,
**nothing staged**.

### Stage ship-readiness checklist

- [ ] `database.py` defines `DatabaseError` and `open_database` and nothing else.
- [ ] `open_database` passes both `read_only=True` and `config={"enable_external_access": False}`.
- [ ] Every `duckdb.Error` on open is re-raised as `DatabaseError` with the path in the message.
- [ ] `tests/test_database.py` proves five write statements and one filesystem read all fail.
- [ ] A text file, a missing path and a directory each raise `DatabaseError` naming the path.
- [ ] No raw `duckdb` exception escapes `open_database`.
- [ ] The full offline suite is green.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage:**

- `src/askcrossroads/database.py` holds `DatabaseError` and `open_database`.
- `tests/test_database.py` exists and passes.
- `main()` is unchanged and still accepts any readable file — the wiring happens in stage 02.
- The default `pytest` run is green with no environment variables set.
- Nothing is staged or committed.

**What the next stage may assume:**

- `open_database(path)` returns a connection on which **no write can succeed** and **no query can
  reach the filesystem**. Neither guarantee needs re-establishing or re-testing anywhere else.
- Failure to open is always `DatabaseError`, never a `duckdb` type. Stage 02 subclasses it for the
  "opened, but not a Crossroads build" case, so one `except DatabaseError` in `main()` will cover
  both.
- `tests/test_database.py` exists, with a `con` fixture giving an open read-only connection to a
  valid synthetic build.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `duckdb.InvalidInputException: unrecognized configuration parameter` | The setting name is misspelled — it is `enable_external_access`, all lower case, with underscores | Fix the key. Do not fall back to setting it after connect: it cannot be re-enabled, and setting it late leaves a window where it was on |
| `test_the_connection_cannot_read_a_file_from_disk` fails with no exception raised | External access was not actually disabled — check the `config=` dict reached `duckdb.connect` and was not swallowed by a keyword typo | Print the connection's setting: `con.execute("SELECT current_setting('enable_external_access')").fetchone()` should report `false` |
| `IOException: Cannot open ... database is locked` | Another process, or an earlier test, still holds the file open | Every test must close its connection; the `con` fixture does this in its teardown. Check for a stray `duckdb.connect` without a matching `close()` |
| `IOException: ... WAL file` on opening a fixture read-only | The fixture was written but not closed cleanly, leaving a write-ahead log a read-only connection cannot replay | The factory in `tests/conftest.py` closes its connection in a `finally` before returning; confirm that is intact |
| `test_opening_does_not_modify_the_file` fails | The engine touched the file despite `read_only=True` | This is a real finding, not a flaky test. Report it before changing anything — the read-only guarantee is the backstop for the entire safety model |
| A write statement succeeds | `read_only=True` was not passed, or was passed positionally into the wrong parameter | Pass it by keyword. Then re-run the whole parameterised write test, not just the failing case |

**Rollback:** restore `src/askcrossroads/database.py` to its docstring-only contents and delete
`tests/test_database.py`. The previous plan's suite then passes unchanged. Nothing else is touched.
