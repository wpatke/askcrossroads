"""The installed `askcrossroads` command, started as a user would start it, for tests.

Shared by tests/test_server.py and tests/test_browser.py. It lives here rather than in either
test file because pytest test files should not import one another.
"""

import os
import signal
import socket
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import httpx2
import pytest

KEY = "test-key-not-a-real-credential"

# Nothing listens on port 9 (the "discard" port). A model call pointed here fails at once,
# offline. A test that does not need the model leaves the command pointed here, so even a
# bug could not reach the real API.
UNREACHABLE = "http://127.0.0.1:9"

# Installing the package creates the `askcrossroads` command in the environment's scripts
# directory. On Windows that command is an .exe file.
SCRIPT_NAME = "askcrossroads.exe" if sys.platform == "win32" else "askcrossroads"
SCRIPT = Path(sysconfig.get_path("scripts")) / SCRIPT_NAME


def base_environment():
    """The smallest environment the command needs to start, before a test adds its own values.

    Built from scratch rather than copied from os.environ, so nothing from the real machine
    (such as a real API key) leaks into a test. Windows also needs SYSTEMROOT, without which
    Python can fail to start.
    """
    environment = {"PATH": os.environ.get("PATH", "")}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environment

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="stops the server with a POSIX Ctrl+C signal"
)


def free_port():
    """A localhost port nothing is listening on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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
            **base_environment(),
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
