# Stage 01 — Text Gate & Statement Checks

> Part of *SQL Safety Validator*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting Constraints and the
> Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

`docs/plans/001_project_foundation/` and `docs/plans/002_database_layer/` must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest -q
```

Expected: a green run of `tests/test_package.py`, `tests/test_config.py`, `tests/test_fixtures.py`,
`tests/test_database.py` and `tests/test_introspection.py`.

`src/askcrossroads/safety.py` should currently contain only its docstring:

```bash
cat src/askcrossroads/safety.py
```

Expected: one line — `"""SQL safety validator: single-SELECT parse check and function check (spec §5)."""`

If `.venv/` does not exist, or `tests/test_database.py` is absent, stop — a previous plan is not
finished.

---

## Objective

Give `safety.py` the `Verdict` result type and its three checks: the original-text gate, the
single-statement check, and the statement-type check. `validate_sql` must return a verdict for every
input and must never raise.

**This plan has two stages, not three.** The overview explains a decision made before this plan was
written: the parse-tree function check that spec §5 describes as a fourth layer is deliberately not
built, because the connection settings established in `002_database_layer`
(`enable_external_access=False`) already stop the same thing at execution time — verified directly
against the engine. Read the overview's *Approach / Architecture* section before writing any code, so
the module docstring below makes sense rather than reading as an omission.

---

## The rule that governs this whole stage

[spec.md §5](../../spec.md):

> DuckDB rewrites `PRAGMA version` into `SELECT * FROM pragma_version()` before reporting its type,
> so a `PRAGMA` statement would pass a check that only looks at parsed type — and some `PRAGMA`
> commands can change database settings, including re-enabling the external access this layer exists
> to block. So the validator also checks the statement's original text and rejects anything that
> doesn't start with `SELECT` or `WITH`, **before the parse-type check ever runs**.

Verified against DuckDB 1.5.5, and it is worse than that paragraph says — the rewrite reaches the
statement's own recorded text as well as its type:

```text
duckdb.extract_statements("PRAGMA version")[0]
    .type   ->  StatementType.SELECT
    .query  ->  'SELECT * FROM pragma_version();'
```

So **both** of these are wrong and let a `PRAGMA` through:

```python
statements[0].type == duckdb.StatementType.SELECT          # WRONG — passes a PRAGMA
statements[0].query.lstrip().upper().startswith("SELECT")  # WRONG — passes a PRAGMA
```

The gate must run on **the string the caller passed in**, before `extract_statements` is consulted at
all. That is the single most important line of code in this module. Do not reorder the gates, and do
not "simplify" the gate to read from the parsed statement.

---

## Implementation Steps

### Step 1 — Replace `src/askcrossroads/safety.py`

Replace the file's contents entirely with the following. Stage 02 adds tests only and does not touch
this file, so what you write here is the module's final form; write exactly this now.

```python
"""SQL safety validator: the boundary model-written SQL must cross (spec §5, §11.3).

AskCrossroads executes queries the user did not write, against the user's own data, with
no human checkpoint before execution (spec §4). This module is what makes that
defensible: given one string of SQL, it answers whether that string may reach the
database, and if not, why.

One of the six layers in spec §5 lives here: the single-SELECT parse check, which rejects
anything that is not exactly one read statement. Spec §5 also describes a second layer
here, a parse-tree walk rejecting file- and network-reading functions -- that layer is
DELIBERATELY NOT IMPLEMENTED in this module. The connection this application opens is
created with `enable_external_access=False` (database.py), which already refuses a
function like `read_csv_auto` at the moment it would run, regardless of whether anything
checked for it beforehand -- verified directly against the engine. A query calling such a
function is a well-formed single SELECT and passes every check in this module; it is
refused only when it actually executes. See docs/plans/003_safety_validator/00-overview.md
for the full reasoning.

This module imports nothing from the rest of the project, and opens no database connection
of its own -- duckdb.extract_statements is a plain function. It takes a string and returns
a verdict, and knows nothing about which database the string might run against.
"""

import re
from dataclasses import dataclass

import duckdb

