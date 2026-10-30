# Stage 02 — Capped Execution

> Part of *Query Orchestration*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting Constraints and the
> row-cap decision.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 01 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -c "
import inspect
from askcrossroads import llm, prompt
assert 'failures' in inspect.signature(llm.ModelClient.generate_sql).parameters
assert 'failures' in inspect.signature(prompt.chart_user_message).parameters
print('stage 01 present')
" && cat src/askcrossroads/query.py
```

Expected: `stage 01 present`, then `query.py` containing only its one-line docstring:
`"""Query execution, the bounded repair loops, the row cap and cancellation (spec §4, §5, §7)."""`

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -q
```

Expected: green.

---

## Objective

Run one SQL string against the open connection exactly as written, and return its columns, with
DuckDB's own type names, plus at most `config.MAX_RESULT_ROWS` rows and a flag saying whether more
existed. First, confirm by direct probe the engine behaviours stages 03 and 04 depend on.

---

## Implementation Steps

### 1. Probe the engine. Do not skip this step.

Stages 03 and 04 depend on five DuckDB behaviours that no earlier plan verified. Check them against
the installed engine (`duckdb` 1.5.x in `.venv`) before writing code. This script writes nothing to
the repository and needs no fixture.

```bash
.venv/bin/python - <<'EOF'
import threading, time, duckdb

print("duckdb", duckdb.__version__)
con = duckdb.connect(":memory:", config={"enable_external_access": False})

# (a) What does description[i][1] look like, and does str() of it give the SQL type name?
con.execute("SELECT COUNT(*) AS total, 'x' AS label, DATE '2023-01-01' AS d, "
            "CAST(1.5 AS DECIMAL(18,3)) AS amount, [1, 2] AS xs")
print("(a)", [(d[0], type(d[1]).__name__, str(d[1])) for d in con.description])

# (b) fetchmany caps, and returns tuples
con.execute("SELECT range AS n FROM range(10) ORDER BY n")
print("(b)", con.fetchmany(3))

# (c) Is InterruptException a subclass of duckdb.Error?
print("(c)", issubclass(duckdb.InterruptException, duckdb.Error))

# (d) interrupt() from another thread stops a running query, and the connection survives
SLOW = "SELECT SUM(range) AS total FROM range(1000000000000)"
outcome = {}
def run():
    try:
        con.execute(SLOW)
        outcome["result"] = con.fetchall()
    except Exception as error:
        outcome["error"] = type(error).__name__
worker = threading.Thread(target=run)
worker.start()
time.sleep(1.0)
started = time.monotonic()
con.interrupt()
worker.join(timeout=10)
print("(d)", outcome, "alive:", worker.is_alive(),
      "seconds to stop:", round(time.monotonic() - started, 2))
print("(d) after:", con.execute("SELECT 1 AS one").fetchall())

# (e) interrupt() on an IDLE connection does not poison the next query
con.interrupt()
print("(e)", con.execute("SELECT 2 AS two").fetchall())

# (f) Streaming or materialised? Peak memory is the tell; just record the time to first rows.
started = time.monotonic()
con.execute("SELECT range AS n FROM range(50000000)")
first = con.fetchmany(5001)
print("(f) seconds to first 5001 of 50M rows:", round(time.monotonic() - started, 2))
EOF
```

What each result must be, and what to do if it is not:

| Probe | Required | If not |
|---|---|---|
| (a) | `str(description[i][1])` gives `BIGINT`, `VARCHAR`, `DATE`, `DECIMAL(18,3)`, `INTEGER[]` | If it gives DB-API codes such as `NUMBER`/`STRING`, use **Fallback A** in step 3 below. |
| (b) | three tuples `[(0,), (1,), (2,)]` | Stop and report. |
| (c) | `True` | Stage 03 must catch `(duckdb.Error, duckdb.InterruptException)` wherever it catches `duckdb.Error`. Record it in the report. |
| (d) | `outcome` holds an `error` naming an interrupt, `alive: False`, seconds to stop well under 5, and `after` is `[(1,)]` | **Stop and report.** Stop/cancel cannot work as designed, and this plan needs revisiting. |
| (e) | `[(2,)]` | `cancel()` in stage 03 is already guarded to interrupt only while executing. Record it, because that guard then becomes load-bearing rather than belt-and-braces. |
| (f) | any number; just record it | If it is a second or more (streaming would return near-instantly), DuckDB is materialising before `fetchmany`. Record it in the report as the overview's Open Question answered. Do not change the design. |

Paste the probe's output into your stage report.

### 2. Write the module header

**File:** `src/askcrossroads/query.py`. Replace the file. Keep the existing line as the first line
of an expanded docstring:

