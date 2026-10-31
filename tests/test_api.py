"""The HTTP routes and the JSON contract (spec §9).

Every test drives the real application (create_app) and the real QueryRunner against a real
synthetic DuckDB database, through FastAPI's TestClient. Only the model is scripted
(FakeModel), so the suite stays offline and deterministic (spec §11.1).
"""

import json
import logging
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import pytest
from fastapi.testclient import TestClient

from askcrossroads import app, config, llm, query
from askcrossroads.database import Database

from conftest import FakeModel
from provider_stub import ProviderStub

KEY = "test-key-not-a-real-credential"

QUESTION = "How many collisions were there in each kind of weather?"

# Readable labels, sorted by the underlying code, on the synthetic build (spec §3).
GOOD_SQL = (
    "SELECT weather_conditions_label AS weather, COUNT(*) AS collisions "
    "FROM collisions_labelled "
    "GROUP BY weather_conditions_label "
    "ORDER BY MIN(weather_conditions)"
)
BAD_SQL = "SELECT no_such_column FROM collisions"

CHART = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "weather", "type": "nominal", "sort": None},
        "y": {"field": "collisions", "type": "quantitative"},
    },
}

ANSWER_KEYS = {
    "status", "answerable", "sql", "explanation", "error", "sql_attempts",
    "columns", "rows", "truncated", "chart", "chart_attempts",
}

BUSY = "A question is already being answered. Wait for it, or press Stop."


def sql(text):
    return llm.SqlResponse(answerable=True, sql=text, explanation=None)


def decline(text):
    return llm.SqlResponse(answerable=False, sql=None, explanation=text)


@pytest.fixture
def database(crossroads_db):
    with Database.open(crossroads_db) as opened:
        yield opened


def client_for(database, model, **client_options):
    """A TestClient for the real application around a runner with this model."""
    runner = query.QueryRunner(database, model)
    return TestClient(app.create_app(runner, database.context), **client_options)


def ask(client, question=QUESTION):
    return client.post("/api/ask", json={"question": question})


class BlockingModel(FakeModel):
    """Pauses inside generate_sql until the test releases it: a question held in flight
    deterministically, with no sleeps."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_sql(self, context, question, failures=()):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("the test never released the model")
        return super().generate_sql(context, question, failures)


def post_in_background(client, path, body=None):
    """POST on a worker thread, as a browser's request would arrive. Returns (thread, outcome)."""
    outcome = {}

    def run():
        outcome["response"] = client.post(path, json=body)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome


class NoConnection:
    """Stands in for the database connection and fails if anything touches it."""

    def __getattr__(self, name):
        raise AssertionError("the route used the database connection")


# --- /api/database ----------------------------------------------------------------------


def test_the_database_route_describes_the_loaded_build(database):
    response = client_for(database, FakeModel()).get("/api/database")
    assert response.status_code == 200
    body = response.json()
    context = database.context

    assert set(body) == {"provenance", "tables", "date_ranges", "coded_columns"}
    assert body["provenance"] == app.json_value(context.provenance.as_dict())
    assert body["provenance"]["crossroads_version"] == "0.9.0"
    assert body["tables"] == [relation.name for relation in context.relations]
    assert "collisions" in body["tables"]
    assert body["coded_columns"] == list(context.coded_column_names)
    assert body["coded_columns"]

    assert len(body["date_ranges"]) == len(context.temporal_ranges) > 0
    for span in body["date_ranges"]:
        assert set(span) == {"relation", "column", "earliest", "latest"}
        assert span["earliest"] is None or isinstance(span["earliest"], str)
        assert span["latest"] is None or isinstance(span["latest"], str)


def test_the_database_route_never_touches_the_connection(database):
    stand_in = SimpleNamespace(connection=NoConnection(), context=database.context)
    runner = query.QueryRunner(stand_in, FakeModel())
    client = TestClient(app.create_app(runner, database.context))
    assert client.get("/api/database").status_code == 200


# --- /api/ask: one test per outcome -----------------------------------------------------


