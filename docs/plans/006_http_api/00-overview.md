# HTTP API & Entry Point — Plan Overview

> Multi-stage plan. Each implementation session gets **this overview + ONE stage file**. Read this
> first. Each stage's *End State* is the contract: if a step does not match reality, adapt the step
> and record the deviation at the end of your report, but deliver the End State.

Put the question engine behind three HTTP routes, and make `askcrossroads` a command that either
starts a localhost-only web server, opens the browser onto it, and stops cleanly on Ctrl+C, or
refuses clearly on the terminal before any port is opened.

---

## Context & Objective

### What exists when this plan starts

`docs/plans/001_project_foundation/` through `005_query_orchestration/` are complete. The whole
question engine exists and is tested offline: `QueryRunner.ask(question)` returns one `Answer` for
every outcome, and `QueryRunner.cancel()` stops it from another thread. **Nothing exposes it over
HTTP yet**, and `askcrossroads` only opens the database, prints a summary and exits.

The pieces this plan consumes, by file:

| File | What this plan uses from it |
|---|---|
| `src/askcrossroads/config.py` | `HOST = "127.0.0.1"`, `PORT = 8000`, `ENV_API_KEY`, `ENV_DATABASE`, `Config(api_key, database_path)`, `load_config(env_file=".env", environ=None)`, `ConfigError`. |
| `src/askcrossroads/database.py` | `Database.open(path)` (raises `DatabaseError`; `NotACrossroadsBuild` is a subclass whose message is the spec §1 refusal), `Database.connection`, `Database.context`. `DatabaseContext` has `.provenance` (with `.as_dict()`, already JSON-safe), `.relations` (each has `.name`, `.is_view`), `.temporal_ranges` (each has `.relation`, `.column`, `.earliest`, `.latest`), `.coded_column_names`. |
| `src/askcrossroads/query.py` | `QueryRunner(database, model)`, `.ask(question) -> Answer`, `.cancel()`; `Busy` (raised by `ask` when a question is already running); status strings `ANSWERED`, `DECLINED`, `FAILED`, `CANCELLED`; `Answer(status, sql, explanation, error, sql_attempts, columns, rows, truncated, chart, chart_attempts)`; `Attempt(sql, error)`; `ChartAttempt(error)`; `execute(connection, sql)`. |
| `src/askcrossroads/llm.py` | `AnthropicClient(api_key)` and `.from_config(settings)`; `ModelError` (message already sanitised); `MalformedResponse`; `parse_chart_response(text)`. |
| `src/askcrossroads/app.py` | `main()` today: loads config, opens the database, prints a summary, exits 0. **This plan rewrites it.** |
| `src/askcrossroads/static/` | Holds only `.gitkeep`. The real page comes from the next plan. |
| `tests/conftest.py` | `crossroads_db` (a valid synthetic build, Crossroads-UK version `"0.9.0"`), `crossroads_db_factory(convention="missing")` (a file that is refused), `FakeModel`. |
| `tests/integration/live_fixtures.py` | `settings`, `database`, `client` fixtures for the marked real-API suite. Each one skips when the key or real build is absent. |

Do not re-derive these facts. They come from the earlier plans:

- **`QueryRunner.ask()` blocks; `cancel()` may be called from any other thread.** `query.py` makes
  no threads. The HTTP layer supplies them: FastAPI runs every plain `def` route on a worker thread
  from its thread pool, so `/api/ask` and `/api/cancel` naturally run on different threads.
- **There is one DuckDB connection, and only `QueryRunner` uses it.** A DuckDB Python connection
  must not be used by two threads at once. So `/api/database` must answer from the frozen
  `DatabaseContext` built at startup and **never** touch the connection.
- **`Answer.rows` holds raw Python values** as DuckDB returned them: `Decimal`, `date`, `datetime`,
  `time`, `timedelta`, `UUID`, very large `int`s, `float('nan')`, lists and dicts. Turning them into
  JSON is this plan's job.
