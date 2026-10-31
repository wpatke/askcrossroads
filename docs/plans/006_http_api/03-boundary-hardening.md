# Stage 03 — Boundary Hardening & End to End

> Part of *HTTP API & Entry Point*. You have the overview + this stage; implement **only** this
> stage. Read [00-overview.md](00-overview.md) first, in particular *Threading, precisely*,
> *Ctrl+C, precisely*, and *Global Testing & Ship*.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 02 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -c "
from askcrossroads import app, config
print(app.listen, app.serve, app.main, config.ENV_PORT)
" && .venv/bin/askcrossroads --help && .venv/bin/python -m pytest tests/test_server.py tests/test_api.py -q
```

Expected: four names print, the help text prints, and both test files are green.

---

## Objective

Make the boundary hold, and prove the whole path through the real command: a local stand-in for
the Anthropic API, Ctrl+C that presses Stop, and tests proving that a full question, Stop, the busy
rejection, an unblocked server, a prompt Ctrl+C, and **no key leak anywhere** all work against the
real `askcrossroads` process. Add the marked real-API HTTP test.

---

## Implementation Steps

### 1. The provider stub: `tests/provider_stub.py`

New file. It is a helper module, not a test file: pytest collects only `test_*.py`. Import it as
`from provider_stub import ProviderStub`, the same way the tests already do
`from conftest import FakeModel`.

```python
"""A stand-in for the Anthropic Messages API on localhost, for offline tests.

The anthropic SDK sends its requests to ANTHROPIC_BASE_URL when that is set. Pointing it here
lets a test run the REAL AnthropicClient, and even the real askcrossroads command, with
scripted replies and no network. Nothing in AskCrossroads is replaced: only the model's words
are scripted.

    with ProviderStub() as provider:
        provider.reply_sql("SELECT 1 AS one")
        provider.reply_chart({"mark": "bar", "encoding": {...}})
        # ... point ANTHROPIC_BASE_URL at provider.url, then ask a question ...
        provider.requests   # what the client sent, in order

reply_error(..., echo_key=True) puts the API key the client sent into the error body, the way
a careless provider might. That is how the tests prove the key cannot leak even then.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _message(text):
    """A minimal successful Messages API reply carrying one text block."""
    return {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": "stub",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _error(status, message):
    kind = "authentication_error" if status == 401 else "invalid_request_error"
    return {"type": "error", "error": {"type": kind, "message": message}}


class ProviderStub:
    """Serves queued replies, one per request, in order. Records every request."""

    def __init__(self):
        self.requests = []   # {"path", "api_key", "body"} per request, oldest first
        self._replies = []
        self._condition = threading.Condition()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                status, payload, delay = stub._take(self.path, self.headers.get("x-api-key"), body)
                if delay:
                    time.sleep(delay)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass  # keep test output quiet

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exception):
        self._server.shutdown()
        self._server.server_close()
        return False

    # --- Scripting replies ----------------------------------------------------------

    def reply_text(self, text, delay=0):
        """A successful reply whose text is `text`, after `delay` seconds."""
        self._queue({"status": 200, "payload": _message(text), "delay": delay})

    def reply_sql(self, sql, delay=0):
        """The SQL call's structured reply: answerable, with this SQL (spec §6)."""
        self.reply_text(json.dumps({"answerable": True, "sql": sql, "explanation": None}), delay)

    def reply_chart(self, spec):
        self.reply_text(json.dumps(spec))

    def reply_error(self, status, echo_key=False):
        """An HTTP error. 400 and 401 are used because the SDK does not retry them."""
        self._queue({"status": status, "payload": None, "delay": 0, "echo_key": echo_key})

    def wait_for_requests(self, count, timeout=10):
        """True once at least `count` requests have arrived."""
        with self._condition:
            return self._condition.wait_for(lambda: len(self.requests) >= count, timeout)

    # --- Internals -----------------------------------------------------------------

    def _queue(self, reply):
        with self._condition:
            self._replies.append(reply)

    def _take(self, path, api_key, body):
        with self._condition:
            self.requests.append({"path": path, "api_key": api_key, "body": body})
            reply = self._replies.pop(0) if self._replies else None
            self._condition.notify_all()
        if reply is None:
            # 400 rather than 500, because the SDK retries a 500 and the test would slow down.
            return 400, _error(400, "The provider stub had no reply queued."), 0
        payload = reply["payload"]
        if payload is None:
            message = "Request rejected."
            if reply["echo_key"]:
                message = f"Request rejected for API key {api_key}."
            payload = _error(reply["status"], message)
        return reply["status"], payload, reply["delay"]
