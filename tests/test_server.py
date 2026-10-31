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
import threading
import time
import webbrowser
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from askcrossroads import app, config

from provider_stub import ProviderStub

KEY = "test-key-not-a-real-credential"

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

BUSY = "A question is already being answered. Wait for it, or press Stop."

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


def test_main_serves_on_localhost_after_validating(
    crossroads_db, configured, served, browser, capsys
):
    port = configured(crossroads_db)
    assert app.main(["--no-browser"]) == 0

    assert len(served) == 1
    assert served[0]["address"] == ("127.0.0.1", port)
    summary = served[0]["database"]
    assert summary.status_code == 200
    assert summary.json()["provenance"]["crossroads_version"] == "0.9.0"

    captured = capsys.readouterr()
    assert f"AskCrossroads is running at http://127.0.0.1:{port}/" in captured.out
    leaked = KEY in captured.out + captured.err
    assert not leaked, "the API key was printed"
    # The socket was closed when the server stopped.
    assert port_is_free(port)


def test_main_opens_the_browser_at_the_printed_address(
    crossroads_db, configured, served, browser, capsys
):
    port = configured(crossroads_db)
    assert app.main([]) == 0
    url = f"http://127.0.0.1:{port}/"
    assert browser == [url]
    assert url in capsys.readouterr().out


def test_no_browser_opens_nothing(crossroads_db, configured, served, browser):
    configured(crossroads_db)
    assert app.main(["--no-browser"]) == 0
    assert browser == []


def test_a_refused_file_never_opens_a_port(
    crossroads_db_factory, configured, served, browser, capsys
):
    port = configured(crossroads_db_factory(convention="missing"))
    assert app.main([]) == 1
    assert served == []
    assert browser == []
    captured = capsys.readouterr()
    assert REFUSAL in captured.err
    assert "pip install crossroads-uk" in captured.err
    assert "running at" not in captured.out
    assert port_is_free(port)


def test_a_taken_port_is_reported_by_name(
    crossroads_db, configured, served, browser, monkeypatch, capsys
):
    configured(crossroads_db)
    occupier = socket.socket()
    try:
        occupier.bind(("127.0.0.1", 0))
        occupier.listen()
        port = occupier.getsockname()[1]
        monkeypatch.setenv("ASKCROSSROADS_PORT", str(port))

        assert app.main([]) == 1
        assert served == []
        assert browser == []
        err = capsys.readouterr().err
        assert "ASKCROSSROADS_PORT" in err
        assert str(port) in err
    finally:
        occupier.close()


def test_a_bad_port_setting_exits_before_opening_the_database(
    crossroads_db, configured, served, monkeypatch, capsys
):
    configured(crossroads_db)
    monkeypatch.setenv("ASKCROSSROADS_PORT", "not-a-port")
    assert app.main([]) == 1
    assert served == []
    captured = capsys.readouterr()
    assert "ASKCROSSROADS_PORT" in captured.err
    assert "Database:" not in captured.out


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


@posix_only
def test_the_real_command_serves_and_stops_cleanly(crossroads_db, real_command):
    command = real_command(crossroads_db)
    command.wait_until_serving()

    page = httpx2.get(command.url + "/", timeout=5)
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "AskCrossroads" in page.text

    summary = httpx2.get(command.url + "/api/database", timeout=5).json()
    assert summary["provenance"]["crossroads_version"] == "0.9.0"

    assert command.stop() == 0
    assert "Traceback" not in command.stderr()
    # The terminal confirms which database was loaded and what built it.
    assert str(crossroads_db) in command.stdout()
    assert "Built by Crossroads-UK 0.9.0" in command.stdout()
    assert "codebook rows" in command.stdout()
    assert f"AskCrossroads is running at {command.url}/" in command.stdout()
    leaked = KEY in command.stdout() + command.stderr()
    assert not leaked, "the API key reached the terminal"


def test_the_real_command_refuses_a_non_crossroads_file(crossroads_db_factory, real_command):
    command = real_command(crossroads_db_factory(convention="missing"))
    assert command.process.wait(timeout=60) != 0
    assert REFUSAL in command.stderr()
    assert "pip install crossroads-uk" in command.stderr()
    assert "running at" not in command.stdout()
    assert port_is_free(command.port)


