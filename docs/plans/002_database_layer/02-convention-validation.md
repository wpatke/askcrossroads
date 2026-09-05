# Stage 02 — Convention Validation & Refusal

> Part of *Database Access, Convention Validation & Introspection*. You have the overview + this
> stage; implement **only** this stage. Read [00-overview.md](00-overview.md) first, in particular
> the Cross-Cutting Constraints and the Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 01 must be complete. Verify:

```bash
.venv/bin/python -c "from askcrossroads.database import open_database, DatabaseError; print('ok')" && .venv/bin/python -m pytest -q
```

Expected: prints `ok`, then a green run including `tests/test_database.py`.

If `open_database` does not exist, stop — stage 01 is not finished.

---

## Objective

Decide, by shape rather than by version, whether an opened file is a Crossroads-UK build; produce the
[spec.md §1](../../spec.md) refusal message when it is not; and wire that into the `askcrossroads`
command so a wrong file is refused on the terminal with a non-zero exit.

---

## The rules that govern this whole stage

**1. Validation is by shape, never by version.** [spec.md §2D](../../spec.md) is explicit:
`schema_version` is displayed but must **not** gate acceptance. Because structure is discovered at
runtime, a newer or older schema is not inherently unusable, and refusing on a version number would
reject working files.

**2. The column check is a subset check.** A future Crossroads-UK release that *adds* a column to one
of the three tables must keep working. Only a table that has **lost** a column we rely on is a
refusal. Getting this backwards — asserting an exact set of columns — would turn every upstream
addition into an outage.

**3. A refusal is a message, never a traceback.** The user gets a written explanation naming what was
missing and the command that produces a valid file. Spec §1 gives the shape:

```text
This file does not look like a Crossroads-UK database: no `codebook` or
`column_manifest` table was found.

AskCrossroads reads databases built by Crossroads-UK:
    pip install crossroads-uk
    crossroads
```

**4. This is the only hardcoded schema knowledge in the project.** `CONVENTION_TABLES` is the whole
of it. Nothing else in `src/` may name a table or a column.

---

## Implementation Steps

### Step 1 — Add `HOW_TO_GET_A_DATABASE` to `src/askcrossroads/config.py`

Two different messages now have to tell the user how to obtain a valid database: the configuration
failure that already exists, and the refusal added in this stage. Write it once.

Find this block in `config.py`:

```python
HOW_TO_CONFIGURE = (
    "Set these in your environment, or create a .env file in the directory you run\n"
    "askcrossroads from:\n"
    "\n"
    f"    {ENV_API_KEY}=your-key-here\n"
    f"    {ENV_DATABASE}=/path/to/your/crossroads.db\n"
    "\n"
    "See .env.example. The database is a file built by Crossroads-UK:\n"
    "\n"
    "    pip install crossroads-uk\n"
    "    crossroads\n"
)
```

Replace it with:

```python
# How a user obtains a valid database. Shown both when configuration is missing and when
# the configured file turns out not to be a Crossroads-UK build, so it is written once
# here rather than drifting apart in two places.
HOW_TO_GET_A_DATABASE = (
    "AskCrossroads reads databases built by Crossroads-UK:\n"
    "\n"
    "    pip install crossroads-uk\n"
    "    crossroads\n"
)

# Shown after any configuration problem, so the message tells the user what to do next
# rather than only what went wrong.
HOW_TO_CONFIGURE = (
    "Set these in your environment, or create a .env file in the directory you run\n"
    "askcrossroads from:\n"
    "\n"
    f"    {ENV_API_KEY}=your-key-here\n"
    f"    {ENV_DATABASE}=/path/to/your/crossroads.db\n"
    "\n"
    "See .env.example.\n"
    "\n"
) + HOW_TO_GET_A_DATABASE
```

**Expected result:** `tests/test_config.py` still passes unchanged — it asserts that the message
contains `"crossroads"`, which it still does.

### Step 2 — Add validation to `src/askcrossroads/database.py`

Append to the file. The final module for this stage is:

```python
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

    (Docstring unchanged from stage 01 — do not edit this function.)
    """
    try:
        return duckdb.connect(
            str(path),
            read_only=True,
            config={"enable_external_access": False},
        )
    except duckdb.Error as error:
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
```

**Expected result:**

```bash
.venv/bin/python -c "from askcrossroads.database import validate_conventions, NotACrossroadsBuild, CONVENTION_TABLES; print(sorted(CONVENTION_TABLES))"
```

prints `['codebook', 'column_manifest', 'crossroads_meta']`.

### Step 3 — Wire `main()` in `src/askcrossroads/app.py`

Replace the whole file with:

