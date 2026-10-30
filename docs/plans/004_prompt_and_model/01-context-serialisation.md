# Stage 01 — Deterministic Context Serialisation

> Part of *Prompt Assembly & Model Interface*. You have the overview + this stage; implement **only**
> this stage.
> Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting Constraints and the
> Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

`docs/plans/001_project_foundation/`, `002_database_layer/` and `003_safety_validator/` must be
complete. Verify:

```bash
.venv/bin/python -m pytest -q
```

Expected: a green run including `tests/test_database.py`, `tests/test_introspection.py` and
`tests/test_safety.py`.

`src/askcrossroads/prompt.py` should currently contain only its docstring:

```bash
cat src/askcrossroads/prompt.py
```

Expected: one line —
`"""Prompt assembly from discovered schema, codebook and provenance (spec §3, §6)."""`

If `.venv/` does not exist, or `tests/test_safety.py` is absent, stop — a previous plan is not
finished.

---

## Objective

Give `prompt.py` one pure function, `serialise_context(context)`, that renders a `DatabaseContext`
into a single string — the cacheable prefix. Its output must be byte-identical for a given database
and must contain the whole payload spec §3 and §6 require. Add the test fixture that proves nothing
about the schema was hardcoded.

---

## Implementation Steps

### 1. Extend the fixture factory so the convention tables can carry different content

**File:** `tests/conftest.py`

The unconventional-build fixture in step 2 needs its own codebook and manifest, so that a test can
assert the standard fixture's names appear *nowhere* in a prefix built from it. Change
`_write_convention_tables` to take the rows as arguments, defaulting to today's behaviour.

Change the signature from:

```python
def _write_convention_tables(con, *, convention, datasets, years, boundary_mode,
                             crossroads_version, schema_version, built_at_utc,
                             extra_meta_column=False, include_meta_row=True):
```

to:

```python
def _write_convention_tables(con, *, convention, datasets, years, boundary_mode,
                             crossroads_version, schema_version, built_at_utc,
                             extra_meta_column=False, include_meta_row=True,
                             codebook_rows=CODEBOOK_ROWS, manifest_rows=MANIFEST_ROWS):
```

and replace the two `executemany` calls in its body so they insert `codebook_rows` and
`manifest_rows` rather than the module constants directly.

**Expected result:** every existing caller is unchanged and every existing test still passes.

### 2. Add the unconventionally-named build

**File:** `tests/conftest.py`

Append, after `build_crossroads_db` and before the fixtures:

```python
# A build that is entirely valid and looks nothing like a real one. Spec §2C's test is "if
# Crossroads-UK added a dataset tomorrow, could AskCrossroads answer questions about it with
# no code change?" — this fixture is how that question gets asked of the prompt. Nothing here
# shares a single name with the standard fixture, so a prompt built from it that mentions
# `collisions` could only have got the name from hardcoded source.

UNCONVENTIONAL_CODEBOOK_ROWS = [
    ("grade", "-1", "Data missing or out of range", True),
    ("grade", "1", "Top", False),
    ("grade", "2", "Middle", False),
    ("grade", "3", "Bottom", False),
]

UNCONVENTIONAL_MANIFEST_ROWS = [
    ("widget", "widget_id", "identity", None),
    ("widget", "observed_on", "datetime", None),
    ("widget", "grade", "coded", "INTEGER"),
    ("widget", "mass_kg", "numeric", "DOUBLE"),
]


def build_unconventional_db(path):
    """A valid Crossroads-UK build whose tables are named nothing like a real one.

    The three convention tables are present and correctly shaped — spec §2C permits exactly
    those names to be assumed, and a file without them would be refused before a prompt was
    ever built. Everything else is deliberately unfamiliar.
    """
    con = duckdb.connect(str(path))
    try:
        _write_convention_tables(
            con,
            convention="full",
            datasets=(STATS19,),
            years=(2023,),
            boundary_mode="snapshot",
            crossroads_version="0.9.0",
            schema_version=4,
            built_at_utc=DEFAULT_BUILT_AT,
            codebook_rows=UNCONVENTIONAL_CODEBOOK_ROWS,
            manifest_rows=UNCONVENTIONAL_MANIFEST_ROWS,
        )
        con.execute(
            "CREATE TABLE widgets ("
            "widget_id VARCHAR, observed_on DATE, grade INTEGER, mass_kg DOUBLE)"
        )
        con.executemany(
            "INSERT INTO widgets VALUES (?, ?, ?, ?)",
            [
                ("w1", date(2023, 3, 1), 1, 4.5),
                ("w2", date(2023, 7, 14), 2, 9.25),
                ("w3", date(2023, 11, 2), 3, 1.0),
            ],
        )
        con.execute(
            "CREATE TABLE gadget_log ("
            "gadget_id VARCHAR, noted_at TIMESTAMP, note VARCHAR)"
        )
        con.execute(
            "CREATE VIEW widget_summary AS "
            "SELECT grade, COUNT(*) AS n FROM widgets GROUP BY grade"
        )
    finally:
        con.close()
    return path


@pytest.fixture
def unconventional_db(tmp_path):
    """A valid build with no familiar names in it at all."""
    return build_unconventional_db(tmp_path / "unconventional.db")
```

