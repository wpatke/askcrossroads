"""The package installs, imports, and exposes a working console script."""

import importlib
import subprocess
import sysconfig
from pathlib import Path

import pytest

import askcrossroads

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
    """The installed `askcrossroads` command resolves and exits cleanly.

    The script lives in the environment's scripts directory, which is inside the
    virtual environment when the tests are run from one.
    """
    script = Path(sysconfig.get_path("scripts")) / "askcrossroads"
    assert script.exists(), f"console script not found at {script}; is the package installed?"
    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ""
