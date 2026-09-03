# Stage 02 — Constants & Configuration

> Part of *Project Foundation & Synthetic Fixtures*. You have the overview + this stage; implement
> **only** this stage. Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting
> Constraints and the Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 01 must be complete. Verify:

```bash
.venv/bin/python -c "import askcrossroads; print(askcrossroads.__version__)" && .venv/bin/python -m pytest -q
```

Expected: prints `0.1.0`, then a green run of `tests/test_package.py`. `src/askcrossroads/config.py`
should currently contain only its docstring.

If `.venv/` does not exist or the import fails, stop — stage 01 is not finished.

---

## Objective

Fill in `config.py` with the four named bounds from [spec.md §10](../../spec.md), the
environment-variable names, the bind host and port, and a loader that reads the API key and database
path from the environment or an untracked `.env`. Give `app.main()` its real behaviour: refuse
clearly and exit non-zero when configuration is missing.

---

## The rule that governs this whole stage

The Anthropic API key is never written to disk, never logged, never printed, never included in an
error message, and never placed into `os.environ`. It exists in memory for the session only.

Two design choices in the code below exist solely to enforce that, and neither is optional:

- The `Config` dataclass marks `api_key` as `repr=False`, so the key cannot appear in a traceback,
  a debugger frame dump, or a stray `print(config)`.
- `load_config` reads `.env` with `dotenv_values`, which **returns a dictionary**, rather than
  `load_dotenv`, which **mutates `os.environ`**. Keeping the key out of the process environment
  means it cannot be inherited by a subprocess or captured in a crash report. The Anthropic client
  is handed the key explicitly in later work.

No test in this stage may use a string that looks like a real credential. Use
`"test-key-not-a-real-credential"`. The string `sk-ant-` must not appear anywhere in the repository.

---

## Implementation Steps

### Step 1 — Write `src/askcrossroads/config.py`

Replace the file's contents entirely with:

```python
"""Named configuration constants and startup configuration loading (spec §10).

Every bound the specification promises is a named constant here, never a literal at a
call site, so each guarantee can be found with a single grep. Nothing else in the
project may carry a bound.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

# --- Bounds (spec §10) -----------------------------------------------------------
# Everything else is the API default: no temperature, no thinking configuration.

MODEL = "claude-opus-5"    # the only supported model in v1 (spec §9)
MAX_TOKENS = 16_000        # required by the API; caps thinking + response together
MAX_ATTEMPTS = 3           # per model call: initial attempt + at most 2 repairs (spec §4)
MAX_RESULT_ROWS = 5_000    # result rows returned to the browser (spec §5)

# --- Where the application listens ------------------------------------------------
# Localhost only. This is a local, single-user tool, not a server (spec §5, §9).

HOST = "127.0.0.1"
PORT = 8000

# --- Configuration sources --------------------------------------------------------

ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_DATABASE = "ASKCROSSROADS_DB"
DEFAULT_ENV_FILE = ".env"

# Shown after any configuration problem, so the message tells the user what to do next
# rather than only what went wrong.
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


class ConfigError(Exception):
    """Raised when the application is not usefully configured.

    The message is written for a person reading a terminal, and never contains the API
    key or any part of it.
    """


@dataclass(frozen=True)
class Config:
    """The configuration for one session.

    ``api_key`` is marked ``repr=False`` deliberately: it must not appear in a traceback,
    a log line, or a debugger dump. It is held in memory for the session only and is
    never written anywhere.
    """

    api_key: str = field(repr=False)
    database_path: Path


def load_config(env_file=DEFAULT_ENV_FILE, environ=None):
    """Read the API key and database path, and check the database path is usable.

    Values come from the real environment first and from ``env_file`` second, so an
    explicitly exported variable always beats a stale ``.env`` entry. ``env_file`` is
    read into a dictionary rather than into ``os.environ``, so the key never enters the
    process environment.

    ``env_file`` may be None to skip the file entirely, and ``environ`` may be a plain
    dict; both exist so tests can be hermetic.

    Returns a Config. Raises ConfigError, listing every problem at once, when the
    application cannot run.
    """
    environ = os.environ if environ is None else environ

    values = {}
    if env_file is not None:
        path = Path(env_file)
        if path.is_file():
            # dotenv_values yields None for a bare "KEY" line with no value; drop those.
            values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    # The real environment wins over the file.
    for name in (ENV_API_KEY, ENV_DATABASE):
        if environ.get(name):
            values[name] = environ[name]

    problems = []

    api_key = (values.get(ENV_API_KEY) or "").strip()
    if not api_key:
        problems.append(f"{ENV_API_KEY} is not set.")

    raw_path = (values.get(ENV_DATABASE) or "").strip()
    database_path = None
    if not raw_path:
        problems.append(f"{ENV_DATABASE} is not set.")
    else:
        database_path = Path(raw_path).expanduser()
        if not database_path.is_file():
            problems.append(f"{ENV_DATABASE} is {database_path}, which is not a file.")
        elif not os.access(database_path, os.R_OK):
            problems.append(f"{ENV_DATABASE} is {database_path}, which is not readable.")

    if problems:
        raise ConfigError("\n".join(problems) + "\n\n" + HOW_TO_CONFIGURE)

    return Config(api_key=api_key, database_path=database_path)
```

