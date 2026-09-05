# Database Access, Convention Validation & Introspection — Plan Overview

> Multi-stage plan. Each implementation session gets **this overview + ONE stage file**. Read this
> first. Each stage's *End State* is the contract — if a step does not match reality, adapt the step
> and record the deviation, but deliver the End State.

Open the user's database safely, prove it is a Crossroads-UK build or refuse it usefully, and
discover everything the rest of the application needs to know about it — assuming nothing.

---

## Context & Objective

### What exists today

`docs/plans/001_project_foundation/` is complete. The repository is an installable package with a
green offline test suite and a synthetic-database factory, and **no application behaviour**:

```text
AskCrossroads/
├── pyproject.toml
├── .env.example
├── docs/
│   ├── spec.md                     # the authoritative product definition
│   └── plans/
│       ├── 001_project_foundation/ # complete
│       └── 002_database_layer/     # this plan
├── src/askcrossroads/
│   ├── __init__.py                 # __version__ from package metadata
│   ├── app.py                      # main(): loads config, prints, returns 0 or 1
│   ├── config.py                   # spec §10 bounds, env var names, load_config()
│   ├── database.py                 # DOCSTRING ONLY — this plan fills it in
│   ├── introspection.py            # DOCSTRING ONLY — this plan fills it in
│   ├── llm.py / prompt.py / query.py / safety.py   # docstring only, untouched here
│   └── static/                     # empty
└── tests/
    ├── conftest.py                 # the synthetic Crossroads-UK database factory
    ├── test_package.py
    ├── test_config.py
    ├── test_fixtures.py
    └── integration/test_conventions.py
```

Two facts from the previous plan that this one builds on directly:

- **`load_config()` already guarantees the database path exists and is readable.** It raises
  `ConfigError` otherwise. This plan therefore does **not** re-check existence or readability as a
  separate step. What it does instead is catch the failure to *open* the file, which also covers the
  narrow window in which the file is deleted or its permissions change between configuration loading
  and the connection being made.
- **`tests/conftest.py` provides `crossroads_db_factory`**, which builds a small real DuckDB file on
  disk with a chosen dataset combination and a chosen convention-table state (`full`, `missing`,
  `wrong_shape`). Every offline test in this plan uses it.

### What this plan changes

After this plan, `askcrossroads` opens the configured file, refuses it with an actionable message if
it is not a Crossroads-UK build, and otherwise reports what it found and exits cleanly. Concretely:

- `src/askcrossroads/database.py` — the read-only sandboxed connection, the convention check,
  provenance, the immutable `DatabaseContext`, and the `Database` object that owns the connection.
- `src/askcrossroads/introspection.py` — runtime structure discovery, codebook and manifest loading,
  and type-keyed temporal ranges.
- `src/askcrossroads/config.py` — one small addition: `HOW_TO_GET_A_DATABASE`, the shared tail of
  every message that has to tell the user how to obtain a valid file.
- `src/askcrossroads/app.py` — `main()` opens and validates the database, and exits non-zero with the
  spec §1 message when it is not a Crossroads-UK build.
- `tests/conftest.py` — three additions to the factory, each mirroring a real upstream table, so the
  central rule of this plan can actually be tested (see *The two traps*, below).
- `tests/test_database.py`, `tests/test_introspection.py` — new.
- `tests/test_package.py`, `tests/test_config.py` — **two existing tests must be repaired**; see
  *Two existing tests break*, below.

Still out of scope after this plan: prompt assembly, the SQL safety validator, query execution, and
the HTTP surface.

---

## Approach / Architecture

### The standing constraint this whole plan serves

[spec.md §2C](../../spec.md):

> AskCrossroads hardcodes no assumption about the schema beyond `codebook`, `column_manifest`, and
> `crossroads_meta` — those table names, their column names, and the `kind` values they use.
> Everything else is discovered from the file at runtime.

