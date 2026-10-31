# Stage 01 — Routes & Response Contract

> Part of *HTTP API & Entry Point*. You have the overview + this stage; implement **only** this
> stage. Read [00-overview.md](00-overview.md) first, in particular *The HTTP contract*, *Threading,
> precisely*, and the decisions table.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Plan 005 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -c "
from askcrossroads import query
print(query.QueryRunner, query.Answer, query.Busy, query.ANSWERED, query.CANCELLED)
from fastapi.testclient import TestClient
print(TestClient)
"
```

Expected: five names and `TestClient` print, with no error. `TestClient` needs the `httpx2`
package, which the `anthropic` SDK already installs. If the import fails, run
`.venv/bin/pip install httpx2` and record it.

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest -q
```

Expected: green. If not, stop and report. This stage must start from a green suite.

---

## Objective

Add the three routes and the JSON conversion to `app.py` as `create_app(runner, context)`, and
prove every body in the overview's contract in-process. `main()` is **not** changed in this stage.

---

## Implementation Steps

### 1. Declare the test client's dependency

`pyproject.toml`, in `[project.optional-dependencies]`:

```toml
dev = [
  "pytest>=8.0",
  # FastAPI's TestClient. Already installed by the anthropic SDK, listed because the tests
  # import it directly.
  "httpx2>=2.0",
]
```

Expected: `.venv/bin/pip install -e ".[dev]"` reports nothing new to install. Running it is
optional. The package is already present.

### 2. Reject non-JSON constants in chart replies

`src/askcrossroads/llm.py`. Python's `json.loads` accepts `NaN`, `Infinity` and `-Infinity`, which
are not JSON. A chart spec carrying one cannot be sent to the browser (`JSONResponse` refuses it),
which would turn a chart problem into the loss of the whole answer.

Add this helper just above `parse_chart_response`:

```python
def _refuse_constant(name):
    """json.loads calls this for NaN, Infinity and -Infinity, which are not valid JSON.

    Python accepts them by default; a browser does not. A chart spec carrying one could not
    be sent to the page, so it is treated as a malformed reply and repaired like any other.
    """
    raise ValueError(f"{name} is not valid JSON")
```

and in `parse_chart_response` change

```python
        spec = json.loads(stripped)
```

to

```python
        spec = json.loads(stripped, parse_constant=_refuse_constant)
```

The existing `except (TypeError, ValueError)` already turns the `ValueError` into
`MalformedResponse("The chart reply was not valid JSON.")`. Nothing else changes.

### 3. Placeholder page

The static directory holds only `.gitkeep`. The mount needs a page to serve at `/`. The next plan
replaces this file entirely.

Create `src/askcrossroads/static/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>AskCrossroads</title>
</head>
<body>
  <h1>AskCrossroads</h1>
  <!-- A placeholder. The question page replaces this file. -->
  <p>The question page has not been built yet. The server is running:
     <a href="/api/database">/api/database</a> describes the loaded database.</p>
</body>
</html>
```

Delete `src/askcrossroads/static/.gitkeep`. The directory now has a real file.
(`tests/test_package.py::test_static_directory_exists` still passes.)

### 4. Rewrite the top of `app.py`: module docstring, imports, constants

`src/askcrossroads/app.py`. **Keep `main()` exactly as it is**, below the new code. Replace the
module docstring and imports with:

```python
"""FastAPI routes and the console entry point (spec §9).

The HTTP layer is deliberately thin. Each route translates between JSON and one call on an
object built at startup: the QueryRunner (query.py) answers questions, and the
DatabaseContext (database.py) describes the open file. No SQL, prompt text or model logic
lives here.

Three routes, exactly (spec §9):

    GET  /api/database   what the loaded database contains, and what built it
    POST /api/ask        one question in, one answer out
    POST /api/cancel     Stop: interrupt the running question. A no-op when none is running

Every other path is the static page.
"""

import datetime
import decimal
import math
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from askcrossroads import query
from askcrossroads.config import ConfigError, load_config
from askcrossroads.database import Database, DatabaseError

STATIC_DIR = Path(__file__).parent / "static"

# A JavaScript number is a 64-bit float, so it silently loses digits beyond this. Integers
# larger than this are sent as strings so the page shows them exactly. A fact about browsers,
# not a tunable bound.
_LARGEST_SAFE_INTEGER = 2**53 - 1

# What the browser is told when a request is not a question it can act on. The {"error": ...}
# shape is used for every error the application produces itself.
EMPTY_QUESTION = "Type a question first."
BAD_REQUEST = 'Send the question as JSON: {"question": "..."}.'
UNEXPECTED = (
    "AskCrossroads hit an unexpected problem. The terminal it is running in has the details."
)
```

