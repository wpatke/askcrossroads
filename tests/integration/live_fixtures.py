"""Shared fixtures for the marked integration suite.

Each one skips, never fails, when the API key or the real build is absent (spec §11.2).

This is deliberately NOT named conftest.py. The offline tests import their test double with
`from conftest import FakeModel`, and a second module called `conftest` here would shadow
tests/conftest.py and break that import. Test files import these fixtures by name instead.
"""

import os
from pathlib import Path

import pytest

from askcrossroads import config, llm
from askcrossroads.database import Database, DatabaseError

ENV_TEST_DB = "ASKCROSSROADS_TEST_DB"


@pytest.fixture(scope="module")
def settings():
    """The API key, from the environment or .env, paired with the test build — or a skip.

    Read through config.load_config so the key is handled exactly as the application
    handles it: never printed, never written anywhere.
    """
    raw = (os.environ.get(ENV_TEST_DB) or "").strip()
    if not raw:
        pytest.skip(f"{ENV_TEST_DB} is not set; no real build to test against")
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"{ENV_TEST_DB} is {path}, which is not a file")
    try:
        return config.load_config(
            environ={**os.environ, config.ENV_DATABASE: str(path)}
        )
    except config.ConfigError:
        # The path was checked above, so the key is what is missing. The ConfigError's own
        # message is deliberately not relayed: a skip reason is no place to risk echoing
        # anything key-shaped.
        pytest.skip(f"{config.ENV_API_KEY} is not set in the environment or .env")


@pytest.fixture(scope="module")
def database(settings):
    try:
        db = Database.open(settings.database_path)
    except DatabaseError:
        pytest.skip(f"{ENV_TEST_DB} is not a readable Crossroads-UK build")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def client(settings):
    return llm.AnthropicClient.from_config(settings)