This plan is where that rule stops being a promise and becomes code. Concretely, after this plan the
**only** literal knowledge of Crossroads-UK anywhere in `src/` is one dictionary in `database.py`:

```python
CONVENTION_TABLES = {
    "codebook":        ("variable", "code", "label", "is_missing"),
    "column_manifest": ("tbl", "col", "kind", "dtype"),
    "crossroads_meta": ("crossroads_version", "schema_version", "built_at_utc", "parameters"),
}
```

No other table name, column name, dataset name, or code value may appear in `src/`. A grep for
`collision`, `weather`, `aadf` or `stats19` across `src/` must find nothing.

Two kinds of knowledge that look like they might breach that rule, and do not:

- **DuckDB's own type names** (`DATE`, `TIMESTAMP`) and **`information_schema`** are generic SQL, not
  knowledge of Crossroads-UK. Using them is how the discovery happens.
- **The manifest `kind` vocabulary** is explicitly permitted by §2C. In practice only one value,
  `coded`, is ever branched on; see *Unknown kinds*, below.

### What the database context carries: everything, unfiltered

`discover_relations` returns **every table and view `information_schema` reports** — including the
bronze `*_raw` landing tables, the audit and quality-log tables, boundary and reference tables, and
the gold views. Nothing is excluded.

This is a decision, not an oversight. Excluding relations by name pattern (`*_raw`, `*_log`) would be
hardcoded knowledge of Crossroads-UK's naming conventions, which §2C forbids, and it would break
silently the day upstream renamed something — the exact failure mode §6 exists to prevent.
[spec.md §3](../../spec.md) measured a real national build at 734 schema columns, which is the whole
catalogue, so the specification's own arithmetic assumes no filtering either.

Deciding what a *prompt* should show is a separate question, answered by later work. That work can
narrow what discovery produced; it cannot widen what discovery threw away.

### Temporal ranges: stored tables only, one query per table

[spec.md §6](../../spec.md) requires the date range of every temporal column, "computed once at load
with `MIN`/`MAX` — keyed on the column's *type*, never its name". Two limits are applied:

- **Stored tables only; views are skipped.** A view is a saved filter over a table, so its range
  either repeats the table's or is narrower by a filter the model is not reasoning about. Checking
  views as well roughly doubles the startup work for a difference that changes no answer. Telling a
  table from a view comes from `information_schema.tables.table_type`, which is standard SQL.
- **One query per table, not per column.** All of a table's temporal columns are asked about in a
  single `SELECT MIN(a), MAX(a), MIN(b), MAX(b) FROM t`. On a real build that is roughly a dozen
  round trips rather than two dozen, and it happens while the user waits for the application to
  start.

### The two traps (why `tests/conftest.py` changes)

The rule is *find date columns by type, never by name*. There are two ways to break it, and the
fixture as it stands can catch neither, so a test of the rule would pass whether the code was right
or wrong:

| Trap | What it catches | Where it comes from |
|---|---|---|
| A column of type `DATE` | Code that only looks for `TIMESTAMP` misses it entirely. | A real build has `bank_holidays.date DATE`. |
| A `VARCHAR` column with a date-like name | Code that matches on names wrongly treats text as a date. | Crossroads-UK's keep-in-place model carries every cleansed field twice, the second as a verbatim string. |

Both are added to the fixture as a `bank_holidays` table mirroring the real upstream one. A third
addition, an always-present empty `quarantine_raw` table with a `TIMESTAMP` column, covers the
empty-table case (`MIN`/`MAX` return `NULL`). All three mirror tables that genuinely exist in a real
build; none is invented for the test.

### Unknown kinds

The 001 overview left an open question for whichever plan first reads the manifest: what should the
application do when `column_manifest.kind` carries a classification it has never seen?

**Answer: nothing special, and the risk it was raised against does not exist here.** The concern was
silent data loss — columns of an unknown kind quietly ignored. But structure comes from
`information_schema`, not from the manifest, so every column of every table is discovered regardless
of how (or whether) the manifest classifies it. The manifest only *adds* a classification. The single
value anything branches on is `coded`; every other value is carried through verbatim and exposed on
the context, so later work can see exactly what a build used.

