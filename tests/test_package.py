"""The package installs, imports, and exposes a working console script."""

import importlib
import subprocess
from pathlib import Path

import pytest

import askcrossroads
from real_command import SCRIPT, base_environment

# The modules the repository blueprint (spec §12) requires. Listing them here means a
# module that is deleted or renamed fails a test rather than failing silently at runtime.
BLUEPRINT_MODULES = [
    "app",
    "config",
    "database",
    "introspection",
    "llm",
    "prompt",
    "query",
    "safety",
]


def test_package_imports():
    assert askcrossroads is not None


def test_version_is_a_string():
    assert isinstance(askcrossroads.__version__, str)
    assert askcrossroads.__version__ != ""


@pytest.mark.parametrize("name", BLUEPRINT_MODULES)
def test_blueprint_module_imports(name):
    assert importlib.import_module(f"askcrossroads.{name}") is not None


def test_static_directory_exists():
    static = Path(askcrossroads.__file__).parent / "static"
    assert static.is_dir()


def test_console_script_runs():
    """The installed `askcrossroads` command resolves and runs.

    The script lives in the environment's scripts directory, which is inside the virtual
    environment when the tests are run from one. `--help` exits before anything is loaded,
    so this needs no database and no key. Starting the real server is covered in
    tests/test_server.py.
    """
    assert SCRIPT.exists(), f"console script not found at {SCRIPT}; is the package installed?"
    result = subprocess.run(
        [str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=base_environment(),
    )
    assert result.returncode == 0, result.stderr
    assert "--no-browser" in result.stdout
