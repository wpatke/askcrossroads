# Stage 04 — Chart Context, Chart Loop, End to End

> Part of *Query Orchestration*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular *Data flow for one question* and the
> distinct-count and numeric-type decisions.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stages 01–03 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -c "
from askcrossroads import query
print(query.QueryRunner, query.Answer, query.ChartAttempt, query.Busy, query.ANSWERED)
" && .venv/bin/python -m pytest tests/test_query.py -q
```

Expected: the names print and `tests/test_query.py` is green.

```bash
grep -n "def _answered" -A12 src/askcrossroads/query.py
```

Expected: `_answered(self, question, sql, result, sql_attempts)`, returning an `ANSWERED` `Answer`
with no chart. This stage replaces only that method's body and adds `result_shape` above the class.

---

## Objective

After the SQL succeeds with at least one row, describe the result's shape from the rows already in
memory, and ask the model for a chart through its own independent loop of `config.MAX_ATTEMPTS`
attempts. A chart failure never costs the user the table. Then prove the whole engine works against
the real API and a real build.

---

## Implementation Steps

### 1. `result_shape`

**File:** `src/askcrossroads/query.py`. Add after `execute`, before the `ANSWERED` constants.

```python
# DuckDB's own names for its numeric types. This is the engine's vocabulary, not knowledge of
# the data, so it does not breach the rule against hardcoding the schema (spec §2C).
_NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "FLOAT", "DOUBLE", "DECIMAL",
}


def _is_numeric(type_name):
    """True for a numeric DuckDB type. DECIMAL carries its precision, e.g. 'DECIMAL(18,3)',
    so only the part before any bracket is compared."""
    return type_name.split("(")[0] in _NUMERIC_TYPES


def result_shape(result):
    """Describe an executed result for the chart call, without sending the result (spec §7).

    Computed in Python from rows already in memory: no extra query, and apart from a few
    sample rows the result never leaves the machine.

    Distinct values are counted for non-numeric columns only, because three severities belong
    on an axis and several hundred district codes do not. Values are counted by repr()
    because a DuckDB LIST or STRUCT comes back as a Python list or dict, which a set cannot
    hold. NULL counts as one distinct value.
    """
    distinct_counts = tuple(
        (name, len({repr(row[index]) for row in result.rows}))
        for index, (name, type_name) in enumerate(result.columns)
        if not _is_numeric(type_name)
    )
    return prompt.ResultShape(
        columns=result.columns,
        row_count=len(result.rows),
        distinct_counts=distinct_counts,
        sample_rows=result.rows[: config.CHART_SAMPLE_ROWS],
    )
```

`row_count` is the number of rows **returned**, which after truncation is the cap. Spec §10 says
the chart reflects only those rows, and this is what tells the model that.

### 2. The chart loop

**File:** `src/askcrossroads/query.py`. Replace the body of `QueryRunner._answered`:

```python
    def _answered(self, question, sql, result, sql_attempts):
        """A successful result, plus a chart if one can be had (spec §7).

        The chart has its own loop of config.MAX_ATTEMPTS tries, independent of the SQL
        loop. It runs only now, after the SQL has succeeded, so the model is only ever asked
        to plot columns that demonstrably exist. If every try fails, the table is still
        returned: a chart is an enhancement, and losing it must never cost the user their
        answer.
        """
        chart = None
        chart_attempts = []

        # An empty result has nothing to plot, so no chart call is made (spec §6).
        if result.rows:
            shape = result_shape(result)
            for _ in range(config.MAX_ATTEMPTS):
                if self._stopped():
                    return Answer(status=CANCELLED, sql_attempts=sql_attempts)
                failures = tuple((None, attempt.error) for attempt in chart_attempts)
                try:
                    spec = self._model.describe_chart(question, sql, shape, failures)
                except llm.MalformedResponse as error:
                    chart_attempts.append(ChartAttempt(error=str(error)))
                    continue
                except llm.ModelError as error:
                    # Not repairable. The table stands on its own.
                    chart_attempts.append(ChartAttempt(error=str(error)))
                    break
                chart_attempts.append(ChartAttempt(error=None))
                # Exactly as the model returned it. Python never edits a chart spec (spec §7).
                chart = spec
                break

        if self._stopped():
            # Stop arrived while the last chart call was in flight.
            return Answer(status=CANCELLED, sql_attempts=sql_attempts)

        return Answer(
            status=ANSWERED,
            sql=sql,
            sql_attempts=sql_attempts,
            columns=result.columns,
            rows=result.rows,
            truncated=result.truncated,
            chart=chart,
            chart_attempts=tuple(chart_attempts),
        )
