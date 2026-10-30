"""Query orchestration: execution, the repair loops, cancellation and charts (spec §4, §5, §7).

Every test here runs against a real DuckDB database, the synthetic Crossroads-UK fixture opened
the way the application opens it, so the read-only and no-external-access settings are the
production ones. Only the model is scripted (FakeModel), because the default suite must be
offline and deterministic (spec §11.1).
"""

import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import duckdb
import pytest

from askcrossroads import config, llm, query, safety
from askcrossroads.database import Database

from conftest import FakeModel


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


# --- Capped execution (spec §5, §10) ----------------------------------------------------


def test_columns_carry_duckdb_type_names(database):
    result = query.execute(
        database.connection,
        "SELECT COUNT(*) AS total, 'x' AS label, DATE '2023-01-01' AS d, "
        "CAST(1.5 AS DECIMAL(18,3)) AS amount",
    )
    assert result.columns == (
        ("total", "BIGINT"),
        ("label", "VARCHAR"),
        ("d", "DATE"),
        ("amount", "DECIMAL(18,3)"),
    )


def test_rows_arrive_in_the_order_the_sql_sorted_them(database):
    result = query.execute(
        database.connection, "SELECT range AS n FROM range(5) ORDER BY n DESC"
    )
    assert result.rows == ((4,), (3,), (2,), (1,), (0,))
    assert result.truncated is False


def test_rows_are_tuples(database):
    result = query.execute(database.connection, "SELECT range FROM range(3) ORDER BY range")
    assert isinstance(result.rows, tuple)
    assert all(isinstance(row, tuple) for row in result.rows)


def test_a_result_exactly_at_the_cap_is_not_truncated(database):
    result = query.execute(
        database.connection,
        f"SELECT range FROM range({config.MAX_RESULT_ROWS}) ORDER BY range",
    )
    assert len(result.rows) == config.MAX_RESULT_ROWS
    assert result.truncated is False


def test_a_result_one_over_the_cap_is_truncated(database):
    result = query.execute(
        database.connection,
        f"SELECT range FROM range({config.MAX_RESULT_ROWS + 1}) ORDER BY range",
    )
    assert len(result.rows) == config.MAX_RESULT_ROWS
    assert result.truncated is True
    # The FIRST rows are the ones kept.
    assert result.rows[-1] == (config.MAX_RESULT_ROWS - 1,)


def test_the_cap_is_read_from_config_at_call_time(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)
    result = query.execute(database.connection, "SELECT range FROM range(10) ORDER BY range")
    assert len(result.rows) == 3
    assert result.truncated is True


def test_the_sql_reaches_duckdb_unmodified(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)
    connection = RecordingConnection(database.connection)
    sql = "SELECT range FROM range(10) ORDER BY range"
    query.execute(connection, sql)
    assert connection.executed == [sql]


def test_an_empty_result_is_not_an_error(database):
    result = query.execute(
        database.connection, "SELECT range FROM range(10) WHERE range < 0"
    )
    assert result.rows == ()
    assert result.truncated is False
    assert result.columns == (("range", "BIGINT"),)


def test_a_bad_query_raises_a_duckdb_error(database):
    with pytest.raises(duckdb.Error):
        query.execute(database.connection, "SELECT no_such_column FROM range(1)")


def test_a_write_is_still_refused_by_the_engine(database):
    """Spec §11.4: the engine guard holds even if the validator were bypassed entirely."""
    with pytest.raises(duckdb.Error):
        query.execute(database.connection, "CREATE TABLE t (x INTEGER)")


def test_a_file_read_is_refused_at_execution(database):
    """Spec §5: the no-external-access layer is what stops this."""
    with pytest.raises(duckdb.Error):
        query.execute(database.connection, "SELECT * FROM read_csv_auto('/etc/hosts')")


def test_real_fixture_data_executes(database):
    result = query.execute(database.connection, "SELECT COUNT(*) AS n FROM collisions")
    assert len(result.rows) == 1
    assert result.rows[0][0] > 0


# --- Helpers for the runner tests --------------------------------------------------------

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

# A minimal Vega-Lite spec for GOOD_SQL's result. Contains no data, as spec §7 requires.
CHART = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "weather", "type": "nominal", "sort": None},
        "y": {"field": "collisions", "type": "quantitative"},
    },
}

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


def ask(database, sql_responses, chart_responses=(CHART,)):
    """Ask QUESTION once with a scripted model. Returns (answer, model).

    One chart reply is queued by default, because every question that returns rows goes on
    to ask for a chart.
    """
    model = FakeModel(sql_responses=sql_responses, chart_responses=chart_responses)
    return query.QueryRunner(database, model).ask(QUESTION), model