```python
"""FastAPI routes and the console entry point.

Nothing is served yet. ``main`` checks that the application is configured, then opens and
validates the configured database, and reports clearly when either fails. The HTTP server
is added as the application is built up.
"""

import sys

from askcrossroads.config import ConfigError, load_config
from askcrossroads.database import DatabaseError, open_database, validate_conventions


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
        connection = open_database(settings.database_path)
        try:
            validate_conventions(connection)
        finally:
            # Nothing else uses the connection yet, so it is closed either way. Later
            # work holds it open for the life of the process.
            connection.close()
    except DatabaseError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # The path is not a secret and is worth confirming back to the user. The API key is
    # never printed.
    print(f"Database: {settings.database_path}")
    print("askcrossroads is configured. The application is not implemented yet.")
    return 0
```

### Step 4 — Add an `extra_meta_column` option to `tests/conftest.py`

Rule 2 above — forward tolerance — needs a build with an *extra* column to test against. Two small
edits to `tests/conftest.py`.

First, in `_write_convention_tables`, add the parameter and change how `crossroads_meta` is created
and populated. Find:

```python
def _write_convention_tables(con, *, convention, datasets, years, boundary_mode,
                             crossroads_version, schema_version, built_at_utc):
```

and add `extra_meta_column=False` to the keyword-only parameters. Then find:

```python
    con.execute(
        "CREATE TABLE crossroads_meta ("
        "crossroads_version VARCHAR, schema_version INTEGER, "
        "built_at_utc TIMESTAMP, parameters VARCHAR)"
    )
```

and replace it with:

```python
    # `extra_meta_column` simulates a future Crossroads-UK release that adds a column.
    # A subset check must still accept the build; an exact-set check would refuse it.
    con.execute(
        "CREATE TABLE crossroads_meta ("
        "crossroads_version VARCHAR, schema_version INTEGER, "
        "built_at_utc TIMESTAMP, parameters VARCHAR"
        + (", build_host VARCHAR" if extra_meta_column else "")
        + ")"
    )
```

Then find the insert immediately below it:

```python
    con.execute(
        "INSERT INTO crossroads_meta VALUES (?, ?, ?, ?)",
        [crossroads_version, schema_version, built_at_utc, parameters],
    )
```

and replace it with a form that names its columns, so it works with or without the extra one:

```python
    con.execute(
        "INSERT INTO crossroads_meta "
        "(crossroads_version, schema_version, built_at_utc, parameters) "
        "VALUES (?, ?, ?, ?)",
        [crossroads_version, schema_version, built_at_utc, parameters],
    )
```

Second, thread the option through `build_crossroads_db`. Add `extra_meta_column=False` to its
keyword-only parameters, document it in the docstring alongside `convention`, and pass it into the
`_write_convention_tables(...)` call.

**Expected result:** `.venv/bin/python -m pytest tests/test_fixtures.py -q` is still green — the
default is `False`, so every existing fixture is byte-identical to before.

### Step 5 — Repair the two tests that this stage breaks

`main()` now opens the configured file. Two existing tests hand it fabricated bytes and expect
success. **They are not flaky and must not be skipped.**

In `tests/test_package.py`, change `test_console_script_runs` to take the `crossroads_db` fixture and
use it. Replace these two lines:

```python
def test_console_script_runs(tmp_path):
```
```python
    database = tmp_path / "crossroads.db"
    database.write_bytes(b"not a real database")
```

with:

```python
def test_console_script_runs(tmp_path, crossroads_db):
```
```python
    database = crossroads_db
```

and update the docstring's final sentence to say the database is a real synthetic build rather than a
placeholder file.

In `tests/test_config.py`, change `test_main_exits_zero_when_configured` the same way. Replace:

```python
def test_main_exits_zero_when_configured(tmp_path, monkeypatch, capsys):
    database = tmp_path / "crossroads.db"
    database.write_bytes(b"not a real database")
```

with:

```python
def test_main_exits_zero_when_configured(tmp_path, monkeypatch, capsys, crossroads_db):
    database = crossroads_db
```

The rest of both tests is unchanged, including the `monkeypatch.chdir(tmp_path)`, which is still
load-bearing: without it `main()` would read the developer's real `.env`.

Leave the `db_file` fixture at `tests/test_config.py:27` alone. It is only used by `load_config`
tests, which check a path and never open it.

### Step 6 — Add the validation tests to `tests/test_database.py`

Append to the existing file. The imports at the top become:

```python
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
```

Then append:

```python
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
```

---

## Testing & Verification

### Primary: the whole suite, offline

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -v
```

**Expected result:** green, including the two repaired tests. If
`test_console_script_runs` or `test_main_exits_zero_when_configured` fails, step 5 was not done.

### The refusal, end to end, exactly as a user would see it

Build a throwaway non-Crossroads DuckDB file and point the command at it:

```bash
cd /tmp && /Users/will/Documents/Code/AskCrossroads/.venv/bin/python -c "import duckdb; con = duckdb.connect('/tmp/not-crossroads.db'); con.execute('CREATE TABLE notes (t VARCHAR)'); con.close()"
```

```bash
cd /tmp && ANTHROPIC_API_KEY=test-key-not-a-real-credential ASKCROSSROADS_DB=/tmp/not-crossroads.db /Users/will/Documents/Code/AskCrossroads/.venv/bin/askcrossroads; echo "exit=$?"
```

**Expected result** on stderr, then `exit=1`:

```text
askcrossroads: This file does not look like a Crossroads-UK database: no `codebook`, `column_manifest` or `crossroads_meta` table was found.

