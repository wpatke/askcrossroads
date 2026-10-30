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
import time
from dataclasses import dataclass

import duckdb

from askcrossroads import config, llm, prompt, safety


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

        Returns once any running query has stopped, which takes a moment. Call it from a
        different thread from the one running ask().
        """
        self._cancelled.set()
        # Keep interrupting for as long as a query is marked as running. One interrupt is
        # not enough: if it arrives in the instant before DuckDB has actually started the
        # query, DuckDB discards it and the query runs to the end. Given up after a fixed
        # time, so a Stop request can never hang.
        deadline = time.monotonic() + config.STOP_RETRY_SECONDS
        while self._executing and time.monotonic() < deadline:
            self._connection.interrupt()
            time.sleep(config.STOP_RETRY_INTERVAL)

    def _stopped(self):
        return self._cancelled.is_set()

    def _execute(self, sql):
        """Run one query, marking it as running so cancel() knows to interrupt it.

        The order matters. This marks the query as running and THEN checks for Stop;
        cancel() records Stop and THEN looks for a running query. Because the two happen in
        opposite orders, at least one side always sees the other: either the query never
        starts, or cancel() finds it running and interrupts it.
        """
        self._executing = True
        try:
            if self._stopped():
                # Stop arrived after the loop's last check. Handled exactly like an
                # interrupted query: the question ends as cancelled.
                raise duckdb.InterruptException("Stopped before the query started.")
            return execute(self._connection, sql)
        finally:
            self._executing = False

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
