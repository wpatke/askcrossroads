# Stage 01 — Package Skeleton & Install

> Part of *Project Foundation & Synthetic Fixtures*. You have the overview + this stage; implement
> **only** this stage. Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting
> Constraints and the Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

No prior stage. Before starting, verify the repository is in the expected state:

```bash
ls -a && git status --short
```

Expected: `CLAUDE.md`, `LICENSE`, `.gitignore`, `docs/`, `.git/` and possibly `.idea/`. There must be
**no** `pyproject.toml`, **no** `src/`, and **no** `tests/`. `git status --short` should show only
the new plan directory as untracked.

If `src/` or `pyproject.toml` already exists, stop and report it rather than overwriting.

---

## Objective

Make the repository an installable Python package with the module layout from
[spec.md §12](../../spec.md), a working console script, and a pytest configuration that separates
the fast offline suite from marked integration tests. No application behaviour.

---

## Implementation Steps

### Step 1 — Create `pyproject.toml`

Create `pyproject.toml` in the repository root with exactly this content:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "askcrossroads"
version = "0.1.0"
description = "Ask questions of a Crossroads-UK road-safety database in plain English."
requires-python = ">=3.11"
license = { file = "LICENSE" }
authors = [{ name = "wpatke" }]
keywords = ["uk", "road-safety", "stats19", "duckdb", "natural-language", "sql"]
classifiers = [
  "Development Status :: 3 - Alpha",
  "Intended Audience :: Science/Research",
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Operating System :: OS Independent",
  "Topic :: Scientific/Engineering",
]
dependencies = [
  "fastapi>=0.115",
  "uvicorn>=0.30",
  "duckdb>=1.5",
  "anthropic>=0.40",
  "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
]

[project.scripts]
askcrossroads = "askcrossroads.app:main"

[tool.hatch.build.targets.wheel]
packages = ["src/askcrossroads"]

[tool.pytest.ini_options]
testpaths = ["tests"]
# Integration tests are slow, need an Anthropic API key, and need a real Crossroads-UK
# build. They are deselected by default so the normal suite stays fast, offline and
# deterministic (spec §11.1). A person runs them deliberately with:
#   pytest -m integration
addopts = "-m 'not integration'"
markers = [
  "integration: real API calls and/or a real Crossroads-UK build (slow, needs a key and network); deselected by default, run with -m integration",
]
```

**Do not add a `readme` field.** `README.md` does not exist yet and hatchling fails the build when
the declared readme file is missing. It is added when the README is written, in later work.

The dependency floors above are minimums, not pins. If `pip install` reports a resolution conflict,
raise the offending floor to whatever resolves and record it as a deviation.

**Expected result:** the file exists. Nothing is installed yet.

### Step 2 — Create the package tree

Create the directory `src/askcrossroads/` and, inside it, the modules listed in spec §12. Every
module is a docstring and nothing else — no imports, no functions, no classes — except
`__init__.py` and `app.py`, given below in full.

`src/askcrossroads/__init__.py`:

```python
"""AskCrossroads: ask questions of a Crossroads-UK road-safety database in plain English."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Read the version back from the installed package metadata rather than repeating the
# number here, so there is exactly one place it is written (pyproject.toml). Running
# straight from a source checkout with nothing installed falls back to a marked
# placeholder rather than raising.
try:
    __version__ = _pkg_version("askcrossroads")
except PackageNotFoundError:  # not installed; no metadata to read
    __version__ = "0.0.0+unknown"
```

`src/askcrossroads/app.py`:

```python
"""FastAPI routes and the console entry point.