The integration guard in `tests/integration/test_conventions.py` stays as it is: it warns a developer
that a new kind has appeared upstream and should be reviewed. It is not a runtime gate.

### Decisions taken, and what was rejected

| Decision | Why | Rejected alternative |
|---|---|---|
| The convention column check is a **subset** check | A future Crossroads-UK release that *adds* a column to `crossroads_meta` must keep working. Only a table that has **lost** a column we rely on is a refusal. | Exact-set equality. Would refuse valid newer builds, which is precisely what spec §2D says not to do. |
| Readability is proved by a `SELECT * FROM <table> LIMIT 1` probe on each convention table | Spec §2D says validation asks whether the tables exist "in a readable form". A table can be in the catalogue and still unreadable. One row each is the cheapest way to find out now, with a clear message, rather than halfway through building a prompt. | Trusting `information_schema` alone. Cheaper, but leaves an unreadable table to fail later and less clearly. |
| `open_database` wraps every `duckdb.Error` in `DatabaseError` | Callers handle one exception type from this module. A missing file, a directory, a text file, and a newer storage format all become the same actionable message naming the path. | Letting `duckdb` exceptions escape. Every caller would then have to know the driver's exception hierarchy. |
| A frozen `DatabaseContext` of facts, with a separate `Database` object owning the connection | The facts are immutable and freely shareable; the connection has a lifecycle. Spec §5 requires exactly one connection, held for the process, because `interrupt()` cancels the connection rather than a query. | One object doing both. The connection's mutability would leak into the thing later work passes around. |
| Every field on the context is a **tuple** | `@dataclass(frozen=True)` only stops fields being reassigned. A list inside a frozen dataclass is still mutable, and a caller that appended to it would change what every later question sees. | Lists and dicts. Convenient, and quietly defeats the immutability the object exists to provide. |
| Codebook and manifest rows are sorted **in Python**, with an explicit key | Spec §3 requires the prompt prefix to be byte-identical for a given database. A Python sort on an explicit key cannot depend on the engine's collation or on row order. | `ORDER BY` in SQL. Almost certainly identical in practice, and one more thing outside our control. |
| An unreadable table contributes **no** temporal range, and does not stop startup | One odd audit table must not stop the application opening a file that is otherwise fine. | Failing the whole open. Turns a cosmetic problem into a refusal. |
| `main()` is wired up in stage 02 | Spec §1's promise — terminal message, non-zero exit — is only real if the installed command actually does it. An error path with no end-to-end test is the one most likely to be broken when someone finally hits it. | Deferring the wiring to the HTTP work. Leaves the §1 guarantee unproven for the length of two more plans. |

### Two existing tests break

`main()` currently accepts any readable file as a database. Once it opens and validates the file,
two tests that hand it fabricated bytes will fail. **Both must be repaired in stage 02, in the same
change that wires `main()` up.** They are not flaky and they are not to be skipped:

| File | Test | Why it breaks | Repair |
|---|---|---|---|
| `tests/test_package.py:59` | `test_console_script_runs` | Writes `b"not a real database"` and asserts exit 0. | Take the `crossroads_db` fixture and pass that real path. |
| `tests/test_config.py:203` | `test_main_exits_zero_when_configured` | Same. | Same. |

`tests/test_config.py:27` (the `db_file` fixture) is **not** affected: it is only used by
`load_config` tests, which check a path and never open it.

---

## Cross-Cutting Constraints

Every stage obeys all of these.

- **Nothing about the schema is hardcoded in `src/` beyond `CONVENTION_TABLES`** (spec §2C). No
  dataset name, no silver table name, no code value. `grep -rniE "collision|weather|aadf|stats19|
  vehicle|casualty" src/` must return nothing.