```

### 3. Share the integration fixtures

`tests/integration/test_prompt_and_model.py` (from the previous plan) defines module-scoped
`settings`, `database` and `client` fixtures that skip cleanly without a key or a build. The new
end-to-end test needs exactly the same three, so move them rather than copy them.

- Create `tests/integration/live_fixtures.py` holding `ENV_TEST_DB`, `settings`, `database` and
  `client`, moved **verbatim** with their imports and docstrings, plus a module docstring saying
  they are shared fixtures that skip, never fail, without a key or a build (spec §11.2).
- **Do not call it `conftest.py`.** The offline tests import their test double with
  `from conftest import FakeModel`. A second module named `conftest` under `tests/integration/`
  shadows `tests/conftest.py`, and that import then fails during collection. This was found while
  implementing this stage.
- Import the fixtures by name in each integration file that uses them:
  `from live_fixtures import client, database, settings  # noqa: F401  (pytest fixtures)`.
- Delete those definitions from `tests/integration/test_prompt_and_model.py`. Remove any imports
  that file no longer uses. Leave its `ANY_BUILD_QUESTION` and its tests alone.
- Leave `tests/integration/test_conventions.py` untouched. Its `real_build` fixture is a different,
  key-free fixture.

Confirm nothing changed behaviourally:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_TEST_DB .venv/bin/python -m pytest -m integration -q -rs
```

Expected: every integration test **skipped**, with reasons naming the missing variable, and no
errors.

### 4. The end-to-end test

**File:** `tests/integration/test_end_to_end.py`. New. This is the file spec §12's blueprint names.

```python
"""A real question through the whole engine: real API, real build (spec §11.2).

Asserts on shape and execution, never on wording. The model is not deterministic (spec §8),
so one failure here is a signal about the prompt rather than proof of a bug; a repeated
failure is a bug.

Skips, never fails, without an API key or a real build. Each run costs a few model calls
against the runner's own key.
"""

import pytest

from askcrossroads import config, query
from askcrossroads.safety import validate_sql

pytestmark = pytest.mark.integration

# Phrased so that any Crossroads-UK build can answer it: every build has dated records. It
# names no table or column (spec §2C).
GROUPED_QUESTION = "How many records are there for each year this database covers?"

# Nothing in any road-safety build can answer this.
ABSENT_QUESTION = "What was the average price of a loaf of bread in each region last year?"


@pytest.fixture(scope="module")
def runner(database, client):
    return query.QueryRunner(database, client)


def test_a_real_question_is_answered_with_a_table_and_a_chart(runner):
    answer = runner.ask(GROUPED_QUESTION)

    assert answer.status == query.ANSWERED, (answer.error, answer.explanation, answer.sql_attempts)
    assert answer.rows, "an answerable question returned no rows"
    assert validate_sql(answer.sql).allowed
    assert answer.sql_attempts[-1] == query.Attempt(sql=answer.sql, error=None)
    assert len(answer.sql_attempts) <= config.MAX_ATTEMPTS

    # The chart: a Vega-Lite spec with a mark and an encoding and no data (spec §7).
    assert answer.chart is not None, answer.chart_attempts
    assert "mark" in answer.chart
    assert isinstance(answer.chart["encoding"], dict)
    assert "data" not in answer.chart
    assert len(answer.chart_attempts) <= config.MAX_ATTEMPTS

    # Every field the chart encodes must be a real result column. That is the point of asking
    # after execution (spec §7). Skipped when the spec derives fields of its own.
    if "transform" not in answer.chart:
        names = {name for name, _ in answer.columns}
        for channel in answer.chart["encoding"].values():
            if isinstance(channel, dict) and "field" in channel:
                assert channel["field"] in names, (channel, names)


def test_an_absent_question_is_declined_without_running_sql(runner):
    answer = runner.ask(ABSENT_QUESTION)

    assert answer.status == query.DECLINED, answer.sql_attempts
    assert answer.explanation
    assert answer.sql is None
    assert answer.chart is None
```

---

## Testing & Verification

### Unit tests for `result_shape` (append to `tests/test_query.py`)

```python
from decimal import Decimal
```

| Test | Input | Assertion |
|---|---|---|
| `test_shape_counts_distinct_values_of_non_numeric_columns_only` | `query.ExecutionResult(columns=(("weather", "VARCHAR"), ("n", "BIGINT"), ("amount", "DECIMAL(18,3)")), rows=(("a", 1, Decimal("1.5")), ("a", 2, Decimal("2.5")), ("b", 3, Decimal("1.5")), (None, 4, Decimal("9"))), truncated=False)` | `shape.distinct_counts == (("weather", 3),)`; `shape.row_count == 4`; `shape.columns` is the input's columns |
| `test_shape_samples_at_most_the_configured_rows` | 20 rows of `(i,)` with column `("n", "BIGINT")` | `shape.sample_rows == result.rows[: config.CHART_SAMPLE_ROWS]`; `row_count == 20` |
| `test_shape_handles_list_values` | `query.execute(database.connection, "SELECT [range, range] AS pair, range AS n FROM range(3) ORDER BY n")` then `result_shape` | no exception; `shape.distinct_counts == (("pair", 3),)` |
| `test_numeric_types_are_recognised` | `_is_numeric` on `"BIGINT"`, `"DOUBLE"`, `"DECIMAL(18,3)"`, `"UBIGINT"` is True; on `"VARCHAR"`, `"DATE"`, `"TIMESTAMP"`, `"BIGINT[]"`, `"BOOLEAN"` is False | as stated |

### The chart loop (append to `tests/test_query.py`)

Add near the other helpers:

```python
CHART = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "weather", "type": "nominal"},
        "y": {"field": "collisions", "type": "quantitative"},
    },
}


