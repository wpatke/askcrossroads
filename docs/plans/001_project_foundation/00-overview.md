# Project Foundation & Synthetic Fixtures — Plan Overview

> Multi-stage plan. Each implementation session gets **this overview + ONE stage file**. Read this
> first. Each stage's *End State* is the contract — if a step does not match reality, adapt the step
> and record the deviation, but deliver the End State.

Stand up the installable-but-empty `askcrossroads` package, its constants and configuration
loading, and — the substance of this plan — the synthetic Crossroads-UK database factory that every
offline test in the project will depend on.

---

## Context & Objective

### What exists today

The repository contains documentation and licensing only:

```text
AskCrossroads/
├── CLAUDE.md            # contributor rules (untracked; local AI-assistant configuration)
├── LICENSE              # MIT
├── .gitignore           # already correct for this work — see below
└── docs/
    ├── ai-disclosure.md
    ├── spec.md          # the authoritative product definition
    └── plans/
        └── 001_project_foundation/   # this plan
```

There is no `pyproject.toml`, no `src/`, no `tests/`, and no virtual environment. Nothing is
importable and nothing is runnable.

`.gitignore` already covers everything this plan produces: `.env` and `.env.*` (with
`!.env.example`), `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.venv/`, `*.db`, `*.duckdb`,
`*.wal`. **Do not modify `.gitignore`.**

### What this plan changes

After this plan the repository is an installable Python package with a green test suite and a test
fixture factory, but with **no application behaviour whatsoever**. Concretely:

- `pyproject.toml` — package metadata, dependencies, the `askcrossroads` console script, and the
  pytest configuration that splits fast offline tests from marked integration tests.
- `src/askcrossroads/` — the module layout from [spec.md §12](../../spec.md), with every module
  present, importable, and empty apart from a docstring.
- `src/askcrossroads/config.py` — the four named bounds from spec §10, the environment-variable
  names, the bind host and port, and the loader that reads them.
- `.env.example` — placeholders only.
- `tests/conftest.py` — a **factory** that builds small synthetic Crossroads-UK DuckDB databases on
  demand with a chosen dataset combination and a chosen convention-table state.
- `tests/test_package.py`, `tests/test_config.py`, `tests/test_fixtures.py` — the tests that prove
  the above.

### Why the fixture factory is the point

AskCrossroads reads DuckDB databases produced by the companion project Crossroads-UK. Real builds
run to gigabytes. [spec.md §11.1](../../spec.md) requires that the default `pytest` run works
**fully offline for anyone who clones the repository** — no API key, no network, and no real build.
That is only possible if the tests construct their own small databases carrying the same convention
tables, in the same shapes, as a real build.

It is a *factory* rather than a single fixture because spec §11.4 requires tests against "builds
with different dataset combinations, including one with no weather tables and one with no AADF
tables", and spec §11.4 also requires a database that lacks the convention tables and one that has
them in the **wrong shape**. Those are parameters of one builder, not five hand-maintained files
that drift apart.

Every offline test written after this plan builds on this factory. If it is subtly wrong, every
later test is quietly testing the wrong thing — which is why it gets its own stage and its own
self-test.

---

## Approach / Architecture

### The shape of a real Crossroads-UK build

The following was read directly off a real build (`crossroads_version` 0.9.0, `schema_version` 4,
`datasets = ["stats19"]`, `years = [2023]`) and off the Crossroads-UK schema documentation. These
are the facts the fixture factory must reproduce. **They are load-bearing — do not paraphrase them
from memory.**

**The three convention tables** (spec §2B — the only structure AskCrossroads is permitted to assume):

```sql
CREATE TABLE codebook (
    variable    VARCHAR,   -- coded column name, e.g. 'collision_severity'
    code        VARCHAR,   -- the integer code AS A STRING, e.g. '1', '-1'
    label       VARCHAR,   -- the DfT English label, e.g. 'Fatal'
    is_missing  BOOLEAN    -- TRUE when the code is a missing/unknown sentinel
);

CREATE TABLE column_manifest (
    tbl         VARCHAR,   -- SOURCE name, singular: 'collision' / 'vehicle' / 'casualty'
    col         VARCHAR,   -- source column name
    kind        VARCHAR,   -- identity | geo | datetime | coded | numeric | text
    dtype       VARCHAR    -- 'INTEGER' or 'DOUBLE' for coded/numeric; NULL otherwise
);

CREATE TABLE crossroads_meta (
    crossroads_version  VARCHAR,    -- e.g. '0.9.0'
    schema_version      INTEGER,    -- e.g. 4
    built_at_utc        TIMESTAMP,
    parameters          VARCHAR     -- a JSON STRING, e.g.
                                    -- '{"boundary_mode": "snapshot", "datasets": ["stats19"], "years": [2023]}'
);
```