### 5. `json_value`

Append after the constants:

```python
def json_value(value):
    """One value from the database, as something strict JSON can carry and a browser reads
    correctly.

    DuckDB hands back Python objects (Decimal, date, datetime, lists, dicts and so on), and
    the standard JSON encoder accepts few of them. Each rule keeps what the user sees honest:

    - NULL stays null. The page shows it as "Not recorded" (spec §3).
    - NaN and the infinities become the strings "NaN", "Infinity" and "-Infinity". They are
      not valid JSON, and turning them into null would show a real value as "Not recorded".
    - An integer too big for a JavaScript number becomes a string, so no digit is lost.
    - A Decimal becomes an ordinary number so it can be charted. One that is not finite, or
      too big, becomes a string for the same reasons as above.
    - Dates and times become ISO 8601 strings, which Vega-Lite reads as dates.
    - Lists and structs keep their shape, with every value inside converted the same way.
    - Anything else (a duration, a UUID) is sent as its text: never blank, never guessed.
    """
    # bool is checked before int because True and False are ints in Python.
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= _LARGEST_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return str(value)
        number = float(value)
        return number if abs(number) <= _LARGEST_SAFE_INTEGER else str(value)
    # datetime.datetime is a subclass of datetime.date, so this covers all three.
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    return str(value)
```

### 6. `answer_to_json` and `describe_database`

Append:

```python
def answer_to_json(answer):
    """The /api/ask response body for one query.Answer (spec §9).

    Every way a question can end is reported in this one shape. `status` says which it was,
    and `answerable` is false only when the model declined (spec §6).

    Rows are arrays in column order rather than objects, because two result columns can share
    a name and an object cannot hold both. The page pairs them with `columns` itself.
    """
    return {
        "status": answer.status,
        "answerable": answer.status != query.DECLINED,
        "sql": answer.sql,
        "explanation": answer.explanation,
        "error": answer.error,
        "sql_attempts": [
            {"sql": attempt.sql, "error": attempt.error} for attempt in answer.sql_attempts
        ],
        "columns": [{"name": name, "type": type_name} for name, type_name in answer.columns],
        "rows": [[json_value(value) for value in row] for row in answer.rows],
        "truncated": answer.truncated,
        # Exactly as the model returned it. Python never edits a chart spec (spec §7).
        "chart": answer.chart,
        "chart_attempts": [{"error": attempt.error} for attempt in answer.chart_attempts],
    }


def describe_database(context):
    """The /api/database response body (spec §9).

    Everything comes from the DatabaseContext read once at startup. Nothing here queries the
    database: the one connection belongs to the QueryRunner, and a DuckDB connection must
    not be used by two threads at once.

    `date_ranges` is one entry per dated column of a stored table, as computed at startup.
    It is a list, not one combined range, because one of those columns records when the
    file was built, which would stretch any combined range to that day.
    """
    return {
        "provenance": json_value(context.provenance.as_dict()),
        "tables": [relation.name for relation in context.relations],
        "date_ranges": [
            {
                "relation": span.relation,
                "column": span.column,
                "earliest": json_value(span.earliest),
                "latest": json_value(span.latest),
            }
            for span in context.temporal_ranges
        ],
        "coded_columns": list(context.coded_column_names),
    }
```

### 7. `create_app`

Append:

