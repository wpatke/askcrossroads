# Stage 03 — The Runner and the SQL Repair Loop

> Part of *Query Orchestration*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular *Data flow for one question*,
> *Stop, precisely*, *Threading: the caller owns the thread*, and the decisions table.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stages 01 and 02 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -c "
from askcrossroads import query
print(query.ExecutionResult, query.execute)
" && .venv/bin/python -m pytest tests/test_query.py -q
```

Expected: both names print, and `tests/test_query.py` is green.

**Read stage 02's report for probe (c).** If `duckdb.InterruptException` is **not** a subclass of
`duckdb.Error`, then everywhere this stage writes `except duckdb.Error`, write
`except (duckdb.Error, duckdb.InterruptException)` instead.

---

## Objective

Add `QueryRunner`: one question at a time, cancellable, running the bounded SQL repair loop of spec
§4 and returning one `Answer` for every outcome. An answered question has no chart yet; stage 04
adds it.

---

## Implementation Steps

All code goes in `src/askcrossroads/query.py`, after `execute`.

### 1. Outcomes and attempt records

```python
# The four ways a question can end. Plain strings, so the HTTP layer can pass them straight
# to the browser.
ANSWERED = "answered"    # SQL ran. The result may be empty: zero rows is an answer (spec §4.2)
DECLINED = "declined"    # the model said this build cannot answer the question (spec §6)
FAILED = "failed"        # every attempt failed, or the model could not be reached
CANCELLED = "cancelled"  # the user pressed Stop


@dataclass(frozen=True)
class Attempt:
    """One try at writing and running the SQL, kept for the attempt history (spec §4.5).

    sql    the SQL the model wrote, or None when its reply could not be used at all
    error  why the attempt failed, verbatim: a DuckDB error, the safety validator's reason, or
           a malformed-reply message. None for the attempt that succeeded.
    """

    sql: str | None
    error: str | None


@dataclass(frozen=True)
class ChartAttempt:
    """One try at describing the chart (spec §7). error is None for the attempt that worked."""

    error: str | None


@dataclass(frozen=True)
class Answer:
    """Everything one question produced. Field names follow spec §9's /api/ask response.

    status          ANSWERED, DECLINED, FAILED or CANCELLED
    sql             the SQL that produced `rows`; set only when ANSWERED
    explanation     the model's reason for declining; set only when DECLINED
    error           why the question ended without an answer; set only when FAILED. Either
                    the last attempt's error, verbatim, or a sanitised provider message.
    sql_attempts    every SQL attempt, in order, including the one that worked (spec §4.5)
    columns         ((name, DuckDB type name), ...) of the result
    rows            the result rows, at most config.MAX_RESULT_ROWS, in SQL order
    truncated       True when the result was capped (spec §10)
    chart           the Vega-Lite spec exactly as the model returned it, or None
    chart_attempts  every chart attempt, in order
    """

    status: str
    sql: str | None = None
    explanation: str | None = None
    error: str | None = None
    sql_attempts: tuple = ()
    columns: tuple = ()
    rows: tuple = ()
    truncated: bool = False
    chart: dict | None = None
    chart_attempts: tuple = ()


class Busy(Exception):
    """A question is already running (spec §5).

    v1 runs one question at a time, because interrupt() cancels the whole connection rather
    than one query: with two questions in flight, Stop would kill both. A second question is
    rejected rather than queued.
    """