**Expected result:** `.venv/bin/python -c "from askcrossroads import config; print(config.MAX_ATTEMPTS)"`
prints `3`.

Note on the file check: this stage validates that the path **exists and is readable**, which is a
question about a file path and needs no database engine. Whether the file is actually a
Crossroads-UK build is a different question, answered by later work that opens it.

### Step 2 — Replace `main()` in `src/askcrossroads/app.py`

Replace the whole file with:

```python
"""FastAPI routes and the console entry point.

Nothing is served yet. ``main`` checks that the application is configured and reports
clearly when it is not; the HTTP server is added as the application is built up.
"""

import sys

from askcrossroads.config import ConfigError, load_config


def main():
    """Console entry point. Returns the process exit code.

    The console script wrapper calls ``sys.exit(main())``, so the value returned here
    becomes the process exit status. A configuration problem exits non-zero on the
    terminal rather than opening a browser onto an error page (spec §1).
    """
    try:
        config = load_config()
    except ConfigError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # The path is not a secret and is worth confirming back to the user. The API key is
    # never printed.
    print(f"Database: {config.database_path}")
    print("askcrossroads is configured. The application is not implemented yet.")
    return 0
```

**Expected result:** running `.venv/bin/askcrossroads` from a directory with no `.env` and with the
variables unset prints the problem list to stderr and exits 1.

### Step 3 — Create `.env.example`

Create `.env.example` in the repository root:

```text
# Copy this file to .env and fill in your own values. .env is gitignored and is
# never committed.

# Your Anthropic API key. Held in memory for the session only: AskCrossroads never
# writes it to disk, logs it, or sends it to the browser.
ANTHROPIC_API_KEY=your-key-here

# Path to the Crossroads-UK database you want to ask questions of. Build one with:
#     pip install crossroads-uk
#     crossroads
ASKCROSSROADS_DB=/path/to/your/crossroads.db
```

**This file must contain placeholders only.** No real key, no fabricated key, nothing resembling a
credential.

`.gitignore` already has `.env`, `.env.*` and `!.env.example`, so this file is tracked and `.env` is
not. Do not change `.gitignore`.

### Step 4 — Create `tests/test_config.py`