# A statement must LOOK like a read before the parser is consulted at all.
#
# This is not "validation by regular expression" -- gates 2 and 3 below both run through
# DuckDB's own parser regardless of what this matches. It is a first-word check that only
# ever narrows what reaches the parser, and it exists because the parser alone cannot
# close one specific hole: DuckDB rewrites `PRAGMA version` into `SELECT * FROM
# pragma_version()` before reporting its type, AND stores that rewritten text on the
# statement. So neither the parsed type nor the parsed text can tell a PRAGMA from a
# SELECT -- only the caller's original string can. Some PRAGMA commands change database
# settings, including re-enabling the external access database.py switched off, which is
# why this matters rather than merely being untidy.
#
# Only leading whitespace is skipped. A leading comment is rejected on purpose: skipping
# over comments here would mean writing a comment scanner of our own, which is exactly the
# string-matching gap spec §5 says to avoid.
_STARTS_LIKE_A_READ = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """Whether one SQL string may be executed, and if not, why.

    `reason` is a short stable code, for branching and for tests. `detail` is the sentence
    a person reads. Both are empty when the verdict is to allow.

    Spec §4 re-runs validation on every repair attempt and spec §4.5 shows the user the
    attempt history, so a rejection is an ordinary outcome on a normal path. That is why
    this is a returned value rather than a raised exception.
    """

    allowed: bool
    reason: str = ""
    detail: str = ""


# The single allowed verdict. Frozen, so one shared instance is safe.
ALLOWED = Verdict(allowed=True)


def _rejected(reason, detail):
    """A rejection carrying both a stable code and a sentence for the user."""
    return Verdict(allowed=False, reason=reason, detail=detail)


def validate_sql(sql):
    """Decide whether `sql` may be executed against the user's database.

    Returns a Verdict. Never raises: every way DuckDB's parser can fail is turned into a
    rejection, because a traceback reaching the user is a defect (spec §1).

    The checks run in a fixed order, cheapest and most decisive first:

    1. the caller's original text must begin with SELECT or WITH;
    2. it must be exactly one statement;
    3. that statement's parsed type must be SELECT.

    This does NOT check for file- or network-reading functions (read_csv_auto, glob, and
    similar). That is a deliberate decision, not a gap yet to be filled: the connection
    this application opens has enable_external_access=False (database.py), which already
    refuses such a function at execution time. A query calling one is a well-formed single
    SELECT and passes every check here.
    """
    if not isinstance(sql, str) or not sql.strip():
        return _rejected("empty", "No SQL was supplied.")

    # Gate 1: the ORIGINAL text. See the note on _STARTS_LIKE_A_READ above -- reading this
    # from the parsed statement instead would let a PRAGMA through.
    if not _STARTS_LIKE_A_READ.match(sql):
        return _rejected(
            "not_a_read",
            "Only queries that begin with SELECT or WITH may be run. "
            "This one does not.",
        )

    # Gate 2 and 3 go through DuckDB's own parser, so there is no gap between what the
    # validator understood and what the engine would run.
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        # The parser could not read it at all. That is a rejection with a reason, never a
        # traceback: callers of this module handle Verdicts, not duckdb exceptions.
        return _rejected("unparseable", f"This is not valid SQL: {error}")

    if len(statements) != 1:
        return _rejected(
            "multiple_statements",
            f"Only one statement may be run at a time; this is {len(statements)}.",
        )

    if statements[0].type != duckdb.StatementType.SELECT:
        # Gate 1 already rejects everything that reaches here by its text, so this gate is
        # a backstop rather than the primary check. It stays because the two gates fail
        # for different reasons, and a change to one must not silently disarm the other.
        return _rejected(
            "not_a_select",
            f"Only SELECT statements may be run; this is a "
            f"{statements[0].type.name} statement.",
        )

    return ALLOWED
```

**Expected result:**

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -c "
from askcrossroads.safety import validate_sql
print(validate_sql('SELECT 1'))
print(validate_sql('PRAGMA version'))
"
```

prints

```text
Verdict(allowed=True, reason='', detail='')
Verdict(allowed=False, reason='not_a_read', detail='Only queries that begin with SELECT or WITH may be run. This one does not.')
```

If the second line says `allowed=True`, gate 1 is not reading the caller's string. Stop and fix that
before doing anything else.

### Step 2 — Create `tests/test_safety.py`

This file is new. Stage 02 appends to it; write exactly this now.