def test_an_answered_question_has_the_documented_shape(database):
    client = client_for(database, FakeModel(sql_responses=[sql(GOOD_SQL)], chart_responses=[CHART]))
    response = ask(client)
    assert response.status_code == 200
    body = response.json()

    assert set(body) == ANSWER_KEYS
    assert body["status"] == "answered"
    assert body["answerable"] is True
    assert body["sql"] == GOOD_SQL
    assert body["explanation"] is None
    assert body["error"] is None
    assert body["sql_attempts"] == [{"sql": GOOD_SQL, "error": None}]
    assert body["columns"] == [
        {"name": "weather", "type": "VARCHAR"},
        {"name": "collisions", "type": "BIGINT"},
    ]
    assert all(isinstance(row, list) for row in body["rows"])
    # Code order, not alphabetical (spec §3).
    assert body["rows"][0][0] == "Fine no high winds"
    assert body["truncated"] is False
    assert body["chart"] == CHART
    assert body["chart_attempts"] == [{"error": None}]


def test_zero_rows_is_answered_with_an_empty_table(database):
    client = client_for(
        database, FakeModel(sql_responses=[sql("SELECT * FROM collisions WHERE 1 = 0")])
    )
    body = ask(client).json()
    assert body["status"] == "answered"
    assert body["rows"] == []
    assert body["columns"]
    assert body["chart"] is None
    assert body["chart_attempts"] == []


def test_a_declined_question(database):
    client = client_for(
        database, FakeModel(sql_responses=[decline("This build covers 2023 only.")])
    )
    body = ask(client).json()
    assert body["status"] == "declined"
    assert body["answerable"] is False
    assert body["explanation"] == "This build covers 2023 only."
    assert body["sql"] is None
    assert body["rows"] == []
    assert body["columns"] == []
    assert body["chart"] is None
    assert body["sql_attempts"] == []


def test_a_failed_question_reports_every_attempt(database):
    bad = [BAD_SQL, "SELECT nope FROM collisions", "SELECT x FROM no_such_table"]
    client = client_for(database, FakeModel(sql_responses=[sql(text) for text in bad]))
    body = ask(client).json()
    assert body["status"] == "failed"
    assert body["answerable"] is True
    assert len(body["sql_attempts"]) == 3
    assert all(attempt["error"] for attempt in body["sql_attempts"])
    assert body["error"] == body["sql_attempts"][-1]["error"]
    assert body["rows"] == []
    assert body["chart"] is None


def test_a_provider_failure_is_reported_as_failed(database):
    message = "The Anthropic API did not respond in time."
    client = client_for(database, FakeModel(sql_responses=[llm.ModelError(message)]))
    body = ask(client).json()
    assert body["status"] == "failed"
    assert body["error"] == message
    assert body["sql_attempts"] == []


def test_a_chart_failure_still_returns_the_table(database):
    model = FakeModel(
        sql_responses=[sql(GOOD_SQL)],
        chart_responses=[llm.MalformedResponse("bad chart") for _ in range(3)],
    )
    body = ask(client_for(database, model)).json()
    assert body["status"] == "answered"
    assert body["rows"]
    assert body["chart"] is None
    assert len(body["chart_attempts"]) == 3
    assert all(attempt["error"] for attempt in body["chart_attempts"])


def test_a_truncated_result_is_reported(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)
    capped = "SELECT range AS n FROM range(10) ORDER BY n"
    model = FakeModel(sql_responses=[sql(capped)], chart_responses=[CHART])
    body = ask(client_for(database, model)).json()
    assert body["truncated"] is True
    assert body["rows"] == [[0], [1], [2]]
    # Exactly what the model wrote: no LIMIT was added (spec §8).
    assert body["sql"] == capped


def test_a_nan_in_a_result_does_not_break_the_response(database):
    model = FakeModel(
        sql_responses=[sql(
            "SELECT 'nan'::DOUBLE AS x, 'infinity'::DOUBLE AS y, "
            "170141183460469231731687303715884105727::HUGEINT AS big"
        )],
        chart_responses=[CHART],
    )
    response = ask(client_for(database, model))
    assert response.status_code == 200
    assert response.json()["rows"] == [
        ["NaN", "Infinity", "170141183460469231731687303715884105727"]
    ]