```python
"""Configuration constants, loading, and the guarantee that the key never leaks."""

import os
import re
from pathlib import Path

import pytest

from askcrossroads import config
from askcrossroads.app import main
from askcrossroads.config import Config, ConfigError, load_config

# Not a credential, and shaped so it can never be mistaken for one.
FAKE_KEY = "test-key-not-a-real-credential"

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_file(tmp_path):
    """A real file on disk to stand in for a database path.

    load_config only checks that the path exists and is readable, so the contents are
    irrelevant here.
    """
    path = tmp_path / "crossroads.db"
    path.write_bytes(b"not a real database")
    return path


# --- The bounds (spec §10) --------------------------------------------------------


def test_bounds_have_their_specified_values():
    """Each bound is checked by value, so a silent change fails a test."""
    assert config.MODEL == "claude-opus-5"
    assert config.MAX_TOKENS == 16_000
    assert config.MAX_ATTEMPTS == 3
    assert config.MAX_RESULT_ROWS == 5_000


def test_bind_target_is_localhost_only():
    assert config.HOST == "127.0.0.1"
    assert isinstance(config.PORT, int)


def test_environment_variable_names():
    assert config.ENV_API_KEY == "ANTHROPIC_API_KEY"
    assert config.ENV_DATABASE == "ASKCROSSROADS_DB"


# --- Loading ----------------------------------------------------------------------


def test_loads_from_the_environment(db_file):
    result = load_config(
        env_file=None,
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert isinstance(result, Config)
    assert result.api_key == FAKE_KEY
    assert result.database_path == db_file


def test_loads_from_an_env_file(tmp_path, db_file):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ANTHROPIC_API_KEY={FAKE_KEY}\nASKCROSSROADS_DB={db_file}\n"
    )
    result = load_config(env_file=env_file, environ={})
    assert result.api_key == FAKE_KEY
    assert result.database_path == db_file


def test_the_environment_beats_the_env_file(tmp_path, db_file):
    """A variable exported in the shell must win over a stale .env entry."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ANTHROPIC_API_KEY=from-the-file\nASKCROSSROADS_DB={db_file}\n"
    )
    result = load_config(
        env_file=env_file,
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert result.api_key == FAKE_KEY


def test_missing_env_file_is_not_an_error(db_file):
    result = load_config(
        env_file="/no/such/.env",
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert result.api_key == FAKE_KEY


def test_loading_does_not_put_the_key_into_the_process_environment(tmp_path, db_file):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ANTHROPIC_API_KEY={FAKE_KEY}\nASKCROSSROADS_DB={db_file}\n"
    )
    load_config(env_file=env_file, environ={})
    assert os.environ.get("ANTHROPIC_API_KEY") != FAKE_KEY


# --- Refusals ---------------------------------------------------------------------


def test_missing_key_is_reported_by_name(db_file):
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={"ASKCROSSROADS_DB": str(db_file)})
    assert "ANTHROPIC_API_KEY" in str(raised.value)


def test_missing_database_is_reported_by_name():
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={"ANTHROPIC_API_KEY": FAKE_KEY})
    assert "ASKCROSSROADS_DB" in str(raised.value)


def test_every_problem_is_reported_at_once():
    """One run should tell the user everything that is wrong, not the first thing."""
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={})
    message = str(raised.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "ASKCROSSROADS_DB" in message


def test_a_database_path_that_does_not_exist_is_refused(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(ConfigError) as raised:
        load_config(
            env_file=None,
            environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(missing)},
        )
    assert str(missing) in str(raised.value)


def test_a_directory_is_not_a_database(tmp_path):
    with pytest.raises(ConfigError) as raised:
        load_config(
            env_file=None,
            environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(tmp_path)},
        )
    assert "not a file" in str(raised.value)


def test_the_refusal_points_at_crossroads_uk(tmp_path):
    """Refusal is not a dead end: it names the command that produces a valid file."""
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={})
    assert "crossroads" in str(raised.value)


# --- The key never leaks ----------------------------------------------------------


def test_repr_does_not_contain_the_key(db_file):
    result = load_config(
        env_file=None,
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert FAKE_KEY not in repr(result)
    assert FAKE_KEY not in str(result)
    # The path is not a secret and should still be visible.
    assert "crossroads.db" in repr(result)


def test_an_error_raised_while_the_key_is_set_does_not_contain_it(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(ConfigError) as raised:
        load_config(
            env_file=None,
            environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(missing)},
        )
    assert FAKE_KEY not in str(raised.value)


def test_env_example_holds_placeholders_only():
    text = (REPO_ROOT / ".env.example").read_text()
    assert "ANTHROPIC_API_KEY=your-key-here" in text
    assert "ASKCROSSROADS_DB=" in text
    # Nothing shaped like a real Anthropic key.
    assert not re.search(r"sk-ant-\S", text)


# --- The console entry point ------------------------------------------------------


def test_main_exits_non_zero_when_not_configured(tmp_path, monkeypatch, capsys):
    """No key, no path, and no .env to fall back on."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ASKCROSSROADS_DB", raising=False)
    assert main() == 1
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err
    assert "ASKCROSSROADS_DB" in captured.err


def test_main_exits_zero_when_configured(tmp_path, monkeypatch, capsys):
    database = tmp_path / "crossroads.db"
    database.write_bytes(b"not a real database")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ASKCROSSROADS_DB", str(database))
    assert main() == 0
    captured = capsys.readouterr()
    assert "crossroads.db" in captured.out
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err
```

**Note on `monkeypatch.chdir(tmp_path)` in the last two tests:** it is load-bearing, not tidiness.
`main()` calls `load_config()` with its default `env_file=".env"`, which resolves relative to the
working directory. Without the `chdir`, a developer's real `.env` in the repository root would be
read and the tests would pass or fail depending on whose machine they run on.

---

## Testing & Verification

### Primary: the whole suite, offline and with no configuration present

```bash
cd /Users/will/Documents/Code/AskCrossroads && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -v
```

**Expected result:** every test in `tests/test_package.py` and `tests/test_config.py` passes.

The `env -u` prefix matters twice over here: it proves the suite needs no credential, and it proves
the configuration tests are hermetic rather than quietly reading whatever the developer happens to
have exported.

### The suite is still green when configuration IS present

A test that only passes on an unconfigured machine is a broken test. Prove both directions:

```bash
ANTHROPIC_API_KEY=test-key-not-a-real-credential ASKCROSSROADS_DB=/tmp/does-not-exist.db .venv/bin/python -m pytest -q
```

**Expected result:** green. Every configuration test passes an explicit `environ` or an explicit
`env_file`, and the two `main()` tests `chdir` away and set their own variables, so none of them can
see the values above.

### The real refusal, end to end, through the installed command

