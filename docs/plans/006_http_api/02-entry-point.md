# Stage 02 — Entry Point & Launch

> Part of *HTTP API & Entry Point*. You have the overview + this stage; implement **only** this
> stage. Read [00-overview.md](00-overview.md) first, in particular *Data flow*, and the decisions
> about binding the socket, the port, and the browser.
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
from askcrossroads import app
print(app.create_app, app.json_value, app.answer_to_json, app.describe_database)
" && test -f src/askcrossroads/static/index.html && .venv/bin/python -m pytest tests/test_api.py -q
```

Expected: four names print, the page exists, and `tests/test_api.py` is green.

```bash
.venv/bin/python -c "import httpx2; print(httpx2.get, httpx2.post, httpx2.TransportError)"
```

Expected: three names print. This stage's tests use `httpx2` as an HTTP client. Its API is
httpx's. If `TransportError` does not exist, use `httpx2.HTTPError` wherever this stage writes
`httpx2.TransportError`, and record it.

---

## Objective

Make `askcrossroads` start the application: validate first, then listen on `127.0.0.1` at
`ASKCROSSROADS_PORT` (default 8000), print the address, open the browser unless `--no-browser`, and
serve until Ctrl+C, which exits 0. Every refusal happens on the terminal, with a non-zero exit,
before a port is opened.

---

## Implementation Steps

### 1. The port setting

`src/askcrossroads/config.py`.

In *Configuration sources*, add after `ENV_DATABASE`:

```python
ENV_PORT = "ASKCROSSROADS_PORT"   # optional; config.PORT when unset
```

Add a third field to `Config`, **after** `database_path`, with a default so existing
`Config(api_key=..., database_path=...)` calls keep working:

```python
    api_key: str = field(repr=False)
    database_path: Path
    port: int = PORT
```

Extend the docstring of `Config` with one line: `port` is the localhost port to listen on.

In `load_config`:

1. Change the loop that lets the real environment win to cover the port too:

   ```python
       for name in (ENV_API_KEY, ENV_DATABASE, ENV_PORT):
   ```

2. After the database-path checks and before `if problems:`, add:

   ```python
       # Optional. Something else on the machine often has 8000 already.
       raw_port = (values.get(ENV_PORT) or "").strip()
       port = PORT
       if raw_port:
           if raw_port.isdigit() and 1 <= int(raw_port) <= 65535:
               port = int(raw_port)
           else:
               problems.append(
                   f"{ENV_PORT} is {raw_port!r}, which is not a port number between 1 and 65535."
               )
   ```

3. Return `Config(api_key=api_key, database_path=database_path, port=port)`.

`HOW_TO_CONFIGURE` is **not** changed. It lists what is required, and the port is optional.

`.env.example`: append

```text

# Optional. The port AskCrossroads listens on, on this machine only. Change it if
# something else is already using 8000.
# ASKCROSSROADS_PORT=8000
```

### 2. `listen`, `serve`, `main`

`src/askcrossroads/app.py`.

Add to the imports (keep them grouped as the file already groups them: standard library, third
party, project):

```python
import argparse
import socket
import webbrowser

import uvicorn