```python
class AskRequest(BaseModel):
    """The /api/ask request body: {"question": "..."}."""

    question: str


def create_app(runner, context):
    """The FastAPI application: the three routes of spec §9 and the static page.

    Takes objects that are already built (`runner` is a query.QueryRunner, `context` the
    open database's DatabaseContext), so a test can hand in a runner with a scripted model.
    It builds nothing itself and opens nothing.

    FastAPI's own documentation pages are switched off: the surface is exactly three routes.
    """
    application = FastAPI(
        title="AskCrossroads", docs_url=None, redoc_url=None, openapi_url=None
    )

    # Computed once. The context never changes while the application runs.
    database_summary = describe_database(context)

    @application.exception_handler(RequestValidationError)
    def bad_request(request, error):
        # FastAPI's default reply here is a technical list of validation details. The page
        # needs one sentence in the same shape as every other error.
        return JSONResponse(status_code=422, content={"error": BAD_REQUEST})

    @application.exception_handler(Exception)
    def unexpected(request, error):
        # A bug. The browser gets a fixed sentence and nothing from the exception, which
        # could carry anything. The traceback still goes to the terminal.
        return JSONResponse(status_code=500, content={"error": UNEXPECTED})

    # Every route below is a plain `def`, never `async def`, deliberately. FastAPI runs a
    # plain `def` route on a worker thread, so a question that takes minutes ties up one
    # thread and leaves the server free to answer /api/cancel. An `async def` route would
    # run the blocking ask() on the server's only event loop and freeze everything, the
    # Stop button included.

    @application.get("/api/database")
    def database():
        return JSONResponse(database_summary)

    @application.post("/api/ask")
    def ask(body: AskRequest):
        question = body.question.strip()
        if not question:
            return JSONResponse(status_code=422, content={"error": EMPTY_QUESTION})
        try:
            answer = runner.ask(question)
        except query.Busy as error:
            # One question at a time (spec §5). Rejected, not queued.
            return JSONResponse(status_code=409, content={"error": str(error)})
        return JSONResponse(answer_to_json(answer))

    @application.post("/api/cancel")
    def cancel():
        runner.cancel()
        return Response(status_code=204)

    # Mounted LAST. A mount at "/" matches every path, so routes declared after it would
    # never be reached. html=True serves index.html for "/".
    application.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return application
```

Note on the two exception handlers: Starlette accepts plain functions as handlers and runs them on a
worker thread. That is fine here, because they do no work. If your FastAPI version rejects a
non-async handler, make **only these two** `async def` and record the deviation. They do not block,
so the rule against `async def` routes is not breached. In that case, ship-readiness check 5
(`grep "async def"`) must show exactly those two lines.

Expected after this step: `.venv/bin/python -c "from askcrossroads import app; print(app.create_app)"`
prints a function, and `askcrossroads` behaves exactly as before (main is untouched).

### 8. Update spec §9's HTTP surface

`docs/spec.md`, section `### HTTP surface`. Replace the code block and the paragraph after it with:

````markdown
```text
GET  /api/database               -> {provenance, tables, date_ranges, coded_columns}
POST /api/ask        {question}  -> {status, answerable, sql, explanation, error,
                                     sql_attempts[], columns, rows, truncated,
                                     chart, chart_attempts[]}
POST /api/cancel                 -> 204; interrupts the running question (§5)
```

One request from the browser still means one question; the two model calls (§4) happen server-side and are reported separately. `sql_attempts[]` carries one entry per attempt with its SQL and its error, which is what §4.5 renders; `chart_attempts[]` is its counterpart, and `chart` is `null` when every attempt failed — the table is unaffected.

Every way a question can end is an ordinary `200` response, told apart by `status`: `answered`, `declined` (§6A — `answerable` is `false` only here, with the reason in `explanation`), `failed` (every attempt failed, or the model could not be reached; the message is in `error`), or `cancelled` (Stop was pressed). Only requests the application cannot act on are HTTP errors, each with an `{"error": "..."}` body: `409` when a question is already running (§5), `422` for an empty question or a malformed request, and `500` for a bug, whose details go to the terminal rather than the browser.

Result rows are arrays in column order. Values the browser cannot hold exactly are sent as strings: integers beyond JavaScript's safe range, and `NaN` and the infinities, which are not JSON and must not be shown as *Not recorded*. Dates and times are ISO 8601 strings.

`date_ranges` lists the earliest and latest value of every dated column of every stored table, as computed on load (§6). It replaces a single `years` field an earlier draft specified: the file holds no single "years" value AskCrossroads may rely on (§2C), and one of those dated columns is the build timestamp, which would stretch any combined range to the day the file was built.
````

Read the whole of §9 afterwards and check nothing else in it now contradicts the new block.

---

## Testing & Verification

### Integration-style route tests (PRIMARY): `tests/test_api.py`

New file. Every test drives the real `create_app` and the real `QueryRunner` against a **real
DuckDB database** (the synthetic fixture opened through `Database.open`, so the production
connection settings apply). Only the model is scripted. Write it exactly as follows, then add the
tests listed after it.

```python
"""The HTTP routes and the JSON contract (spec §9).

Every test drives the real application (create_app) and the real QueryRunner against a real
synthetic DuckDB database, through FastAPI's TestClient. Only the model is scripted
(FakeModel), so the suite stays offline and deterministic (spec §11.1).
"""

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from askcrossroads import app, config, llm, query
from askcrossroads.database import Database

from conftest import FakeModel

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
```