```bash
cd /tmp && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB /Users/will/Documents/Code/AskCrossroads/.venv/bin/askcrossroads; echo "exit=$?"
```

**Expected result:** the problem list and the `HOW_TO_CONFIGURE` block on stderr, then `exit=1`.
The `cd /tmp` ensures no `.env` is found.

### The configured path, end to end

```bash
cd /tmp && touch /tmp/fake-crossroads.db && ANTHROPIC_API_KEY=test-key-not-a-real-credential ASKCROSSROADS_DB=/tmp/fake-crossroads.db /Users/will/Documents/Code/AskCrossroads/.venv/bin/askcrossroads; echo "exit=$?"
```

**Expected result:**

```text
Database: /tmp/fake-crossroads.db
askcrossroads is configured. The application is not implemented yet.
exit=0
```

Then clean up: `rm -f /tmp/fake-crossroads.db`.

### No credential anywhere in the repository

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -rniE "sk-ant" --exclude-dir=.git --exclude-dir=.venv . ; echo "matches=$?"
```

**Expected result:** no output, and `matches=1` (grep's "nothing found"). A match here is a stop
condition — investigate before doing anything else.

### Nothing was committed

```bash
git status --short
```

**Expected result:** untracked and modified files listed, **nothing staged**.

### Stage ship-readiness checklist

- [ ] `config.py` defines `MODEL`, `MAX_TOKENS`, `MAX_ATTEMPTS`, `MAX_RESULT_ROWS` with the spec §10 values.
- [ ] `config.py` defines `HOST`, `PORT`, `ENV_API_KEY`, `ENV_DATABASE`, `DEFAULT_ENV_FILE`.
- [ ] `Config.api_key` is declared with `field(repr=False)`.
- [ ] `load_config` uses `dotenv_values`, not `load_dotenv` — `os.environ` is never mutated.
- [ ] `load_config` reports every problem in one error, not the first.
- [ ] `.env.example` exists with placeholders only and is tracked by git.
- [ ] `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
- [ ] The suite is also green with both variables set to throwaway values.
- [ ] `askcrossroads` exits 1 with an actionable message when unconfigured, 0 when configured.
- [ ] `grep -rniE "sk-ant"` over the repository finds nothing.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage:**

- `src/askcrossroads/config.py` holds the four bounds, the bind target, the environment-variable
  names, `ConfigError`, `Config`, and `load_config`.
- `src/askcrossroads/app.py` has a `main()` that loads configuration, prints an actionable message
  to stderr and returns 1 on failure, and returns 0 on success.
- `.env.example` exists at the repository root with placeholders only.
- `tests/test_config.py` exists and passes.
- The default `pytest` run is green with no environment variables set.
- Nothing is staged or committed.

**What the next stage may assume:**

- `from askcrossroads.config import MAX_RESULT_ROWS` and friends work; no other module in the
  project needs to carry a bound.
- `load_config()` returns a `Config` whose `database_path` **exists and is readable**. Later work
  that opens the database does not need to re-check either; it starts from "the file is there" and
  asks the different question of whether it is a Crossroads-UK build.
- `Config` is frozen, so it can be passed around freely without defensive copying.
- The test suite has an established pattern for hermetic environment handling: pass an explicit
  `environ` dict and an explicit `env_file`, or `monkeypatch.chdir` away from the repository root.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `ModuleNotFoundError: dotenv` | `python-dotenv` missing from `dependencies` in `pyproject.toml` | Add it and re-run `pip install -e ".[dev]"` |
| `TypeError: non-default argument 'database_path' follows default argument` | `field(repr=False)` was written with a default value attached | `field(repr=False)` sets no default; remove any `= None` or `= ""` |
| Configuration tests pass alone but fail in a full run, or vice versa | A test read the developer's real `.env` or a real exported variable | Every test must pass an explicit `environ` and `env_file`, or `monkeypatch.chdir` to `tmp_path` |
| `test_main_exits_non_zero_when_not_configured` fails with exit 0 | A `.env` exists in the temporary working directory, or the `delenv` calls are missing | Confirm the `chdir` and both `delenv` calls are present |
| The key appears in a test failure message | `field(repr=False)` is missing, or a test asserted on the key inside an error string | Restore `repr=False`; never assert a key's presence in output |
| `askcrossroads` exits 0 when unconfigured | `main()` is not returning the value, or the console script is stale | Re-run `pip install -e ".[dev]"`, then `python -c "from askcrossroads.app import main; print(main())"` |

**Rollback:** revert `config.py` and `app.py` to their stage-01 contents (docstring-only `config.py`;
`app.py` with the placeholder `main()` printing "not implemented yet" and returning 0), and delete
`.env.example` and `tests/test_config.py`. Stage 01's suite then passes unchanged. Nothing else is
touched.
