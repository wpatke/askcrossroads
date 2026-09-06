"""The SQL safety validator: the densest suite in the project (spec §11.3).

Every rejection gets its own explicitly named case. One test asserting "bad SQL is
rejected" would pass while nine of the ten rejections were broken, which is why spec §11.3
requires a case per rejection.

The positive cases carry equal weight. A validator that rejects everything is not safe, it
is broken, and only the positive cases prove the difference.
"""

import duckdb
import pytest

from askcrossroads.safety import Verdict, validate_sql


# --- The result type ---------------------------------------------------------------


def test_a_verdict_is_immutable():
    """Spec §4 passes verdicts around a repair loop; nothing may edit one in flight."""
    verdict = validate_sql("SELECT 1")
    with pytest.raises(Exception):
        verdict.allowed = False


def test_every_rejection_carries_a_reason_and_a_sentence():
    """The reason code is what the repair loop branches on; the detail is what is shown."""
    verdict = validate_sql("DROP TABLE t")
    assert verdict.allowed is False
    assert verdict.reason
    assert verdict.detail


def test_an_allowed_verdict_carries_no_reason():
    assert validate_sql("SELECT 1") == Verdict(allowed=True, reason="", detail="")


# --- Gate 1: the original text (spec §5) -------------------------------------------
# Every one of these parses to a statement type that is NOT select, except PRAGMA, which
# is the whole reason this gate reads the caller's string rather than the parsed one.


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE collisions",
        "DELETE FROM collisions",
        "UPDATE collisions SET speed_limit = 0",
        "INSERT INTO collisions (source_row_key) VALUES ('x')",
        "CREATE TABLE intruder (i INTEGER)",
        "ATTACH 'elsewhere.db' AS elsewhere",
        "COPY collisions TO 'out.csv'",
    ],
)
def test_a_statement_that_is_not_a_read_is_rejected(statement):
    verdict = validate_sql(statement)
    assert verdict.allowed is False
    assert verdict.reason == "not_a_read"


def test_pragma_is_rejected_even_though_it_parses_as_a_select():
    """The case spec §11.3 calls out by name.

    DuckDB rewrites `PRAGMA version` into a SELECT before reporting its type, and stores
    the rewritten text on the statement, so neither the parsed type nor the parsed text
    can tell the two apart. Only the caller's original string can. Some PRAGMA commands
    re-enable the external access database.py switched off, so this is a real hole.
    """
    verdict = validate_sql("PRAGMA version")
    assert verdict.allowed is False
    # The reason matters as much as the rejection: if this ever reads "not_a_select", the
    # gate order has been changed and the hole is open again for other PRAGMA commands.
    assert verdict.reason == "not_a_read"


def test_the_pragma_hole_is_real_and_this_gate_is_what_closes_it():
    """Proves the premise rather than trusting it.

    If this test ever fails, DuckDB has changed and the ordering rationale in the module
    comment needs revisiting -- but the gate is still correct, so nothing is urgent.
    """
    statement = duckdb.extract_statements("PRAGMA version")[0]
    assert statement.type == duckdb.StatementType.SELECT
    assert statement.query.lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize(
    "statement",
    [
        "SET enable_external_access = true",
        "INSTALL httpfs",
        "LOAD httpfs",
        "EXPORT DATABASE 'somewhere'",
        "CALL pragma_version()",
        "DESCRIBE SELECT 1",
        "EXPLAIN SELECT 1",
    ],
)
def test_other_non_read_statements_are_rejected(statement):
    """Not required by spec §11.3, but each is a real way to reach outside a SELECT.

    SET is the direct route to re-enabling external access; INSTALL and LOAD are how a
    query would reach for an extension carrying more file-reading functions.
    """
    assert validate_sql(statement).allowed is False


def test_a_leading_comment_is_rejected_on_purpose():
    """A deliberate false negative, documented in the plan overview.

    Skipping over leading comments would mean writing a comment scanner of our own, which
    is the string-matching gap spec §5 says to avoid. Fail-closed is the right side for a
    boundary; the prompt work is where SQL is kept free of preamble.
    """
    verdict = validate_sql("-- collisions by year\nSELECT 1")
    assert verdict.allowed is False
    assert verdict.reason == "not_a_read"


def test_leading_whitespace_is_fine():
    assert validate_sql("\n   SELECT 1").allowed is True


def test_lower_case_is_fine():
    assert validate_sql("select 1").allowed is True


# --- Gate 2: exactly one statement (spec §11.3) ------------------------------------


def test_multiple_statements_are_rejected():
    verdict = validate_sql("SELECT 1; SELECT 2")
    assert verdict.allowed is False
    assert verdict.reason == "multiple_statements"


def test_a_statement_smuggled_after_a_valid_select_is_rejected():
    """Spec §11.3 requires this case by name. Gate 1 passes it -- gate 2 is what stops it."""
    verdict = validate_sql("SELECT 1; DROP TABLE collisions")
    assert verdict.allowed is False
    assert verdict.reason == "multiple_statements"


def test_a_single_trailing_semicolon_is_still_one_statement():
    assert validate_sql("SELECT 1;").allowed is True


def test_comment_smuggling_is_resolved_by_the_parser_not_by_string_matching():
    """Spec §5 names this case.

    The semicolon and the DROP are inside a comment, so this is one harmless statement.
    A validator that searched the text for ";" or "DROP" would reject it wrongly -- which
    is the sort of false rejection that makes a boundary get relaxed later.
    """
    assert validate_sql("SELECT 1 -- ; DROP TABLE collisions").allowed is True


# --- Structural rejections: never a traceback --------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_empty_input_is_rejected(value):
    verdict = validate_sql(value)
    assert verdict.allowed is False
    assert verdict.reason == "empty"


def test_none_is_rejected_rather_than_crashing():
    assert validate_sql(None).allowed is False


def test_unparseable_sql_is_rejected_with_a_reason():
    """Passes gate 1 by its first word, then fails to parse."""
    verdict = validate_sql("SELECT FROM WHERE ((")
    assert verdict.allowed is False
    assert verdict.reason == "unparseable"


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "NOT SQL AT ALL !!", "SELECT FROM WHERE ((", "PRAGMA version",
     "SELECT 1; DROP TABLE t"],
)
def test_no_duckdb_error_ever_escapes(value):
    """Callers handle Verdicts. They must never have to know duckdb's exception types."""
    try:
        validate_sql(value)
    except duckdb.Error as error:
        pytest.fail(f"a raw duckdb error escaped validate_sql: {error!r}")


# --- The positive cases (spec §11.3) -----------------------------------------------
# Required, not optional. A validator that rejects everything is broken, and only these
# tests tell the difference.


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "SELECT * FROM collisions",
        "SELECT collision_severity, COUNT(*) FROM collisions GROUP BY 1 ORDER BY 1",
        "WITH counted AS (SELECT COUNT(*) AS n FROM collisions) SELECT n FROM counted",
        "SELECT * FROM collisions WHERE source_row_key IN (SELECT source_row_key FROM vehicles)",
        "SELECT a.source_row_key FROM collisions a JOIN vehicles b USING (accident_index)",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT date_trunc('month', datetime_local) AS m FROM collisions ORDER BY m",
    ],
)
def test_an_ordinary_read_is_allowed(query):
    verdict = validate_sql(query)
    assert verdict.allowed is True, f"wrongly rejected ({verdict.reason}): {verdict.detail}"