```

### 2. `QueryRunner`

```python
class QueryRunner:
    """Answers questions against the one open database, one question at a time.

    Created once, at startup, and shared. `ask()` blocks until the question is finished;
    `cancel()` may be called from any other thread while it runs. This class creates no
    threads of its own: the caller's thread is the worker thread spec §5 describes.

    `database` is the application's Database: only its `.connection` and `.context` are used.
    `model` is any llm.ModelClient.
    """

    def __init__(self, database, model):
        self._connection = database.connection
        self._context = database.context
        self._model = model
        # Held for the whole of one question. Acquired without waiting, so a second
        # question is refused at once rather than queued.
        self._lock = threading.Lock()
        # Set by cancel(). Checked before and after every model call and every query, so
        # once Stop is pressed nothing new is started.
        self._cancelled = threading.Event()
        self._executing = False

    @property
    def executing(self):
        """True while a query is running on the connection."""
        return self._executing

    def ask(self, question):
        """Answer one question. Returns an Answer. Raises Busy if one is already running."""
        if not self._lock.acquire(blocking=False):
            raise Busy("A question is already being answered. Wait for it, or press Stop.")
        try:
            # A Stop pressed for an earlier question must not cancel this one.
            self._cancelled.clear()
            return self._answer(question)
        finally:
            self._lock.release()

    def cancel(self):
        """Stop the current question (spec §5, §9). A no-op when nothing is running.

        Stops a running query at once. A model call already in flight is NOT aborted: it
        finishes, its reply is discarded, and nothing further is started. The browser
        stops waiting on its own, so the user is not kept waiting for that call.
        """
        self._cancelled.set()
        # Only interrupt while a query is actually running. An interrupt on an idle
        # connection has nothing to stop.
        if self._executing:
            self._connection.interrupt()

    def _stopped(self):
        return self._cancelled.is_set()

    def _execute(self, sql):
        """Run one query, marking it as running so cancel() knows to interrupt it."""
        self._executing = True
        try:
            return execute(self._connection, sql)
        finally:
            self._executing = False
```

### 3. The SQL repair loop

```python
    def _answer(self, question):
        """The bounded SQL loop of spec §4, then the result.

        Exactly config.MAX_ATTEMPTS tries. A try FAILS, and is fed back to the model, when:
        the reply is malformed, the safety validator rejects the SQL, or the SQL raises an
        error when run. Nothing else is retried:

        - zero rows is an answer, never retried (spec §4.2);
        - a decline is an answer, never retried (spec §6);
        - a ModelError (auth, rate limit, network, policy refusal) would fail the same way
          again, so the question ends there;
        - an error caused by Stop is not the SQL's fault, so it is not an attempt at all.
        """
        attempts = []
        for _ in range(config.MAX_ATTEMPTS):
            if self._stopped():
                return Answer(status=CANCELLED, sql_attempts=tuple(attempts))

            # Every earlier attempt failed (a success returns below), so all of them are
            # fed back.
            failures = tuple((attempt.sql, attempt.error) for attempt in attempts)
            try:
                reply = self._model.generate_sql(self._context, question, failures)
            except llm.MalformedResponse as error:
                attempts.append(Attempt(sql=None, error=str(error)))
                continue
            except llm.ModelError as error:
                return Answer(status=FAILED, error=str(error), sql_attempts=tuple(attempts))

            if self._stopped():
                return Answer(status=CANCELLED, sql_attempts=tuple(attempts))

            if not reply.answerable:
                return Answer(
                    status=DECLINED,
                    explanation=reply.explanation,
                    sql_attempts=tuple(attempts),
                )

            # Validated on EVERY attempt: a repaired query is new, unchecked model output
            # (spec §4.4).
            verdict = safety.validate_sql(reply.sql)
            if not verdict.allowed:
                attempts.append(Attempt(sql=reply.sql, error=verdict.detail))
                continue

            try:
                result = self._execute(reply.sql)
            except duckdb.Error as error:
                if self._stopped():
                    # Interrupted by Stop: not a failed attempt, and not repaired.
                    return Answer(status=CANCELLED, sql_attempts=tuple(attempts))
                attempts.append(Attempt(sql=reply.sql, error=str(error)))
                continue

            if self._stopped():
                # Stop arrived after the query had already finished.
                return Answer(status=CANCELLED, sql_attempts=tuple(attempts))

            attempts.append(Attempt(sql=reply.sql, error=None))
            return self._answered(question, reply.sql, result, tuple(attempts))

        # Every attempt failed. The DuckDB error is reported as-is (spec §6), never softened.
        return Answer(status=FAILED, error=attempts[-1].error, sql_attempts=tuple(attempts))

    def _answered(self, question, sql, result, sql_attempts):
        """A successful result. Stage 04 adds the chart call here."""
        return Answer(
            status=ANSWERED,
            sql=sql,
            sql_attempts=sql_attempts,
            columns=result.columns,
            rows=result.rows,
            truncated=result.truncated,
        )