Five properties of that shape are easy to get wrong and each one matters:

1. **`codebook.code` is `VARCHAR`, not `INTEGER`.** Codes are stored as strings (`'1'`, `'-1'`) so
   the original is preserved exactly. Any join against a coded column casts the column to
   `VARCHAR`, never the code to an integer.
2. **`column_manifest.tbl` holds the singular *source* name, not the table name.** The manifest says
   `'collision'`; the table in the database is `collisions`. Code that joins `tbl` against
   `information_schema.tables.table_name` will match nothing.
3. **The manifest covers STATS19 columns only.** On a real build its `tbl` values are exactly
   `collision`, `vehicle`, `casualty`. Weather and AADF columns are **not in the manifest at all**.
   Anything that assumes the manifest describes every column in the database is wrong.
4. **Only some manifest `col` values appear verbatim in the silver table.** Verified on a real
   build: every `coded`, `identity`, `numeric` and `text` column appears verbatim; **no `datetime`
   or `geo` column does** — the manifest carries the upstream `date`, `time`,
   `location_easting_osgr`, `location_northing_osgr`, while the silver table carries the derived
   `datetime_local`, `easting`, `northing`.
5. **`dtype` is `NULL` for `identity`, `geo`, `datetime` and `text`**, and is `'INTEGER'` or
   `'DOUBLE'` only for `coded` and `numeric`.

**The labelled views.** For each STATS19 silver table, a `<table>_labelled` view adds a
`<col>_label` text twin for every **coded** column, produced by a left join:

```sql
CREATE VIEW collisions_labelled AS
SELECT s.*,
       cb0.label AS collision_severity_label
FROM collisions AS s
LEFT JOIN codebook AS cb0
       ON cb0.variable = 'collision_severity'
      AND cb0.code = CAST(s.collision_severity AS VARCHAR);
```

Two consequences the fixture must reproduce, because both are documented behaviour in
[spec.md §3](../../spec.md):

- A coded column with **no matching codebook variable** still gets a `_label` column, and that
  column is `NULL` for every row. On the real build `enhanced_severity_collision` is exactly this
  case: the manifest classifies it `coded`, but the codebook carries the words transposed as
  `enhanced_collision_severity`, so `enhanced_severity_collision_label` is `NULL` for all
  104,258 rows. Nothing tries to repair this by matching names fuzzily.
- A codebook variable that the manifest classifies as `numeric` (for example `speed_limit`) gets
  **no** label twin, because the view only adds twins for coded columns. A speed limit stays a
  number.

### The standing constraint this plan must not break

[spec.md §2C](../../spec.md):

> AskCrossroads hardcodes no assumption about the schema beyond `codebook`, `column_manifest`, and
> `crossroads_meta` — those table names, their column names, and the `kind` values they use.
> Everything else is discovered from the file at runtime.

In this plan that means: `src/askcrossroads/` contains **no table name, no column name, and no code
value** of any kind. The literal STATS19-shaped data written in this plan lives entirely under
`tests/`, where it is test input rather than an application assumption.

### Decisions taken, and what was rejected

| Decision | Why | Rejected alternative |
|---|---|---|
| Static `version = "0.1.0"` in `pyproject.toml` | One literal, edited by hand. No tags exist and v1 is not published, so a git-derived version would report a fallback and add a build dependency for nothing. | Deriving the version from git tags. Appropriate for a data pipeline that must pin the exact code behind a published figure; unnecessary here. Switching later is a few lines. |
| `hatchling` as the build backend | Matches Crossroads-UK, needs no `setup.py`, and handles the `src/` layout with one line. | setuptools — more configuration for the same result. |
| Fixture databases are written to a **file on disk** under pytest's `tmp_path` | Spec §5 requires `duckdb.connect(path, read_only=True)` with `enable_external_access = False`. A read-only connection needs a real file, so an in-memory fixture could never exercise the safety layer. | `:memory:` fixtures. Faster, but cannot test the guarantee that matters most. |
| Fixture data is small, hand-written literals inside `tests/` | Fast, readable, offline, and self-contained: anyone who clones the repository can run `pytest` with no sibling checkout. | Generating fixture rows from Crossroads-UK's reference CSVs. More realistic, but makes the offline suite depend on a sibling repository, which breaks the guarantee in spec §11.1. |
| The convention-table *state* (`full` / `missing` / `wrong_shape`) is a factory parameter | Spec §11.4 requires all three, and they differ only in how the convention tables are written. One builder, one place to change. | Three separate fixture functions that drift apart. |
| `load_config` reads `.env` with `dotenv_values` and never touches `os.environ` | The API key stays out of the process environment, so it cannot leak into a child process or a crash dump. The Anthropic client is passed the key explicitly in later work. | `load_dotenv()`, which mutates `os.environ` as a side effect and makes tests order-dependent. |