from askcrossroads import config, llm, query
```

`from askcrossroads import query` already exists. Merge it into the line above rather than
importing twice. Keep `from askcrossroads.config import ConfigError, load_config` and the
`database` import.

Update the module docstring's last paragraph to:

```text
Every other path is the static page. `main` is the `askcrossroads` command: it validates
everything first, then listens on localhost and serves until Ctrl+C.
```

Append after `create_app`:

```python
def listen(port):
    """A socket bound to localhost (config.HOST) and listening on `port`.

    Opened here rather than by uvicorn for two reasons. A port that is already in use becomes
    an ordinary OSError that main() can explain in plain words. And the browser can be opened
    straight away: its first request waits in this socket's queue until the server is ready,
    instead of being refused.

    Localhost only, always. This server spends the user's API credit and shows their data,
    so nothing else on their network may reach it (spec §9).
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Lets the application be restarted at once after it stops, instead of waiting for
        # the operating system to release the port.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((config.HOST, port))
        listener.listen()
    except OSError:
        listener.close()
        raise
    return listener


def serve(application, listener):
    """Run the web server on `listener` until the user presses Ctrl+C."""
    server = uvicorn.Server(uvicorn.Config(application, log_level="info"))
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        # After shutting down cleanly, uvicorn re-raises the Ctrl+C it caught. Ctrl+C is how
        # the user stops the application, so it is not an error and gets no traceback.
        pass


def _parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="askcrossroads",
        description="Ask questions of a Crossroads-UK road-safety database in plain English.",
    )
    parser.add_argument(
        "--no-browser",
        dest="open_browser",
        action="store_false",
        help="do not open a browser tab; just print the address",
    )
    return parser.parse_args(argv)


def _print_summary(context):
    """Confirm on the terminal which database was loaded. The API key is never printed."""
    provenance = context.provenance
    # The path is not a secret and is worth confirming back to the user.
    print(f"Database: {context.path}")
    print(
        f"Built by Crossroads-UK {provenance.crossroads_version}, "
        f"schema {provenance.schema_version}, at {provenance.built_at_utc}"
    )
    print(
        f"{len(context.tables)} tables, {len(context.views)} views, "
        f"{len(context.codebook)} codebook rows, "
        f"{len(context.coded_column_names)} coded columns, "
        f"{len(context.temporal_ranges)} dated columns"
    )
```

Replace the existing `main` with:

```python
def main(argv=None):
    """Console entry point. Returns the process exit code.

    The console script wrapper calls ``sys.exit(main())``, so the value returned here becomes
    the process exit status. `argv` is the command-line arguments after the program name;
    None means the real ones. Tests pass a list.

    The order is the point (spec §1). Configuration is checked, then the database is opened
    and validated, and only then is a port opened. A problem at any of those steps is printed
    on the terminal with a non-zero exit, so the user never gets a browser window onto a
    broken server.
    """
    arguments = _parse_arguments(argv)

    try:
        settings = load_config()
    except ConfigError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # NotACrossroadsBuild subclasses DatabaseError, so one except covers both "the file
    # would not open" and "the file opened but is not a Crossroads-UK build".
    try:
        database = Database.open(settings.database_path)
    except DatabaseError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # Open for as long as the server runs, and closed when it stops.
    with database:
        _print_summary(database.context)

        try:
            listener = listen(settings.port)
        except OSError as error:
            print(
                f"askcrossroads: could not listen on {config.HOST} port {settings.port}: "
                f"{error.strerror or error}.\n"
                f"Another program may be using that port. Set {config.ENV_PORT} to use a "
                "different one.",
                file=sys.stderr,
            )
            return 1

        with listener:
            # The key goes straight into the model client. Nothing here keeps or prints it.
            runner = query.QueryRunner(database, llm.AnthropicClient.from_config(settings))
            url = f"http://{config.HOST}:{settings.port}/"
            # flush=True so the address appears at once, even when output is piped to a file.
            print(f"AskCrossroads is running at {url}", flush=True)
            print("Press Ctrl+C to stop.", flush=True)
            if arguments.open_browser:
                webbrowser.open(url)
            serve(create_app(runner, database.context), listener)

    return 0
```

Expected: `.venv/bin/askcrossroads --help` prints usage with `--no-browser` and exits 0.

### 3. Spec §9: the port and the launch

`docs/spec.md`, section `### The database and the key come from configuration`. After its last
paragraph (the one ending *"a fair trade for a page with one job (§13)."*), add:

```markdown
The application listens on `127.0.0.1` only, on port 8000 unless `ASKCROSSROADS_PORT` says otherwise. The host is deliberately not configurable: a server that spends the user's API credit and shows their data must not be reachable from the rest of their network. On start it prints its address and opens it in the default browser; `askcrossroads --no-browser` prints the address only. Ctrl+C stops it.
```

### 4. Update the existing tests that ran `main`

`tests/test_config.py`:

- `test_environment_variable_names`: add `assert config.ENV_PORT == "ASKCROSSROADS_PORT"`.
- `test_main_exits_non_zero_when_not_configured`: call `main([])` instead of `main()`. Called with no
  argument under pytest, argparse would read **pytest's** command line. Also
  `monkeypatch.delenv("ASKCROSSROADS_PORT", raising=False)`.
- **Delete** `test_main_exits_zero_when_configured`. `main` now serves until Ctrl+C. The tests in
  `tests/test_server.py` below replace it.
- `test_env_example_holds_placeholders_only`: add `assert "ASKCROSSROADS_PORT" in text`.
- Add:

  ```python
  def test_the_port_defaults_when_unset(db_file):
      result = load_config(
          env_file=None,
          environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
      )
      assert result.port == config.PORT == 8000


  def test_the_port_can_be_set_in_the_environment_or_env_file(tmp_path, db_file):
      environ = {"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)}
      assert load_config(env_file=None, environ={**environ, "ASKCROSSROADS_PORT": "8123"}).port == 8123

      env_file = tmp_path / ".env"
      env_file.write_text("ASKCROSSROADS_PORT=8124\n")
      assert load_config(env_file=env_file, environ=environ).port == 8124


  @pytest.mark.parametrize("value", ["abc", "0", "65536", "-1", "80.5", "8000x"])
  def test_a_bad_port_is_refused_by_name(db_file, value):
      with pytest.raises(ConfigError) as raised:
          load_config(
              env_file=None,
              environ={
                  "ANTHROPIC_API_KEY": FAKE_KEY,
                  "ASKCROSSROADS_DB": str(db_file),
                  "ASKCROSSROADS_PORT": value,
              },
          )
      assert "ASKCROSSROADS_PORT" in str(raised.value)
  ```

`tests/test_package.py`: replace `test_console_script_runs` with this. It no longer runs the
server: that blocks until Ctrl+C, and `tests/test_server.py` covers it properly.

```python
def test_console_script_runs():
    """The installed `askcrossroads` command resolves and runs.

    `--help` exits before anything is loaded, so this needs no database and no key. Starting
    the real server is covered in tests/test_server.py.
    """
    script = Path(sysconfig.get_path("scripts")) / "askcrossroads"
    assert script.exists(), f"console script not found at {script}; is the package installed?"
    result = subprocess.run(
        [str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert "--no-browser" in result.stdout
```

Remove imports from `tests/test_package.py` that are now unused (for example the `crossroads_db`
fixture argument and `tmp_path` are gone from this test). Keep the rest of the file unchanged.

---

## Testing & Verification

### Integration tests (PRIMARY): `tests/test_server.py`

New file. Two halves: `main` in-process with `serve` replaced, which is exact about what `main`
does before serving, and the **real installed command** as a subprocess, which proves it actually
serves and stops. Write the header and helpers exactly as follows. Stage 03 extends this file.

```python
"""The `askcrossroads` command: validation before any port is opened, localhost only, the
browser, and a clean stop (spec §1, §9).