```

### 2. Probe: the SDK really talks to the stub

Run this before writing any test that relies on it. It prints no key. It prints only whether
comparisons held.

```bash
.venv/bin/python - <<'EOF'
import os, sys
sys.path.insert(0, "tests")
import anthropic
from provider_stub import ProviderStub
from askcrossroads import config

KEY = "test-key-not-a-real-credential"
with ProviderStub() as stub:
    os.environ["ANTHROPIC_BASE_URL"] = stub.url
    stub.reply_text("hello")
    reply = anthropic.Anthropic(api_key=KEY).messages.create(
        model=config.MODEL, max_tokens=16, messages=[{"role": "user", "content": "hi"}]
    )
    print("(a) text:", reply.content[0].text)
    print("(b) path:", stub.requests[0]["path"])
    print("(c) key sent:", stub.requests[0]["api_key"] == KEY)
    stub.reply_error(401, echo_key=True)
    try:
        anthropic.Anthropic(api_key=KEY).messages.create(
            model=config.MODEL, max_tokens=16, messages=[{"role": "user", "content": "hi"}]
        )
    except anthropic.AuthenticationError as error:
        print("(d) SDK error carries the echoed key:", KEY in str(error))
    print("(e) requests:", len(stub.requests))
EOF
```

| Probe | Expected | If not |
|---|---|---|
| (a) | `text: hello` | The SDK did not parse the stub's reply. Compare `_message` with the SDK's `Message` model and add the missing field. |
| (b) | `path: /v1/messages` | Use whatever path is printed. The stub serves any POST path, so nothing changes. |
| (c) | `key sent: True` | The stub is not reading the key header. Check the header name the SDK sends. |
| (d) | `... True` | If `False`, the echo tests would prove nothing. Find where the SDK puts the error body (try `error.body`) and make the stub's echo land somewhere the SDK includes in `str(error)`. |
| (e) | `requests: 2` | More means the SDK retried. Use a status it does not retry (400 or 401). |

If (a) or (c) fails because `ANTHROPIC_BASE_URL` is ignored (`stub.requests` is empty and the call
went elsewhere), **stop and report to the user**. Every test in this stage depends on it.

### 3. Ctrl+C presses Stop

`src/askcrossroads/app.py`. Add `import threading` to the standard-library imports.

Add, just above `serve`:

```python
class _StopOnExit(uvicorn.Server):
    """uvicorn's server, except that Ctrl+C also presses Stop on the running question.

    uvicorn shuts down gracefully: it waits for requests in progress to finish. Without this,
    a question running a long query would keep the process alive until the query ended,
    which could be minutes after the user pressed Ctrl+C.
    """

    def __init__(self, server_config, runner):
        super().__init__(server_config)
        self._runner = runner

    def handle_exit(self, sig, frame):
        # uvicorn calls this from its signal handler. cancel() can wait a moment for a query
        # to stop, and a signal handler must not wait, so Stop is pressed on its own thread.
        threading.Thread(target=self._runner.cancel, daemon=True).start()
        super().handle_exit(sig, frame)
```

Change `serve` to take and use the runner:

```python
def serve(application, listener, runner):
    """Run the web server on `listener` until the user presses Ctrl+C.

    Ctrl+C also presses Stop on any running question, so the server stops promptly.
    """
    server = _StopOnExit(uvicorn.Config(application, log_level="info"), runner)
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        # After shutting down cleanly, uvicorn re-raises the Ctrl+C it caught. Ctrl+C is how
        # the user stops the application, so it is not an error and gets no traceback.
        pass
```

In `main`, change the call to `serve(create_app(runner, database.context), listener, runner)`.

Expected: `tests/test_server.py` from stage 02 still passes unchanged (`fake_serve` accepts the
extra argument).

### 4. Spec §12: the new test files

`docs/spec.md`, section `## 12. Repository Blueprint`. In the `tests/` part of the tree, after the
`test_api.py` line, add:

