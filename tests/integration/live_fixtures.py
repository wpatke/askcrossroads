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

# The one build that hand-worked expected numbers belong to: the answer set (test_answers.py)
# and test_empty_periods.py. Tests that use the `pinned` fixture skip on any other build,
# because checking these numbers against other data would compare them with figures from a
# different database (spec §11.2).
#
# To make this build yourself, install crossroads-uk 1.0.4 with its weather extra
# (pip install "crossroads-uk[weather]==1.0.4"; ERA5 also needs a free Copernicus CDS key),
# then run:
#
#   import crossroads as cr
#   client = cr.init_engine(database_path="crossroads_2023_complete.db")
#   client.build(datasets=["stats19", "era5_weather", "aadf", "bank_holidays"],
#                years=[2023], boundary_mode="snapshot")
#   client.close()
#
# The `crossroads` wizard gives the same build if given the same answers.
PINNED_VERSION = "1.0.4"
PINNED_SCHEMA = 4
PINNED_DATASETS = ["aadf", "bank_holidays", "era5_weather", "stats19"]
PINNED_YEARS = [2023]
PINNED_BOUNDARY_MODE = "snapshot"


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


@pytest.fixture(scope="module")
def pinned(database):
    """The configured build, if it is the one the expected numbers were worked out on."""
    provenance = database.context.provenance
    parameters = provenance.parameters
    built = (
        provenance.crossroads_version,
        provenance.schema_version,
        sorted(parameters.get("datasets") or []),
        parameters.get("years"),
        parameters.get("boundary_mode"),
    )
    wanted = (
        PINNED_VERSION,
        PINNED_SCHEMA,
        PINNED_DATASETS,
        PINNED_YEARS,
        PINNED_BOUNDARY_MODE,
    )
    if built != wanted:
        pytest.skip(
            "the expected numbers are for Crossroads-UK {} (schema {}, datasets {}, years {}, "
            "{} boundaries); this build is {}".format(*wanted, built)
        )
    return database