# --- The SQL repair loop (spec §4) ------------------------------------------------------


def test_a_good_query_is_answered_first_time(database):
    answer, model = ask(database, [sql(GOOD_SQL)])
    assert answer.status == query.ANSWERED
    assert answer.sql == GOOD_SQL
    assert answer.sql_attempts == (query.Attempt(GOOD_SQL, None),)
    assert [name for name, _ in answer.columns] == ["weather", "collisions"]
    # Code order, not alphabetical (spec §3).
    assert answer.rows[0][0] == "Fine no high winds"
    assert answer.chart is CHART
    assert [call[0] for call in model.calls] == ["generate_sql", "describe_chart"]
    assert model.calls[0] == ("generate_sql", QUESTION, ())


def test_a_sql_error_is_repaired_with_the_error_fed_back(database):
    answer, model = ask(database, [sql(BAD_SQL), sql(GOOD_SQL)])
    assert answer.status == query.ANSWERED
    first, second = answer.sql_attempts
    assert first.sql == BAD_SQL
    assert "no_such_column" in first.error
    assert second.error is None
    assert model.calls[1][2] == ((BAD_SQL, first.error),)


def test_three_failures_end_the_question(database):
    bad = [BAD_SQL, "SELECT nope FROM collisions", "SELECT x FROM no_such_table"]
    answer, model = ask(database, [sql(text) for text in bad])
    assert answer.status == query.FAILED
    assert len(answer.sql_attempts) == config.MAX_ATTEMPTS == 3
    assert all(attempt.error for attempt in answer.sql_attempts)
    assert answer.error == answer.sql_attempts[-1].error
    # FakeModel raises on a fourth call, so over-calling would fail loudly.
    assert len(model.calls) == 3
    assert len(model.calls[2][2]) == 2


