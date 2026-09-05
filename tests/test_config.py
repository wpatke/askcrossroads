"""Configuration constants, loading, and the guarantee that the key never leaks."""

import os
import re
from pathlib import Path

import pytest

from askcrossroads import config
from askcrossroads.app import main
from askcrossroads.config import Config, ConfigError, load_config

# Not a credential, and shaped so it can never be mistaken for one.
FAKE_KEY = "test-key-not-a-real-credential"

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_file(tmp_path):
    """A real file on disk to stand in for a database path.

    load_config only checks that the path exists and is readable, so the contents are
    irrelevant here.
    """
    path = tmp_path / "crossroads.db"
    path.write_bytes(b"not a real database")
    return path


# --- The bounds (spec §10) --------------------------------------------------------


def test_bounds_have_their_specified_values():
    """Each bound is checked by value, so a silent change fails a test."""
    assert config.MODEL == "claude-opus-5"
    assert config.MAX_TOKENS == 16_000
    assert config.MAX_ATTEMPTS == 3
    assert config.MAX_RESULT_ROWS == 5_000


def test_bind_target_is_localhost_only():
    assert config.HOST == "127.0.0.1"
    assert isinstance(config.PORT, int)


def test_environment_variable_names():
    assert config.ENV_API_KEY == "ANTHROPIC_API_KEY"
    assert config.ENV_DATABASE == "ASKCROSSROADS_DB"


# --- Loading ----------------------------------------------------------------------


def test_loads_from_the_environment(db_file):
    result = load_config(
        env_file=None,
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert isinstance(result, Config)
    assert result.api_key == FAKE_KEY
    assert result.database_path == db_file


def test_loads_from_an_env_file(tmp_path, db_file):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ANTHROPIC_API_KEY={FAKE_KEY}\nASKCROSSROADS_DB={db_file}\n"
    )
    result = load_config(env_file=env_file, environ={})
    assert result.api_key == FAKE_KEY
    assert result.database_path == db_file


def test_the_environment_beats_the_env_file(tmp_path, db_file):
    """A variable exported in the shell must win over a stale .env entry."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ANTHROPIC_API_KEY=from-the-file\nASKCROSSROADS_DB={db_file}\n"
    )
    result = load_config(
        env_file=env_file,
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert result.api_key == FAKE_KEY


def test_missing_env_file_is_not_an_error(db_file):
    result = load_config(
        env_file="/no/such/.env",
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert result.api_key == FAKE_KEY


def test_loading_does_not_put_the_key_into_the_process_environment(tmp_path, db_file):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ANTHROPIC_API_KEY={FAKE_KEY}\nASKCROSSROADS_DB={db_file}\n"
    )
    load_config(env_file=env_file, environ={})
    assert os.environ.get("ANTHROPIC_API_KEY") != FAKE_KEY


# --- Refusals ---------------------------------------------------------------------


def test_missing_key_is_reported_by_name(db_file):
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={"ASKCROSSROADS_DB": str(db_file)})
    assert "ANTHROPIC_API_KEY" in str(raised.value)


def test_missing_database_is_reported_by_name():
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={"ANTHROPIC_API_KEY": FAKE_KEY})
    assert "ASKCROSSROADS_DB" in str(raised.value)


def test_every_problem_is_reported_at_once():
    """One run should tell the user everything that is wrong, not the first thing."""
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={})
    message = str(raised.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "ASKCROSSROADS_DB" in message


def test_a_database_path_that_does_not_exist_is_refused(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(ConfigError) as raised:
        load_config(
            env_file=None,
            environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(missing)},
        )
    assert str(missing) in str(raised.value)


def test_a_directory_is_not_a_database(tmp_path):
    with pytest.raises(ConfigError) as raised:
        load_config(
            env_file=None,
            environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(tmp_path)},
        )
    assert "not a file" in str(raised.value)


def test_the_refusal_points_at_crossroads_uk(tmp_path):
    """Refusal is not a dead end: it names the command that produces a valid file."""
    with pytest.raises(ConfigError) as raised:
        load_config(env_file=None, environ={})
    assert "crossroads" in str(raised.value)


# --- The key never leaks ----------------------------------------------------------


def test_repr_does_not_contain_the_key(db_file):
    result = load_config(
        env_file=None,
        environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(db_file)},
    )
    assert FAKE_KEY not in repr(result)
    assert FAKE_KEY not in str(result)
    # The path is not a secret and should still be visible.
    assert "crossroads.db" in repr(result)


def test_an_error_raised_while_the_key_is_set_does_not_contain_it(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(ConfigError) as raised:
        load_config(
            env_file=None,
            environ={"ANTHROPIC_API_KEY": FAKE_KEY, "ASKCROSSROADS_DB": str(missing)},
        )
    assert FAKE_KEY not in str(raised.value)


def test_env_example_holds_placeholders_only():
    text = (REPO_ROOT / ".env.example").read_text()
    assert "ANTHROPIC_API_KEY=your-key-here" in text
    assert "ASKCROSSROADS_DB=" in text
    # Nothing shaped like a real Anthropic key.
    assert not re.search(r"sk-ant-\S", text)


# --- The console entry point ------------------------------------------------------


def test_main_exits_non_zero_when_not_configured(tmp_path, monkeypatch, capsys):
    """No key, no path, and no .env to fall back on."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ASKCROSSROADS_DB", raising=False)
    assert main() == 1
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err
    assert "ASKCROSSROADS_DB" in captured.err


def test_main_exits_zero_when_configured(tmp_path, monkeypatch, capsys, crossroads_db):
    database = crossroads_db
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ASKCROSSROADS_DB", str(database))
    assert main() == 0
    captured = capsys.readouterr()
    assert str(database) in captured.out
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err