Tests to write in that file (each name exactly as given, each assertion required):

**`/api/database`**

- `test_the_database_route_describes_the_loaded_build(database)`: `GET /api/database` → 200.
  `set(body) == {"provenance", "tables", "date_ranges", "coded_columns"}`.
  `body["provenance"] == app.json_value(database.context.provenance.as_dict())`, and
  `body["provenance"]["crossroads_version"] == "0.9.0"`.
  `body["tables"] == [r.name for r in database.context.relations]`, and `"collisions"` is in it.
  `body["coded_columns"] == list(database.context.coded_column_names)`, and it is non-empty.
  `len(body["date_ranges"]) == len(database.context.temporal_ranges) > 0`. Every entry has exactly
  the keys `relation, column, earliest, latest`, and `earliest`/`latest` are strings or `None`.
- `test_the_database_route_never_touches_the_connection(database)`: build the runner over
  `SimpleNamespace(connection=NoConnection(), context=database.context)`, where `NoConnection` is a
  class whose `__getattr__` raises `AssertionError("the route used the database connection")`.
  `GET /api/database` → 200.

**`/api/ask`: one test per outcome**

- `test_an_answered_question_has_the_documented_shape(database)`: model `[sql(GOOD_SQL)]`, chart
  `[CHART]`. 200. `set(body) == ANSWER_KEYS`. `status == "answered"`, `answerable is True`,
  `sql == GOOD_SQL`, `explanation is None`, `error is None`,
  `sql_attempts == [{"sql": GOOD_SQL, "error": None}]`,
  `columns == [{"name": "weather", "type": "VARCHAR"}, {"name": "collisions", "type": "BIGINT"}]`,
  every row is a `list`, `rows[0][0] == "Fine no high winds"` (code order, spec §3),
  `truncated is False`, `chart == CHART`, `chart_attempts == [{"error": None}]`.
- `test_zero_rows_is_answered_with_an_empty_table(database)`: model
  `[sql("SELECT * FROM collisions WHERE 1 = 0")]`, no chart replies. `status == "answered"`,
  `rows == []`, `columns` non-empty, `chart is None`, `chart_attempts == []`.
- `test_a_declined_question(database)`: model `[decline("This build covers 2023 only.")]`.
  `status == "declined"`, `answerable is False`, `explanation == "This build covers 2023 only."`,
  `sql is None`, `rows == []`, `columns == []`, `chart is None`, `sql_attempts == []`.
- `test_a_failed_question_reports_every_attempt(database)`: three bad SQL replies (`BAD_SQL`,
  `"SELECT nope FROM collisions"`, `"SELECT x FROM no_such_table"`). `status == "failed"`,
  `answerable is True`, `len(sql_attempts) == 3`, each has a non-empty `error`,
  `error == sql_attempts[-1]["error"]`, `rows == []`, `chart is None`.
- `test_a_provider_failure_is_reported_as_failed(database)`: model
  `[llm.ModelError("The Anthropic API did not respond in time.")]`. `status == "failed"`,
  `error == "The Anthropic API did not respond in time."`, `sql_attempts == []`.
- `test_a_chart_failure_still_returns_the_table(database)`: model `[sql(GOOD_SQL)]`, chart replies
  three `llm.MalformedResponse("bad chart")`. `status == "answered"`, `rows` non-empty,
  `chart is None`, `len(chart_attempts) == 3`, each with a non-empty `error`.
- `test_a_truncated_result_is_reported(database, monkeypatch)`:
  `monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)`; model
  `[sql("SELECT range AS n FROM range(10) ORDER BY n")]`, chart `[CHART]`. `truncated is True`,
  `rows == [[0], [1], [2]]`, `sql` unchanged (no `LIMIT`).
- `test_a_nan_in_a_result_does_not_break_the_response(database)`: model
  `[sql("SELECT 'nan'::DOUBLE AS x, 'infinity'::DOUBLE AS y, "
  "170141183460469231731687303715884105727::HUGEINT AS big")]`, chart `[CHART]`. 200, and
  `rows == [["NaN", "Infinity", "170141183460469231731687303715884105727"]]`.

**One question at a time, and Stop (spec §5)**