```python
"""Query execution, the bounded repair loops, the row cap and cancellation (spec §4, §5, §7).

This module is the engine room. Given a question, it asks the model for SQL, checks it with
the safety validator, runs it against the user's read-only database, repairs it within strict
bounds when it fails, and then asks for a chart (spec §4). The model is handed in as an
llm.ModelClient, so every path here can be tested offline with a scripted stand-in.

The SQL is ALWAYS run exactly as the model wrote it. The row cap is applied by fetching one row
more than the cap and truncating, never by adding a LIMIT to the query, because the SQL shown
to the user must be the SQL that ran (spec §8, §10).
"""

import threading
from dataclasses import dataclass

import duckdb

from askcrossroads import config, llm, prompt, safety
```

`threading`, `llm`, `prompt` and `safety` are not used until stages 03 and 04. Import them now
anyway so the import block is written once. If a linter complains about unused imports, that is
expected for this stage only.

### 3. `ExecutionResult` and `execute`

**File:** `src/askcrossroads/query.py`

```python
@dataclass(frozen=True)
class ExecutionResult:
    """What one executed query returned, capped at config.MAX_RESULT_ROWS rows.

    columns    ((name, type_name), ...) in result order. type_name is DuckDB's own name
               for the type: "VARCHAR", "BIGINT", "DATE", "DECIMAL(18,3)", "INTEGER[]".
    rows       a tuple of row tuples, in the order the SQL returned them. The SQL is told to
               sort its result (spec §8), so this IS the display order.
    truncated  True when the query produced more rows than the cap (spec §10).
    """

    columns: tuple
    rows: tuple
    truncated: bool


def execute(connection, sql):
    """Run `sql` exactly as written and return at most config.MAX_RESULT_ROWS rows.

    Fetches one row MORE than the cap: if that extra row exists, the result was bigger than
    the cap and is reported as truncated (spec §10). Nothing is added to the SQL.

    Raises duckdb.Error when the query fails, including when it is interrupted by Stop. The
    caller decides what a failure means. This function does not catch anything.

    The connection is the application's one read-only connection, with external access
    switched off (database.py). Those settings, not this function, are what stop a write or a
    file read.
    """
    connection.execute(sql)
    columns = tuple((name, str(type_code)) for name, type_code, *_ in connection.description)
    rows = connection.fetchmany(config.MAX_RESULT_ROWS + 1)
    truncated = len(rows) > config.MAX_RESULT_ROWS
    return ExecutionResult(
        columns=columns,
        rows=tuple(tuple(row) for row in rows[: config.MAX_RESULT_ROWS]),
        truncated=truncated,
    )
```

**Fallback A.** Use this only if probe (a) showed DB-API codes. Replace the `columns =` line with a
relation-based read of the types. Probe first that `connection.sql(sql).types` gives SQL type names
and that `connection.sql(sql).fetchmany(n)` honours `interrupt()`, by re-running probe (d) with
`con.sql(SLOW).fetchall()`. Then restructure `execute` as:

```python
    relation = connection.sql(sql)
    columns = tuple(zip(relation.columns, (str(t) for t in relation.types)))
    rows = relation.fetchmany(config.MAX_RESULT_ROWS + 1)
```

Record the deviation. The End State does not change: column type names must be DuckDB SQL type
names.

---

## Testing & Verification

### Tests: `tests/test_query.py` (new file)

These run against a **real** DuckDB database: the synthetic Crossroads-UK fixture, opened through
`Database.open`, so the connection has the production read-only and no-external-access settings.
Most queries use DuckDB's built-in `range()` table function, which needs no table and no external
access and makes row counts exact.

```python
"""Query orchestration: execution, the repair loops, cancellation and charts (spec §4, §5, §7).

Every test here runs against a real DuckDB database, the synthetic Crossroads-UK fixture opened
the way the application opens it, so the read-only and no-external-access settings are the
production ones. Only the model is scripted (FakeModel), because the default suite must be
offline and deterministic (spec §11.1).
"""

import duckdb
import pytest

from askcrossroads import config, query
from askcrossroads.database import Database


@pytest.fixture
def database(crossroads_db):
    """The application's own Database object over a synthetic build."""
    with Database.open(crossroads_db) as opened:
        yield opened


class RecordingConnection:
    """Wraps a real connection and records every SQL string it is asked to execute.

    This is how a test proves the SQL reached DuckDB exactly as written, with no LIMIT added
    (spec §8, §10).
    """

    def __init__(self, connection):
        self._connection = connection
        self.executed = []

    def execute(self, sql, *args):
        self.executed.append(sql)
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)
```