# --- One question at a time, and Stop (spec §5) -----------------------------------------


def test_a_second_question_while_one_runs_is_rejected(database):
    model = BlockingModel(sql_responses=[sql("SELECT 1 AS one")], chart_responses=[CHART])
    client = client_for(database, model)
    worker, outcome = post_in_background(client, "/api/ask", {"question": QUESTION})
    assert model.entered.wait(timeout=5)

    second = ask(client, "Another question")
    assert second.status_code == 409
    assert second.json() == {"error": BUSY}

    model.release.set()
    worker.join(timeout=10)
    assert outcome["response"].status_code == 200
    assert outcome["response"].json()["status"] == "answered"
    # The rejected question never reached the model.
    assert [call[1] for call in model.calls if call[0] == "generate_sql"] == [QUESTION]


def test_cancel_stops_a_question_in_flight(database):
    model = BlockingModel(sql_responses=[sql(GOOD_SQL)])
    client = client_for(database, model)
    worker, outcome = post_in_background(client, "/api/ask", {"question": QUESTION})
    assert model.entered.wait(timeout=5)

    stopped = client.post("/api/cancel")
    assert stopped.status_code == 204
    assert stopped.content == b""

    model.release.set()
    worker.join(timeout=10)
    body = outcome["response"].json()
    assert body["status"] == "cancelled"
    assert body["sql_attempts"] == []
    assert body["rows"] == []


def test_cancel_with_nothing_running_is_a_no_op(database):
    model = FakeModel(sql_responses=[sql("SELECT 1 AS one")], chart_responses=[CHART])
    client = client_for(database, model)
    assert client.post("/api/cancel").status_code == 204
    assert ask(client).json()["status"] == "answered"


# --- Requests the application cannot act on ---------------------------------------------


def test_an_empty_question_is_rejected_without_calling_the_model(database):
    model = FakeModel()
    client = client_for(database, model)
    for question in ("", "   ", "\n\t"):
        response = ask(client, question)
        assert response.status_code == 422
        assert response.json() == {"error": app.EMPTY_QUESTION}
    assert model.calls == []


def test_a_malformed_request_is_rejected(database):
    model = FakeModel()
    client = client_for(database, model)
    responses = [
        client.post("/api/ask", json={}),
        client.post("/api/ask", json={"question": 5}),
        client.post(
            "/api/ask", content="not json", headers={"content-type": "application/json"}
        ),
        client.post("/api/ask"),
    ]
    for response in responses:
        assert response.status_code == 422
        assert response.json() == {"error": app.BAD_REQUEST}
    assert model.calls == []


def test_an_unexpected_error_is_a_generic_500(database):
    # Nothing queued, so generate_sql raises AssertionError: a stand-in for a bug.
    model = FakeModel()
    client = client_for(database, model, raise_server_exceptions=False)
    response = ask(client)
    assert response.status_code == 500
    assert response.json() == {"error": app.UNEXPECTED}
    assert "AssertionError" not in response.text

    # The lock was released, so the next question works.
    model.sql_responses.append(sql("SELECT 1 AS one"))
    model.chart_responses.append(CHART)
    next_response = ask(client)
    assert next_response.status_code == 200
    assert next_response.json()["status"] == "answered"


# --- The page and the surface -----------------------------------------------------------


def test_the_page_is_served_at_the_root(database):
    response = client_for(database, FakeModel()).get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AskCrossroads" in response.text


def test_the_surface_is_exactly_three_routes(database):
    client = client_for(database, FakeModel())
    for path in ("/docs", "/redoc", "/openapi.json", "/api/nothing"):
        assert client.get(path).status_code == 404, path