```

`question` is unused in `_answered` until stage 04. Keep the parameter so stage 04 changes only the
body.

---

## Testing & Verification

All tests are appended to `tests/test_query.py`. They drive the real `QueryRunner` against the real
synthetic database from stage 02's `database` fixture. **Nothing mocks DuckDB, the validator, or the
prompt.** Only the model is scripted.

### Helpers (append once, near the top of the file)

```python
import threading
import time
from types import SimpleNamespace

from askcrossroads import llm, safety

from conftest import FakeModel

QUESTION = "How many collisions were there in each kind of weather?"

# What a good model reply looks like for that question on the synthetic build: readable labels,
# sorted by the underlying code (spec §3).
GOOD_SQL = (
    "SELECT weather_conditions_label AS weather, COUNT(*) AS collisions "
    "FROM collisions_labelled "
    "GROUP BY weather_conditions_label "
    "ORDER BY MIN(weather_conditions)"
)
BAD_SQL = "SELECT no_such_column FROM collisions"

# Runs for minutes unless interrupted. range() is DuckDB's built-in row generator: it needs no
# table and no external access, so the validator and the connection both allow it.
SLOW_SQL = "SELECT SUM(range) AS total FROM range(1000000000000)"


def sql(text):
    return llm.SqlResponse(answerable=True, sql=text, explanation=None)


def decline(text):
    return llm.SqlResponse(answerable=False, sql=None, explanation=text)


def recording(database):
    """A Database stand-in whose connection records every SQL string executed.

    QueryRunner uses only `.connection` and `.context`, so this proves exactly what reached
    DuckDB without touching any private attribute.
    """
    connection = RecordingConnection(database.connection)
    return SimpleNamespace(connection=connection, context=database.context), connection


