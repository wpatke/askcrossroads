"""The package installs, imports, and exposes a working console script."""

import importlib
import os
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


def test_console_script_runs(tmp_path):
    """The installed `askcrossroads` command resolves and exits cleanly when configured.

    The script lives in the environment's scripts directory, which is inside the
    virtual environment when the tests are run from one. A database path and an API
    key are supplied explicitly (never the developer's own ambient environment or a
    real credential) so this test proves the script is wired up correctly rather than
    depending on whatever happens to be configured on the machine it runs on.
    """
    script = Path(sysconfig.get_path("scripts")) / "askcrossroads"
    assert script.exists(), f"console script not found at {script}; is the package installed?"

    database = tmp_path / "crossroads.db"
    database.write_bytes(b"not a real database")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ANTHROPIC_API_KEY": "test-key-not-a-real-credential",
        "ASKCROSSROADS_DB": str(database),
    }
    result = subprocess.run(
        [str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ""
