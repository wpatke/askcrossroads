"""SQL safety validator: the boundary model-written SQL must cross (spec §5, §11.3).

AskCrossroads executes queries the user did not write, against the user's own data, with
no human checkpoint before execution (spec §4). This module is what makes that
defensible: given one string of SQL, it answers whether that string may reach the
database, and if not, why.

One of the six layers in spec §5 lives here: the single-SELECT parse check, which rejects
anything that is not exactly one read statement. Spec §5 also describes a second layer
here, a parse-tree walk rejecting file- and network-reading functions -- that layer is
DELIBERATELY NOT IMPLEMENTED in this module. The connection this application opens is
created with `enable_external_access=False` (database.py), which already refuses a
function like `read_csv_auto` at the moment it would run, regardless of whether anything
checked for it beforehand -- verified directly against the engine. A query calling such a
function is a well-formed single SELECT and passes every check in this module; it is
refused only when it actually executes. See docs/plans/003_safety_validator/00-overview.md
for the full reasoning.

This module imports nothing from the rest of the project, and opens no database connection
of its own -- duckdb.extract_statements is a plain function. It takes a string and returns
a verdict, and knows nothing about which database the string might run against.
"""

import re
from dataclasses import dataclass

import duckdb

# A statement must LOOK like a read before the parser is consulted at all.
#
# This is not "validation by regular expression" -- gates 2 and 3 below both run through
# DuckDB's own parser regardless of what this matches. It is a first-word check that only
# ever narrows what reaches the parser, and it exists because the parser alone cannot
# close one specific hole: DuckDB rewrites `PRAGMA version` into `SELECT * FROM
# pragma_version()` before reporting its type, AND stores that rewritten text on the
# statement. So neither the parsed type nor the parsed text can tell a PRAGMA from a
# SELECT -- only the caller's original string can. Some PRAGMA commands change database
# settings, including re-enabling the external access database.py switched off, which is
# why this matters rather than merely being untidy.
#
# Only leading whitespace is skipped. A leading comment is rejected on purpose: skipping
# over comments here would mean writing a comment scanner of our own, which is exactly the
# string-matching gap spec §5 says to avoid.
_STARTS_LIKE_A_READ = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """Whether one SQL string may be executed, and if not, why.

    `reason` is a short stable code, for branching and for tests. `detail` is the sentence
    a person reads. Both are empty when the verdict is to allow.

    Spec §4 re-runs validation on every repair attempt and spec §4.5 shows the user the
    attempt history, so a rejection is an ordinary outcome on a normal path. That is why
    this is a returned value rather than a raised exception.
    """

    allowed: bool
    reason: str = ""
    detail: str = ""


# The single allowed verdict. Frozen, so one shared instance is safe.
ALLOWED = Verdict(allowed=True)


def _rejected(reason, detail):
    """A rejection carrying both a stable code and a sentence for the user."""
    return Verdict(allowed=False, reason=reason, detail=detail)


def validate_sql(sql):
    """Decide whether `sql` may be executed against the user's database.

    Returns a Verdict. Never raises: every way DuckDB's parser can fail is turned into a
    rejection, because a traceback reaching the user is a defect (spec §1).

    The checks run in a fixed order, cheapest and most decisive first:

    1. the caller's original text must begin with SELECT or WITH;
    2. it must be exactly one statement;
    3. that statement's parsed type must be SELECT.

    This does NOT check for file- or network-reading functions (read_csv_auto, glob, and
    similar). That is a deliberate decision, not a gap yet to be filled: the connection
    this application opens has enable_external_access=False (database.py), which already
    refuses such a function at execution time. A query calling one is a well-formed single
    SELECT and passes every check here.
    """
    if not isinstance(sql, str) or not sql.strip():
        return _rejected("empty", "No SQL was supplied.")

    # Gate 1: the ORIGINAL text. See the note on _STARTS_LIKE_A_READ above -- reading this
    # from the parsed statement instead would let a PRAGMA through.
    if not _STARTS_LIKE_A_READ.match(sql):
        return _rejected(
            "not_a_read",
            "Only queries that begin with SELECT or WITH may be run. "
            "This one does not.",
        )

    # Gate 2 and 3 go through DuckDB's own parser, so there is no gap between what the
    # validator understood and what the engine would run.
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        # The parser could not read it at all. That is a rejection with a reason, never a
        # traceback: callers of this module handle Verdicts, not duckdb exceptions.
        return _rejected("unparseable", f"This is not valid SQL: {error}")

    if len(statements) != 1:
        return _rejected(
            "multiple_statements",
            f"Only one statement may be run at a time; this is {len(statements)}.",
        )

    if statements[0].type != duckdb.StatementType.SELECT:
        # Gate 1 already rejects everything that reaches here by its text, so this gate is
        # a backstop rather than the primary check. It stays because the two gates fail
        # for different reasons, and a change to one must not silently disarm the other.
        return _rejected(
            "not_a_select",
            f"Only SELECT statements may be run; this is a "
            f"{statements[0].type.name} statement.",
        )

    return ALLOWED
