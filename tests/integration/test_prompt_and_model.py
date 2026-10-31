"""The prompt and model client against the live Anthropic API and a real build (spec §11.2).

The offline suite proves the prefix is stable and the request is shaped correctly. It
cannot prove the live API accepts the response schema, that a real question produces SQL
that runs, or that the prompt cache actually registers a read — "a silent failure that no
offline test can detect, and which quietly triples cost when it breaks". That is what this
file is for.

Marked `integration`: it needs an API key, a network, and a real Crossroads-UK build. Per
spec §11.2 it SKIPS — never fails — when any of those is absent.

Cost: a handful of real calls at roughly the size of the whole prefix each, less once the
cache is warm. It is never part of the default run.

Every assertion is on shape and execution, never on generated wording: the model is not
deterministic (spec §8).
"""

import pytest

from askcrossroads import config, llm, prompt
from askcrossroads.safety import validate_sql

from live_fixtures import client, database, settings  # noqa: F401  (pytest fixtures)

# Everything in this module needs a real build and a real key.
pytestmark = pytest.mark.integration

# Answerable by any valid build, and phrased so the test needs to know nothing about the
# schema (spec §2C binds tests too).
ANY_BUILD_QUESTION = "How many records does this database hold in total?"


def test_a_real_question_produces_executable_sql(client, database):
    """End to end, and the proof that the live API accepts SQL_RESPONSE_SCHEMA."""
    result = client.generate_sql(database.context, ANY_BUILD_QUESTION)

    assert result.answerable is True, result.explanation
    verdict = validate_sql(result.sql)
    assert verdict.allowed, verdict
    rows = database.connection.execute(result.sql).fetchall()
    assert len(rows) >= 1


def test_an_unanswerable_question_is_declined(client, database):
    """The decline path is real, not just plumbing (spec §11.2).

    The model is not deterministic (spec §8), so a single failure here is a signal about
    the prompt rather than proof of a bug — the same rule spec §11.2 sets for the answer
    set. Asserts on shape only, never on wording.
    """
    result = client.generate_sql(
        database.context,
        "What was the average price of a loaf of bread in each region last year?",
    )

    assert result.answerable is False, result.sql
    assert result.explanation


# A result the SQL has already counted: one row per category, with the count in `total`.
COUNTED_QUESTION = "How many in each category?"
COUNTED_SQL = "SELECT category, COUNT(*) AS total FROM t GROUP BY category ORDER BY category"
COUNTED_SHAPE = prompt.ResultShape(
    columns=(("category", "VARCHAR"), ("total", "BIGINT")),
    row_count=3,
    distinct_counts=(("category", 3),),
    sample_rows=(("Alpha", 10), ("Beta", 7), ("Gamma", 1)),
)


def _channels(encoding):
    """Every encoding channel of a spec. A channel may hold a list (several tooltips)."""
    for value in encoding.values():
        for channel in value if isinstance(value, list) else [value]:
            if isinstance(channel, dict):
                yield channel


def test_a_real_chart_call_returns_a_spec(client):
    """Spec §11.2: a chart call returning a spec that parses as Vega-Lite, with no data."""
    spec = client.describe_chart(COUNTED_QUESTION, COUNTED_SQL, COUNTED_SHAPE)

    # describe_chart already rejects a spec with data or without mark/encoding; these
    # restate the contract so a regression in the parser cannot hide one here.
    assert "data" not in spec
    assert "mark" in spec
    assert isinstance(spec["encoding"], dict)


def test_a_chart_of_counted_rows_does_not_count_them_again(client):
    """The SQL has already counted, so the chart must plot the count column as it is (spec §7).

    Counting the rows again would draw every bar at 1: a believable chart that contradicts
    the table beside it, which no offline test can catch, because only the real model writes
    the spec. Asserts on the spec's shape, never its wording. The model is not deterministic
    (spec §8), so a single failure is a signal about the chart prompt rather than proof of a
    bug.
    """
    spec = client.describe_chart(COUNTED_QUESTION, COUNTED_SQL, COUNTED_SHAPE)
    channels = list(_channels(spec["encoding"]))

    assert any(channel.get("field") == "total" for channel in channels), spec
    assert not any("aggregate" in channel for channel in channels), spec
    for step in spec.get("transform", []):
        assert "aggregate" not in step and "joinaggregate" not in step, spec


def test_the_second_call_registers_a_cache_read(client, database):
    """The one failure no offline test can detect (spec §11.2).

    Two DIFFERENT questions over the same prefix. If anything volatile has crept in before
    the breakpoint, the second call re-pays full price for the whole prefix and this fails.

    Goes through client._send deliberately: the public API does not expose usage, and
    testing through the real request path is the point.
    """
    output_config = {
        "format": {"type": "json_schema", "schema": llm.SQL_RESPONSE_SCHEMA}
    }
    usages = []
    for question in (
        "How many rows are in the largest table?",
        "What is the earliest date held anywhere in this database?",
    ):
        response = client._send(
            system=prompt.sql_system_blocks(database.context),
            messages=[{"role": "user", "content": question}],
            output_config=output_config,
        )
        usages.append(response.usage)

    assert (usages[1].cache_read_input_tokens or 0) > 0, usages[1]


def test_the_prefix_is_the_expected_size(client, database, capsys):
    """The prefix size recorded in spec §3, measured on whichever build is configured.

    Only a loose sanity range is asserted — a tight one would fail on a legitimately
    different build. The figure itself is printed for the person running the suite.
    """
    count = client._client.messages.count_tokens(
        model=config.MODEL,
        system=prompt.sql_system_blocks(database.context),
        messages=[{"role": "user", "content": "x"}],
    )

    with capsys.disabled():
        print(f"\n[prompt prefix] {count.input_tokens:,} input tokens")

    # Above the minimum cacheable prefix, and well inside the context window.
    assert 512 < count.input_tokens < 200_000