```text
│   ├── test_server.py                  # The real command: refusal, localhost, Stop, Ctrl+C
│   ├── provider_stub.py                # Local stand-in for the Anthropic API (offline tests)
```

and inside `integration/`, after `test_end_to_end.py`:

```text
│       ├── test_api_live.py            # A real question over HTTP
```

Keep the tree's alignment and box-drawing characters consistent with the lines around them.

---

## Testing & Verification

Shared constants. Add these to **`tests/test_server.py`** under the existing constants (same values
as in `tests/test_api.py`):

```python
from provider_stub import ProviderStub

QUESTION = "How many collisions were there in each kind of weather?"
GOOD_SQL = (
    "SELECT weather_conditions_label AS weather, COUNT(*) AS collisions "
    "FROM collisions_labelled "
    "GROUP BY weather_conditions_label "
    "ORDER BY MIN(weather_conditions)"
)
CHART = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "weather", "type": "nominal", "sort": None},
        "y": {"field": "collisions", "type": "quantitative"},
    },
}
# Runs for minutes unless interrupted. range() needs no table and no external access.
SLOW_SQL = "SELECT SUM(range) AS total FROM range(1000000000000)"

# What llm.py tells the user when the provider rejects the key (llm._sanitise).
REJECTED_KEY = "rejected the configured key"


@pytest.fixture
def provider():
    with ProviderStub() as stub:
        yield stub


def ask_in_background(url, question=QUESTION):
    """POST /api/ask on a thread, as the page does while the user waits. Returns (thread, outcome)."""
    outcome = {}

    def run():
        outcome["response"] = httpx2.post(url + "/api/ask", json={"question": question}, timeout=60)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, outcome
```