- **The API key is absolute.** Never hardcode a key, or any credential, in source, tests, docs,
  plans, comments, or commit messages — not even a truncated or obviously fake one. Nothing in this
  plan touches the key; nothing in this plan may print, log, or embed one. Use
  `"test-key-not-a-real-credential"` where a test needs a placeholder. Nothing shaped like a real
  Anthropic key may appear anywhere in the repository, in any file, for any reason.
- **Every bound is a named constant in `config.py`** (spec §10), never a literal at a call site.
- **The default test suite is offline and deterministic** (spec §11.1). No test in the default run
  may require an API key, a network connection, or a real Crossroads-UK build.
- **Layout follows the spec §12 blueprint exactly.** No new module is added. `database.py` holds the
  connection, validation and provenance; `introspection.py` holds `information_schema` discovery.
  `introspection.py` must **never import `database.py`** — the dependency runs one way only, or the
  import cycle is immediate.
- **A refusal is never a traceback.** Every path that ends in "this file is not usable" must produce
  a written message naming what was wrong and how to get a valid file. A `duckdb` exception reaching
  the user is a defect.
- **Code is simple and readable.** Plain-language comments where they earn their place. The explicit
  ten-line version over the clever two-hundred-line abstraction. Every line must be readable and
  understandable by a person before it is committed.
- **Never stage, commit, or amend anything in git.** Leave all changes in the working tree and tell
  the user what changed. This is absolute and has no exception.
- **Every stage leaves `pytest` green** before the next stage starts.
- **Python 3.11 or newer.** On the development machine the system `python3` is 3.9.6 and will not
  work. Every command assumes the existing virtual environment at `.venv/`.

### The commands every stage uses

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -m pytest
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

---

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Sandboxed connection | `open_database()` opens a file read-only with the database's own external access switched off, and turns every driver failure into one actionable `DatabaseError`. | A valid build opens and can be read. Every write attempted directly on the connection fails. A query cannot read a file from disk. A text file, a missing path and a directory each raise `DatabaseError` naming the path, and no raw `duckdb` exception escapes. `pytest` green. | — | [01-connection.md](01-connection.md) |
| 02 | Convention validation & refusal | Shape-based detection of the three convention tables, the spec §1 refusal message, and `main()` wired to open, validate, and exit non-zero on refusal. Repairs the two tests that break as a result. | A valid build validates silently. A build with no convention tables, and one whose tables have the wrong shape, both raise `NotACrossroadsBuild` carrying a message that names what was missing and points at `pip install crossroads-uk`. `askcrossroads` pointed at either exits **1** with that message on stderr. A build with an *extra* column is still accepted. `pytest` green. | 01 | [02-convention-validation.md](02-convention-validation.md) |
| 03 | Introspection & the database context | `information_schema` discovery, codebook and manifest loading, type-keyed temporal ranges over stored tables, provenance, and the immutable `DatabaseContext` assembled by `Database.open()`. Extends the fixture factory with the two traps and an empty table. | `Database.open(path)` returns an object holding one connection and one frozen context carrying provenance, every relation and column, the whole codebook and manifest, and a temporal range per date-typed column of every stored table. Two opens of the same file produce identical contexts. `askcrossroads` prints a provenance line and exits 0. `pytest` green. | 01, 02 | [03-introspection-context.md](03-introspection-context.md) |

Stages must be done in order.

---

## Global Testing & Ship

Everything below runs offline against synthetic databases. The default `pytest` run must stay usable
by anyone who clones the repository, with no key, no network and no real build (spec §11.1).