AskCrossroads reads databases built by Crossroads-UK:

    pip install crossroads-uk
    crossroads
```

No traceback. Then clean up: `rm -f /tmp/not-crossroads.db`.

### Nothing about the schema leaked into `src/`

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/
```

**Expected result:** no output. `codebook`, `column_manifest` and `crossroads_meta` appear only inside
`CONVENTION_TABLES` and in docstrings; nothing else does.

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

- [ ] `config.py` defines `HOW_TO_GET_A_DATABASE`, and `HOW_TO_CONFIGURE` is built from it.
- [ ] `database.py` defines `CONVENTION_TABLES`, `NotACrossroadsBuild`, and `validate_conventions`.
- [ ] `NotACrossroadsBuild` subclasses `DatabaseError`.
- [ ] The column check is a **subset** check — an extra column is accepted.
- [ ] Each convention table is probed with a real read, not trusted from the catalogue alone.
- [ ] The refusal message names what was wrong and includes `pip install crossroads-uk`.
- [ ] `main()` opens and validates, and returns 1 with the message on stderr when either fails.
- [ ] `tests/conftest.py` takes `extra_meta_column`, defaulting to `False`, and `tests/test_fixtures.py` is still green unchanged.
- [ ] `tests/test_package.py::test_console_script_runs` and `tests/test_config.py::test_main_exits_zero_when_configured` now use the `crossroads_db` fixture and pass.
- [ ] The console script exits non-zero with the §1 message and **no traceback** for both invalid conventions and for a file that is not a database.
- [ ] The full offline suite is green.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage:**

- `src/askcrossroads/database.py` holds `CONVENTION_TABLES`, `DatabaseError`, `NotACrossroadsBuild`,
  `open_database`, and `validate_conventions`.
- `src/askcrossroads/config.py` holds `HOW_TO_GET_A_DATABASE`.
- `src/askcrossroads/app.py`'s `main()` opens and validates the configured database.
- `tests/conftest.py` accepts `extra_meta_column`.
- `tests/test_database.py` covers all three convention cases plus the console script end to end.
- The two previously-broken tests are repaired.
- Nothing is staged or committed.

**What the next stage may assume:**

- A connection that has passed `validate_conventions` has all three convention tables, each carrying
  at least its required columns, each readable. Stage 03 reads them without re-checking.
- `NotACrossroadsBuild` exists and is the right exception for "the file is not a Crossroads build",
  including for problems only discoverable once the tables are actually read.
- `config.HOW_TO_GET_A_DATABASE` is the standard tail of any such message.
- `main()` already has the shape stage 03 extends: load configuration, open the database, report and
  return 1 on `DatabaseError`, otherwise print and return 0.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `ImportError: cannot import name 'config'` in `database.py` | `from askcrossroads import config` shadowed by the `config=` keyword inside `open_database` | They do not collide — the keyword is an argument name, not a binding. If the import genuinely fails, check for a local variable named `config` in the same function |
| `test_an_extra_column_does_not_make_a_build_invalid` fails | The column check was written as set equality | Change it to a subset check. This is rule 2 of this stage and is not a preference |
| `test_a_build_with_the_wrong_shape_is_refused` reports "was found" in the message | The wrong-shape tables were treated as absent | `_convention_columns` groups by table name; a table with any column at all must appear as a key. Check the `IN (...)` placeholders match the number of names |
| `test_console_script_runs` fails with a refusal | Step 5 was skipped | Give the test the `crossroads_db` fixture. Do not revert `main()` |
| The console-script tests fail with "console script not found" | The package is not installed in `.venv`, or the tests are running under a different interpreter | `.venv/bin/python -m pip install -e ".[dev]"`, then re-run with `.venv/bin/python -m pytest` |
| The subprocess tests hang | The command is waiting on stdin, or a real `.env` in the working directory changed the path | `cwd` is `tmp_path` and the environment is passed explicitly; confirm both. The 60-second timeout will fail the test rather than hang the suite |
| `tests/test_fixtures.py` fails after step 4 | The `INSERT` was left as `VALUES (?, ?, ?, ?)` without naming its columns, so it breaks when the extra column exists | Use the named-column form given in step 4 |
| The refusal prints a traceback | An exception other than `DatabaseError` escaped — most likely a `duckdb.Error` from the readability probe | The probe must catch `duckdb.Error` and turn it into a problem string, never let it out |

**Rollback:** revert `app.py` to its stage-01 contents (config check only), revert the
`HOW_TO_CONFIGURE` block in `config.py`, remove everything after `open_database` in `database.py`,
revert the two test repairs in step 5, and remove `extra_meta_column` from `tests/conftest.py`.
Stage 01's suite then passes unchanged.
