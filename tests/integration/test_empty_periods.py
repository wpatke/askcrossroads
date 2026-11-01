"""A month with nothing in it comes back as 0, from the real model on a real build (spec §11.2).

The SQL instructions ask for a row for every time period, including empty ones. Without
that, a month with no rows simply vanishes from the result, and a line chart joins its
neighbours straight across, turning a real zero into an invented value. Only the real model
writes the SQL, so only a real call can show whether it follows the instruction.

The question below was found by hand to have an empty month: in the pinned build, Wales had
no fatal collisions in February 2023. As in the answer set (spec §11.2), the expected numbers
were worked out by a person, in SQL, and that query is kept here so a disagreement can be
settled by reading it. The test skips on any other build, because these numbers belong to
this one.

A single failure is a signal about the prompt rather than proof of a bug: the model is not
deterministic (spec §8).
"""

import pytest

from askcrossroads.safety import validate_sql

from live_fixtures import client, database, settings  # noqa: F401  (pytest fixtures)

pytestmark = pytest.mark.integration

# The build these numbers were worked out on. To make it yourself, install crossroads-uk
# 1.0.4 with its weather extra (pip install "crossroads-uk[weather]==1.0.4"; ERA5 also needs
# a free Copernicus CDS key), then run:
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
PINNED_YEARS = [2023]

QUESTION = "How many fatal collisions happened each month in Wales?"

# Worked out by hand. ctyua_code is the council area a collision lies in, and every Welsh
# council's code starts with W; collision_severity 1 is Fatal in the codebook. The same
# twelve numbers come from the police's own highway-authority code, and from the four Welsh
# police forces.
HAND_SQL = """
SELECT EXTRACT(month FROM datetime_local) AS month, COUNT(*) AS fatal
FROM collisions
WHERE collision_severity = 1 AND ctyua_code LIKE 'W%'
GROUP BY month
ORDER BY month
"""

# January to December 2023. February is the empty month.
EXPECTED = [5, 0, 7, 8, 9, 9, 10, 8, 11, 6, 7, 10]


@pytest.fixture(scope="module")
def pinned(database):
    """The configured build, if it is the one the expected numbers were worked out on."""
    provenance = database.context.provenance
    built = (
        provenance.crossroads_version,
        provenance.schema_version,
        provenance.parameters.get("years"),
    )
    if built != (PINNED_VERSION, PINNED_SCHEMA, PINNED_YEARS):
        pytest.skip(
            f"the expected numbers are for Crossroads-UK {PINNED_VERSION}, schema "
            f"{PINNED_SCHEMA}, years {PINNED_YEARS}; this build is {built}"
        )
    return database


def test_the_hand_written_answer_still_holds(pinned):
    """The expected numbers, re-derived from the build itself.

    Note that the plain GROUP BY gives February no row at all: exactly the gap the SQL
    instruction exists to close.
    """
    counts = dict(pinned.connection.execute(HAND_SQL).fetchall())
    assert 2 not in counts
    assert [counts.get(month, 0) for month in range(1, 13)] == EXPECTED


def test_an_empty_month_comes_back_as_zero(client, pinned):
    result = client.generate_sql(pinned.context, QUESTION)
    assert result.answerable is True, result.explanation
    assert validate_sql(result.sql).allowed, result.sql
    rows = pinned.connection.execute(result.sql).fetchall()

    # One row per month of the build's only year: none dropped, and none invented outside it.
    assert len(rows) == 12, (result.sql, rows)
    # The count is the last whole number in each row. A month number, if the query has one,
    # comes before it; a month given as a date or a name is not a whole number at all.
    counts = [
        [value for value in row if isinstance(value, int) and not isinstance(value, bool)][-1]
        for row in rows
    ]
    assert counts == EXPECTED, (result.sql, rows)