```python
"""The SQL safety validator: the densest suite in the project (spec §11.3).

Every rejection gets its own explicitly named case. One test asserting "bad SQL is
rejected" would pass while nine of the ten rejections were broken, which is why spec §11.3
requires a case per rejection.

The positive cases carry equal weight. A validator that rejects everything is not safe, it
is broken, and only the positive cases prove the difference.
"""

import duckdb
import pytest

from askcrossroads.safety import Verdict, validate_sql


# --- The result type ---------------------------------------------------------------


def test_a_verdict_is_immutable():
    """Spec §4 passes verdicts around a repair loop; nothing may edit one in flight."""
    verdict = validate_sql("SELECT 1")
    with pytest.raises(Exception):
        verdict.allowed = False


def test_every_rejection_carries_a_reason_and_a_sentence():
    """The reason code is what the repair loop branches on; the detail is what is shown."""
    verdict = validate_sql("DROP TABLE t")
    assert verdict.allowed is False
    assert verdict.reason
    assert verdict.detail


def test_an_allowed_verdict_carries_no_reason():
    assert validate_sql("SELECT 1") == Verdict(allowed=True, reason="", detail="")


# --- Gate 1: the original text (spec §5) -------------------------------------------
# Every one of these parses to a statement type that is NOT select, except PRAGMA, which
# is the whole reason this gate reads the caller's string rather than the parsed one.


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE collisions",
        "DELETE FROM collisions",
        "UPDATE collisions SET speed_limit = 0",
        "INSERT INTO collisions (source_row_key) VALUES ('x')",
        "CREATE TABLE intruder (i INTEGER)",
        "ATTACH 'elsewhere.db' AS elsewhere",
        "COPY collisions TO 'out.csv'",
    ],
)
def test_a_statement_that_is_not_a_read_is_rejected(statement):
    verdict = validate_sql(statement)
    assert verdict.allowed is False
    assert verdict.reason == "not_a_read"


def test_pragma_is_rejected_even_though_it_parses_as_a_select():
    """The case spec §11.3 calls out by name.

    DuckDB rewrites `PRAGMA version` into a SELECT before reporting its type, and stores
    the rewritten text on the statement, so neither the parsed type nor the parsed text
    can tell the two apart. Only the caller's original string can. Some PRAGMA commands
    re-enable the external access database.py switched off, so this is a real hole.
    """
    verdict = validate_sql("PRAGMA version")
    assert verdict.allowed is False
    # The reason matters as much as the rejection: if this ever reads "not_a_select", the
    # gate order has been changed and the hole is open again for other PRAGMA commands.
    assert verdict.reason == "not_a_read"


def test_the_pragma_hole_is_real_and_this_gate_is_what_closes_it():
    """Proves the premise rather than trusting it.

    If this test ever fails, DuckDB has changed and the ordering rationale in the module
    comment needs revisiting -- but the gate is still correct, so nothing is urgent.
    """
    statement = duckdb.extract_statements("PRAGMA version")[0]
    assert statement.type == duckdb.StatementType.SELECT
    assert statement.query.lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize(
    "statement",
    [
        "SET enable_external_access = true",
        "INSTALL httpfs",
        "LOAD httpfs",
        "EXPORT DATABASE 'somewhere'",
        "CALL pragma_version()",
        "DESCRIBE SELECT 1",
        "EXPLAIN SELECT 1",
    ],
)
def test_other_non_read_statements_are_rejected(statement):
    """Not required by spec §11.3, but each is a real way to reach outside a SELECT.

    SET is the direct route to re-enabling external access; INSTALL and LOAD are how a
    query would reach for an extension carrying more file-reading functions.
    """
    assert validate_sql(statement).allowed is False


def test_a_leading_comment_is_rejected_on_purpose():
    """A deliberate false negative, documented in the plan overview.

    Skipping over leading comments would mean writing a comment scanner of our own, which
    is the string-matching gap spec §5 says to avoid. Fail-closed is the right side for a
    boundary; the prompt work is where SQL is kept free of preamble.
    """
    verdict = validate_sql("-- collisions by year\nSELECT 1")
    assert verdict.allowed is False
    assert verdict.reason == "not_a_read"


def test_leading_whitespace_is_fine():
    assert validate_sql("\n   SELECT 1").allowed is True


def test_lower_case_is_fine():
    assert validate_sql("select 1").allowed is True


# --- Gate 2: exactly one statement (spec §11.3) ------------------------------------


def test_multiple_statements_are_rejected():
    verdict = validate_sql("SELECT 1; SELECT 2")
    assert verdict.allowed is False
    assert verdict.reason == "multiple_statements"


def test_a_statement_smuggled_after_a_valid_select_is_rejected():
    """Spec §11.3 requires this case by name. Gate 1 passes it -- gate 2 is what stops it."""
    verdict = validate_sql("SELECT 1; DROP TABLE collisions")
    assert verdict.allowed is False
    assert verdict.reason == "multiple_statements"


def test_a_single_trailing_semicolon_is_still_one_statement():
    assert validate_sql("SELECT 1;").allowed is True


def test_comment_smuggling_is_resolved_by_the_parser_not_by_string_matching():
    """Spec §5 names this case.

    The semicolon and the DROP are inside a comment, so this is one harmless statement.
    A validator that searched the text for ";" or "DROP" would reject it wrongly -- which
    is the sort of false rejection that makes a boundary get relaxed later.
    """
    assert validate_sql("SELECT 1 -- ; DROP TABLE collisions").allowed is True


# --- Structural rejections: never a traceback --------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_empty_input_is_rejected(value):
    verdict = validate_sql(value)
    assert verdict.allowed is False
    assert verdict.reason == "empty"


def test_none_is_rejected_rather_than_crashing():
    assert validate_sql(None).allowed is False


def test_unparseable_sql_is_rejected_with_a_reason():
    """Passes gate 1 by its first word, then fails to parse."""
    verdict = validate_sql("SELECT FROM WHERE ((")
    assert verdict.allowed is False
    assert verdict.reason == "unparseable"


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "NOT SQL AT ALL !!", "SELECT FROM WHERE ((", "PRAGMA version",
     "SELECT 1; DROP TABLE t"],
)
def test_no_duckdb_error_ever_escapes(value):
    """Callers handle Verdicts. They must never have to know duckdb's exception types."""
    try:
        validate_sql(value)
    except duckdb.Error as error:
        pytest.fail(f"a raw duckdb error escaped validate_sql: {error!r}")


# --- The positive cases (spec §11.3) -----------------------------------------------
# Required, not optional. A validator that rejects everything is broken, and only these
# tests tell the difference.


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "SELECT * FROM collisions",
        "SELECT collision_severity, COUNT(*) FROM collisions GROUP BY 1 ORDER BY 1",
        "WITH counted AS (SELECT COUNT(*) AS n FROM collisions) SELECT n FROM counted",
        "SELECT * FROM collisions WHERE source_row_key IN (SELECT source_row_key FROM vehicles)",
        "SELECT a.source_row_key FROM collisions a JOIN vehicles b USING (accident_index)",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT date_trunc('month', datetime_local) AS m FROM collisions ORDER BY m",
    ],
)
def test_an_ordinary_read_is_allowed(query):
    verdict = validate_sql(query)
    assert verdict.allowed is True, f"wrongly rejected ({verdict.reason}): {verdict.detail}"
```