- `test_a_second_question_while_one_runs_is_rejected(database)`: `BlockingModel(sql_responses=
  [sql("SELECT 1 AS one")], chart_responses=[CHART])`. Start `/api/ask` with
  `post_in_background`. `assert model.entered.wait(timeout=5)`. A second `ask(client, "Another")`
  returns 409, and its JSON is
  `{"error": "A question is already being answered. Wait for it, or press Stop."}`. Then
  `model.release.set()`, `worker.join(timeout=10)`. The first response is 200 with
  `status == "answered"`. The model's `generate_sql` calls carry only `QUESTION`.
- `test_cancel_stops_a_question_in_flight(database)`: `BlockingModel(sql_responses=[sql(GOOD_SQL)])`.
  Start `/api/ask` in the background, wait for `entered`, then `client.post("/api/cancel")` returns
  204 with an empty body. Release, join. The first response has `status == "cancelled"`,
  `sql_attempts == []`, `rows == []`.
- `test_cancel_with_nothing_running_is_a_no_op(database)`: `POST /api/cancel` → 204. Then a normal
  question (model `[sql("SELECT 1 AS one")]`, chart `[CHART]`) is answered.

**Requests the application cannot act on**

- `test_an_empty_question_is_rejected_without_calling_the_model(database)`: for each of `""`,
  `"   "`, `"\n\t"`: 422 and JSON `{"error": app.EMPTY_QUESTION}`. Afterwards `model.calls == []`.
- `test_a_malformed_request_is_rejected(database)`: each of these is 422 with JSON
  `{"error": app.BAD_REQUEST}`: `client.post("/api/ask", json={})`,
  `client.post("/api/ask", json={"question": 5})`,
  `client.post("/api/ask", content="not json", headers={"content-type": "application/json"})`,
  `client.post("/api/ask")`. Afterwards `model.calls == []`.
- `test_an_unexpected_error_is_a_generic_500(database)`: `model = FakeModel()` (nothing queued, so
  `generate_sql` raises `AssertionError`, standing in for a bug). Use
  `client_for(database, model, raise_server_exceptions=False)`. `ask(client)` → 500 and JSON
  `{"error": app.UNEXPECTED}`. `"AssertionError"` is not in `response.text`. Then queue
  `sql("SELECT 1 AS one")` and `CHART` on the model. The next `ask(client)` is 200 and `answered`:
  the lock was released.

**The page and the surface**

- `test_the_page_is_served_at_the_root(database)`: `GET /` → 200, `"text/html"` in the
  `content-type` header, `"AskCrossroads"` in the text.
- `test_the_surface_is_exactly_three_routes(database)`: `GET /docs`, `GET /redoc`,
  `GET /openapi.json` and `GET /api/nothing` each → 404.

**JSON values**

- `test_json_value_rules()`, parametrised over `(value, expected)`:
  `(None, None)`, `(True, True)`, `("x", "x")`, `(42, 42)`, `(2**53 - 1, 2**53 - 1)`,
  `(2**53, "9007199254740992")`, `(-(2**60), str(-(2**60)))`, `(1.5, 1.5)`,
  `(float("nan"), "NaN")`, `(float("inf"), "Infinity")`, `(float("-inf"), "-Infinity")`,
  `(Decimal("12.345"), 12.345)`, `(Decimal("NaN"), "NaN")`, `(Decimal("1e400"), "1E+400")`,
  `(date(2023, 4, 1), "2023-04-01")`, `(datetime(2023, 4, 1, 8, 30), "2023-04-01T08:30:00")`,
  `(time(8, 30), "08:30:00")`, `(timedelta(minutes=90), "1:30:00")`, `((1, 2), [1, 2])`,
  `({"a": float("nan")}, {"a": "NaN"})`, `({1: "x"}, {"1": "x"})`, `([[1], [2]], [[1], [2]])`.
  (Import `Decimal` from `decimal` and `date, datetime, time, timedelta` from `datetime`.)