class BlockingChartModel(FakeModel):
    """A FakeModel that pauses inside describe_chart until the test releases it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def describe_chart(self, question, sql, shape, failures=()):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("the test never released the model")
        return super().describe_chart(question, sql, shape, failures)
```

| Test | Model queues | Assertions |
|---|---|---|
| `test_the_chart_call_comes_after_the_sql_and_sees_the_real_columns` | sql `[sql(GOOD_SQL)]`, chart `[CHART]` | `ANSWERED`; `[call[0] for call in model.calls] == ["generate_sql", "describe_chart"]`; the chart call's `sql == GOOD_SQL`, its `shape.columns == answer.columns` (which came from DuckDB), `shape.row_count == len(answer.rows)`, and its failures `== ()` (spec §11.4) |
| `test_the_chart_spec_is_returned_unedited` | chart `[CHART]` | `answer.chart is CHART`, the same object; `chart_attempts == (query.ChartAttempt(None),)` |
| `test_zero_rows_makes_no_chart_call` | sql `[sql("SELECT * FROM collisions WHERE 1 = 0")]`, chart `[]` | `ANSWERED`; `rows == ()`; `chart is None`; `chart_attempts == ()`; no `describe_chart` in `model.calls` (spec §6). An empty chart queue means any chart call would fail loudly |
| `test_a_malformed_chart_is_repaired_with_the_error_fed_back` | chart `[llm.MalformedResponse("The chart specification had no mark."), CHART]` | `answer.chart is CHART`; `chart_attempts == (ChartAttempt("The chart specification had no mark."), ChartAttempt(None))`; the second chart call's failures `== ((None, "The chart specification had no mark."),)` |
| `test_three_chart_failures_still_return_the_table` | chart: three `MalformedResponse`s | `ANSWERED`; `rows` non-empty and equal to a direct `query.execute(database.connection, GOOD_SQL).rows`; `chart is None`; `len(chart_attempts) == 3`, each with an error; exactly 3 `describe_chart` calls (spec §7, §11.4) |
| `test_a_chart_model_error_still_returns_the_table` | chart `[llm.ModelError("The Anthropic API did not respond in time.")]` | `ANSWERED`; rows present; `chart is None`; `chart_attempts == (ChartAttempt("The Anthropic API did not respond in time."),)`; one `describe_chart` call |
| `test_the_two_loops_have_independent_budgets` | sql `[sql(BAD_SQL), sql("SELECT nope FROM collisions"), sql(GOOD_SQL)]`, chart `[Malformed, Malformed, CHART]` | `ANSWERED` with `chart is CHART`; `len(model.calls) == 6`, the spec §4 worst case; 3 SQL attempts, 3 chart attempts |
| `test_a_truncated_result_is_charted_from_the_capped_rows` | cap patched to 3; sql `[sql("SELECT range AS n, CAST(range AS VARCHAR) AS label FROM range(10) ORDER BY n")]`, chart `[CHART]` | `answer.truncated is True`; the chart call's `shape.row_count == 3` |
| `test_stop_during_the_chart_call_cancels` | `BlockingChartModel(sql_responses=[sql(GOOD_SQL)], chart_responses=[CHART])`; `ask_in_background`; `entered.wait(5)`; `cancel()`; `release.set()`; `join(5)` | `CANCELLED`; `answer.chart is None` |

The stage 03 test `test_a_good_query_is_answered_first_time` asserted `chart is None` and
`model.calls == [("generate_sql", QUESTION, ())]`. With the chart loop in place, that test now makes
a chart call. Give it `chart_responses=[CHART]` and update its two assertions to `chart is CHART` and
the two-call list. Search for other stage 03 tests whose `ANSWERED` path now reaches the chart call
with an empty chart queue, and give each one `chart_responses=[CHART]`:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest tests/test_query.py -q
```

Any test that fails with *"describe_chart was called more times than this test queued replies
for"* needs `chart_responses=[CHART]`. For the background-thread tests (Busy, the slow-query Stop
test's follow-up question), the same error surfaces as an `AssertionError` in `outcome["error"]`, or
as a `KeyError` on `outcome["answer"]`. Treat it the same way. That is the only change those tests
need. The ones whose
result is empty (zero rows) correctly need nothing.

### Integration: the real thing

```bash
.venv/bin/python -m pytest -m integration tests/integration/test_end_to_end.py -v
```

Run with `ANTHROPIC_API_KEY` set (in the environment or `.env`) and `ASKCROSSROADS_TEST_DB` pointing
at a real Crossroads-UK build. Expected: both tests pass. Cost: roughly three to five model calls,
with the prefix cached after the first.

Then prove the skip path:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_TEST_DB .venv/bin/python -m pytest -m integration -q -rs
```

Expected: everything skipped, nothing failed, nothing errored. If a `.env` in the repository root
supplies the key, the key-missing skip will not trigger. Unsetting `ASKCROSSROADS_TEST_DB` alone is
enough to prove the build-missing skip.

### Commands

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

```bash
for i in 1 2 3; do env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest tests/test_query.py -q || break; done
```

```bash
grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/ ; grep -rniE "labelled|_label" src/askcrossroads/prompt.py src/askcrossroads/query.py ; grep -n "Thread(" src/askcrossroads/query.py ; echo "all three greps above must print nothing"
```

```bash
grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .
```

Expected: nothing.

### Stage ship-readiness checklist (also the plan's)

1. The default suite is green, and `tests/test_query.py` is green three runs in a row.
2. `pytest -m integration` passes with a key and a real build, and skips cleanly without them.
3. All greps above print nothing.
4. `grep -n "LIMIT" src/askcrossroads/query.py` shows only docstring or comment lines.
5. `askcrossroads` pointed at a valid build still prints its provenance line and exits 0.
6. `git status` shows the changes and **nothing staged**.

---

## End State / Handoff

This is the contract the next plan, the HTTP API, builds on:

- `query.QueryRunner(database, model)`: create once at startup with the opened `Database` and an
  `llm.AnthropicClient`. `ask(question) -> Answer` blocks and must run on a worker thread (a
  FastAPI sync route does this). `cancel()` may be called from any thread and is a no-op when idle.
  `ask()` raises `query.Busy` when a question is already running.
- `Answer` fields map onto spec §9's `/api/ask` response:

  | `/api/ask` field | From `Answer` |
  |---|---|
  | `answerable` | `status != DECLINED` |
  | `sql` | `sql` |
  | `sql_attempts[]` | `sql_attempts`, each `{sql, error}` |
  | `columns` | `columns`, each `(name, type)` |
  | `rows` | `rows`. **Raw Python values** (`date`, `datetime`, `Decimal`, lists). JSON encoding is the HTTP layer's job |
  | `truncated` | `truncated` |
  | `chart` | `chart`, a dict or `None` |
  | `chart_attempts[]` | `chart_attempts`, each `{error}` |
  | `explanation` | `explanation` |
  | (not in spec §9) | `status` and `error`, which the HTTP plan adds so the browser can tell failed and cancelled apart from answered |

- `result_shape(result)` builds the spec §7 chart context from in-memory rows.
- The integration fixtures live in `tests/integration/live_fixtures.py`, ready for the HTTP and
  answer-set plans to reuse.

---

## Failure Modes & Rollback

| Failure | Why it happens | Guardrail |
|---|---|---|
| A chart failure loses the table | Returning early, or raising, from the chart loop | `test_three_chart_failures_still_return_the_table`, `test_a_chart_model_error_still_returns_the_table` |
| A chart call for an empty result | The `if result.rows` guard removed | `test_zero_rows_makes_no_chart_call` with an empty chart queue |
| The spec gets "fixed up" in Python | Adding a sort, or injecting data | `test_the_chart_spec_is_returned_unedited` asserts identity |
| `result_shape` crashes on a LIST column | `set(values)` on unhashable values | `repr()`; `test_shape_handles_list_values` |
| Integration suite errors instead of skipping after the fixture move | A fixture left behind, or an import removed that is still used | The skip-path command in step 3 |
| The live chart encodes a column that does not exist | Prompt quality | The field check in the end-to-end test. A single failure is a signal about the prompt (spec §8), not a code bug |

**Rollback:** restore `_answered` to stage 03's body. Delete `result_shape`, `_is_numeric`,
`_NUMERIC_TYPES`, this stage's tests, and `tests/integration/test_end_to_end.py`. Move the three
fixtures back into `tests/integration/test_prompt_and_model.py` and delete
`tests/integration/live_fixtures.py`. No commit is made at any point.