(Add `import threading` to the file's imports.)

### Integration tests (PRIMARY): the real command, `tests/test_server.py`

Each is `@posix_only`, starts `command = real_command(crossroads_db, provider_url=provider.url)`,
and calls `command.wait_until_serving()` first.

- **`test_a_question_through_the_real_command(crossroads_db, real_command, provider)`.** Queue
  `provider.reply_sql(GOOD_SQL)` and `provider.reply_chart(CHART)`. `POST /api/ask` (timeout 60) →
  200, `status == "answered"`, `sql == GOOD_SQL`, `rows[0][0] == "Fine no high winds"`,
  `chart == CHART`. `len(provider.requests) == 2`. Every request's `api_key == KEY`, so the key
  reaches the provider and that is the only place it goes. Every request's `body["model"] ==
  config.MODEL`. The first request body has an `"output_config"` key (the SQL call's structured
  output). The second does not (the chart call). Then `command.stop() == 0`.
- **`test_stop_over_http_while_a_query_runs(crossroads_db, real_command, provider)`.** Queue
  `provider.reply_sql(SLOW_SQL)`. `worker, outcome = ask_in_background(command.url)`.
  `assert provider.wait_for_requests(1)`, then `time.sleep(0.5)` so the slow query is under way.
  Then, in order:
  1. **The server is not blocked.** Time `httpx2.get(url + "/api/database", timeout=5)`: 200, in
     under 2 seconds. A blocked event loop would not answer at all.
  2. **One question at a time.** `httpx2.post(url + "/api/ask", json={"question": "Another"},
     timeout=5)` → 409, and its `error` is the `query.Busy` message.
     `len(provider.requests) == 1`: the rejected question never reached the model.
  3. **Stop.** `httpx2.post(url + "/api/cancel", timeout=10)` → 204.
  4. `worker.join(timeout=15)`, and `not worker.is_alive()`. The first response is 200 with
     `status == "cancelled"` and `sql_attempts == []`.
  5. **The connection survives.** Queue `reply_sql("SELECT 1 AS one")` and `reply_chart(CHART)`.
     A new `POST /api/ask` → `status == "answered"`.
  6. `command.stop() == 0`.
- **`test_ctrl_c_during_a_running_query(crossroads_db, real_command, provider)`.** Queue
  `reply_sql(SLOW_SQL)`. Start `ask_in_background`, `wait_for_requests(1)`, `time.sleep(0.5)`.
  Record the time, then `status = command.stop(timeout=20)`. `status == 0`, and it took under
  15 seconds. Without step 3 this test fails: the query would hold the server open for minutes.
  `worker.join(timeout=10)`. The ask's response is 200 with `status == "cancelled"`.
  `"Traceback"` is not in `command.stderr()`.
- **`test_the_key_never_reaches_the_real_commands_output(crossroads_db, real_command, provider)`.**
  Queue, in order: `reply_error(401, echo_key=True)` (the SQL call fails authentication),
  `reply_sql(GOOD_SQL)`, `reply_error(400, echo_key=True)` (the chart call fails). Then:
  - `first = POST /api/ask`: `status == "failed"`, and `REJECTED_KEY` is in `error`.
  - `second = POST /api/ask`: `status == "answered"`, rows non-empty, `chart is None`,
    `chart_attempts[0]["error"]` non-empty.
  - `summary = GET /api/database`.
  - `provider.requests[0]["api_key"] == KEY`, proving the stub had the key to echo.
  - `command.stop() == 0`.
  - `everything = first.text + second.text + summary.text + command.stdout() + command.stderr()`.
    `leaked = KEY in everything`, then `assert not leaked, "the API key reached a response or the
    terminal"`.

### Integration tests: in-process, `tests/test_api.py`

Add to the imports: `import logging`, `import anthropic`, `from provider_stub import ProviderStub`.
Add `KEY = "test-key-not-a-real-credential"` and the same `provider` fixture as above.

- **`test_the_stub_really_echoes_the_key(provider)`**: the control for the next test. Queue
  `reply_error(401, echo_key=True)`. Calling `anthropic.Anthropic(api_key=KEY,
  base_url=provider.url).messages.create(model=config.MODEL, max_tokens=16,
  messages=[{"role": "user", "content": "hi"}])` raises `anthropic.AuthenticationError`.
  `echoed = KEY in str(raised.value)`, then `assert echoed, "the stub did not echo the key, so the
  leak tests would prove nothing"`.
- **`test_the_key_never_reaches_a_response_or_a_log(database, provider, monkeypatch, caplog)`.**
  `monkeypatch.setenv("ANTHROPIC_BASE_URL", provider.url)`. `caplog.set_level(logging.DEBUG)`,
  which captures every logger, including the SDK's and the HTTP client's. Queue the same three
  replies as the real-command key test. Build
  `TestClient(app.create_app(query.QueryRunner(database, llm.AnthropicClient(KEY)), database.context))`.
  Ask twice and `GET /api/database`. Assert the same outcomes as the real-command key test, and
  `len(provider.requests) == 3`. Then
  `everything = first.text + second.text + summary.text + caplog.text`, and the
  `leaked = ...; assert not leaked` check. `caplog.text` includes formatted tracebacks. If this
  fails only because a **third-party** logger prints the key at DEBUG, stop and report to the user
  (see the overview's *Open Questions*). Do not weaken the test.

### Marked real-API test: `tests/integration/test_api_live.py`

New file:

```python
"""A real question over HTTP: real API, real build (spec §11.2).

Proves what the offline suite cannot: that a real build's real values (dates, decimals) come
back as strict JSON through the real routes, and that the real key never appears in a response.

Skips, never fails, without an API key or a real build.
"""

import json

import pytest
from fastapi.testclient import TestClient

from askcrossroads import app, query

from live_fixtures import client, database, settings  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.integration

# Answerable by any Crossroads-UK build, and names no table or column (spec §2C).
GROUPED_QUESTION = "How many records are there for each year this database covers?"


@pytest.fixture(scope="module")
def http(database, client):
    return TestClient(app.create_app(query.QueryRunner(database, client), database.context))


def test_the_database_summary_describes_the_real_build(http):
    body = http.get("/api/database").json()
    assert body["provenance"]["crossroads_version"]
    assert body["tables"]
    assert body["date_ranges"]
    assert body["coded_columns"]


def test_a_real_question_over_http(http, settings):
    response = http.post("/api/ask", json={"question": GROUPED_QUESTION})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered", (body["error"], body["explanation"], body["sql_attempts"])
    assert body["rows"]
    # The body is strict JSON: no NaN or Infinity slipped through.
    json.loads(response.text, parse_constant=lambda name: pytest.fail(f"{name} in the response"))
    # Never `assert key not in text`: on failure pytest would print the real key.
    leaked = settings.api_key in response.text
    assert not leaked, "the API key appeared in the response"
```

### Commands

Run the probe (step 2) first. Then:

```bash
.venv/bin/python -m pytest tests/test_server.py tests/test_api.py -v
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

Expected: green, and no `askcrossroads` process left behind.

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_TEST_DB .venv/bin/python -m pytest -m integration
```

Expected: every integration test **skips** (no key, no build). Not one fails. If you have a key and
a real build available, the user may also run `.venv/bin/python -m pytest -m integration` and
expect passes. Do not run the marked suite with the user's key unless the user asks: it costs them
money.

### Stage ship-readiness checklist (also the plan's)

1. The default suite is green, and leaves no `askcrossroads` process behind.
2. The marked suite skips cleanly without a key and build.
3. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
4. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
5. `grep -n "async def" src/askcrossroads/app.py` returns nothing (or only the two exception
   handlers, if stage 01 recorded that fallback).
6. `grep -rn "0\.0\.0\.0\|--reload\|reload=True" src/` returns nothing.
7. `grep -n "api_key" src/askcrossroads/app.py` returns nothing. `app.py` passes `settings` to
   `AnthropicClient.from_config` and never reads the key itself.
8. `grep -n "KEY not in\|api_key not in" tests/test_api.py tests/test_server.py tests/integration/test_api_live.py`
   returns nothing. Every leak check this plan adds uses the `leaked = ...; assert not leaked`
   pattern. (Older tests in `tests/test_config.py` use the placeholder key directly and are left
   as they are.)
9. `git status`: the new and modified files, **nothing staged**.

---

## End State / Handoff

- `tests/provider_stub.py` provides `ProviderStub` (`url`, `reply_text`, `reply_sql`,
  `reply_chart`, `reply_error(status, echo_key)`, `requests`, `wait_for_requests`), which the
  next plans may reuse for any offline test of the real command.
- Ctrl+C presses Stop. `app.serve(application, listener, runner)` uses `_StopOnExit`, and
  `main` passes the runner.
- Proven against the real `askcrossroads` process: a full question through the real
  `AnthropicClient`; Stop over HTTP cancels a running query while `/api/database` answers in under
  2 seconds; a second question gets 409 without reaching the model; the connection is usable after
  Stop; Ctrl+C during a running query exits 0 within 15 seconds with no traceback.
- Proven in a real process and in-process with DEBUG logging captured: the key appears in no
  response body, no log record, and no terminal output, even when the provider echoes it.
- `tests/integration/test_api_live.py` passes with a key and build, and skips without.
- Spec §12 lists the new test files.
- **For the next plan (the page):** build against the overview's *HTTP contract*. Rows are arrays,
  so build the Vega-Lite `data.values` objects from `columns` + `rows`. Show 409 as a message (Stop
  then an immediate re-ask can hit it). Render `null` as *Not recorded*. The strings `"NaN"` and
  `"Infinity"` are real values and must be shown as they are.

---

## Failure Modes & Rollback

- **Probe (a)–(e) fails** → see the probe table. If `ANTHROPIC_BASE_URL` is ignored, stop and
  report. Do not point any test at the real API.
- **`test_stop_over_http_while_a_query_runs` step 1 fails (slow `/api/database`)** → a route was
  written `async def`, or something blocks the event loop. Fix the route. Never raise the
  2-second bound to make it pass.
- **The Ctrl+C test times out** → `_StopOnExit.handle_exit` is not being called (check that `serve`
  builds `_StopOnExit`, not `uvicorn.Server`), or `cancel()` is running on the signal handler's own
  thread and blocking it.
- **The Ctrl+C test gets a closed connection instead of a `cancelled` response** → uvicorn closed
  the connection before the response was written. The contract is that the process exits 0
  promptly with no traceback. If that holds and only the response is missing, change that one
  assertion to accept either a `cancelled` response or a closed connection, and record exactly what
  was observed.
- **The in-process key test fails on a third-party DEBUG log line** → stop and report to the user,
  quoting the logger name only (never the line, which contains the key).
- **A subprocess is left running after a failure** → the `real_command` fixture kills every
  command it started. If one survives, `pkill -f "askcrossroads --no-browser"`, and fix the fixture.
- **Rollback:** `git checkout -- src/askcrossroads/app.py docs/spec.md tests/test_server.py
  tests/test_api.py`, and delete `tests/provider_stub.py` and `tests/integration/test_api_live.py`.
  This returns the repository to the end of stage 02.
