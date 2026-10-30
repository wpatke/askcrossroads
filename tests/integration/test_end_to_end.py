"""A real question through the whole engine: real API, real build (spec §11.2).

Asserts on shape and execution, never on wording. The model is not deterministic (spec §8),
so one failure here is a signal about the prompt rather than proof of a bug; a repeated
failure is a bug.

Skips, never fails, without an API key or a real build. Each run costs a few model calls
against the runner's own key.
"""

import pytest

from askcrossroads import config, query
from askcrossroads.safety import validate_sql

from live_fixtures import client, database, settings  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.integration

# Phrased so that any Crossroads-UK build can answer it: every build has dated records. It
# names no table or column (spec §2C).
GROUPED_QUESTION = "How many records are there for each year this database covers?"

# Nothing in any road-safety build can answer this.
ABSENT_QUESTION = "What was the average price of a loaf of bread in each region last year?"


@pytest.fixture(scope="module")
def runner(database, client):
    return query.QueryRunner(database, client)


def test_a_real_question_is_answered_with_a_table_and_a_chart(runner):
    answer = runner.ask(GROUPED_QUESTION)

    assert answer.status == query.ANSWERED, (answer.error, answer.explanation, answer.sql_attempts)
    assert answer.rows, "an answerable question returned no rows"
    assert validate_sql(answer.sql).allowed
    assert answer.sql_attempts[-1] == query.Attempt(sql=answer.sql, error=None)
    assert len(answer.sql_attempts) <= config.MAX_ATTEMPTS

    # The chart: a Vega-Lite spec with a mark and an encoding and no data (spec §7).
    assert answer.chart is not None, answer.chart_attempts
    assert "mark" in answer.chart
    assert isinstance(answer.chart["encoding"], dict)
    assert "data" not in answer.chart
    assert len(answer.chart_attempts) <= config.MAX_ATTEMPTS

    # Every field the chart encodes must be a real result column. That is the point of asking
    # after execution (spec §7). Skipped when the spec derives fields of its own.
    if "transform" not in answer.chart:
        names = {name for name, _ in answer.columns}
        for channel in answer.chart["encoding"].values():
            if isinstance(channel, dict) and "field" in channel:
                assert channel["field"] in names, (channel, names)


def test_an_absent_question_is_declined_without_running_sql(runner):
    answer = runner.ask(ABSENT_QUESTION)

    assert answer.status == query.DECLINED, answer.sql_attempts
    assert answer.explanation
    assert answer.sql is None
    assert answer.chart is None