# --- The real command, end to end -------------------------------------------------------
# The real command is pointed at a local stand-in for the Anthropic API, so these run the
# real AnthropicClient over real HTTP, offline, with only the model's words scripted.


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


def post_question(command, question=QUESTION):
    return httpx2.post(command.url + "/api/ask", json={"question": question}, timeout=60)


@posix_only
def test_a_question_through_the_real_command(crossroads_db, real_command, provider):
    provider.reply_sql(GOOD_SQL)
    provider.reply_chart(CHART)
    command = real_command(crossroads_db, provider_url=provider.url)
    command.wait_until_serving()

    response = post_question(command)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["sql"] == GOOD_SQL
    assert body["rows"][0][0] == "Fine no high winds"
    assert body["chart"] == CHART

    assert len(provider.requests) == 2
    # The key reaches the provider, and that is the only place it goes.
    assert all(request["api_key"] == KEY for request in provider.requests)
    assert all(request["body"]["model"] == config.MODEL for request in provider.requests)
    # The SQL call asks for structured output; the chart call does not.
    assert "output_config" in provider.requests[0]["body"]
    assert "output_config" not in provider.requests[1]["body"]

    assert command.stop() == 0


@posix_only
def test_stop_over_http_while_a_query_runs(crossroads_db, real_command, provider):
    provider.reply_sql(SLOW_SQL)
    command = real_command(crossroads_db, provider_url=provider.url)
    command.wait_until_serving()

    worker, outcome = ask_in_background(command.url)
    assert provider.wait_for_requests(1)
    time.sleep(0.5)  # let the slow query get under way

    # The server is not blocked: a running question ties up a worker thread, not the server.
    started = time.monotonic()
    summary = httpx2.get(command.url + "/api/database", timeout=5)
    assert summary.status_code == 200
    assert time.monotonic() - started < 2

    # One question at a time (spec §5), and the rejected one never reaches the model.
    second = httpx2.post(command.url + "/api/ask", json={"question": "Another"}, timeout=5)
    assert second.status_code == 409
    assert second.json() == {"error": BUSY}
    assert len(provider.requests) == 1

    # Stop.
    assert httpx2.post(command.url + "/api/cancel", timeout=10).status_code == 204
    worker.join(timeout=15)
    assert not worker.is_alive(), "Stop did not end the running question"
    body = outcome["response"].json()
    assert body["status"] == "cancelled"
    assert body["sql_attempts"] == []

    # The connection survives for the next question.
    provider.reply_sql("SELECT 1 AS one")
    provider.reply_chart(CHART)
    assert post_question(command).json()["status"] == "answered"

    assert command.stop() == 0


@posix_only
def test_ctrl_c_during_a_running_query(crossroads_db, real_command, provider):
    provider.reply_sql(SLOW_SQL)
    command = real_command(crossroads_db, provider_url=provider.url)
    command.wait_until_serving()

    worker, outcome = ask_in_background(command.url)
    assert provider.wait_for_requests(1)
    time.sleep(0.5)  # let the slow query get under way

    started = time.monotonic()
    assert command.stop(timeout=20) == 0
    assert time.monotonic() - started < 15

    worker.join(timeout=10)
    assert outcome["response"].status_code == 200
    assert outcome["response"].json()["status"] == "cancelled"
    assert "Traceback" not in command.stderr()


@posix_only
def test_the_key_never_reaches_the_real_commands_output(crossroads_db, real_command, provider):
    provider.reply_error(401, echo_key=True)   # the SQL call fails authentication
    provider.reply_sql(GOOD_SQL)
    provider.reply_error(400, echo_key=True)   # the chart call fails
    command = real_command(crossroads_db, provider_url=provider.url)
    command.wait_until_serving()

    first = post_question(command)
    assert first.json()["status"] == "failed"
    assert REJECTED_KEY in first.json()["error"]

    second = post_question(command)
    body = second.json()
    assert body["status"] == "answered"
    assert body["rows"]
    assert body["chart"] is None
    assert body["chart_attempts"][0]["error"]

    summary = httpx2.get(command.url + "/api/database", timeout=5)

    # The stub really had the key to echo back.
    assert provider.requests[0]["api_key"] == KEY
    assert command.stop() == 0

    everything = first.text + second.text + summary.text + command.stdout() + command.stderr()
    leaked = KEY in everything
    assert not leaked, "the API key reached a response or the terminal"
