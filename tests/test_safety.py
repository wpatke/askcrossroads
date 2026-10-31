"""The SQL safety validator: the densest suite in the project (spec §11.3).

Every rejection gets its own explicitly named case. One test asserting "bad SQL is
rejected" would pass while nine of the ten rejections were broken, which is why spec §11.3
requires a case per rejection.

The positive cases carry equal weight. A validator that rejects everything is not safe, it
is broken, and only the positive cases prove the difference.
"""

import duckdb
import pytest

from askcrossroads.database import open_database
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
# Every one of these parses to a statement type that is NOT select. Gate 1 rejects them
# by their text before the parser is consulted; gate 3 would also reject them by type.


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


def test_pragma_is_rejected_by_its_text():
    """The case spec §11.3 calls out by name.

    Some PRAGMA commands re-enable the external access database.py switched off, so this
    must never get through. DuckDB before 2.0 reported `PRAGMA version` as a SELECT, which
    is why gate 1 reads the caller's original string rather than the parsed statement.
    """
    verdict = validate_sql("PRAGMA version")
    assert verdict.allowed is False
    # The reason matters as much as the rejection: if this ever reads "not_a_select", the
    # gate order has been changed and PRAGMA is now relying on the parser alone.
    assert verdict.reason == "not_a_read"


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


@pytest.mark.parametrize(
    "query",
    [
        "/* collisions by year */ SELECT 1",
        "/* multi\n   line */\nSELECT 1",
    ],
    ids=["on one line", "spanning lines"],
)
def test_a_leading_block_comment_is_rejected_on_purpose(query):
    """The /* */ counterpart of the case above, rejected for exactly the same reason.

    Gate 1 reads the caller's ORIGINAL text and skips only whitespace, so any comment
    before the first keyword hides that keyword from it -- whether the comment is a --
    line or a /* block, and whether or not it spans lines. The model is asked for bare
    SQL, so this shape should not arrive; if it ever does, failing closed is the right
    side of the line for a boundary.
    """
    verdict = validate_sql(query)
    assert verdict.allowed is False, f"a leading block comment must not pass: {query!r}"
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


@pytest.mark.parametrize(
    "query",
    [
        "SELECT /* inline note */ 1",
        "SELECT 1 /* ; DROP TABLE collisions */",
        "WITH /* note */ a AS (SELECT 1 AS n) SELECT n FROM a",
    ],
    ids=["inline", "hiding a semicolon and a DROP", "inside a CTE"],
)
def test_a_block_comment_after_the_first_keyword_is_fine(query):
    """The /* */ counterpart of the smuggling case above.

    Once gate 1 has seen SELECT or WITH, comments are the parser's business rather than
    ours. DuckDB reads the second case as one harmless statement, because the semicolon
    and the DROP are inside the comment -- a validator that searched the raw text for ";"
    or "DROP" would reject it wrongly, which is the sort of false rejection that gets a
    boundary relaxed later.
    """
    verdict = validate_sql(query)
    assert verdict.allowed is True, f"wrongly rejected ({verdict.reason}): {verdict.detail}"


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


# =====================================================================================
# The spec §11.3 matrix
# =====================================================================================
# One case per rejection the specification names AND this module implements, written in
# the specification's own order, so the suite can be read against §11.3 line by line.
# This deliberately overlaps the check-by-check tests above: those prove the mechanism,
# this proves the contract.
#
# Spec §11.3 also names "any file-reading function" as a required rejection. That check
# is deliberately not built here (see docs/plans/003_safety_validator/00-overview.md), so
# it is not restated as a rejection below. It reappears further down this file as an
# ACCEPTANCE, paired with proof that the database connection refuses it anyway.


SPEC_11_3_REJECTIONS = [
    ("DROP", "DROP TABLE collisions"),
    ("DELETE", "DELETE FROM collisions"),
    ("UPDATE", "UPDATE collisions SET speed_limit = 0"),
    ("INSERT", "INSERT INTO collisions (source_row_key) VALUES ('x')"),
    ("CREATE", "CREATE TABLE intruder (i INTEGER)"),
    ("ATTACH", "ATTACH 'elsewhere.db' AS elsewhere"),
    ("PRAGMA", "PRAGMA version"),
    ("COPY", "COPY collisions TO 'out.csv'"),
    ("multiple statements", "SELECT 1; SELECT 2"),
    ("statement smuggled after a SELECT", "SELECT 1; DROP TABLE collisions"),
]


@pytest.mark.parametrize(
    "query",
    [query for _, query in SPEC_11_3_REJECTIONS],
    ids=[name for name, _ in SPEC_11_3_REJECTIONS],
)
def test_spec_11_3_rejection(query):
    """Every rejection spec §11.3 requires that this module implements, each its own case."""
    verdict = validate_sql(query)
    assert verdict.allowed is False, f"spec §11.3 requires this to be rejected: {query}"
    assert verdict.reason, "a rejection must say why"
    assert verdict.detail, "a rejection must carry a sentence for the user"


SPEC_11_3_ACCEPTIONS = [
    ("ordinary SELECT", "SELECT collision_severity FROM collisions"),
    ("SELECT with aggregate and ORDER BY",
     "SELECT collision_severity, COUNT(*) AS n FROM collisions "
     "GROUP BY collision_severity ORDER BY collision_severity"),
    ("CTE",
     "WITH counted AS (SELECT COUNT(*) AS n FROM collisions) SELECT n FROM counted"),
    ("nested CTE",
     "WITH a AS (SELECT * FROM collisions), b AS (SELECT * FROM a) SELECT COUNT(*) FROM b"),
    ("subquery in WHERE",
     "SELECT * FROM collisions WHERE accident_index IN "
     "(SELECT accident_index FROM vehicles)"),
    ("scalar subquery in the select list",
     "SELECT (SELECT COUNT(*) FROM vehicles) AS vehicles FROM collisions"),
    ("derived table", "SELECT n FROM (SELECT COUNT(*) AS n FROM collisions) t"),
    ("join",
     "SELECT c.source_row_key FROM collisions c "
     "JOIN vehicles v ON c.accident_index = v.accident_index"),
    ("union", "SELECT 1 AS n UNION ALL SELECT 2 AS n"),
]


