# Stage 02 — The Rejection and Acceptance Matrix

> Part of *SQL Safety Validator*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting Constraints, the
> deviation note in *Approach / Architecture*, and the Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 01 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -c "
from askcrossroads.safety import validate_sql
for q in ['SELECT 1', 'PRAGMA version', 'SELECT 1; DROP TABLE t',
          \"SELECT * FROM read_csv_auto('/etc/hosts')\"]:
    v = validate_sql(q)
    print(f'{v.allowed!s:6} {v.reason:20} {q[:40]}')
"
```

Expected:

```text
True                        SELECT 1
False  not_a_read           PRAGMA version
False  multiple_statements  SELECT 1; DROP TABLE t
True                        SELECT * FROM read_csv_auto('/etc/ho
```

**Read that last line carefully.** `validate_sql` allows the `read_csv_auto` query. That is correct
and deliberate — see the overview's *Approach / Architecture* section on why the parse-tree function
check was not built. This stage's job includes proving that the connection settings from
`002_database_layer`, not this validator, are what actually stop that query.

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_safety.py -q
```

Expected: green.

If any of the four verdicts above is wrong, stop — stage 01 is not finished.

---

## Objective

Turn the validator from *tested* into *provably correct*, within the scope this plan actually
covers: the complete [spec.md §11.3](../../spec.md) matrix for every rejection this module
implements, an explicitly named case per rejection, proof that every accepted query really executes,
proof that a file-reading query passes this validator and is refused only by the database connection,
and a guard on the one DuckDB behaviour the whole design rests on.

**This stage adds tests, not behaviour.** `src/askcrossroads/safety.py` should need no change. If
these tests find a defect, fixing it is in scope — but no new feature is, and in particular **do not
add a function check here**. That was a decision made in the overview, not an oversight to correct.

---

## The rule that governs this stage

[spec.md §11.3](../../spec.md):

> Tested most densely of anything in the project, with an explicit case per rejection rather than one
> test asserting "bad SQL is rejected".
>
> Positive cases are equally required: ordinary `SELECT`s, CTEs (`WITH … SELECT`), and subqueries
> must **pass**. A validator that rejects everything is not safe, it is broken, and only positive
> cases prove the difference.

And [spec.md §11](../../spec.md) on why this module in particular:

> A boundary that has not been tested is a boundary that is assumed rather than enforced.

**Note on scope.** [spec.md §11.3](../../spec.md) also lists "any file-reading function" among the
required rejections, marked there as *specified, not implemented* and explained in [spec.md
§5](../../spec.md). This plan does not implement that check — see the overview's deviation note — so
that line item is deliberately not restated as a rejection here. It is restated instead as an
*acceptance*, paired with proof that the connection refuses the query anyway. That pairing is what
this stage exists to nail down: the property that must hold is "the question is still answered
correctly, by a different mechanism," not "the rejection still happens here." The spec says the same
thing in its own words, immediately below that line item.

The specification's required rejections that this module *does* implement, in full, are the contract
this stage discharges:

| Required rejection | Covered by |
|---|---|
| `DROP` | its own case |
| `DELETE` | its own case |
| `UPDATE` | its own case |
| `INSERT` | its own case |
| `CREATE` | its own case |
| `ATTACH` | its own case |
| `PRAGMA` (note the `SELECT`-rewrite) | its own case, plus a guard test on the rewrite |
| `COPY` | its own case |
| Multiple statements separated by `;` | its own case |
| A trailing statement smuggled after a valid `SELECT` | its own case |

---

## Implementation Steps

### Step 1 — Append the spec §11.3 matrix to `tests/test_safety.py`

Stage 01 already covers these behaviours. This block restates them **as the specification's own
list**, so that a reader can put the suite and spec §11.3 side by side and see one case per line.
That duplication is deliberate: it is what makes a missing case visible.

Do not modify or remove anything the earlier stage wrote.

```python
# =====================================================================================
# The spec §11.3 matrix
# =====================================================================================
# One case per rejection the specification names AND this module implements, written in
# the specification's own order, so the suite can be read against §11.3 line by line.
# This deliberately overlaps the check-by-check tests in stage 01: those prove the
# mechanism, this proves the contract.
#
# Spec §11.3 also names "any file-reading function" as a required rejection. That check
# is deliberately not built here (see docs/plans/003_safety_validator/00-overview.md), so
# it is not restated as a rejection below. It reappears further down this file as an
# ACCEPTANCE, paired with proof that the database connection refuses it anyway.


SPEC_11_3_REJECTIONS = [
    ("DROP", "DROP TABLE collisions"),
    ("DELETE", "DELETE FROM collisions"),
    ("UPDATE", "UPDATE collisions SET speed_limit = 0"),
    ("INSERT", "INSERT INTO collisions (source_row_key) VALUES ('x')"),
    ("CREATE", "CREATE TABLE intruder (i INTEGER)"),
    ("ATTACH", "ATTACH 'elsewhere.db' AS elsewhere"),
    ("PRAGMA", "PRAGMA version"),
    ("COPY", "COPY collisions TO 'out.csv'"),
    ("multiple statements", "SELECT 1; SELECT 2"),
    ("statement smuggled after a SELECT", "SELECT 1; DROP TABLE collisions"),
]


@pytest.mark.parametrize(
    "query",
    [query for _, query in SPEC_11_3_REJECTIONS],
    ids=[name for name, _ in SPEC_11_3_REJECTIONS],
)
def test_spec_11_3_rejection(query):
    """Every rejection spec §11.3 requires that this module implements, each its own case."""
    verdict = validate_sql(query)
    assert verdict.allowed is False, f"spec §11.3 requires this to be rejected: {query}"
    assert verdict.reason, "a rejection must say why"
    assert verdict.detail, "a rejection must carry a sentence for the user"


SPEC_11_3_ACCEPTIONS = [
    ("ordinary SELECT", "SELECT collision_severity FROM collisions"),
    ("SELECT with aggregate and ORDER BY",
     "SELECT collision_severity, COUNT(*) AS n FROM collisions "
     "GROUP BY collision_severity ORDER BY collision_severity"),
    ("CTE",
     "WITH counted AS (SELECT COUNT(*) AS n FROM collisions) SELECT n FROM counted"),
    ("nested CTE",
     "WITH a AS (SELECT * FROM collisions), b AS (SELECT * FROM a) SELECT COUNT(*) FROM b"),
    ("subquery in WHERE",
     "SELECT * FROM collisions WHERE accident_index IN "
     "(SELECT accident_index FROM vehicles)"),
    ("scalar subquery in the select list",
     "SELECT (SELECT COUNT(*) FROM vehicles) AS vehicles FROM collisions"),
    ("derived table", "SELECT n FROM (SELECT COUNT(*) AS n FROM collisions) t"),
    ("join",
     "SELECT c.source_row_key FROM collisions c "
     "JOIN vehicles v ON c.accident_index = v.accident_index"),
    ("union", "SELECT 1 AS n UNION ALL SELECT 2 AS n"),
]


@pytest.mark.parametrize(
    "query",
    [query for _, query in SPEC_11_3_ACCEPTIONS],
    ids=[name for name, _ in SPEC_11_3_ACCEPTIONS],
)
def test_spec_11_3_acception(query):
    """The half that proves the validator is safe rather than merely broken.

    Spec §11.3: "A validator that rejects everything is not safe, it is broken, and only
    positive cases prove the difference."
    """
    verdict = validate_sql(query)
    assert verdict.allowed is True, (
        f"spec §11.3 requires this to pass, but it was rejected "
        f"({verdict.reason}): {verdict.detail}"
    )
```

### Step 2 — Append the proof that accepted queries really execute

A validator that says yes to nonsense is not doing its job either. These tests run every accepted
query against a real synthetic build, through the same read-only connection the application uses.

```python
# =====================================================================================
# The validator and the engine agree
# =====================================================================================
# Everything above asks the validator what it thinks. These tests check that against the
# database itself, so a verdict is never merely internally consistent.


@pytest.fixture
def con(crossroads_db):
    """A read-only connection to a valid synthetic build, opened exactly as the
    application opens the user's file: read_only=True, external access off."""
    connection = open_database(crossroads_db)
    yield connection
    connection.close()


@pytest.mark.parametrize(
    "query",
    [query for _, query in SPEC_11_3_ACCEPTIONS],
    ids=[name for name, _ in SPEC_11_3_ACCEPTIONS],
)
def test_an_accepted_query_actually_runs(con, query):
    """Accepting a query the database cannot run would make the positive cases hollow.

    Note what this does NOT do: it does not assert anything about the rows. The database
    is a fixture, not a subject. All that matters here is that the validator's "yes"
    corresponds to something the engine will accept.
    """
    assert validate_sql(query).allowed is True
    con.execute(query).fetchall()


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE collisions",
        "DELETE FROM collisions",
        "UPDATE collisions SET speed_limit = 0",
        "INSERT INTO collisions (source_row_key) VALUES ('x')",
        "CREATE TABLE intruder (i INTEGER)",
    ],
)
def test_the_engine_also_refuses_every_write_the_validator_rejects(con, statement):
    """Spec §11.4 requires a write to fail as if the validator had been bypassed entirely.

    tests/test_database.py already proves the engine refuses these. This restates it from
    the validator's side, so the two guarantees are visibly the same guarantee rather than
    two facts that happen to both be true today.
    """
    assert validate_sql(statement).allowed is False
    with pytest.raises(duckdb.Error):
        con.execute(statement)
```

### Step 3 — Append the proof behind the decision in the overview

**This is the most important block in this stage.** It exists to make the deviation recorded in
`00-overview.md` a tested fact rather than an assertion: a query calling a file- or network-reading
function is accepted by `validate_sql`, and is refused only because the connection was opened with
`enable_external_access=False`.

```python
# =====================================================================================
# The decision this validator rests on: file-reading functions pass HERE, and are
# refused by the CONNECTION instead (see docs/plans/003_safety_validator/00-overview.md)
# =====================================================================================
# safety.py deliberately does not check for read_csv_auto, glob, and similar. The
# database connection this application opens (database.py, from 002_database_layer) is
# created with enable_external_access=False, which refuses these at the moment they would
# run. Both halves of that claim are asserted together below, so a change to EITHER side
# -- this validator becoming stricter, or the connection settings being loosened -- is
# caught by this same test rather than by two unrelated ones drifting apart.
#
# Verified directly, not assumed: each of the four calls below was run against a real
# connection with these settings and raised duckdb.PermissionException with the message
# "file system operations are disabled by configuration".


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM read_csv_auto('/etc/hosts')",
        "SELECT * FROM read_parquet('somewhere.parquet')",
        "SELECT * FROM glob('/*')",
        "SELECT * FROM read_text('/etc/hosts')",
    ],
)
def test_file_reading_queries_pass_validation_but_the_connection_refuses_them(con, query):
    """The explicit proof behind the decision not to build a parse-tree function check.

    If this test's first assertion ever starts failing, someone has added a function
    check to safety.py -- that is fine, but the overview and this stage's docstring
    should be updated to say so rather than left describing a decision that no longer
    holds.

    If the SECOND assertion ever starts failing -- the connection allows the read -- that
    is a serious finding. It means the one layer this design relies on for file- and
    network-reading functions has gone silent, and nothing else in this module would
    catch it. Report it prominently; do not add a function check as a quiet workaround
    without saying why the decision changed.
    """
    verdict = validate_sql(query)
    assert verdict.allowed is True, (
        "this validator does not check for file-reading functions by design (see "
        "00-overview.md) -- if it now rejects this, the design note is out of date"
    )
    with pytest.raises(duckdb.Error):
        con.execute(query)
```

Add the import this block (and step 2) needs to the top of the file, after the existing
`askcrossroads.safety` import:

```python
from askcrossroads.database import open_database
```

### Step 4 — Append the guard on DuckDB's behaviour

```python
# =====================================================================================
# A guard on the engine behaviour this module depends on
# =====================================================================================
# safety.py's check 1 exists, and runs first, only because of this fact about DuckDB. It
# is not documented as a stable contract, and a DuckDB upgrade could change it without
# anything else in this project noticing.
#
# A failure here is not necessarily a defect in safety.py -- check 1 would still correctly
# reject PRAGMA by its text either way. It means the reasoning recorded in safety.py's
# module comment has gone stale and needs rewriting.


def test_duckdb_still_rewrites_pragma_into_a_select():
    """Check 1 exists, and runs first, only because of this.

    If DuckDB ever stops rewriting PRAGMA, check 1 is no longer load-bearing for this
    particular case -- but it stays regardless, because it is also what rejects SET,
    INSTALL, LOAD, EXPORT, DESCRIBE and CALL.
    """
    statement = duckdb.extract_statements("PRAGMA version")[0]
    assert statement.type == duckdb.StatementType.SELECT, (
        "DuckDB no longer reports PRAGMA as a SELECT -- the rationale in safety.py's "
        "module comment needs revisiting"
    )
    assert statement.query.lstrip().upper().startswith("SELECT"), (
        "DuckDB no longer stores the rewritten text on the statement -- check 1 could "
        "now read statement.query safely, though there is no reason to change it"
    )
```

### Step 5 — Confirm the final import block

The top of `tests/test_safety.py` should now read:

```python
import duckdb
import pytest

from askcrossroads.database import open_database
from askcrossroads.safety import Verdict, validate_sql
```

Note that the test file imports `database.py` while `safety.py` does not. That asymmetry is the
point: the module under test is independent, and only the tests reach for a real database to check
the validator's verdicts against the engine's — including, in this stage, the case where the engine
is doing work the validator deliberately does not.

---

## Testing & Verification

### Primary: the full safety suite, every case named

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_safety.py -v
```

**Expected result:** green. The output must show, among the rest, one line per entry in
`SPEC_11_3_REJECTIONS` with the specification's own labels (`DROP`, `DELETE`, `UPDATE`, `INSERT`,
`CREATE`, `ATTACH`, `PRAGMA`, `COPY`, `multiple statements`, `statement smuggled after a SELECT`),
one line per entry in `SPEC_11_3_ACCEPTIONS`, and four
`test_file_reading_queries_pass_validation_but_the_connection_refuses_them` cases.

Read that output against [spec.md §11.3](../../spec.md) line by line. Every rejection the
specification names that this module implements must have its own line. The file-reading-function
line item will **not** appear as a rejection — that is expected; confirm it appears instead as one of
the four cases proving the connection refuses it.

### Count the cases

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_safety.py -q --collect-only | tail -3
```

**Expected result:** well over sixty collected tests.

### The whole suite, offline, with no configuration at all

```bash
cd /Users/will/Documents/Code/AskCrossroads && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

**Expected result:** green. No API key, no network, no real Crossroads-UK build (spec §11.1).

### The positives are still positive

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_safety.py -v -k "acception or actually_runs"
```

**Expected result:** green. A failure here means the validator has become too strict, which removes
questions the user could have asked — as much a defect as letting a write through.

### The load-bearing proof, on its own

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_safety.py -v -k "file_reading_queries_pass_validation"
```

**Expected result:** four passes. This is the test that justifies the whole decision recorded in the
overview. If it is not green, the decision to skip the function check is not actually safe on this
codebase and must be revisited before anything else in this plan is considered done.

### The validator source did not change in this stage

```bash
cd /Users/will/Documents/Code/AskCrossroads && git diff --stat src/askcrossroads/safety.py
```

**Expected result:** no output, unless these tests found a genuine defect. If they did, say so
prominently in your report and describe the defect and the fix. Do not add a function check here —
that would silently reverse the decision in the overview rather than report a defect in it.

### The integration suite is untouched

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest -m integration -q
```

**Expected result:** one test collected, skipped, no unknown-marker warning.

### Nothing was committed

```bash
git status --short
```

**Expected result:** `tests/test_safety.py` modified, **nothing staged**.

### Stage ship-readiness checklist

- [ ] Every rejection named in spec §11.3 that this module implements appears as its own labelled
      case in the test output.
- [ ] Every positive shape named in spec §11.3 — ordinary `SELECT`, CTE, subquery — appears as its
      own labelled case and passes.
- [ ] Every accepted query in `SPEC_11_3_ACCEPTIONS` executes without error against the synthetic
      build.
- [ ] A write statement is proved to be refused by the engine as well as by the validator.
- [ ] All four file-reading queries are proved to pass `validate_sql` **and** to be refused by the
      connection. This is the load-bearing proof for the whole plan's central decision.
- [ ] The DuckDB guard test passes, and its failure message explains what a failure would mean.
- [ ] `src/askcrossroads/safety.py` is unchanged by this stage.
- [ ] The full offline suite is green with no environment variables set.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage — this completes the plan:**

- `src/askcrossroads/safety.py` is complete: three checks, five reason codes, no project imports, no
  connection of its own, and no caller yet.
- `tests/test_safety.py` discharges spec §11.3 for every rejection this module implements, and
  separately proves — rather than merely asserts in a comment — that file-reading functions are
  handled correctly by a different layer.
- The default `pytest` run is green offline, with no key, no network and no real build.
- Nothing is staged or committed.

**What later work may assume:**

- `validate_sql(sql)` takes any value and returns a `Verdict(allowed, reason, detail)`. It never
  raises, and it needs no connection, no configuration and no database from its caller.
- The reason codes are `empty`, `not_a_read`, `multiple_statements`, `not_a_select` and
  `unparseable`. `detail` is a sentence written for a person and safe to display.
- **A query calling a file- or network-reading function will be allowed by `validate_sql`, and will
  fail when executed**, as an ordinary `duckdb.Error`. Whatever calls `validate_sql` and then
  executes the query (`query.py`, later work) must be prepared to treat that failure exactly like any
  other SQL execution error — spec §4's repair loop already does this for execution errors generally.
  This is not a special case to add handling for; it is the existing execution-error path, reached by
  one more route than before.
- The validator must be re-run on **every** attempt of the repair loop, not only the first — spec
  §4.4 is explicit that a repaired query is new, unvalidated model output. Nothing in this module
  remembers a previous verdict, so there is no cache to get wrong.
- A rejection is **not** a SQL execution error. Whether it consumes a repair attempt, is fed back to
  the model, or is reported straight to the user is decided by the plan that builds `query.py`; this
  module supplies a stable reason code so that choice can be made cleanly.
- `docs/spec.md` §5 and §11.3 still *list* the parse-tree function check, and mark it *specified, not
  implemented*, with the reasoning in the §5 subsection *The parse-tree function check is specified
  but not implemented*. The spec and this code therefore agree. If the check is ever built, that
  subsection names the two markers to delete.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `test_an_accepted_query_actually_runs` fails with `Catalog Error: Table ... does not exist` | A query in `SPEC_11_3_ACCEPTIONS` names something the fixture does not build | Fix the query to match `tests/conftest.py`. The validator is not at fault — it does not know what tables exist, and must not |
| `test_file_reading_queries_pass_validation_but_the_connection_refuses_them` fails on the first assertion (`verdict.allowed is True`) | Something added a function check to `safety.py` | This may be a deliberate later change, but it contradicts this plan's own decision. Do not "fix" the test by asserting `False` instead — update `00-overview.md` to say the decision changed and why, first |
| The same test fails on the second assertion (no exception raised) | `enable_external_access=False` is not reaching the connection | **This is the serious one.** It means the single layer this whole plan relies on for file access has gone silent. Report it prominently before doing anything else — it is not a `safety.py` bug, it is a `database.py` regression that this design has no second layer to catch |
| `test_duckdb_still_rewrites_pragma_into_a_select` fails | DuckDB changed | Not urgent and not a security regression: check 1 still rejects `PRAGMA` by its text regardless of what type it parses to. Update the comment in `safety.py` so the recorded reasoning matches reality |
| The engine accepts a write | `read_only=True` is not reaching the connection | A defect in `database.py`, not here. Report it prominently — it removes the backstop for the entire safety model |
| A `SPEC_11_3_ACCEPTIONS` entry is rejected | The validator has become too strict | Do not delete the case to make the suite green. Find out which check fired, using `validate_sql(query).reason`, and fix the check |

**Rollback:** delete the four blocks appended in steps 1–4 and the one added import.
`tests/test_safety.py` then returns to its stage 01 state and passes unchanged. No source file is
touched by this stage, so there is nothing else to undo.
