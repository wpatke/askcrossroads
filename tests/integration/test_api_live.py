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