@pytest.mark.parametrize(
    "query",
    [query for _, query in SPEC_11_3_ACCEPTIONS],
    ids=[name for name, _ in SPEC_11_3_ACCEPTIONS],
)
def test_spec_11_3_acception(query):
    """The half that proves the validator is safe rather than merely broken.

    Spec §11.3: "A validator that rejects everything is not safe, it is broken, and only
    positive cases prove the difference."
    """
    verdict = validate_sql(query)
    assert verdict.allowed is True, (
        f"spec §11.3 requires this to pass, but it was rejected "
        f"({verdict.reason}): {verdict.detail}"
    )


# =====================================================================================
# The validator and the engine agree
# =====================================================================================
# Everything above asks the validator what it thinks. These tests check that against the
# database itself, so a verdict is never merely internally consistent.


@pytest.fixture
def con(crossroads_db):
    """A read-only connection to a valid synthetic build, opened exactly as the
    application opens the user's file: read_only=True, external access off."""
    connection = open_database(crossroads_db)
    yield connection
    connection.close()


@pytest.mark.parametrize(
    "query",
    [query for _, query in SPEC_11_3_ACCEPTIONS],
    ids=[name for name, _ in SPEC_11_3_ACCEPTIONS],
)
def test_an_accepted_query_actually_runs(con, query):
    """Accepting a query the database cannot run would make the positive cases hollow.

    Note what this does NOT do: it does not assert anything about the rows. The database
    is a fixture, not a subject. All that matters here is that the validator's "yes"
    corresponds to something the engine will accept.
    """
    assert validate_sql(query).allowed is True
    con.execute(query).fetchall()


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE collisions",
        "DELETE FROM collisions",
        "UPDATE collisions SET speed_limit = 0",
        "INSERT INTO collisions (source_row_key) VALUES ('x')",
        "CREATE TABLE intruder (i INTEGER)",
    ],
)
def test_the_engine_also_refuses_every_write_the_validator_rejects(con, statement):
    """Spec §11.4 requires a write to fail as if the validator had been bypassed entirely.

    tests/test_database.py already proves the engine refuses these. This restates it from
    the validator's side, so the two guarantees are visibly the same guarantee rather than
    two facts that happen to both be true today.
    """
    assert validate_sql(statement).allowed is False
    with pytest.raises(duckdb.Error):
        con.execute(statement)


# =====================================================================================
# The decision this validator rests on: file-reading functions pass HERE, and are
# refused by the CONNECTION instead (see docs/plans/003_safety_validator/00-overview.md)
# =====================================================================================
# safety.py deliberately does not check for read_csv_auto, glob, and similar. The
# database connection this application opens (database.py, from 002_database_layer) is
# created with enable_external_access=False, which refuses these at the moment they would
# run. Both halves of that claim are asserted together below, so a change to EITHER side
# -- this validator becoming stricter, or the connection settings being loosened -- is
# caught by this same test rather than by two unrelated ones drifting apart.
#
# Verified directly, not assumed: each of the four calls below was run against a real
# connection with these settings and raised duckdb.PermissionException with the message
# "file system operations are disabled by configuration".


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM read_csv_auto('/etc/hosts')",
        "SELECT * FROM read_parquet('somewhere.parquet')",
        "SELECT * FROM glob('/*')",
        "SELECT * FROM read_text('/etc/hosts')",
    ],
)
def test_file_reading_queries_pass_validation_but_the_connection_refuses_them(con, query):
    """The explicit proof behind the decision not to build a parse-tree function check.

    If this test's first assertion ever starts failing, someone has added a function
    check to safety.py -- that is fine, but the overview and this stage's docstring
    should be updated to say so rather than left describing a decision that no longer
    holds.

    If the SECOND assertion ever starts failing -- the connection allows the read -- that
    is a serious finding. It means the one layer this design relies on for file- and
    network-reading functions has gone silent, and nothing else in this module would
    catch it. Report it prominently; do not add a function check as a quiet workaround
    without saying why the decision changed.
    """
    verdict = validate_sql(query)
    assert verdict.allowed is True, (
        "this validator does not check for file-reading functions by design (see "
        "00-overview.md) -- if it now rejects this, the design note is out of date"
    )
    with pytest.raises(duckdb.Error):
        con.execute(query)


# =====================================================================================
# A guard on the engine behaviour this module depends on
# =====================================================================================
# safety.py's module comment says DuckDB 2.0 reports PRAGMA as its own statement type, so
# check 3 rejects it independently of check 1. That is not documented as a stable
# contract, and a DuckDB upgrade could change it without anything else in this project
# noticing.
#
# A failure here is not a defect in safety.py -- check 1 still rejects PRAGMA by its text
# either way. It means PRAGMA is back to relying on check 1 alone, and the reasoning in
# safety.py's module comment needs rewriting.


def test_duckdb_reports_pragma_as_its_own_statement_type():
    """Check 3 is a second, independent barrier against PRAGMA only because of this."""
    statement = duckdb.extract_statements("PRAGMA version")[0]
    assert statement.type == duckdb.StatementType.PRAGMA, (
        "DuckDB no longer reports PRAGMA as a PRAGMA statement -- the rationale in "
        "safety.py's module comment needs revisiting"
    )