---

## Cross-Cutting Constraints

Every stage obeys all of these. They come from [spec.md](../../spec.md) and from the repository's
contributor rules.

- **Nothing about the schema is hardcoded in `src/`** (spec §2C, above). The only fixed knowledge
  permitted anywhere in the application is the three convention table names, their column names,
  and the `kind` values the manifest uses.
- **The API key is absolute.** Never hardcode a key, or any credential, in source, tests, docs,
  plans, comments, or commit messages — not even a truncated or obviously fake one. The key is read
  from the environment or from an untracked `.env`, held in memory for the session only, and never
  logged, printed, written to disk, included in an error message, or echoed to a client.
  `.env.example` carries `ANTHROPIC_API_KEY=your-key-here` and nothing else.
- **Every bound is a named constant in `config.py`** (spec §10), never a literal at a call site, so
  the guarantees in the spec are greppable.
- **The default test suite is offline and deterministic** (spec §11.1). No test in the default run
  may require an API key, a network connection, or a real Crossroads-UK build.
- **Layout follows the spec §12 blueprint exactly.** No module is added that the blueprint does not
  list; the console entry point is `main()` inside `app.py`, not a new `cli.py`.
- **Code is simple and readable.** Plain-language comments where they earn their place. The explicit
  ten-line version over the clever two-hundred-line abstraction. Every line must be readable and
  understandable by a person before it is committed.
- **Never stage, commit, or amend anything in git.** Leave all changes in the working tree and tell
  the user what changed. This is absolute and has no exception.
- **Every stage leaves `pytest` green** before the next stage starts.
- **Python 3.11 or newer is required.** On the development machine the system `python3` is 3.9.6 and
  **will not work**; `/opt/homebrew/bin/python3.12` is available. Every command in these plans
  assumes a virtual environment at `.venv/` in the repository root, created from a 3.11+
  interpreter.

### The commands every stage uses

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
```

```bash
.venv/bin/python -m pip install --upgrade pip
```

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

```bash
.venv/bin/python -m pytest
```

---

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Package skeleton & install | Create `pyproject.toml`, the `src/askcrossroads/` module tree from spec §12, and the pytest configuration with the `integration` marker. | `pip install -e ".[dev]"` succeeds in a clean 3.11+ virtual environment; `import askcrossroads` works; every blueprint module imports; the `askcrossroads` command resolves and exits 0; `pytest` is green. | — | [01-package-skeleton.md](01-package-skeleton.md) |
| 02 | Constants & configuration | Fill in `config.py` with the spec §10 bounds, the environment-variable names, the bind host/port, and `load_config()`. Write `.env.example`. Give `app.main()` its real "not configured" behaviour. | `askcrossroads` with no configuration prints an actionable message naming what is missing and exits **non-zero**; with valid configuration it prints a "not yet implemented" line and exits **0**. The API key never appears in a `repr`, a log line, or an error message. `pytest` is green. | 01 | [02-configuration.md](02-configuration.md) |
| 03 | Synthetic fixture factory & test split | Build the parameterised Crossroads-UK database factory in `tests/conftest.py`, the self-test proving it produces the shapes spec §2 describes, and the integration-marked guard against an unknown `kind` appearing upstream. | A test can call one factory function and get a real `.db` file on disk with the chosen dataset combination and convention-table state. The self-test proves the convention tables, the labelled view, the `is_missing` code and the uncovered-coded-column case are all correct. `pytest` is green, and `pytest -m integration` skips cleanly with no real build present. | 01, 02 | [03-fixture-factory.md](03-fixture-factory.md) |

Stages must be done in order. Stage 03 depends on 02 only for a green suite and a settled test
layout, not for any code it imports.

---

## Global Testing & Ship

There is no application behaviour in this plan, so there is no end-to-end user journey to test. What
must be provable at the end is that the **foundation is sound**:

| What must be proved | How | Stage |
|---|---|---|
| Someone who clones the repository can install it in a clean 3.11+ environment | `python3.12 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"` completes with no error | 01 |
| The package and every blueprint module import | `tests/test_package.py` imports each module by name | 01 |
| The console script is wired to `askcrossroads.app:main` and its exit code is the return value of `main()` | `tests/test_package.py` runs the installed script as a subprocess and asserts the exit code | 01, 02 |
| The default `pytest` run is fully offline and needs no key, no network and no real build | The whole suite passes with `ANTHROPIC_API_KEY` and `ASKCROSSROADS_DB` unset — see the command below | 01–03 |
| Integration tests are registered and excluded by default | `pytest --collect-only -q` collects nothing marked `integration`; `pytest -m integration --collect-only -q` collects them and emits no "unknown marker" warning | 01 |
| Every bound in spec §10 is a named constant with the specified value | `tests/test_config.py` asserts each constant's exact value | 02 |
| The API key cannot leak through a `repr` or an error message | `tests/test_config.py` asserts the key's value appears in neither | 02 |
| The fixture factory produces databases whose convention tables have the shapes spec §2 describes | `tests/test_fixtures.py` | 03 |
| The factory can produce a build with no weather tables and one with no AADF tables (spec §11.4) | `tests/test_fixtures.py`, parameterised over dataset combinations | 03 |
| The factory can produce the two invalid builds spec §11.4 requires (convention tables absent; convention tables present but wrong shape) | `tests/test_fixtures.py` | 03 |
| No classification has appeared in a real build's `column_manifest` that this project does not know about | `tests/integration/test_conventions.py`, marked `integration`; skips when `ASKCROSSROADS_TEST_DB` is unset | 03 |