Nothing is served yet. ``main`` exists so the ``askcrossroads`` console script declared
in pyproject.toml resolves to something real; it gains its configuration checks and,
later, the HTTP server, as the application is built up.
"""


def main():
    """Console entry point. Returns the process exit code.

    The console script wrapper calls ``sys.exit(main())``, so the value returned here
    becomes the process exit status.
    """
    print("askcrossroads is installed. The application is not implemented yet.")
    return 0
```

The remaining modules are docstring-only. Create each with the docstring given, and no other
content:

| File | Docstring |
|---|---|
| `src/askcrossroads/config.py` | `"""Named configuration constants and startup configuration loading (spec §10)."""` |
| `src/askcrossroads/database.py` | `"""Read-only DuckDB connection, Crossroads-UK convention validation, and provenance (spec §1, §2B, §5)."""` |
| `src/askcrossroads/introspection.py` | `"""Runtime schema discovery from the database's own catalogue (spec §2A)."""` |
| `src/askcrossroads/prompt.py` | `"""Prompt assembly from discovered schema, codebook and provenance (spec §3, §6)."""` |
| `src/askcrossroads/llm.py` | `"""Thin provider interface and its Anthropic implementation (spec §9)."""` |
| `src/askcrossroads/safety.py` | `"""SQL safety validator: single-SELECT parse check and function check (spec §5)."""` |
| `src/askcrossroads/query.py` | `"""Query execution, the bounded repair loops, the row cap and cancellation (spec §4, §5, §7)."""` |

Also create the static-asset directory so the blueprint layout is complete and git will track it:

- `src/askcrossroads/static/.gitkeep` — an empty file.

**Expected result:**

```text
src/askcrossroads/
├── __init__.py
├── app.py
├── config.py
├── database.py
├── introspection.py
├── llm.py
├── prompt.py
├── query.py
├── safety.py
└── static/
    └── .gitkeep
```

### Step 3 — Create the tests directory and the package test

Create `tests/test_package.py`. Do **not** create `tests/__init__.py` — pytest's rootdir-based
collection works without it, and Crossroads-UK follows the same convention.

`tests/test_package.py`:

```python
"""The package installs, imports, and exposes a working console script."""

import importlib
import subprocess
import sysconfig
from pathlib import Path

import pytest

import askcrossroads

# The modules the repository blueprint (spec §12) requires. Listing them here means a
# module that is deleted or renamed fails a test rather than failing silently at runtime.
BLUEPRINT_MODULES = [
    "app",
    "config",
    "database",
    "introspection",
    "llm",
    "prompt",
    "query",
    "safety",
]


def test_package_imports():
    assert askcrossroads is not None


def test_version_is_a_string():
    assert isinstance(askcrossroads.__version__, str)
    assert askcrossroads.__version__ != ""


@pytest.mark.parametrize("name", BLUEPRINT_MODULES)
def test_blueprint_module_imports(name):
    assert importlib.import_module(f"askcrossroads.{name}") is not None


def test_static_directory_exists():
    static = Path(askcrossroads.__file__).parent / "static"
    assert static.is_dir()


def test_console_script_runs():
    """The installed `askcrossroads` command resolves and exits cleanly.

    The script lives in the environment's scripts directory, which is inside the
    virtual environment when the tests are run from one.
    """
    script = Path(sysconfig.get_path("scripts")) / "askcrossroads"
    assert script.exists(), f"console script not found at {script}; is the package installed?"
    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ""
```

**Expected result:** the file exists; the tests do not pass yet because nothing is installed.

### Step 4 — Create the virtual environment and install

The system `python3` on this machine is 3.9.6 and will not satisfy `requires-python = ">=3.11"`.
Use the 3.12 interpreter explicitly.

```bash
cd /Users/will/Documents/Code/AskCrossroads && /opt/homebrew/bin/python3.12 -m venv .venv
```

```bash
.venv/bin/python -m pip install --upgrade pip
```

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

**Expected result:** the install completes with `Successfully installed ...` including
`askcrossroads-0.1.0`. If the interpreter path above does not exist, find one with
`ls /opt/homebrew/bin/python3.1*` or `command -v python3.11 python3.12 python3.13`, and use any
3.11+ interpreter. Record which one you used.

### Step 5 — Verify the install by hand before running the suite

```bash
.venv/bin/python -c "import askcrossroads; print(askcrossroads.__version__)"
```

**Expected result:** prints `0.1.0`. If it prints `0.0.0+unknown`, the editable install did not
register metadata — re-run step 4 and read the output rather than proceeding.

```bash
.venv/bin/askcrossroads; echo "exit=$?"
```

**Expected result:**

```text
askcrossroads is installed. The application is not implemented yet.
exit=0
```

---

## Testing & Verification

### Primary: the suite runs green, offline, from a clean environment

```bash
cd /Users/will/Documents/Code/AskCrossroads && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -v
```

**Expected result:** all tests in `tests/test_package.py` pass — 12 tests (2 + 8 parameterised
module imports + static directory + console script). No warnings about unknown markers. The
`env -u` prefix proves the suite needs neither an API key nor a database path, which is the
guarantee in spec §11.1.