class BlockingModel(FakeModel):
    """A FakeModel that pauses inside generate_sql until the test releases it.

    This holds a question 'in flight' deterministically, with no sleeps or timing guesses.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_sql(self, context, question, failures=()):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("the test never released the model")
        return super().generate_sql(context, question, failures)


def ask_in_background(runner, question):
    """Run runner.ask on a worker thread, as the HTTP layer will. Returns (thread, outcome)."""
    outcome = {}

    def run():
        try:
            outcome["answer"] = runner.ask(question)
        except BaseException as error:
            outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome
```

### The SQL loop

| Test | Model queue | Assertions |
|---|---|---|
| `test_a_good_query_is_answered_first_time` | `[sql(GOOD_SQL)]` | `status == ANSWERED`; `answer.sql == GOOD_SQL`; `sql_attempts == (Attempt(GOOD_SQL, None),)`; `[name for name, _ in answer.columns] == ["weather", "collisions"]`; `answer.rows[0][0] == "Fine no high winds"`, which is code order, not alphabetical; `chart is None`; `model.calls == [("generate_sql", QUESTION, ())]` |
| `test_a_sql_error_is_repaired_with_the_error_fed_back` | `[sql(BAD_SQL), sql(GOOD_SQL)]` | `ANSWERED`; two attempts; `sql_attempts[0].sql == BAD_SQL`; `"no_such_column" in sql_attempts[0].error`; `sql_attempts[1].error is None`; `model.calls[1][2] == ((BAD_SQL, sql_attempts[0].error),)` |
| `test_three_failures_end_the_question` | three *different* bad queries, e.g. `BAD_SQL`, `"SELECT nope FROM collisions"`, `"SELECT x FROM no_such_table"` | `FAILED`; `len(sql_attempts) == config.MAX_ATTEMPTS == 3`; every attempt has an error; `answer.error == sql_attempts[-1].error`; `len(model.calls) == 3`; the third call's failures has 2 entries. FakeModel raises `AssertionError` on a 4th call, so over-calling fails the test loudly |
| `test_the_attempt_limit_is_the_config_constant` | `monkeypatch.setattr(config, "MAX_ATTEMPTS", 2)`; two bad queries | `FAILED`; exactly 2 calls |
| `test_zero_rows_is_an_answer_and_is_never_retried` | `[sql("SELECT * FROM collisions WHERE 1 = 0")]` only | `ANSWERED`; `rows == ()`; one attempt with `error is None`; `len(model.calls) == 1`. **Spec §4.2's most important case.** |
| `test_a_decline_is_terminal_and_executes_nothing` | `[decline("This build covers 2023 only.")]`, using `recording(database)` | `DECLINED`; `explanation == "This build covers 2023 only."`; `sql is None`; `sql_attempts == ()`; `connection.executed == []`; `len(model.calls) == 1` |
| `test_a_decline_after_a_failure_keeps_the_history` | `[sql(BAD_SQL), decline("Not in this build.")]` | `DECLINED`; `len(sql_attempts) == 1` |
| `test_a_malformed_reply_is_a_failed_attempt` | `[llm.MalformedResponse("The model's reply was not valid JSON."), sql(GOOD_SQL)]` | `ANSWERED`; `sql_attempts[0] == Attempt(None, "The model's reply was not valid JSON.")`; `model.calls[1][2] == ((None, "The model's reply was not valid JSON."),)` |
| `test_a_model_error_is_not_retried` | `[llm.ModelError("The Anthropic API is rate limiting this key. Wait a moment and try again.")]` | `FAILED`; `answer.error` is that exact sentence; `sql_attempts == ()`; `len(model.calls) == 1` |
| `test_a_rejected_query_is_fed_back_and_never_executed` | `[sql("DROP TABLE collisions"), sql(GOOD_SQL)]`, using `recording(database)` | `ANSWERED`; `sql_attempts[0].error == safety.validate_sql("DROP TABLE collisions").detail`; `connection.executed == [GOOD_SQL]` |
| `test_a_repaired_query_is_validated_again` | `[sql(BAD_SQL), sql("PRAGMA version"), sql(GOOD_SQL)]`, using `recording(database)` | `ANSWERED`; three attempts; `sql_attempts[1].error == safety.validate_sql("PRAGMA version").detail`; `connection.executed == [BAD_SQL, GOOD_SQL]`, so the PRAGMA never reached DuckDB (spec §4.4) |
| `test_a_file_read_costs_an_attempt_and_is_repaired` | `[sql("SELECT * FROM read_csv_auto('/etc/hosts')"), sql(GOOD_SQL)]` | `ANSWERED`; `sql_attempts[0].error` is not None. Documents spec §5's stated cost of the unbuilt function check |
| `test_the_row_cap_applies_to_a_repaired_attempt` | `monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)`; `[sql(BAD_SQL), sql("SELECT range FROM range(10) ORDER BY range")]` | `ANSWERED`; `len(rows) == 3`; `truncated is True`; `answer.sql == "SELECT range FROM range(10) ORDER BY range"` exactly (spec §4.3, §8) |

Import `Attempt`, `ANSWERED` and the other names via `query.` in the tests (for example
`query.Attempt`, `query.ANSWERED`) rather than `from ... import`, to match how `test_safety.py`
refers to its module.

### One question at a time, and Stop

| Test | What it does | Assertions |
|---|---|---|
| `test_a_second_question_while_one_runs_is_busy` | `BlockingModel(sql_responses=[sql("SELECT 1 AS one")])`; `ask_in_background`; `model.entered.wait(5)`; then `runner.ask("another")` | raises `query.Busy`; after `model.release.set()` and `join(5)`, the first answer is `ANSWERED`; `len(model.calls) == 1`, so the rejected question never reached the model |
| `test_the_lock_is_released_after_each_question` | `FakeModel([sql("SELECT 1 AS one"), sql("SELECT 2 AS two")])`; ask twice in sequence | both `ANSWERED` |
| `test_the_lock_is_released_even_after_an_unexpected_error` | `FakeModel(sql_responses=[])`: the first `ask` hits FakeModel's over-call `AssertionError`. Then give the runner a fresh queue: `model.sql_responses.append(sql("SELECT 1 AS one"))` | the first `ask` raises `AssertionError`; the second returns `ANSWERED`, not `Busy` |
| `test_cancel_with_nothing_running_is_a_no_op` | `runner.cancel()` before any question; then `ask` with `[sql("SELECT 1 AS one")]` | `ANSWERED` (spec §5) |
| `test_stop_during_a_model_call_starts_nothing_new` | `BlockingModel(sql_responses=[sql(GOOD_SQL)])` with `recording(database)`; `ask_in_background`; `entered.wait(5)`; `runner.cancel()`; `release.set()`; `join(5)` | `CANCELLED`; `connection.executed == []`; `sql_attempts == ()`; `len(model.calls) == 1` |
| `test_stop_interrupts_a_running_query_and_the_connection_survives` | see below | see below |

```python
def test_stop_interrupts_a_running_query_and_the_connection_survives(database):
    """Spec §11.4: a running query is interrupted when Stop is pressed, and the connection is
    left usable for the next question."""
    model = FakeModel(sql_responses=[sql(SLOW_SQL), sql("SELECT 1 AS one")])
    runner = query.QueryRunner(database, model)
    worker, outcome = ask_in_background(runner, QUESTION)

    deadline = time.monotonic() + 5
    while not runner.executing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.executing, "the slow query never started"
    # Let DuckDB get properly under way, so this tests interrupting a RUNNING query.
    time.sleep(0.2)

    stopped_at = time.monotonic()
    runner.cancel()
    worker.join(timeout=10)

    assert not worker.is_alive(), "Stop did not interrupt the query"
    assert time.monotonic() - stopped_at < 5
    answer = outcome["answer"]
    assert answer.status == query.CANCELLED
    assert answer.sql_attempts == ()          # an interrupted query is not a failed attempt
    assert len(model.calls) == 1               # and nothing new was started

    # The same connection answers the next question.
    assert runner.ask(QUESTION).status == query.ANSWERED
```

If this test hangs, do not lengthen the timeouts. Probe (d) from stage 02 is the reference: if the
probe passed and this test does not, the bug is in `cancel()` or `_execute()`.

### Commands

```bash
.venv/bin/python -m pytest tests/test_query.py -v
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

Run the whole suite three times in a row to flush out timing flakiness in the threading tests:

```bash
for i in 1 2 3; do env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest tests/test_query.py -q || break; done
```

```bash
grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/ ; grep -n "Thread(" src/askcrossroads/query.py ; echo "both greps above must print nothing"
```

### Stage ship-readiness checklist

1. The default suite is green, three runs in a row.
2. Both greps print nothing.
3. `grep -nE "^import|^from" src/askcrossroads/query.py` shows only `threading`, `time`, `dataclasses`,
   `duckdb`, and `from askcrossroads import config, llm, prompt, safety`.
4. `git status` shows `query.py` and `tests/test_query.py` changed, and **nothing staged**.

---

## End State / Handoff

- `query.QueryRunner(database, model)` with `ask(question) -> Answer`, `cancel()`, and the
  read-only `executing` property.
- `query.Busy`, raised by `ask()` while a question is running.
- `query.Answer`, `query.Attempt`, `query.ChartAttempt`, and the four status strings `ANSWERED`,
  `DECLINED`, `FAILED`, `CANCELLED`.
- The SQL loop satisfies every spec §4 bound and every spec §11.4 case on the SQL side, all proven
  against a real DuckDB database.
- `ANSWERED` currently always has `chart is None` and `chart_attempts == ()`. Stage 04 changes only
  `QueryRunner._answered` and adds `result_shape`.

---

## Failure Modes & Rollback

| Failure | Why it happens | Guardrail |
|---|---|---|
| Zero rows triggers a retry | Treating an empty result as an error | `test_zero_rows_is_an_answer_and_is_never_retried` |
| A fourth model call | Off-by-one in the loop, or `MalformedResponse` not counted | `test_three_failures_end_the_question`; FakeModel's over-call `AssertionError` |
| Stop is "repaired" | Recording the interrupt error as an attempt | The `_stopped()` check comes **first** inside `except duckdb.Error`; `sql_attempts == ()` assertion |
| A rate limit burns all attempts | `ModelError` caught as a failed attempt | `test_a_model_error_is_not_retried` |
| The lock is never released | A `return` or exception path outside `try/finally` | `test_the_lock_is_released_even_after_an_unexpected_error` |
| A Stop for one question cancels the next | The flag is never cleared | `ask()` clears it inside the lock; the slow-query test's final `ask` |
| Threading tests flake | Timing assumptions | `BlockingModel` events instead of sleeps; the triple run |

**Rollback:** delete everything in `query.py` after `execute`, and the tests this stage appended to
`tests/test_query.py`. Stage 02's code and tests remain valid. No commit is made at any point.