- `test_every_kind_of_duckdb_value_survives_json(database)`: run this through
  `query.execute(database.connection, ...)`:

  ```sql
  SELECT NULL AS nothing, TRUE AS flag, 42 AS small,
         170141183460469231731687303715884105727::HUGEINT AS huge,
         1.5::DOUBLE AS real, 'nan'::DOUBLE AS not_a_number,
         CAST(12.345 AS DECIMAL(18,3)) AS amount,
         DATE '2023-04-01' AS day, TIMESTAMP '2023-04-01 08:30:00' AS moment,
         TIME '08:30:00' AS clock, INTERVAL 90 MINUTE AS duration,
         '6b0cbf55-7d5f-4d0b-9d43-5f1e2f0e6a01'::UUID AS id,
         [1, 2, 3] AS numbers, {'a': 1, 'b': 'x'} AS pair
  ```

  Convert row 0 with `[app.json_value(v) for v in result.rows[0]]`. Assert
  `json.dumps(converted, allow_nan=False)` does not raise, and that `converted` equals
  `[None, True, 42, "170141183460469231731687303715884105727", 1.5, "NaN", 12.345, "2023-04-01",
  "2023-04-01T08:30:00", "08:30:00", "1:30:00", "6b0cbf55-7d5f-4d0b-9d43-5f1e2f0e6a01",
  [1, 2, 3], {"a": 1, "b": "x"}]`. If DuckDB returns a different Python type for one column than
  this assumes (for example the interval), adjust **that one expected value** to what `json_value`
  correctly produces for the type DuckDB actually returns, and record it. The `allow_nan=False`
  dump must pass regardless.

### Unit test: `tests/test_llm.py`

Append next to the other chart-parsing tests:

```python
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_chart_with_a_non_json_constant_is_malformed(constant):
    """Python's json accepts these; a browser does not. A spec carrying one could not be sent
    to the page, so it must be repaired rather than cost the user their table."""
    text = '{"mark": "bar", "encoding": {"x": {"field": "a", "scale": {"domainMax": ' + constant + '}}}}'
    with pytest.raises(llm.MalformedResponse):
        llm.parse_chart_response(text)
```

### Commands

```bash
.venv/bin/python -m pytest tests/test_api.py -v
```

Expected: every test passes.

```bash
.venv/bin/python -m pytest tests/test_llm.py -q
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

Expected: green, including `tests/test_package.py::test_console_script_runs` (main is unchanged).

### Stage ship-readiness checklist

1. The full default suite is green.
2. `grep -n "async def" src/askcrossroads/app.py` returns nothing, or only the two exception
   handlers if step 7's documented fallback was needed.
3. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
4. `grep -n "jsonable_encoder" src/askcrossroads/app.py` returns nothing. Read the three route
   functions and the two exception handlers: every `return` in them returns a `JSONResponse` or a
   `Response`, never a plain dict.
5. `docs/spec.md` §9 shows the new HTTP surface block, and `years` no longer appears in §9.
6. `git status`: nothing staged.

---

## End State / Handoff

- `app.create_app(runner, context)` returns a FastAPI application serving exactly
  `GET /api/database`, `POST /api/ask`, `POST /api/cancel`, and the static directory at `/`.
- Bodies match the overview's contract exactly, including `status`/`error`, the 409/422/500 error
  bodies, and the 204 for cancel. `app.EMPTY_QUESTION`, `app.BAD_REQUEST` and `app.UNEXPECTED` are
  the error strings.
- `app.json_value`, `app.answer_to_json` and `app.describe_database` exist and are tested.
- `llm.parse_chart_response` rejects `NaN`/`Infinity`/`-Infinity`.
- `src/askcrossroads/static/index.html` is a placeholder. `.gitkeep` is gone.
- `main()` is **unchanged**: the next stage rewrites it.
- `pyproject.toml` lists `httpx2` in `dev`. Spec §9's HTTP surface is updated.
- Default `pytest` is green.

---

## Failure Modes & Rollback

- **`TestClient` import fails** → `httpx2` missing. `.venv/bin/pip install httpx2`, then record it.
- **A 500 where a 200 was expected, with `ValueError: Out of range float values are not JSON
  compliant`** → a value reached `JSONResponse` without going through `json_value`. Find which field
  and route it through `json_value`. Never switch `allow_nan` on.
- **`GET /api/database` or `/api/ask` returns 404** → the static mount was declared before the routes.
  It must be last.
- **A concurrency test hangs** → the `BlockingModel` was never released or `entered` never fired.
  Every wait has a timeout, so the test fails instead of hanging. If `TestClient` cannot serve two
  requests from two threads in your version, use one `TestClient` per thread over the **same**
  `app.create_app(...)` application, and record it.
- **The unexpected-error test still raises in the test** → `raise_server_exceptions=False` was not
  passed to that test's client.
- **Rollback:** `git checkout -- src/askcrossroads/app.py src/askcrossroads/llm.py pyproject.toml
  docs/spec.md tests/test_llm.py`, `git checkout -- src/askcrossroads/static/.gitkeep`, and delete
  `src/askcrossroads/static/index.html` and `tests/test_api.py`. Nothing was committed, so nothing
  else is affected.