# --- JSON values ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        ("x", "x"),
        (42, 42),
        (2**53 - 1, 2**53 - 1),
        (2**53, "9007199254740992"),
        (-(2**60), str(-(2**60))),
        (1.5, 1.5),
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
        (Decimal("12.345"), 12.345),
        (Decimal("NaN"), "NaN"),
        (Decimal("1e400"), "1E+400"),
        (date(2023, 4, 1), "2023-04-01"),
        (datetime(2023, 4, 1, 8, 30), "2023-04-01T08:30:00"),
        (time(8, 30), "08:30:00"),
        (timedelta(minutes=90), "1:30:00"),
        ((1, 2), [1, 2]),
        ({"a": float("nan")}, {"a": "NaN"}),
        ({1: "x"}, {"1": "x"}),
        ([[1], [2]], [[1], [2]]),
    ],
)
def test_json_value_rules(value, expected):
    assert app.json_value(value) == expected


def test_every_kind_of_duckdb_value_survives_json(database):
    result = query.execute(
        database.connection,
        """
        SELECT NULL AS nothing, TRUE AS flag, 42 AS small,
               170141183460469231731687303715884105727::HUGEINT AS huge,
               1.5::DOUBLE AS real, 'nan'::DOUBLE AS not_a_number,
               CAST(12.345 AS DECIMAL(18,3)) AS amount,
               DATE '2023-04-01' AS day, TIMESTAMP '2023-04-01 08:30:00' AS moment,
               TIME '08:30:00' AS clock, INTERVAL 90 MINUTE AS duration,
               '6b0cbf55-7d5f-4d0b-9d43-5f1e2f0e6a01'::UUID AS id,
               [1, 2, 3] AS numbers, {'a': 1, 'b': 'x'} AS pair
        """,
    )
    converted = [app.json_value(value) for value in result.rows[0]]
    # Raises if anything is not strict JSON.
    json.dumps(converted, allow_nan=False)
    assert converted == [
        None, True, 42, "170141183460469231731687303715884105727", 1.5, "NaN", 12.345,
        "2023-04-01", "2023-04-01T08:30:00", "08:30:00", "1:30:00",
        "6b0cbf55-7d5f-4d0b-9d43-5f1e2f0e6a01", [1, 2, 3], {"a": 1, "b": "x"},
    ]


# --- The API key never leaves the process (spec §9) -------------------------------------
# These use the REAL AnthropicClient, pointed at a local stand-in for the Anthropic API that
# echoes the key back in its errors, as a careless provider might.


@pytest.fixture
def provider():
    with ProviderStub() as stub:
        yield stub


def test_the_stub_really_echoes_the_key(provider):
    """The control for the next test: without sanitising, the key WOULD be in the error."""
    provider.reply_error(401, echo_key=True)
    with pytest.raises(anthropic.AuthenticationError) as raised:
        anthropic.Anthropic(api_key=KEY, base_url=provider.url).messages.create(
            model=config.MODEL, max_tokens=16, messages=[{"role": "user", "content": "hi"}]
        )
    echoed = KEY in str(raised.value)
    assert echoed, "the stub did not echo the key, so the leak tests would prove nothing"


def test_the_key_never_reaches_a_response_or_a_log(database, provider, monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", provider.url)
    # Every logger, the SDK's and the HTTP client's included, at its most talkative.
    caplog.set_level(logging.DEBUG)

    provider.reply_error(401, echo_key=True)   # the SQL call fails authentication
    provider.reply_sql(GOOD_SQL)
    provider.reply_error(400, echo_key=True)   # the chart call fails

    runner = query.QueryRunner(database, llm.AnthropicClient(KEY))
    client = TestClient(app.create_app(runner, database.context))

    first = ask(client)
    assert first.json()["status"] == "failed"
    assert "rejected the configured key" in first.json()["error"]

    second = ask(client)
    body = second.json()
    assert body["status"] == "answered"
    assert body["rows"]
    assert body["chart"] is None
    assert body["chart_attempts"][0]["error"]

    summary = client.get("/api/database")

    assert len(provider.requests) == 3
    assert all(request["api_key"] == KEY for request in provider.requests)

    # caplog.text includes formatted tracebacks.
    everything = first.text + second.text + summary.text + caplog.text
    leaked = KEY in everything
    assert not leaked, "the API key reached a response or a log record"