def test_the_attempt_limit_is_the_config_constant(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_ATTEMPTS", 2)
    answer, model = ask(database, [sql(BAD_SQL), sql("SELECT nope FROM collisions")])
    assert answer.status == query.FAILED
    assert len(model.calls) == 2


def test_zero_rows_is_an_answer_and_is_never_retried(database):
    """Spec §4.2, the case that matters most: retrying on zero rows manufactures results."""
    answer, model = ask(database, [sql("SELECT * FROM collisions WHERE 1 = 0")])
    assert answer.status == query.ANSWERED
    assert answer.rows == ()
    assert len(answer.sql_attempts) == 1
    assert answer.sql_attempts[0].error is None
    assert len(model.calls) == 1


def test_a_decline_is_terminal_and_executes_nothing(database):
    stand_in, connection = recording(database)
    model = FakeModel(sql_responses=[decline("This build covers 2023 only.")])
    answer = query.QueryRunner(stand_in, model).ask(QUESTION)
    assert answer.status == query.DECLINED
    assert answer.explanation == "This build covers 2023 only."
    assert answer.sql is None
    assert answer.sql_attempts == ()
    assert connection.executed == []
    assert len(model.calls) == 1


def test_a_decline_after_a_failure_keeps_the_history(database):
    answer, _ = ask(database, [sql(BAD_SQL), decline("Not in this build.")])
    assert answer.status == query.DECLINED
    assert len(answer.sql_attempts) == 1


def test_a_malformed_reply_is_a_failed_attempt(database):
    message = "The model's reply was not valid JSON."
    answer, model = ask(database, [llm.MalformedResponse(message), sql(GOOD_SQL)])
    assert answer.status == query.ANSWERED
    assert answer.sql_attempts[0] == query.Attempt(None, message)
    assert model.calls[1][2] == ((None, message),)


def test_a_model_error_is_not_retried(database):
    message = "The Anthropic API is rate limiting this key. Wait a moment and try again."
    answer, model = ask(database, [llm.ModelError(message)])
    assert answer.status == query.FAILED
    assert answer.error == message
    assert answer.sql_attempts == ()
    assert len(model.calls) == 1


def test_a_rejected_query_is_fed_back_and_never_executed(database):
    stand_in, connection = recording(database)
    model = FakeModel(
        sql_responses=[sql("DROP TABLE collisions"), sql(GOOD_SQL)], chart_responses=[CHART]
    )
    answer = query.QueryRunner(stand_in, model).ask(QUESTION)
    assert answer.status == query.ANSWERED
    detail = safety.validate_sql("DROP TABLE collisions").detail
    assert answer.sql_attempts[0].error == detail
    assert model.calls[1][2] == (("DROP TABLE collisions", detail),)
    assert connection.executed == [GOOD_SQL]


def test_a_repaired_query_is_validated_again(database):
    """Spec §4.4: a repaired query is new model output and is checked again."""
    stand_in, connection = recording(database)
    model = FakeModel(
        sql_responses=[sql(BAD_SQL), sql("PRAGMA version"), sql(GOOD_SQL)],
        chart_responses=[CHART],
    )
    answer = query.QueryRunner(stand_in, model).ask(QUESTION)
    assert answer.status == query.ANSWERED
    assert len(answer.sql_attempts) == 3
    assert answer.sql_attempts[1].error == safety.validate_sql("PRAGMA version").detail
    assert connection.executed == [BAD_SQL, GOOD_SQL]


def test_a_file_read_costs_an_attempt_and_is_repaired(database):
    """Spec §5: the file-reading check is not built, so the connection refuses this when it
    runs, at the cost of one attempt."""
    answer, _ = ask(
        database, [sql("SELECT * FROM read_csv_auto('/etc/hosts')"), sql(GOOD_SQL)]
    )
    assert answer.status == query.ANSWERED
    assert answer.sql_attempts[0].error is not None


def test_the_row_cap_applies_to_a_repaired_attempt(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)
    capped = "SELECT range FROM range(10) ORDER BY range"
    answer, _ = ask(database, [sql(BAD_SQL), sql(capped)])
    assert answer.status == query.ANSWERED
    assert len(answer.rows) == 3
    assert answer.truncated is True
    # Exactly what the model wrote: no LIMIT was added (spec §8).
    assert answer.sql == capped


# --- One question at a time, and Stop (spec §5) -----------------------------------------


def test_a_second_question_while_one_runs_is_busy(database):
    model = BlockingModel(sql_responses=[sql("SELECT 1 AS one")], chart_responses=[CHART])
    runner = query.QueryRunner(database, model)
    worker, outcome = ask_in_background(runner, QUESTION)
    assert model.entered.wait(timeout=5)

    with pytest.raises(query.Busy):
        runner.ask("A second question")

    model.release.set()
    worker.join(timeout=5)
    assert outcome["answer"].status == query.ANSWERED
    # The rejected question never reached the model.
    assert [call[1] for call in model.calls if call[0] == "generate_sql"] == [QUESTION]


def test_the_lock_is_released_after_each_question(database):
    model = FakeModel(
        sql_responses=[sql("SELECT 1 AS one"), sql("SELECT 2 AS two")],
        chart_responses=[CHART, CHART],
    )
    runner = query.QueryRunner(database, model)
    assert runner.ask(QUESTION).status == query.ANSWERED
    assert runner.ask(QUESTION).status == query.ANSWERED


def test_the_lock_is_released_even_after_an_unexpected_error(database):
    model = FakeModel(sql_responses=[], chart_responses=[CHART])
    runner = query.QueryRunner(database, model)
    # FakeModel has nothing queued, so the first question fails in an unexpected way.
    with pytest.raises(AssertionError):
        runner.ask(QUESTION)
    model.sql_responses.append(sql("SELECT 1 AS one"))
    assert runner.ask(QUESTION).status == query.ANSWERED


def test_cancel_with_nothing_running_is_a_no_op(database):
    runner = query.QueryRunner(
        database, FakeModel(sql_responses=[sql("SELECT 1 AS one")], chart_responses=[CHART])
    )
    runner.cancel()
    assert runner.ask(QUESTION).status == query.ANSWERED


def test_stop_during_a_model_call_starts_nothing_new(database):
    stand_in, connection = recording(database)
    model = BlockingModel(sql_responses=[sql(GOOD_SQL)])
    runner = query.QueryRunner(stand_in, model)
    worker, outcome = ask_in_background(runner, QUESTION)
    assert model.entered.wait(timeout=5)

    runner.cancel()
    model.release.set()
    worker.join(timeout=5)

    answer = outcome["answer"]
    assert answer.status == query.CANCELLED
    assert connection.executed == []
    assert answer.sql_attempts == ()
    assert len(model.calls) == 1


def test_stop_interrupts_a_running_query_and_the_connection_survives(database):
    """Spec §11.4: a running query is interrupted when Stop is pressed, and the connection is
    left usable for the next question."""
    model = FakeModel(
        sql_responses=[sql(SLOW_SQL), sql("SELECT 1 AS one")], chart_responses=[CHART]
    )
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


# --- The chart context (spec §7) --------------------------------------------------------


def test_shape_counts_distinct_values_of_non_numeric_columns_only():
    result = query.ExecutionResult(
        columns=(("weather", "VARCHAR"), ("n", "BIGINT"), ("amount", "DECIMAL(18,3)")),
        rows=(
            ("a", 1, Decimal("1.5")),
            ("a", 2, Decimal("2.5")),
            ("b", 3, Decimal("1.5")),
            (None, 4, Decimal("9")),
        ),
        truncated=False,
    )
    shape = query.result_shape(result)
    # "a", "b" and NULL: three distinct values.
    assert shape.distinct_counts == (("weather", 3),)
    assert shape.row_count == 4
    assert shape.columns == result.columns


def test_shape_samples_at_most_the_configured_rows():
    result = query.ExecutionResult(
        columns=(("n", "BIGINT"),), rows=tuple((i,) for i in range(20)), truncated=False
    )
    shape = query.result_shape(result)
    assert shape.sample_rows == result.rows[: config.CHART_SAMPLE_ROWS]
    assert shape.row_count == 20


def test_shape_handles_list_values(database):
    result = query.execute(
        database.connection,
        "SELECT [range, range] AS pair, range AS n FROM range(3) ORDER BY n",
    )
    shape = query.result_shape(result)
    assert shape.distinct_counts == (("pair", 3),)


def test_numeric_types_are_recognised():
    for type_name in ("BIGINT", "DOUBLE", "DECIMAL(18,3)", "UBIGINT"):
        assert query._is_numeric(type_name), type_name
    for type_name in ("VARCHAR", "DATE", "TIMESTAMP", "BIGINT[]", "BOOLEAN"):
        assert not query._is_numeric(type_name), type_name


# --- The chart loop (spec §7) -----------------------------------------------------------


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


def _chart_calls(model):
    return [call for call in model.calls if call[0] == "describe_chart"]


def test_the_chart_call_comes_after_the_sql_and_sees_the_real_columns(database):
    """Spec §11.4: never before the SQL succeeds, and given the real result columns."""
    answer, model = ask(database, [sql(GOOD_SQL)], [CHART])
    assert answer.status == query.ANSWERED
    assert [call[0] for call in model.calls] == ["generate_sql", "describe_chart"]
    _, _, chart_sql, shape, failures = model.calls[1]
    assert chart_sql == GOOD_SQL
    # These came back from DuckDB, not from the model.
    assert shape.columns == answer.columns
    assert shape.row_count == len(answer.rows)
    assert failures == ()


def test_the_chart_spec_is_returned_unedited(database):
    answer, _ = ask(database, [sql(GOOD_SQL)], [CHART])
    # The same object: Python never edits a chart spec (spec §7).
    assert answer.chart is CHART
    assert answer.chart_attempts == (query.ChartAttempt(None),)


def test_zero_rows_makes_no_chart_call(database):
    """Spec §6: nothing to plot. No chart reply is queued, so a chart call would fail loudly."""
    answer, model = ask(database, [sql("SELECT * FROM collisions WHERE 1 = 0")], [])
    assert answer.status == query.ANSWERED
    assert answer.rows == ()
    assert answer.chart is None
    assert answer.chart_attempts == ()
    assert _chart_calls(model) == []


def test_a_malformed_chart_is_repaired_with_the_error_fed_back(database):
    message = "The chart specification had no mark."
    answer, model = ask(database, [sql(GOOD_SQL)], [llm.MalformedResponse(message), CHART])
    assert answer.chart is CHART
    assert answer.chart_attempts == (query.ChartAttempt(message), query.ChartAttempt(None))
    assert _chart_calls(model)[1][4] == ((None, message),)


def test_three_chart_failures_still_return_the_table(database):
    """Spec §7: a chart is an enhancement; losing it must never cost the user the table."""
    failures = [llm.MalformedResponse(f"bad chart {i}") for i in range(3)]
    answer, model = ask(database, [sql(GOOD_SQL)], failures)
    assert answer.status == query.ANSWERED
    assert answer.rows
    assert answer.rows == query.execute(database.connection, GOOD_SQL).rows
    assert answer.chart is None
    assert len(answer.chart_attempts) == 3
    assert all(attempt.error for attempt in answer.chart_attempts)
    assert len(_chart_calls(model)) == 3


def test_a_chart_model_error_still_returns_the_table(database):
    message = "The Anthropic API did not respond in time."
    answer, model = ask(database, [sql(GOOD_SQL)], [llm.ModelError(message)])
    assert answer.status == query.ANSWERED
    assert answer.rows
    assert answer.chart is None
    assert answer.chart_attempts == (query.ChartAttempt(message),)
    assert len(_chart_calls(model)) == 1


def test_the_two_loops_have_independent_budgets(database):
    """Spec §4: six model calls is the worst case for a question."""
    answer, model = ask(
        database,
        [sql(BAD_SQL), sql("SELECT nope FROM collisions"), sql(GOOD_SQL)],
        [llm.MalformedResponse("one"), llm.MalformedResponse("two"), CHART],
    )
    assert answer.status == query.ANSWERED
    assert answer.chart is CHART
    assert len(answer.sql_attempts) == 3
    assert len(answer.chart_attempts) == 3
    assert len(model.calls) == 6


def test_a_truncated_result_is_charted_from_the_capped_rows(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)
    answer, model = ask(
        database,
        [sql("SELECT range AS n, CAST(range AS VARCHAR) AS label FROM range(10) ORDER BY n")],
        [CHART],
    )
    assert answer.truncated is True
    assert _chart_calls(model)[0][3].row_count == 3


def test_stop_during_the_chart_call_cancels(database):
    model = BlockingChartModel(sql_responses=[sql(GOOD_SQL)], chart_responses=[CHART])
    runner = query.QueryRunner(database, model)
    worker, outcome = ask_in_background(runner, QUESTION)
    assert model.entered.wait(timeout=5)

    runner.cancel()
    model.release.set()
    worker.join(timeout=5)

    answer = outcome["answer"]
    assert answer.status == query.CANCELLED
    assert answer.chart is None


# --- Stop is never silently lost (spec §5) ----------------------------------------------
# Each test below presses Stop at an exact, awkward moment, with no sleeps, so it fails
# every time if the gap it covers is reopened.


def finish(worker, connection, timeout=10):
    """Wait for a background question. Returns True if it stopped by itself in time.

    If it did not, the test is about to fail, but the query is still running and the
    database cannot be closed until it ends. So interrupt it until it stops: a failing test
    must fail promptly, not hang the suite while a query runs for minutes.
    """
    worker.join(timeout=timeout)
    stopped_by_itself = not worker.is_alive()
    deadline = time.monotonic() + 10
    while worker.is_alive() and time.monotonic() < deadline:
        connection.interrupt()
        worker.join(timeout=0.05)
    return stopped_by_itself


def test_stop_during_validation_never_runs_the_query(database, monkeypatch):
    """Stop pressed after the loop's last check but before the query is marked as running,
    here while the safety validator is working."""
    stand_in, connection = recording(database)
    runner = query.QueryRunner(stand_in, FakeModel(sql_responses=[sql(SLOW_SQL)]))
    real_validate = safety.validate_sql

    def validate_then_stop(text):
        runner.cancel()
        return real_validate(text)

    monkeypatch.setattr(safety, "validate_sql", validate_then_stop)

    worker, outcome = ask_in_background(runner, QUESTION)
    assert finish(worker, database.connection), "the stopped query ran anyway"
    answer = outcome["answer"]
    assert answer.status == query.CANCELLED
    assert connection.executed == []
    assert answer.sql_attempts == ()


class StopBeforeDuckDBStarts(RecordingConnection):
    """Presses Stop after the runner's last check, but before DuckDB has started the query.

    Its first interrupt therefore reaches DuckDB while it is idle, and DuckDB discards it.
    Only a Stop that keeps interrupting can halt the query that follows.
    """

    def __init__(self, connection):
        super().__init__(connection)
        self.runner = None
        self.first_interrupt = threading.Event()

    def interrupt(self):
        self.first_interrupt.set()
        self._connection.interrupt()

    def execute(self, sql, *args):
        threading.Thread(target=self.runner.cancel, daemon=True).start()
        assert self.first_interrupt.wait(timeout=5), "Stop never sent an interrupt"
        return super().execute(sql, *args)


def test_an_interrupt_sent_before_duckdb_starts_is_not_lost(database):
    connection = StopBeforeDuckDBStarts(database.connection)
    stand_in = SimpleNamespace(connection=connection, context=database.context)
    runner = query.QueryRunner(stand_in, FakeModel(sql_responses=[sql(SLOW_SQL)]))
    connection.runner = runner

    worker, outcome = ask_in_background(runner, QUESTION)
    assert finish(worker, database.connection), "the stopped query kept running"
    answer = outcome["answer"]
    assert answer.status == query.CANCELLED
    assert answer.sql_attempts == ()