- **`ModelError` messages are already sanitised** (fixed sentences written in `llm.py`; nothing from
  the provider's response body). DuckDB errors are shown verbatim on purpose (spec §6).

### What this plan changes

- `src/askcrossroads/app.py`: the routes (`create_app`), the JSON conversion (`json_value`,
  `answer_to_json`, `describe_database`), the launch path (`main`, `listen`, `serve`), and the
  Ctrl+C handling.
- `src/askcrossroads/config.py`: `ENV_PORT = "ASKCROSSROADS_PORT"` and a `port` field on `Config`.
- `src/askcrossroads/llm.py`: one line, so that a chart spec containing `NaN` or `Infinity` is a
  malformed reply rather than a crash (see *Decisions*).
- `src/askcrossroads/static/index.html`: a minimal placeholder page, replaced by the next plan.
- `pyproject.toml`: `httpx2` added to the `dev` extra (FastAPI's `TestClient` needs it).
- `.env.example`: a commented-out `ASKCROSSROADS_PORT` line.
- `docs/spec.md`: §9 (HTTP surface, configuration and launch) and §12 (test files).
- Tests: `tests/test_api.py` (new), `tests/test_server.py` (new), `tests/provider_stub.py` (new),
  `tests/integration/test_api_live.py` (new), and small edits to `tests/test_config.py`,
  `tests/test_package.py`, `tests/test_llm.py`.

Out of scope: everything the browser renders (the next plan), the answer set and README (a later
plan), and any change to how a question is answered (`query.py` is not modified).

---

## Approach / Architecture

### The shape of `app.py`

```text
app.py
  json_value(value)            one DuckDB value -> strict-JSON value
  answer_to_json(answer)       query.Answer     -> the /api/ask body
  describe_database(context)   DatabaseContext  -> the /api/database body
  create_app(runner, context)  -> FastAPI app with the three routes + the static page
  listen(port)                 -> a socket bound and listening on 127.0.0.1:port
  serve(application, listener, runner)   runs uvicorn on that socket until Ctrl+C
  main(argv=None)              the console entry point
```

`create_app` takes already-built objects and builds nothing itself. That is what makes the routes
testable in-process: a test builds `QueryRunner(database, FakeModel(...))` and hands it over. Only
`main` builds the real things.

### Data flow

```text
askcrossroads [--no-browser]
  main()
    load_config()            ConfigError        -> print to stderr, exit 1     (no port opened)
    Database.open(path)      DatabaseError      -> print §1 message, exit 1    (no port opened)
    print summary
    listen(port)             OSError (in use)   -> print "set ASKCROSSROADS_PORT", exit 1
    runner = QueryRunner(database, AnthropicClient(key))
    print "AskCrossroads is running at http://127.0.0.1:PORT/"
    webbrowser.open(url)     unless --no-browser (the socket is already listening, so the
                             browser's first request simply waits for uvicorn to accept it)
    serve(create_app(runner, context), listener, runner)
      Ctrl+C -> Stop the running question, uvicorn shuts down, main returns 0

Browser
  GET  /                -> static/index.html
  GET  /api/database    -> describe_database(context), computed once at startup
  POST /api/ask         -> runner.ask(question) on a worker thread -> answer_to_json
                            Busy -> 409; blank question / bad body -> 422
  POST /api/cancel      -> runner.cancel() on another worker thread -> 204
```

### The HTTP contract (the next plan builds the page against exactly this)

Every error the application produces itself has the body `{"error": "<a sentence for a person>"}`.

**`GET /api/database`** → `200`

```json
{
  "provenance": {"crossroads_version": "0.9.0", "schema_version": 4,
                 "built_at_utc": "2026-01-01T00:00:00", "parameters": {"years": [2023], "...": "..."}},
  "tables": ["bank_holidays", "casualties", "collisions", "collisions_labelled", "..."],
  "date_ranges": [{"relation": "collisions", "column": "date",
                   "earliest": "2023-01-01", "latest": "2023-12-31"}],
  "coded_columns": ["collision_severity", "weather_conditions", "..."]
}
```

- `tables` is every table **and view** the questions can draw on, in name order (the order of
  `context.relations`).
- `date_ranges` replaces the spec's former `years` field. It is one entry per date or timestamp
  column of a stored table, exactly as computed at startup. It is a list rather than a single year
  range because one of those columns is the build timestamp, which would stretch any combined range
  to the day the file was built.

**`POST /api/ask`** with body `{"question": "..."}` → `200` for **every** way a question can end:

```json
{
  "status": "answered",
  "answerable": true,
  "sql": "SELECT ...",
  "explanation": null,
  "error": null,
  "sql_attempts": [{"sql": "SELECT ...", "error": "Binder Error: ..."}, {"sql": "SELECT ...", "error": null}],
  "columns": [{"name": "weather", "type": "VARCHAR"}, {"name": "collisions", "type": "BIGINT"}],
  "rows": [["Fine no high winds", 1234], ["Raining no high winds", 210]],
  "truncated": false,
  "chart": {"mark": "bar", "encoding": {"...": "..."}},
  "chart_attempts": [{"error": null}]
}
```

| `status` | `answerable` | Set | Empty |
|---|---|---|---|
| `"answered"` | `true` | `sql`, `columns`, `rows` (may be `[]`, zero rows is an answer), `truncated`, `chart` (may be `null`), attempts | `explanation`, `error` |
| `"declined"` | `false` | `explanation`, `sql_attempts` (earlier failed tries, if any) | `sql`, `error`, `chart`; `rows` and `columns` are `[]` |
| `"failed"` | `true` | `error`, `sql_attempts` | `sql`, `explanation`, `chart`; `rows` and `columns` are `[]` |
| `"cancelled"` | `true` | `sql_attempts` so far | everything else |

`answerable` is `false` only for a decline, as spec §6 defines it. The browser should branch on
`status`. Other responses from `/api/ask`:

- `409` `{"error": "A question is already being answered. Wait for it, or press Stop."}`, the
  `query.Busy` message, when a question is already running.
- `422` `{"error": "Type a question first."}`, when the question is empty or only whitespace. The
  model is not called.
- `422` `{"error": "Send the question as JSON: {\"question\": \"...\"}."}`, when the body is missing,
  is not JSON, or `question` is not a string.
- `500` `{"error": "AskCrossroads hit an unexpected problem. The terminal it is running in has the
  details."}`, for a bug. The traceback goes to the terminal, never the browser.

**`POST /api/cancel`** → `204`, no body, whether or not anything was running.

**JSON value rules** (`json_value`), applied to every row value, every date-range bound and the
provenance block:

| DuckDB gives Python | JSON gets | Why |
|---|---|---|
| `None` | `null` | The page renders it as *Not recorded* (spec §3). |
| `bool`, `str` | as is | |
| `int` within ±(2⁵³−1) | number | |
| `int` beyond that (e.g. `HUGEINT`) | string of its digits | A JavaScript number silently loses digits beyond 2⁵³. |
| finite `float` | number | |
| `NaN`, `+inf`, `-inf` | `"NaN"`, `"Infinity"`, `"-Infinity"` | Not valid JSON. `null` would display a real value as *Not recorded*: a silent wrong answer. |
| `Decimal` | number (via `float`); a string if not finite or beyond 2⁵³ | Numbers can be charted. Display precision is enough for a browsing tool (spec §8). |
| `date`, `datetime`, `time` | ISO 8601 string | Vega-Lite reads ISO strings as dates. |
| `list`, `tuple` | array, each item converted | DuckDB `LIST`. |
| `dict` | object with string keys, each value converted | DuckDB `STRUCT` / `MAP`. |
| anything else (`timedelta`, `UUID`, bytes, …) | `str(value)` | Never blank, never guessed. |

Rows are **arrays in column order**, not objects. Two result columns can share a name
(`SELECT 1 AS a, 2 AS a`), which an object cannot represent. Vega-Lite wants objects, so the page
builds them from `columns` + `rows` when it renders the chart. That is a note for the next plan.

### Threading, precisely

- `/api/ask` and `/api/cancel` are plain `def` functions. FastAPI runs those on its worker thread
  pool, so a question that takes three minutes occupies one worker thread and **does not block the
  event loop**. `/api/cancel` and `/api/database` keep answering instantly while it runs. Writing
  `async def` here would run the blocking `runner.ask()` on the event loop itself and freeze the
  whole server, Stop button included. A stage 03 test proves the loop stays free.
- A second `/api/ask` while one is running gets `409` immediately (`QueryRunner` acquires its lock
  without waiting). Nothing is queued (spec §5).
- `/api/cancel` calls `runner.cancel()`, which may take a moment while it makes sure a running query
  stops. It returns `204` once that is done.

### Ctrl+C, precisely

uvicorn's graceful shutdown waits for in-flight requests to finish. A question running a
three-minute join would therefore keep the process alive for three minutes after Ctrl+C. So `serve`
uses a small `uvicorn.Server` subclass whose `handle_exit` (uvicorn's signal handler) first presses
Stop (`runner.cancel()`, on a separate thread, because a signal handler must not wait), then lets
uvicorn shut down as usual. The interrupted question returns `cancelled`, its response is sent, and
the process exits. After a clean shutdown uvicorn re-raises the signal, which surfaces as
`KeyboardInterrupt` in `serve`. `serve` catches it, so Ctrl+C ends with exit status 0 and no
traceback.

### Decisions taken, and what was rejected

| Decision | Why | Rejected alternative |
|---|---|---|
| **Every question outcome is HTTP 200, with `status` and `error` fields added to the spec's response** | Answered, declined, failed and cancelled are all normal outcomes (the same reasoning as `query.Answer`). One body shape keeps the page's code to one `switch`. Spec §9 is updated to list the two fields. | HTTP error codes for failed/cancelled: two body shapes, and a failure's attempt history would have to go into an error body. |
| **`date_ranges` replaces `years`** | The file stores no single "years" value AskCrossroads may rely on (§2C lists only the convention tables' names, columns and `kind` values). The per-column ranges are already discovered at startup. Spec §9 is updated. | Reading `years` from inside `crossroads_meta.parameters`: a JSON key outside §2C, and a duplicate of what `provenance.parameters` already shows. A min/max over all ranges: stretched by the build timestamp. |
| **`create_app(runner, context)` factory, no module-level app** | Tests inject a runner with `FakeModel`. Nothing is built at import time, so importing `app.py` never opens a database or reads a key. | A global `app = FastAPI()` configured at import: untestable without monkeypatching globals. |
| **Return `JSONResponse(...)` from every route** | Bypasses FastAPI's `jsonable_encoder`, so `json_value` is the **only** conversion and its rules are the rules. `JSONResponse` encodes with `allow_nan=False`, so anything non-strict fails loudly in tests rather than reaching a browser that cannot parse it. | Returning dicts: `jsonable_encoder` would silently apply its own, different rules for `Decimal` and `datetime`, and would still crash on `NaN`. |
| **`parse_chart_response` rejects `NaN`/`Infinity` constants** | Python's `json.loads` accepts them by default. A chart spec carrying one would make `JSONResponse` raise, and the user would lose the **table** for a chart problem, which spec §7 forbids. Rejecting them makes it a malformed reply, repaired by the existing chart loop. | Converting them inside the spec: Python never edits a returned spec (spec §7). |
| **API docs routes switched off** (`docs_url=None`, `redoc_url=None`, `openapi_url=None`) | Spec §9's surface is exactly three routes. FastAPI's `/docs` page also loads JavaScript from a CDN. | Leaving FastAPI's defaults on. |
| **`main` binds the socket itself (`listen`) and hands it to uvicorn** | A taken port is then an ordinary `OSError` in our code, answered with a message naming `ASKCROSSROADS_PORT`, instead of a uvicorn log line followed by `sys.exit`. Binding happens strictly after database validation, so a refused file never opens a port. The socket is listening before the browser opens, so the browser's first request cannot be refused. | `uvicorn.run(host, port)`: the bind error is uvicorn's, and opening the browser needs a polling loop to know when the server is up. |
| **Port from `ASKCROSSROADS_PORT` (default 8000); host always `127.0.0.1`** | Port 8000 is often taken. The override also lets tests start the real command on a free port. The host is **not** configurable: a server spending the user's API credit must not be reachable from their network. | A fixed port; a configurable host. |
| **Open the browser by default; `--no-browser` turns it off** | The audience does not write SQL. Opening the page removes a step where they could get stuck. The URL is printed too. Tests always pass `--no-browser`. | Print the URL only. |
| **Offline tests start the real `askcrossroads` command, pointed at a local stand-in for the Anthropic API** (`tests/provider_stub.py`, reached through the SDK's `ANTHROPIC_BASE_URL` environment variable) | Proves what in-process tests cannot: the installed command starts, binds localhost, serves, answers a question through the **real** `AnthropicClient` and real HTTP, stops on Stop and on Ctrl+C, and never puts the key in a response or the terminal, even when the stand-in deliberately echoes the key back in an error. No key and no network needed. | In-process `TestClient` only: nothing would ever show that the real command serves a request. |

### What `app.py` imports when this plan is done

Standard library: `argparse`, `datetime`, `decimal`, `math`, `socket`, `sys`, `threading`,
`webbrowser`, `pathlib.Path`. Third party: `uvicorn`, and from `fastapi`: `FastAPI`,
`RequestValidationError`, `JSONResponse`, `Response`, `StaticFiles`; `pydantic.BaseModel`. Project:
`config`, `llm`, `query`, and `Database`, `DatabaseError` from `database`. **Not** `anthropic`,
**not** `duckdb`, **not** `safety`, **not** `prompt`.

---

## Cross-Cutting Constraints

Every stage obeys all of these.

- **Never stage, commit, or amend anything in git.** Leave all changes in the working tree and tell
  the user what changed. This is absolute.
- **The API key is absolute.** Never hardcode a key or any credential in source, tests, docs,
  comments, or commit messages, not even a truncated or obviously fake one. Where a test needs a
  key, use the literal `"test-key-not-a-real-credential"`, as `tests/test_llm.py` does. The key is
  never printed, logged, put in an error message, or sent to the browser. In tests, check for a leak
  with `leaked = KEY in text` then `assert not leaked, "..."`. Never write `assert KEY not in text`:
  on failure pytest prints both operands, which for the marked suite would print the real key.
- **Tests never read the developer's real configuration.** Every in-process test that calls `main`
  does `monkeypatch.chdir(tmp_path)` and sets the environment explicitly. Every subprocess gets an
  environment built from scratch (only `PATH`, `HOME=tmp_path` and the variables it needs), never a
  copy of `os.environ`. The repository root contains the developer's real `.env`.
- **No offline test can reach the network.** Every offline test that constructs a real
  `AnthropicClient` sets `ANTHROPIC_BASE_URL` to the local stub or to the unreachable
  `http://127.0.0.1:9`.
- **Nothing about the schema is hardcoded in `src/`** (spec §2C). `app.py` names no table, column,
  view, dataset or code.
  `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` must return nothing. (The
  placeholder `index.html` must not mention any either.) Tests may use the synthetic fixture's
  names.
- **Every bound is a named constant in `config.py`.** This plan adds `ENV_PORT`. It adds no numeric
  bound. The JavaScript safe-integer limit in `app.py` is a fact about browsers, not a tunable, and
  stays in `app.py`.
- **`/api/database` never touches the database connection.** It serves a dict computed once from the
  frozen context.
- **Routes are thin.** No SQL, prompt text or model logic in `app.py`. If a route grows an `if`
  about SQL, the code belongs in `query.py`, and that is out of scope here.
- **No `--reload`, no `0.0.0.0`, no `async def` route.** `grep -n "async def" src/askcrossroads/app.py`
  must return nothing.
- **Code is simple and readable.** Plain-language comments where they earn their place. Every line
  must be readable by a person before commit.
- **Python 3.11 or newer, via the existing `.venv/`.** The system `python3` on the development
  machine is 3.9 and will not work.
- **Every stage leaves the default `pytest` run green** before the next stage starts.
- **Never mention private planning documents** in anything written to the repository.

### The commands every stage uses

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

The default, offline suite, with every variable that could leak the developer's configuration
removed:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

The marked suite (real API, real build; skips cleanly without them):

```bash
.venv/bin/python -m pytest -m integration
```

---

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Routes & response contract | `json_value`, `answer_to_json`, `describe_database`, `create_app` with the three routes, error handlers, and the static mount. Placeholder `index.html`. `httpx2` dev dependency. `parse_chart_response` rejects `NaN`. Spec §9 surface updated. | `tests/test_api.py` drives all three routes in-process with `TestClient`, a real synthetic DuckDB and `FakeModel`, and every body matches the contract above, including 409, 422 and 500. Every DuckDB value type survives `json.dumps(allow_nan=False)`. `main()` is unchanged. `pytest` green. | Plan 005 complete | [01-routes-and-contract.md](01-routes-and-contract.md) |
| 02 | Entry point & launch | `ASKCROSSROADS_PORT` in config. `main(argv)` with `--no-browser`: validate, then `listen` on 127.0.0.1, print the URL, open the browser, `serve` with uvicorn until Ctrl+C. | The installed `askcrossroads` command serves `/` and `/api/database` on 127.0.0.1, exits 0 on Ctrl+C with no traceback, refuses a non-Crossroads file with the §1 message and a non-zero exit **without opening a port**, and reports a taken port by naming `ASKCROSSROADS_PORT`. The browser opens unless `--no-browser`. `pytest` green. | 01 | [02-entry-point.md](02-entry-point.md) |
| 03 | Boundary hardening & end to end | The local provider stub. Ctrl+C presses Stop. Real-command tests of a full question, Stop over HTTP, the busy rejection, and an unblocked event loop. Key-leak tests, in-process and in a real process, against a stub that echoes the key. Marked real-API HTTP test. Spec §12 updated. | A question goes through the real command, the real `AnthropicClient` and the stub, and comes back `answered`. Stop over HTTP cancels a running query while `/api/database` still answers instantly, and a second question gets 409. Ctrl+C during a running query exits 0 within seconds. The key appears in no response body, no log record, and no terminal output. `pytest` green; `pytest -m integration` passes or skips cleanly. | 02 | [03-boundary-hardening.md](03-boundary-hardening.md) |

Stages must be done in order.

---

## Global Testing & Ship

Three layers, each proving something the others cannot:

1. **In-process routes** (`tests/test_api.py`, stage 01). FastAPI's `TestClient` against
   `create_app(QueryRunner(real synthetic Database, FakeModel), context)`. It is fast and
   deterministic, and covers every branch of the contract.
2. **The real command** (`tests/test_server.py`, stages 02–03). The installed `askcrossroads` script
   runs as a subprocess on a free localhost port, with `--no-browser`. For stage 03 it is pointed
   through `ANTHROPIC_BASE_URL` at `tests/provider_stub.py`, a small HTTP server inside the test
   process that answers like the Anthropic Messages API with scripted replies. This is the real
   launch path, real uvicorn, the real thread pool, real HTTP, the real `AnthropicClient`. Only the
   model's words are scripted.
3. **The real model over HTTP** (`tests/integration/test_api_live.py`, stage 03, marked
   `integration`). `TestClient` around `create_app` with the real `AnthropicClient` and the real
   build from `ASKCROSSROADS_TEST_DB`. It proves that real data's real Python types (dates, decimals)
   serialise, and that the real key does not appear in the response.

| What must be proved | Test | Stage |
|---|---|---|
| `/api/database`, `/api/ask`, `/api/cancel` return the documented shapes | `test_api.py` route tests | 01 |
| Every DuckDB value type becomes strict JSON; `NaN` never becomes `null` | `test_every_kind_of_duckdb_value_survives_json`, `test_a_nan_in_a_result_does_not_break_the_response` | 01 |
| A chart spec containing `NaN` is malformed, so the table survives | `test_llm.py::test_a_chart_with_a_non_json_constant_is_malformed` | 01 |
| A second question while one runs is rejected (spec §5) | `test_a_second_question_while_one_runs_is_rejected` (in-process); `test_stop_over_http_while_a_query_runs` (real command) | 01, 03 |
| Cancel with nothing running is a no-op (spec §5) | `test_cancel_with_nothing_running_is_a_no_op` | 01 |
| `/api/database` never touches the connection | `test_the_database_route_never_touches_the_connection` | 01 |
| A bug is a generic 500, and the next question still works | `test_an_unexpected_error_is_a_generic_500` | 01 |
| A non-Crossroads file is refused with the §1 message, non-zero, before a port is bound | `test_a_refused_file_never_opens_a_port` (in-process), `test_the_real_command_refuses_a_non_crossroads_file` | 02 |
| The server listens on 127.0.0.1 only | `test_main_serves_on_localhost_after_validating` | 02 |
| A taken port is reported by name | `test_a_taken_port_is_reported_by_name` | 02 |
| The browser opens at the printed address, and not with `--no-browser` | `test_main_opens_the_browser_at_the_printed_address`, `test_no_browser_opens_nothing` | 02 |
| The real command serves the page and the database summary, and exits 0 on Ctrl+C | `test_the_real_command_serves_and_stops_cleanly` | 02 |
| **A real question end to end through the real command** | `test_a_question_through_the_real_command` | 03 |
| **Stop over real HTTP interrupts a running query; the event loop stays free** | `test_stop_over_http_while_a_query_runs` | 03 |
| **Ctrl+C during a running query exits promptly and cleanly** | `test_ctrl_c_during_a_running_query` | 03 |
| **The key never reaches a response body or a log record**, even when the provider echoes it | `test_the_key_never_reaches_a_response_or_a_log` | 03 |
| **The key never reaches the real command's terminal output or responses** | `test_the_key_never_reaches_the_real_commands_output` | 03 |
| A real question over HTTP against the real model and real build | `tests/integration/test_api_live.py` | 03 |

### Ship-readiness for this plan

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest`
   is green.
2. `.venv/bin/python -m pytest -m integration` passes with a key and a real build present, and
   **skips cleanly**, never fails, without them.
3. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
4. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
5. `grep -n "async def" src/askcrossroads/app.py` returns nothing.
6. `grep -rn "0\.0\.0\.0\|--reload\|reload=True" src/` returns nothing.
7. `grep -n "api_key" src/askcrossroads/app.py` shows only `llm.AnthropicClient.from_config(settings)`
   or nothing. `app.py` never reads the key itself.
8. Manual run is **not** required and not part of shipping. The real-command tests in
   `tests/test_server.py` are the proof.
9. `git status` shows the new and modified files and **nothing staged**.

---

## Performance

- **`/api/ask` is dominated by model calls** (seconds each, up to six per question). The HTTP layer
  adds one pass over the result: `json_value` per cell. At the cap of 5,000 rows × 20 columns that
  is 100,000 cheap calls, tens of milliseconds, then one `json.dumps`. Nothing to optimise.
- **`/api/database` is computed once**, in `create_app`, from the frozen context. Each request only
  serialises a small dict.
- **Thread pool.** FastAPI's default pool has 40 worker threads. At most one is ever busy answering
  (the rest of `/api/ask` calls get 409 at once), and one more may be in `/api/cancel`.
- **Startup** is unchanged apart from binding a socket. Database validation and introspection
  dominate, as before.

---

## Open Questions / Risks

- **Stop, then asking again at once, can return 409.** When Stop is pressed during a model call,
  that call is not aborted (plan 005's design): it finishes, and its reply is discarded. Until then
  the one-question lock is held, so a new question asked within those seconds gets 409. The page
  (next plan) must show the 409 message, not treat it as a crash. A button that disables itself
  until `/api/ask` actually returns would avoid it entirely.
- **A model call in flight delays Ctrl+C.** Ctrl+C presses Stop, but a model call already in flight
  finishes first (seconds, typically). A second Ctrl+C makes uvicorn quit without waiting.
  FastAPI's worker threads are not daemon threads, so the interpreter may still wait for that one
  call to return before the process ends. Acceptable for v1. Record what you observe.
- **The SDK's `ANTHROPIC_BASE_URL` support is an external behaviour** that stage 03's tests rely on.
  Stage 03 begins with a probe that confirms it. If it ever stops working, the stage 03 tests fail
  loudly at "the stub received no request" rather than reaching the real API. Even if they did reach
  it, they only ever carry the placeholder key.
- **Third-party DEBUG logging.** The key-leak test captures log records at DEBUG level, including
  the `anthropic` and `httpx2` loggers. If a third-party library is found to log the key at DEBUG,
  **stop and report it to the user**. Do not weaken the test. The application never enables DEBUG
  logging itself.
- **No question length limit.** A user could paste a very long question. It costs their own credit,
  and the API rejects anything over the context window with a sanitised `ModelError`. Revisit only
  with an observed problem.
- **`str()` fallback for unusual types.** `BLOB` values display as Python's text form
  (`b'\x00...'`). They are rare in Crossroads-UK builds and are never blank. Revisit with an
  observed instance.