| Test | Setup | Assertion |
|---|---|---|
| `test_columns_carry_duckdb_type_names` | `query.execute(database.connection, "SELECT COUNT(*) AS total, 'x' AS label, DATE '2023-01-01' AS d, CAST(1.5 AS DECIMAL(18,3)) AS amount")` | `result.columns == (("total", "BIGINT"), ("label", "VARCHAR"), ("d", "DATE"), ("amount", "DECIMAL(18,3)"))` |
| `test_rows_arrive_in_the_order_the_sql_sorted_them` | `SELECT range AS n FROM range(5) ORDER BY n DESC` | `result.rows == ((4,), (3,), (2,), (1,), (0,))` and `result.truncated is False` |
| `test_rows_are_tuples` | any query | `isinstance(result.rows, tuple)` and every row `isinstance(row, tuple)` |
| `test_a_result_exactly_at_the_cap_is_not_truncated` | `f"SELECT range FROM range({config.MAX_RESULT_ROWS}) ORDER BY range"` | `len(result.rows) == config.MAX_RESULT_ROWS`, `truncated is False` |
| `test_a_result_one_over_the_cap_is_truncated` | `range(config.MAX_RESULT_ROWS + 1)` | `len(result.rows) == config.MAX_RESULT_ROWS`, `truncated is True`, and the last kept row is `(config.MAX_RESULT_ROWS - 1,)`, so the *first* rows were kept |
| `test_the_cap_is_read_from_config_at_call_time` | `monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)`, then `range(10)` | 3 rows, `truncated is True` |
| `test_the_sql_reaches_duckdb_unmodified` | `conn = RecordingConnection(database.connection)`; `sql = "SELECT range FROM range(10) ORDER BY range"`; `query.execute(conn, sql)` with the cap patched to 3 | `conn.executed == [sql]`. No `LIMIT`, no wrapping, no whitespace change |
| `test_an_empty_result_is_not_an_error` | `SELECT range FROM range(10) WHERE range < 0` | `rows == ()`, `truncated is False`, `columns == (("range", "BIGINT"),)` |
| `test_a_bad_query_raises_a_duckdb_error` | `SELECT no_such_column FROM range(1)` | `pytest.raises(duckdb.Error)` |
| `test_a_write_is_still_refused_by_the_engine` | `CREATE TABLE t (x INTEGER)` passed **directly** to `query.execute`, bypassing the validator | `pytest.raises(duckdb.Error)`. Spec §11.4: the engine guard holds even if the validator were bypassed |
| `test_a_file_read_is_refused_at_execution` | `SELECT * FROM read_csv_auto('/etc/hosts')` | `pytest.raises(duckdb.Error)`. Spec §5: the no-external-access layer is what stops this |
| `test_real_fixture_data_executes` | `SELECT COUNT(*) AS n FROM collisions` | one row, `n` is a positive integer. This proves the fixture tables, not only `range()`, run through `execute` |

The DuckDB type name for `range` is `BIGINT`. If probe (a) shows otherwise, use what it shows and
note it.

### Commands

```bash
.venv/bin/python -m pytest tests/test_query.py -v
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

```bash
grep -n "LIMIT" src/askcrossroads/query.py
```

Expected: only lines inside the docstring or comments.

```bash
grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/
```

Expected: nothing.

### Stage ship-readiness checklist

1. The probe output is in the stage report, with probes (b) and (d) passing.
2. The default suite is green.
3. `query.py` contains no code that adds `LIMIT`.
4. `git status` shows `src/askcrossroads/query.py` modified and `tests/test_query.py` new, and
   **nothing staged**.

---

## End State / Handoff

- `query.ExecutionResult(columns, rows, truncated)`, frozen. Columns are `(name, DuckDB type name)`
  pairs. Rows are a tuple of tuples in SQL order.
- `query.execute(connection, sql)` runs `sql` unmodified, returns at most `config.MAX_RESULT_ROWS`
  rows (read at call time), sets `truncated` exactly when more existed, and lets `duckdb.Error`
  propagate.
- The probe results are recorded. In particular, it is known whether `InterruptException`
  subclasses `duckdb.Error` (c), and that interrupt from another thread works and leaves the
  connection usable (d).
- `tests/test_query.py` exists with the `database` fixture and `RecordingConnection`, ready for
  stages 03 and 04 to append to.

---

## Failure Modes & Rollback

| Failure | Why it happens | Guardrail |
|---|---|---|
| A `LIMIT` sneaks into the SQL | "Optimising" the cap | `test_the_sql_reaches_duckdb_unmodified`; the grep |
| The cap keeps the wrong rows | Slicing from the end, or fetching all then slicing | `test_a_result_one_over_the_cap_is_truncated` checks the last kept row |
| A test patch of the cap has no effect | `from askcrossroads.config import MAX_RESULT_ROWS` binds the value at import | `test_the_cap_is_read_from_config_at_call_time` |
| Type names are DB-API codes | Engine version difference | Probe (a) and Fallback A; `test_columns_carry_duckdb_type_names` |
| Stop cannot work | Interrupt does not cross threads on this engine version | Probe (d) is a hard stop before any code is written |

**Rollback:** restore `src/askcrossroads/query.py` to its one-line docstring and delete
`tests/test_query.py`. No other file is touched by this stage. No commit is made at any point.