| What must be proved | How | Stage |
|---|---|---|
| The engine rejects every write, as if the SQL validator had been bypassed entirely (spec §11.4) | `tests/test_database.py` runs `CREATE`, `INSERT`, `UPDATE`, `DELETE`, `DROP` directly on the connection and asserts each raises | 01 |
| A query cannot reach the filesystem (spec §5) | `tests/test_database.py` writes a real CSV, then asserts `read_csv_auto` on it fails | 01 |
| A file that is not a database produces a message, not a traceback | `tests/test_database.py` asserts `DatabaseError` and that no `duckdb` exception escapes | 01 |
| All three convention cases behave as specified (spec §11.4) | `tests/test_database.py`, parameterised over `convention="full" / "missing" / "wrong_shape"` | 02 |
| The wrong-shape case produces the §1 message rather than a crash | `tests/test_database.py` asserts the message names the missing columns and the install command | 02 |
| A newer build with an extra column is not refused (spec §2D) | `tests/test_database.py`, using a fixture with an added `crossroads_meta` column | 02 |
| The installed command refuses a non-Crossroads file on the terminal and exits non-zero (spec §1) | `tests/test_database.py` runs the console script as a subprocess against a `missing` fixture | 02 |
| Structure is discovered, not assumed; a weatherless build yields no weather tables (spec §11.4) | `tests/test_introspection.py`, parameterised over dataset combinations | 03 |
| Date ranges are keyed on type, never on name (spec §6, §2C) | `tests/test_introspection.py`: a `DATE` column is found; a `VARCHAR` column with a date-like name is not | 03 |
| Ranges reflect the data actually present | `tests/test_introspection.py` builds for `years=(2019,)` and asserts the range falls in 2019 | 03 |
| The context is genuinely immutable and deterministic | `tests/test_introspection.py` asserts every field is a tuple, assignment raises, and two opens of one file are equal | 03 |
| A column absent from the manifest is still discovered | `tests/test_introspection.py` checks a column of an optional dataset, which the manifest never covers | 03 |
| Provenance is read and decoded, and is never used to gate acceptance (spec §2D) | `tests/test_database.py` asserts the values and that an unusual `schema_version` still opens | 03 |

**Ship-readiness for this plan** — all of the following must hold before it is considered done:

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
2. `.venv/bin/python -m pytest -m integration` still collects `tests/integration/test_conventions.py`
   and skips it cleanly with no real build present.
3. `askcrossroads` pointed at a non-Crossroads file prints the §1 message and exits 1.
4. `askcrossroads` pointed at a valid build prints a provenance line and exits 0.
5. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
6. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
7. `git status` shows the new and modified files as untracked or modified and **nothing staged**.

---

## Open Questions / Risks

- **`information_schema` is not filtered by schema.** A Crossroads-UK build puts everything in `main`,
  so in practice every relation discovered belongs to one schema. `Relation` carries its `schema`
  anyway, so two same-named relations in different schemas remain distinguishable if that ever
  becomes real. Nothing downstream currently uses the field.
- **Startup cost has not been measured against a real build.** The reasoning is that DuckDB answers
  `MIN`/`MAX` from column statistics and there are roughly a dozen stored tables carrying date
  columns, so this should be well under a second. That is an expectation, not a measurement. It is
  worth timing `askcrossroads` against a real build once, and if it is slow the fix is to narrow
  which relations are ranged, not to make the work lazy — spec §3 requires the context to be built
  once and stay fixed.
- **Boundary mode changes which columns are temporal.** In `snapshot` mode Crossroads-UK writes the
  boundary `valid_from` / `valid_to` columns as untyped-`NULL` `INTEGER` placeholders; in `temporal`
  mode they carry real `DATE` values. The type-keyed rule handles this correctly with no special
  case — they are simply not temporal in one mode and are in the other — but it is worth knowing why
  the same build parameter changes the range list.
- **A table that cannot be read contributes no range, silently.** This is deliberate (startup must
  not die for one odd audit table), but it means a genuinely broken table produces no signal. If that
  ever matters, the fix is to record the skipped relations on the context rather than to make the
  failure fatal.
- **The refusal exit code is `1`, the same as a configuration failure.** Spec §1 requires only
  "non-zero". Distinct codes would let a script tell the two apart; nothing needs that yet.