The offline guarantee is checked explicitly, not assumed:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

**Ship-readiness for this plan** — all of the following must hold before it is considered done:

1. `.venv/bin/python -m pip install -e ".[dev]"` succeeds from a clean virtual environment.
2. `.venv/bin/python -c "import askcrossroads; print(askcrossroads.__version__)"` prints `0.1.0`.
3. `.venv/bin/askcrossroads` with no configuration prints the actionable message and exits non-zero.
4. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
5. `.venv/bin/python -m pytest -m integration` collects exactly one test and **skips** it when
   `ASKCROSSROADS_TEST_DB` is unset, with no unknown-marker warning. With the variable pointed at a
   real Crossroads-UK build, that test passes.
6. `grep -rniE "sk-ant|api[_-]?key\s*=\s*[\"'][^\"']" src tests docs .env.example` returns no real
   credential. The only matches should be placeholders and variable names.
7. `git status` shows the new files as untracked or modified and **nothing staged or committed**.

---

## Open Questions / Risks

- **A new `kind` value in `column_manifest` fails silently, so it is explicitly guarded.** The
  `kind` vocabulary (`identity`, `geo`, `datetime`, `coded`, `numeric`, `text`) is one of the few
  things spec §2C permits the application to know. If Crossroads-UK ever adds a seventh
  classification, nothing else in this project would fail: columns of the new kind would simply be
  ignored, with no error anywhere. That is the failure mode spec §6 exists to prevent — a clean run
  that quietly omits data — so it gets a check rather than an accepted risk. Stage 03 covers it from
  two directions: the vocabulary is written down **once**, as a single named tuple in
  `tests/conftest.py`, and `tests/integration/test_conventions.py` reads a real build and fails if
  its manifest uses anything that tuple does not list.
- **The guard only fires when someone runs it.** `tests/integration/test_conventions.py` needs a
  real Crossroads-UK build and skips without one, so drift is caught on the next deliberate
  `pytest -m integration` run rather than the moment it happens. That is the correct trade under
  spec §11.2 — the default suite must stay offline — but it means the check is worth running after
  any Crossroads-UK upgrade, not only before a release.
- **Nothing yet handles an unknown `kind` at runtime.** The guard reports drift to a developer; it
  does not tell the application what to do about it. Deciding whether an unfamiliar classification
  should be ignored, surfaced, or refused belongs to the work that reads the manifest, not to this
  plan.
- **`readme = "README.md"` must not be declared in `pyproject.toml` yet.** No `README.md` exists,
  and hatchling fails the build when the declared readme is missing. The field is added at the same
  time as the README, in later work.
- **`httpx` is not a test dependency yet.** FastAPI's `TestClient` requires it, but there are no
  routes to test in this plan. It is added by the plan that introduces the HTTP surface.
- **Dependency version floors are conservative and unverified against a resolver.** The floors given
  in stage 01 are stated as minimums, not pins. If `pip install` reports a conflict, raise the
  offending floor to whatever resolves, and record the change in the stage's deviations note.