### The integration split is real, not decorative

```bash
.venv/bin/python -m pytest --collect-only -q
```

**Expected result:** lists only the `test_package.py` tests. The `addopts` deselection is active.

```bash
.venv/bin/python -m pytest -m integration
```

**Expected result:** exits reporting no tests collected (nothing is marked yet), and — this is the
part that matters — emits **no** `PytestUnknownMarkWarning`. A warning here means the `markers`
entry in `pyproject.toml` is wrong and every later integration test would be silently mis-marked.

(Stage 03 adds the first `integration`-marked test, so from that point on this command collects one
test and skips it. Zero collected is the correct answer *now*.)

### The console script's exit code comes from `main()`

This is what makes the "exit non-zero on a bad configuration" requirement achievable in the next
stage, so prove the wiring now:

```bash
.venv/bin/python -c "from askcrossroads.app import main; print(main())"
```

**Expected result:** prints the message, then `0`.

### Nothing was committed

```bash
git status --short
```

**Expected result:** new files listed as untracked (`??`), **nothing** staged. Do not run
`git add` or `git commit`.

### Stage ship-readiness checklist

- [ ] `pyproject.toml` exists, declares `version = "0.1.0"`, and has **no** `readme` field.
- [ ] All eight blueprint modules plus `__init__.py` and `static/.gitkeep` exist under `src/askcrossroads/`.
- [ ] `.venv/bin/python -m pip install -e ".[dev]"` succeeded.
- [ ] `.venv/bin/python -c "import askcrossroads; print(askcrossroads.__version__)"` prints `0.1.0`.
- [ ] `.venv/bin/askcrossroads` prints a line and exits 0.
- [ ] `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
- [ ] `pytest -m integration` produces no unknown-marker warning.
- [ ] No credential-shaped string anywhere: `grep -rniE "sk-ant" src tests pyproject.toml` returns nothing.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage:**

- `pyproject.toml` exists at the repository root with the metadata, dependencies, console script and
  pytest configuration above.
- `src/askcrossroads/` contains `__init__.py`, `app.py` (with a placeholder `main()`), seven
  docstring-only modules, and `static/.gitkeep`.
- `tests/test_package.py` exists and passes.
- `.venv/` exists, built from a 3.11+ interpreter, with the package installed in editable mode.
- The default `pytest` run is green with no environment variables set.
- Nothing is staged or committed.

**What the next stage may assume:**

- `askcrossroads.config` is importable and empty apart from its docstring — it is stage 02's to
  fill in.
- `askcrossroads.app.main()` exists, returns an integer exit code, and is reachable through the
  installed `askcrossroads` console script. Stage 02 replaces its body.
- `tests/` is a working pytest package with `testpaths` and the `integration` marker configured.
  New test files can be added with no further setup.
- The commands in the overview's *The commands every stage uses* section all work.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `pip install -e .` fails with `Package 'askcrossroads' requires a different Python` | The virtual environment was built from Python 3.9 (the system default) | Delete `.venv/` and rebuild with an explicit 3.11+ interpreter as in step 4 |
| Build fails with a readme error | A `readme` field was added to `pyproject.toml` | Remove the field; `README.md` does not exist yet |
| `__version__` prints `0.0.0+unknown` | Package metadata not installed — the editable install did not complete | Re-run `pip install -e ".[dev]"` and read its output |
| `test_console_script_runs` fails with "console script not found" | Tests were run with a Python outside `.venv` | Run pytest as `.venv/bin/python -m pytest`, never as a bare `pytest` from another environment |
| `PytestUnknownMarkWarning: Unknown pytest.mark.integration` | The `markers` list in `[tool.pytest.ini_options]` is missing or misspelled | Correct it in `pyproject.toml`; this must be fixed now, not later |
| Import of a blueprint module fails | A module file is missing or has a syntax error | Create or fix the file; each is a docstring only |

**Rollback:** every change in this stage is a new file. To undo it completely:

```bash
cd /Users/will/Documents/Code/AskCrossroads && rm -rf src tests .venv pyproject.toml askcrossroads.egg-info src/askcrossroads.egg-info
```

Nothing tracked by git is modified, so `git status` returns to showing only the plan directory.