The first half calls main() in-process with `serve` replaced, so each test can see exactly
what main did before serving. The second half runs the real installed command as a
subprocess on a free port, which is the only way to prove it really serves and really stops.

Every test builds its own environment. None reads the developer's real .env or key, and none
can reach the network: the model client is pointed at a local address where nothing listens.
"""

import os
import signal
import socket
import subprocess
import sys
import sysconfig
import time
import webbrowser
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from askcrossroads import app, config

KEY = "test-key-not-a-real-credential"

# Nothing listens on port 9 (the "discard" port). A model call pointed here fails at once,
# offline. No test in this half should make one, but if a bug did, it could not reach the
# real API.
UNREACHABLE = "http://127.0.0.1:9"

SCRIPT = Path(sysconfig.get_path("scripts")) / "askcrossroads"

REFUSAL = "does not look like a Crossroads-UK database"

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="stops the server with a POSIX Ctrl+C signal"
)


def free_port():
    """A localhost port nothing is listening on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_is_free(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


# --- main() in-process ------------------------------------------------------------------


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Point main() at a database on a free port, with a placeholder key, from a directory
    with no .env in it. Returns a function taking the database path; it returns the port."""

    def configure(database):
        port = free_port()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        monkeypatch.setenv("ASKCROSSROADS_DB", str(database))
        monkeypatch.setenv("ASKCROSSROADS_PORT", str(port))
        monkeypatch.setenv("ANTHROPIC_BASE_URL", UNREACHABLE)
        return port

    return configure


@pytest.fixture
def served(monkeypatch):
    """Replace app.serve, recording what main() handed it instead of running a server.

    The recorded application is exercised through TestClient while main() is still inside
    serve, so a test sees the real, wired-up application main() built.
    """
    calls = []

    def fake_serve(application, listener, *rest):
        calls.append({
            "address": listener.getsockname(),
            "database": TestClient(application).get("/api/database"),
        })

    monkeypatch.setattr(app, "serve", fake_serve)
    return calls


@pytest.fixture
def browser(monkeypatch):
    """Record webbrowser.open calls instead of opening anything."""
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *args, **kwargs: opened.append(url))
    return opened
```

In-process tests (names exactly as given):

- `test_main_serves_on_localhost_after_validating(crossroads_db, configured, served, browser, capsys)`:
  `port = configured(crossroads_db)`. `app.main(["--no-browser"]) == 0`. `len(served) == 1`.
  `served[0]["address"] == ("127.0.0.1", port)`. `served[0]["database"].status_code == 200`, and
  its JSON's `provenance.crossroads_version == "0.9.0"`. stdout contains
  `f"AskCrossroads is running at http://127.0.0.1:{port}/"`. The key is in neither stdout nor
  stderr: `leaked = KEY in captured.out + captured.err` then `assert not leaked`. Afterwards
  `port_is_free(port)` is true, so the socket was closed.
- `test_main_opens_the_browser_at_the_printed_address(crossroads_db, configured, served, browser, capsys)`:
  `app.main([]) == 0`. `browser == [f"http://127.0.0.1:{port}/"]`, and that URL is in stdout.
- `test_no_browser_opens_nothing(crossroads_db, configured, served, browser)`:
  `app.main(["--no-browser"]) == 0` and `browser == []`.
- `test_a_refused_file_never_opens_a_port(crossroads_db_factory, configured, served, browser, capsys)`:
  `port = configured(crossroads_db_factory(convention="missing"))`. `app.main([]) == 1`.
  `served == []`, `browser == []`. stderr contains `REFUSAL` and `"pip install crossroads-uk"`.
  `"running at"` is not in stdout. `port_is_free(port)`.
- `test_a_taken_port_is_reported_by_name(crossroads_db, configured, served, browser, monkeypatch, capsys)`:
  `configured(crossroads_db)`. Then open `occupier = socket.socket()`, bind it to
  `("127.0.0.1", 0)`, call `listen()`, and
  `monkeypatch.setenv("ASKCROSSROADS_PORT", str(occupier.getsockname()[1]))`. With the occupier
  still open: `app.main([]) == 1`, `served == []`, `browser == []`, and stderr contains
  `"ASKCROSSROADS_PORT"` and the port number. Close the occupier in a `finally`.
- `test_a_bad_port_setting_exits_before_opening_the_database(crossroads_db, configured, served, monkeypatch, capsys)`:
  `configured(crossroads_db)`, then `monkeypatch.setenv("ASKCROSSROADS_PORT", "not-a-port")`.
  `app.main([]) == 1`, `served == []`, `"ASKCROSSROADS_PORT"` in stderr, and `"Database:"` not in
  stdout.

The real command. Append these helpers after the in-process tests:

```python
# --- The real command -------------------------------------------------------------------


class RealCommand:
    """The installed `askcrossroads` script, started as a user would start it.

    Output goes to files rather than pipes, so a chatty server can never block on a full pipe.
    The environment is built from scratch: never a copy of os.environ.
    """

    def __init__(self, workdir, database, provider_url=UNREACHABLE):
        if not SCRIPT.exists():
            pytest.fail(f"console script not found at {SCRIPT}; run .venv/bin/pip install -e .")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._stdout = open(workdir / "stdout.txt", "w+")
        self._stderr = open(workdir / "stderr.txt", "w+")
        environment = {
            "PATH": os.environ.get("PATH", ""),
            # Somewhere with nothing in it, so no real profile or .env is ever read.
            "HOME": str(workdir),
            "PYTHONUNBUFFERED": "1",
            "ANTHROPIC_API_KEY": KEY,
            "ASKCROSSROADS_DB": str(database),
            "ASKCROSSROADS_PORT": str(self.port),
            "ANTHROPIC_BASE_URL": provider_url,
        }
        self.process = subprocess.Popen(
            [str(SCRIPT), "--no-browser"],
            cwd=workdir,
            env=environment,
            stdout=self._stdout,
            stderr=self._stderr,
        )

    def wait_until_serving(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"askcrossroads exited early:\n{self.stderr()}")
            try:
                if httpx2.get(self.url + "/api/database", timeout=1).status_code == 200:
                    return
            except httpx2.TransportError:
                pass
            time.sleep(0.1)
        raise AssertionError(f"askcrossroads never started serving:\n{self.stderr()}")

    def stop(self, timeout=20):
        """Press Ctrl+C and wait. Returns the exit status.

        Killed if it has not stopped in time, so a failing test never leaves a server running.
        """
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise AssertionError(f"askcrossroads did not stop within {timeout}s of Ctrl+C")

    def stdout(self):
        self._stdout.flush()
        return Path(self._stdout.name).read_text()

    def stderr(self):
        self._stderr.flush()
        return Path(self._stderr.name).read_text()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self._stdout.close()
        self._stderr.close()


@pytest.fixture
def real_command(tmp_path):
    """Start the real command: real_command(database, provider_url=...). Always cleaned up."""
    started = []

    def start(database, **options):
        workdir = tmp_path / f"run{len(started)}"
        workdir.mkdir()
        command = RealCommand(workdir, database, **options)
        started.append(command)
        return command

    yield start
    for command in started:
        command.close()
```

Real-command tests:

- `@posix_only test_the_real_command_serves_and_stops_cleanly(crossroads_db, real_command)`:
  start, `wait_until_serving()`. `httpx2.get(url + "/")` → 200, `"text/html"` in its content type,
  `"AskCrossroads"` in the text. `httpx2.get(url + "/api/database").json()["provenance"]
  ["crossroads_version"] == "0.9.0"`. `command.stop() == 0`. `"Traceback"` is not in
  `command.stderr()`. stdout contains `f"AskCrossroads is running at {command.url}/"`. The key is
  in neither stdout nor stderr (the `leaked = ...; assert not leaked` pattern).
- `test_the_real_command_refuses_a_non_crossroads_file(crossroads_db_factory, real_command)`: start
  with `crossroads_db_factory(convention="missing")`. `command.process.wait(timeout=60) != 0`.
  `REFUSAL` and `"pip install crossroads-uk"` are in stderr. `"running at"` is not in stdout.
  `port_is_free(command.port)`.

### Commands

```bash
.venv/bin/python -m pytest tests/test_server.py tests/test_config.py tests/test_package.py -v
```

Expected: all pass.

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

Expected: green. The suite must not hang. Every subprocess has a timeout, and the fixture kills
anything still running.

### Stage ship-readiness checklist

1. The full default suite is green, and it leaves no `askcrossroads` process behind
   (`pgrep -fl askcrossroads` shows nothing of this repository's).
2. `.venv/bin/askcrossroads --help` exits 0 and mentions `--no-browser`.
3. `grep -rn "0\.0\.0\.0\|--reload\|reload=True" src/` returns nothing.
4. `grep -n "config.HOST" src/askcrossroads/app.py` shows `listen` binding `config.HOST`. No other
   host is used anywhere.
5. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
6. `git status`: nothing staged.

---

## End State / Handoff

- `config.ENV_PORT == "ASKCROSSROADS_PORT"`. `Config.port` defaults to `config.PORT`, and a bad
  value is a `ConfigError` naming the variable.
- `app.main(argv=None)` validates configuration, then the database, then opens the port, in that
  order, and returns 1 at the first failure with a message on stderr. Otherwise it prints
  `AskCrossroads is running at http://127.0.0.1:PORT/`, opens the browser unless `--no-browser`,
  and calls `app.serve(application, listener)`. It returns 0 when that returns.
- `app.listen(port)` returns a socket listening on `127.0.0.1:port`.
- `app.serve(application, listener)` runs uvicorn on that socket and swallows the
  `KeyboardInterrupt` uvicorn re-raises after Ctrl+C.
- The installed command serves `/` and `/api/database`, and exits 0 on Ctrl+C with no traceback.
- **Not yet:** Ctrl+C does not press Stop, so Ctrl+C during a long query waits for the query.
  Stage 03 adds that by giving `serve` a third argument, `runner`. The in-process `fake_serve`
  above already accepts extra arguments.
- Spec §9 documents the port, the localhost-only host, the browser, and `--no-browser`.
  `.env.example` documents `ASKCROSSROADS_PORT`.
- Default `pytest` is green.

---

## Failure Modes & Rollback

- **A test hangs at `main`** → `serve` was not replaced (the `served` fixture is missing from that
  test), so a real server started. Every in-process `main` test must use `served`.
- **argparse error `unrecognized arguments` under pytest** → `main()` was called without a list. Pass
  `[]`.
- **`wait_until_serving` times out** → read the stderr it prints. Common causes: the console script
  is stale (`.venv/bin/pip install -e .`); uvicorn refused the passed socket (check that `listen`
  calls `listen()` before returning).
- **`stop()` returns a negative status such as `-2`** → the process died of the signal instead of
  handling it. `serve` must catch `KeyboardInterrupt`. Check that uvicorn's own signal handling ran:
  it only installs handlers on the main thread, which `main` is.
- **The taken-port test passes on one OS and not another** → `SO_REUSEADDR` semantics differ.
  macOS and Linux refuse a bind to an address another socket is listening on, which is what the
  test relies on. Windows is not a supported development platform for this test. Record any
  difference.
- **Rollback:** `git checkout -- src/askcrossroads/app.py src/askcrossroads/config.py .env.example
  docs/spec.md tests/test_config.py tests/test_package.py` and delete `tests/test_server.py`. This
  returns the repository to the end of stage 01.