`date` and `duckdb` are already imported at the top of the file; `STATS19` and `DEFAULT_BUILT_AT`
are already defined there.

**Expected result:** `.venv/bin/python -m pytest -q` is still green, and the new fixture is
importable by name from any test under `tests/`.

### 3. Write `serialise_context`

**File:** `src/askcrossroads/prompt.py` — replace the file, keeping the existing docstring line as
the first line of an expanded module docstring.

The module imports only `json` from the standard library and `introspection` from the package. It
must not import `database`, `llm`, or `duckdb`.

Write these pieces, in this order:

**3a. The absent-value sentinel and the escape helper.**

```python
# What a NULL or an absent value renders as. One fixed string everywhere, so a missing
# dtype and a missing date range look the same to the model and neither renders as
# Python's `None`.
ABSENT = "(none)"


def _escape(value):
    """Render one field of a delimited row, on one line, with the delimiter escaped.

    No DfT label is known to contain a pipe or a newline, but nothing guarantees it, and one
    that did would silently corrupt every row after it. Backslash is escaped first, so the
    escaping is reversible.
    """
    if value is None:
        return ABSENT
    text = str(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    # Collapse any line break, so one row is always one line.
    return " ".join(text.split())
```

**3b. Identifier rendering.**

```python
# A name that needs no quoting: the shape SQL accepts bare.
_PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(name):
    """Render a table or column name for the DDL description.

    Ordinary names are left bare so the SQL the model writes back stays readable — spec §3
    cares about that, because the user is shown the query. Anything unusual is quoted using
    introspection's own quoting, so a column called `order` or `a"b` is still described
    correctly.
    """
    if name and _PLAIN_IDENTIFIER.match(name):
        return name
    return introspection.quote_identifier(name or "")
```

`re` is imported at the top of the module.

**3c. Value rendering for dates.**

```python
def _temporal_value(value):
    """Render one end of a date range.

    isoformat() where the value has one, so a DATE and a TIMESTAMP both render in a form the
    model can compare against a year in a question (spec §6).
    """
    if value is None:
        return ABSENT
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)
```

**3d. The five section builders.** Each returns a string with no trailing newline. Each iterates the
context's tuples **in the order they arrive** — they are already explicitly sorted by
`introspection.py`, and re-sorting here would hide the day that stopped being true.

```python
def _build_section(provenance):
    """What produced this file (spec §2D).

    parameters is re-serialised with sorted keys rather than passed through as the raw
    string upstream wrote, so two builds that recorded the same parameters render the same
    bytes whatever whitespace upstream used.
    """
    parameters = provenance.parameters
    return "\n".join([
        "## Build",
        f"crossroads_version | {_escape(provenance.crossroads_version)}",
        f"schema_version | {_escape(provenance.schema_version)}",
        f"built_at_utc | {_temporal_value(provenance.built_at_utc)}",
        "parameters | "
        + (json.dumps(parameters, sort_keys=True) if parameters else ABSENT),
    ])


def _schema_section(relations):
    """Every table and view, with every column and its declared type (spec §2A).

    Written as DDL because a model reads CREATE TABLE natively. These statements describe;
    they are not meant to be executed, which is why a view is rendered as a column list with
    no SELECT behind it. Do not "fix" that by inventing a definition we do not have.
    """
    blocks = ["## Tables and views"]
    for relation in relations:
        keyword = "CREATE VIEW" if relation.is_view else "CREATE TABLE"
        name = f"{_identifier(relation.schema)}.{_identifier(relation.name)}"
        columns = ",\n".join(
            f"  {_identifier(column.name)} {column.data_type}"
            for column in relation.columns
        )
        blocks.append(f"{keyword} {name} (\n{columns}\n);")
    return "\n\n".join(blocks)


def _date_range_section(ranges):
    """The earliest and latest value of every date-typed column (spec §6).

    This is what lets the model say "this build contains 2023 only" instead of returning an
    empty table for a question about 2019.
    """
    lines = ["## Date ranges", "relation | column | earliest | latest"]
    for item in ranges:
        lines.append(
            f"{_escape(item.relation)} | {_escape(item.column)} | "
            f"{_temporal_value(item.earliest)} | {_temporal_value(item.latest)}"
        )
    return "\n".join(lines)


def _manifest_section(entries):
    """Every column_manifest row (spec §2B). `tbl` is the singular SOURCE name."""
    lines = ["## Column classifications", "source | column | kind | dtype"]
    for entry in entries:
        lines.append(
            f"{_escape(entry.tbl)} | {_escape(entry.col)} | "
            f"{_escape(entry.kind)} | {_escape(entry.dtype)}"
        )
    return "\n".join(lines)


def _codebook_section(entries):
    """The whole codebook, including is_missing (spec §3).

    is_missing matters and is not decoration: Crossroads-UK replaces those codes with NULL
    when it builds the silver tables, so a model that does not know a code was removed will
    write a filter on it and get nothing back.
    """
    lines = ["## Codebook", "variable | code | label | missing"]
    for entry in entries:
        lines.append(
            f"{_escape(entry.variable)} | {_escape(entry.code)} | "
            f"{_escape(entry.label)} | {'yes' if entry.is_missing else 'no'}"
        )
    return "\n".join(lines)
```

**3e. The public function.**

```python
def serialise_context(context):
    """Render a DatabaseContext as the cacheable prompt prefix (spec §3).

    THE WHOLE PAYLOAD GOES IN. No relevance filtering, no selection heuristic. A filter
    scoring the question's words against column names and label text sounds like an obvious
    saving and fails silently on exactly the questions worth asking: a question about drunk
    drivers matches no column name and no label text, so the model would get no codes for
    the column it needs, guess, and return wrong rows instead of an error. The whole payload
    is roughly 2% of the context window.

    THE OUTPUT MUST BE BYTE-STABLE. For a given database this returns exactly the same
    characters every time, which is what makes the prompt cache work. Three rules keep it
    true, and every one of them is tested:

    - Nothing volatile appears anywhere. No timestamp, no question, no per-request
      identifier, and not the database path — the path is stable for one file but differs
      between machines holding the same build.
    - Rows are emitted in the order the context carries them. introspection.py sorts them in
      Python with explicit keys for this reason. Sorting again here would work and would
      hide the day that stopped being true.
    - Every value goes through a renderer that fixes its form: dates through isoformat,
      absent values through one sentinel, delimiters escaped.

    Returns one string, ending in a newline.
    """
    sections = [
        "# Database context",
        _build_section(context.provenance),
        _schema_section(context.relations),
        _date_range_section(context.temporal_ranges),
        _manifest_section(context.column_manifest),
        _codebook_section(context.codebook),
    ]
    return "\n\n".join(sections) + "\n"
```

**Expected result:** `serialise_context` imports cleanly and returns a string beginning
`# Database context\n\n## Build\n`.

### 4. Sanity-check the output by eye, once

Not a test — a five-second look, so the next stage is written against something you have seen:

```bash
.venv/bin/python -c "
from pathlib import Path
import tempfile
from tests.conftest import build_crossroads_db, STATS19, ERA5_WEATHER
from askcrossroads.database import Database
from askcrossroads import prompt
with tempfile.TemporaryDirectory() as d:
    p = build_crossroads_db(Path(d) / 'x.db', datasets=(STATS19, ERA5_WEATHER))
    with Database.open(p) as db:
        text = prompt.serialise_context(db.context)
print(text[:1200])
print('...')
print('TOTAL CHARACTERS:', len(text))
"
```

Expected: the five sections in order, readable DDL, and delimited rows one per line.

---

## Testing & Verification

**File:** `tests/test_prompt.py` — new.

The byte-stability tests are the point of this stage. Spec §11.4: *"The same database always produces
the exact same prefix, byte for byte […] This is what makes caching work, and if it silently breaks,
nothing else will catch it."*

### Integration tests (PRIMARY — real `DatabaseContext`s from real DuckDB files)

Every test below builds an actual database with the factory and opens it with `Database.open`, so
the prefix is produced from a context that came through the whole real path. Nothing is stubbed.

| Test | Setup | Assertion |
|---|---|---|
| `test_prefix_is_stable_across_repeated_calls` | one `crossroads_db`, opened once | `serialise_context(ctx) == serialise_context(ctx)` |
| `test_prefix_is_stable_across_reopens` | open the **same file** twice | the two prefixes are equal |
| `test_prefix_is_stable_across_identical_builds` | `crossroads_db_factory()` twice with identical arguments (two different files) | the two prefixes are equal |
| `test_prefix_changes_when_the_years_change` | `years=(2019,)` vs `years=(2023,)` | the two prefixes differ |
| `test_prefix_changes_when_the_datasets_change` | `datasets=(STATS19,)` vs `datasets=(STATS19, ERA5_WEATHER)` | the two prefixes differ |
| `test_prefix_contains_no_path` | any build | `str(ctx.path)` is not in the prefix, and neither is `ctx.path.name` |
| `test_prefix_contains_every_codebook_row` | any build | the `## Codebook` section has exactly `len(ctx.codebook)` rows after its header |
| `test_prefix_contains_every_manifest_row` | any build | the `## Column classifications` section has exactly `len(ctx.column_manifest)` rows after its header |
| `test_prefix_contains_every_relation` | any build | one `CREATE TABLE`/`CREATE VIEW` per `ctx.relations` entry, and every relation name appears |
| `test_prefix_contains_every_column` | any build | for one named relation, every column name and its declared type appear on a line together |
| `test_prefix_contains_every_date_range` | build with `BANK_HOLIDAYS` | the `## Date ranges` section has exactly `len(ctx.temporal_ranges)` rows after its header |
| `test_prefix_carries_is_missing` | any build | at least one codebook row ends `| yes` and at least one ends `| no` |
| `test_prefix_carries_build_parameters` | build with known `years`/`datasets` | the years and dataset names appear in the `## Build` section |
| `test_empty_table_range_renders_as_absent` | any build (`quarantine_raw` is always empty) | its row renders both ends as `(none)`, not as `None` |
| `test_a_weatherless_build_offers_no_weather_relation` | `datasets=(STATS19,)` | no relation name from the weather dataset appears anywhere in the prefix |

### The hardcoding test — the one that proves spec §2C

```python
def test_an_unfamiliar_build_produces_a_prompt_naming_its_own_tables(unconventional_db):
    """Nothing about the schema is hardcoded (spec §2C).

    This build is entirely valid and shares no name with the standard fixture. A prefix that
    mentioned a familiar table could only have got the name from source.
    """
    with Database.open(unconventional_db) as database:
        text = prompt.serialise_context(database.context)

    for name in ("widgets", "gadget_log", "widget_summary", "grade", "mass_kg"):
        assert name in text

    for name in ("collisions", "vehicles", "collision_severity", "weather_conditions"):
        assert name not in text
```

### Unit tests (the renderers, where a case is hard to reach through a real build)

| Test | Assertion |
|---|---|
| `test_escape_escapes_the_delimiter` | `prompt._escape("a|b") == "a\\|b"` |
| `test_escape_escapes_backslash_first` | `prompt._escape("a\\|b")` round-trips unambiguously |
| `test_escape_collapses_newlines` | `prompt._escape("a\nb") == "a b"` |
| `test_escape_renders_none_as_the_sentinel` | `prompt._escape(None) == prompt.ABSENT` |
| `test_identifier_leaves_ordinary_names_bare` | `prompt._identifier("collisions") == "collisions"` |
| `test_identifier_quotes_unusual_names` | `prompt._identifier("order by")` is quoted |

### Runnable commands

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_prompt.py -v
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

### Stage ship-readiness checklist

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green, including
   every pre-existing test.
2. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
3. `grep -rn "import" src/askcrossroads/prompt.py` shows only `json`, `re`, and
   `from askcrossroads import introspection`.
4. `git status` shows `src/askcrossroads/prompt.py` and `tests/conftest.py` modified,
   `tests/test_prompt.py` untracked, and **nothing staged**.

---

## End State / Handoff

After this stage:

- `prompt.serialise_context(context)` returns the complete, byte-stable prefix for any valid build.
- `prompt.ABSENT`, `prompt._escape`, `prompt._identifier` and `prompt._temporal_value` exist as the
  rendering primitives; the next stage reuses `_escape` for the chart call's sample rows.
- `tests/conftest.py` exposes `build_unconventional_db` and the `unconventional_db` fixture.
- `prompt.py` still contains **no** system prompt text, no `cache_control`, and no knowledge of the
  Anthropic API. Stage 02 adds those.

The next stage may assume `serialise_context` is deterministic and complete, and may place its
output into a message block without re-checking it.

---

## Failure Modes & Rollback

| Failure | Why it happens | Guardrail |
|---|---|---|
| Prefix differs between two runs | Iterating a `set` or `dict` built inside a section builder, or calling `sorted()` on a key that ties | The three stability tests. Never build an intermediate `set`; iterate the context's tuples directly. |
| Prefix differs between machines | Something machine-dependent got in — the path, a locale-dependent `str()`, a float repr | `test_prefix_contains_no_path`; render dates through `isoformat` only. |
| A codebook label with a pipe corrupts every following row | Unescaped delimiter | `_escape`, tested directly. |
| `None` leaks into the text as `None` | A field rendered with an f-string instead of `_escape` | `test_empty_table_range_renders_as_absent`; route **every** field through `_escape` or `_temporal_value`. |
| An existing test breaks | The `_write_convention_tables` signature change | Both new parameters have defaults; run the full suite after step 1 before going further. |

**Rollback:** `prompt.py` and `conftest.py` are tracked, so
`git checkout -- src/askcrossroads/prompt.py tests/conftest.py` restores both. `tests/test_prompt.py`
is untracked — delete it. Nothing else is touched, and no commit is made at any point.