**A note on the reject list.** `DROP`, `DELETE`, `UPDATE`, `INSERT`, `CREATE`, `ATTACH`, `PRAGMA` and
`COPY` are the cases [spec.md §11.3](../../spec.md) requires and are not negotiable. If any one of
them is ever found to pass, **that is a finding, not a test to soften**: record it prominently in
your report and fix the validator, never the test.

---

## Testing & Verification

### Primary: this stage's suite, with every case visible

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest tests/test_safety.py -v
```

**Expected result:** green, with each rejection appearing as its own named case — seven
`test_a_statement_that_is_not_a_read_is_rejected` cases, seven
`test_other_non_read_statements_are_rejected` cases, and eight `test_an_ordinary_read_is_allowed`
cases, among the rest.

### The whole suite, offline

```bash
cd /Users/will/Documents/Code/AskCrossroads && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

**Expected result:** every test in `tests/test_package.py`, `tests/test_config.py`,
`tests/test_fixtures.py`, `tests/test_database.py`, `tests/test_introspection.py` and the new
`tests/test_safety.py` passes. Nothing that passed before this stage may now fail.

### The validator is genuinely independent of the rest of the project

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -n "^import\|^from" src/askcrossroads/safety.py
```

**Expected result:** `re`, `dataclasses` and `duckdb` only. **No `askcrossroads.` import.** If one
appears, stage 02 cannot test this module in isolation.

### Nothing about the schema leaked into `src/`

```bash
cd /Users/will/Documents/Code/AskCrossroads && grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/
```

**Expected result:** matches only in the `database.py` and `introspection.py` module docstrings, as
before this stage. `safety.py` must contribute **no** match — it validates a string and knows nothing
about any schema.

### The integration suite is untouched

```bash
cd /Users/will/Documents/Code/AskCrossroads && .venv/bin/python -m pytest -m integration -q
```

**Expected result:** one test collected, skipped, no unknown-marker warning.

### Nothing was committed

```bash
git status --short
```

**Expected result:** `src/askcrossroads/safety.py` modified and `tests/test_safety.py` untracked,
**nothing staged**.

### Stage ship-readiness checklist

- [ ] `safety.py` defines `Verdict`, `ALLOWED`, `_rejected` and `validate_sql`, and nothing else.
- [ ] Gate 1 matches against the **`sql` argument**, never against `statements[0].query`.
- [ ] Gate 1 runs before `extract_statements` is called.
- [ ] `PRAGMA version` is rejected with reason `not_a_read`, not `not_a_select`.
- [ ] `SELECT 1; DROP TABLE t` is rejected with reason `multiple_statements`.
- [ ] `SELECT 1 -- ; DROP TABLE t` is **allowed**.
- [ ] `validate_sql` returns a `Verdict` for `None`, `""` and unparseable text, and raises nothing.
- [ ] All eight positive queries are allowed.
- [ ] `safety.py` imports no project module.
- [ ] The full offline suite is green.
- [ ] Nothing staged or committed in git.

---

## End State / Handoff

**Observable state after this stage:**

- `src/askcrossroads/safety.py` holds `Verdict`, `ALLOWED`, `_rejected` and `validate_sql` with all
  three of its checks. The module is complete — stage 02 adds tests only.
- `tests/test_safety.py` exists and passes.
- No caller anywhere invokes `validate_sql` yet — `query.py` is later work, untouched here.
- The default `pytest` run is green with no environment variables set.
- Nothing is staged or committed.

**What the next stage may assume:**

- `validate_sql(sql)` exists, takes any value, and always returns a `Verdict` — it never raises.
- The reason codes `empty`, `not_a_read`, `multiple_statements`, `not_a_select` and `unparseable` are
  established and stable, and **no more are added** — there is no further check in this plan.
  File-reading and network-reaching functions pass `validate_sql` and are refused only by the
  connection settings in `database.py`; this is a deliberate decision recorded in the plan overview,
  not an omission for stage 02 to fill in.
- `tests/test_safety.py` exists with a positive-case parameterised test that stage 02 extends rather
  than replaces.

---

## Failure Modes & Rollback

| Failure | Cause | Recovery |
|---|---|---|
| `test_pragma_is_rejected_even_though_it_parses_as_a_select` fails | Gate 1 is reading `statements[0].query` instead of the `sql` argument, or runs after the parse | Move the gate to the top of the function and match against `sql`. This is the defect the whole stage exists to prevent — do not work around it by adding `PRAGMA` to a word blacklist |
| The same test fails with reason `not_a_select` rather than `not_a_read` | The gates are in the wrong order — the type check is firing first | Reorder. The rejection is currently correct by accident, and will stop being correct for other `PRAGMA` commands |
| `AttributeError: module 'duckdb' has no attribute 'StatementType'` | An older DuckDB than the project depends on | Confirm `.venv/bin/python -c "import duckdb; print(duckdb.__version__)"` reports 1.5 or newer; reinstall with `.venv/bin/pip install -e ".[dev]"` |
| `test_comment_smuggling_is_resolved_by_the_parser...` fails | Something is searching the text for `;` or for keywords rather than letting the parser decide | Remove the string search. Spec §5 is explicit that this case must resolve through the parser |
| `test_an_ordinary_read_is_allowed` fails on the CTE case | Gate 1's regex is anchored to `SELECT` only | The alternation must include `WITH`. Check the `\b` is present so `WITHOUT` would not match |
| `test_none_is_rejected_rather_than_crashing` raises `AttributeError` | The `isinstance` check is missing or runs after `.strip()` | The type check must come first, before any string method is called |
| A `duckdb.Error` escapes | `extract_statements` is called outside the `try`, or the except clause names a narrower type | Catch `duckdb.Error` — it is the base class of every error the driver raises |

**Rollback:** restore `src/askcrossroads/safety.py` to its docstring-only contents and delete
`tests/test_safety.py`. The previous plans' suites then pass unchanged. Nothing else is touched.
