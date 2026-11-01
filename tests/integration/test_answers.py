"""The answer set: questions with known answers, checked as numbers (spec §11.2).

Everything else in the suite tests that the machinery works. This tests whether the answers
are right. Each question in answers.yaml is asked the ordinary way, through the same engine
the browser uses, and the numbers that come back are compared with numbers a person worked
out by hand.

The check is on the numbers, never on the SQL: two correct queries for one question can look
nothing alike. Because the model's column names and column order cannot be predicted, an
answer passes when it has one row per expected number and SOME column holds exactly those
numbers, in any order. That does not check which label each number sits beside, only that
the right numbers came back.

A failure is a signal, not proof of a bug. The model is not deterministic (spec §8), so one
wrong answer says something about the prompt rather than showing the code is broken. The
value is in watching the pass rate over time.

Skips, never fails, without an API key or the pinned build. A full run asks about fifteen
questions, each costing up to a few model calls against the runner's own key.
"""

import math
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from askcrossroads import query
from askcrossroads.safety import validate_sql

from live_fixtures import client, database, pinned, settings  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.integration

ANSWERS = yaml.safe_load((Path(__file__).parent / "answers.yaml").read_text())

# The shapes spec §11.2 says the set must include at least once each.
REQUIRED_SHAPES = {"coded_group", "three_table_join", "date_range", "no_rows", "declined"}

# How far a decimal answer may be from the expected one, so a rounded answer still counts.
DECIMAL_TOLERANCE = 0.01

DECLINED = [entry for entry in ANSWERS if entry["expected"] == "declined"]
ANSWERABLE = [entry for entry in ANSWERS if entry["expected"] != "declined"]


def is_number(value):
    # A bool is an int in Python, but True is not a count.
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def same_number(got, expected):
    """A whole number must match exactly; a decimal must be within DECIMAL_TOLERANCE."""
    if isinstance(expected, int):
        return got == expected
    return math.isclose(got, expected, rel_tol=DECIMAL_TOLERANCE)


def holds_expected_numbers(rows, expected):
    """True when the rows are one per expected number and some column holds exactly those."""
    if len(rows) != len(expected):
        return False
    if not rows:
        return True
    wanted = sorted(expected)
    # zip(*rows) turns the rows into columns.
    for column in zip(*rows):
        if not all(is_number(value) for value in column):
            continue
        got = sorted(float(value) for value in column)
        if all(same_number(g, w) for g, w in zip(got, wanted)):
            return True
    return False


@pytest.fixture(scope="module")
def runner(pinned, client):
    return query.QueryRunner(pinned, client)


def test_the_set_covers_every_required_shape():
    covered = {shape for entry in ANSWERS for shape in entry["shapes"]}
    assert REQUIRED_SHAPES <= covered, REQUIRED_SHAPES - covered


@pytest.mark.parametrize("entry", ANSWERABLE, ids=lambda entry: entry["question"])
def test_the_hand_written_answer_still_holds(pinned, entry):
    """The expected numbers, re-derived from the build itself. Makes no model call.

    Catches a typing mistake in answers.yaml, and a build that differs from the one the
    numbers were worked out on in a way the pin did not notice.
    """
    rows = pinned.connection.execute(entry["sql"]).fetchall()
    assert holds_expected_numbers(rows, entry["expected"]), rows


@pytest.mark.parametrize("entry", ANSWERABLE, ids=lambda entry: entry["question"])
def test_the_model_gets_the_right_numbers(runner, entry):
    answer = runner.ask(entry["question"])

    assert answer.status == query.ANSWERED, (answer.error, answer.explanation, answer.sql)
    assert validate_sql(answer.sql).allowed
    assert not answer.truncated
    assert holds_expected_numbers(answer.rows, entry["expected"]), (answer.sql, answer.rows)


@pytest.mark.parametrize("entry", DECLINED, ids=lambda entry: entry["question"])
def test_the_model_declines_what_this_build_cannot_answer(runner, entry):
    answer = runner.ask(entry["question"])

    assert answer.status == query.DECLINED, (answer.sql, answer.rows)
    # A decline runs no SQL at all.
    assert answer.sql is None
