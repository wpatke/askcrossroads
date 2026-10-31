"""Prompt assembly from discovered schema, codebook and provenance (spec §3, §6).

This module decides WHAT is said to the model: the deterministic, cacheable prompt prefix
built from the frozen DatabaseContext, the two system prompts, and the chart call's user
message. How it is sent, and what comes back, is llm.py's job. This module never imports
llm.py; the dependency runs one way only.

It takes an already-built DatabaseContext and never opens a connection: a prompt function
that queried the database would make the prefix a function of when it was built, not of
what the build contains.
"""

import json
import re
from dataclasses import dataclass

from askcrossroads import config, introspection

# What a NULL or an absent value renders as. One fixed string everywhere, so a missing
# dtype and a missing date range look the same to the model and neither renders as
# Python's `None`.
ABSENT = "(none)"


def _escape(value):
    """Render one field of a delimited row, on one line, with the delimiter escaped.

    No DfT label is known to contain a pipe or a newline, but nothing guarantees it, and one
    that did would silently corrupt every row after it. Backslash is escaped first, so the
    escaping is reversible.
    """
    if value is None:
        return ABSENT
    text = str(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    # Collapse any line break, so one row is always one line.
    return " ".join(text.split())


# A name that needs no quoting: the shape SQL accepts bare.
_PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(name):
    """Render a table or column name for the DDL description.

    Ordinary names are left bare so the SQL the model writes back stays readable — spec §3
    cares about that, because the user is shown the query. Anything unusual is quoted using
    introspection's own quoting, so a column called `order by` or `a"b` is still described
    correctly.
    """
    if name and _PLAIN_IDENTIFIER.match(name):
        return name
    return introspection.quote_identifier(name or "")


def _temporal_value(value):
    """Render one end of a date range.

    isoformat() where the value has one, so a DATE and a TIMESTAMP both render in a form the
    model can compare against a year in a question (spec §6).
    """
    if value is None:
        return ABSENT
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


# --- The five sections of the prefix -------------------------------------------------
# Each returns a string with no trailing newline. Each iterates the context's tuples IN THE
# ORDER THEY ARRIVE: introspection.py already sorts them with explicit keys, and sorting
# again here would hide the day that stopped being true.


def _build_section(provenance):
    """What produced this file (spec §2D).

    parameters is re-serialised with sorted keys rather than passed through as the raw
    string upstream wrote, so two builds that recorded the same parameters render the same
    bytes whatever whitespace upstream used.
    """
    parameters = provenance.parameters
    return "\n".join([
        "## Build",
        f"crossroads_version | {_escape(provenance.crossroads_version)}",
        f"schema_version | {_escape(provenance.schema_version)}",
        f"built_at_utc | {_temporal_value(provenance.built_at_utc)}",
        "parameters | "
        + (json.dumps(parameters, sort_keys=True) if parameters else ABSENT),
    ])


def _schema_section(relations):
    """Every table and view, with every column and its declared type (spec §2A).

    Written as DDL because a model reads CREATE TABLE natively. These statements describe;
    they are not meant to be executed, which is why a view is rendered as a column list with
    no SELECT behind it. Do not "fix" that by inventing a definition we do not have.
    """
    blocks = ["## Tables and views"]
    for relation in relations:
        keyword = "CREATE VIEW" if relation.is_view else "CREATE TABLE"
        name = f"{_identifier(relation.schema)}.{_identifier(relation.name)}"
        columns = ",\n".join(
            f"  {_identifier(column.name)} {column.data_type}"
            for column in relation.columns
        )
        blocks.append(f"{keyword} {name} (\n{columns}\n);")
    return "\n\n".join(blocks)


def _date_range_section(ranges):
    """The earliest and latest value of every date-typed column (spec §6).

    This is what lets the model say "this build contains 2023 only" instead of returning an
    empty table for a question about 2019.
    """
    lines = ["## Date ranges", "relation | column | earliest | latest"]
    for item in ranges:
        lines.append(
            f"{_escape(item.relation)} | {_escape(item.column)} | "
            f"{_temporal_value(item.earliest)} | {_temporal_value(item.latest)}"
        )
    return "\n".join(lines)


def _manifest_section(entries):
    """Every column_manifest row (spec §2B). `tbl` is the singular SOURCE name."""
    lines = ["## Column classifications", "source | column | kind | dtype"]
    for entry in entries:
        lines.append(
            f"{_escape(entry.tbl)} | {_escape(entry.col)} | "
            f"{_escape(entry.kind)} | {_escape(entry.dtype)}"
        )
    return "\n".join(lines)


def _codebook_section(entries):
    """The whole codebook, including is_missing (spec §3).

    is_missing matters and is not decoration: Crossroads-UK replaces those codes with NULL
    when it builds the silver tables, so a model that does not know a code was removed will
    write a filter on it and get nothing back.
    """
    lines = ["## Codebook", "variable | code | label | missing"]
    for entry in entries:
        lines.append(
            f"{_escape(entry.variable)} | {_escape(entry.code)} | "
            f"{_escape(entry.label)} | {'yes' if entry.is_missing else 'no'}"
        )
    return "\n".join(lines)


def serialise_context(context):
    """Render a DatabaseContext as the cacheable prompt prefix (spec §3).

    THE WHOLE PAYLOAD GOES IN. No relevance filtering, no selection heuristic. A filter
    scoring the question's words against column names and label text sounds like an obvious
    saving and fails silently on exactly the questions worth asking: a question about drunk
    drivers matches no column name and no label text, so the model would get no codes for
    the column it needs, guess, and return wrong rows instead of an error. The whole payload
    is a small fraction of the context window (spec §3 records the measured size).

    THE OUTPUT MUST BE BYTE-STABLE. For a given database this returns exactly the same
    characters every time, which is what makes the prompt cache work. Three rules keep it
    true, and every one of them is tested:

    - Nothing volatile appears anywhere. No timestamp, no question, no per-request
      identifier, and not the database path — the path is stable for one file but differs
      between machines holding the same build.
    - Rows are emitted in the order the context carries them. introspection.py sorts them in
      Python with explicit keys for this reason. Sorting again here would work and would
      hide the day that stopped being true.
    - Every value goes through a renderer that fixes its form: dates through isoformat,
      absent values through one sentinel, delimiters escaped.

    Returns one string, ending in a newline.
    """
    sections = [
        "# Database context",
        _build_section(context.provenance),
        _schema_section(context.relations),
        _date_range_section(context.temporal_ranges),
        _manifest_section(context.column_manifest),
        _codebook_section(context.codebook),
    ]
    return "\n\n".join(sections) + "\n"


# --- The SQL call (spec §6) ------------------------------------------------------------

# The system prompt for the SQL call (spec §6).
#
# Everything here is phrased by OUTCOME. Spec §2C binds prompt text exactly as it binds
# source: no table name, no column name, no dataset name, no code value, and no example
# query, because every one of those is an assumption about a build that may not hold. In
# particular the instruction about readable values must never name the views that provide
# them or the suffix they use — a build without them simply does not offer them in the
# schema described above, and the model uses what is there.
#
# The wording is one constant rather than fragments joined at a call site, so it can be
# revised in one place when the answer set in a later plan shows what needs revising.
SQL_SYSTEM_PROMPT = """\
You write DuckDB SQL to answer questions about the UK road-safety database described above.

That description is complete and it is specific to this build: every table and view it
contains, every column and its type, the range of dates held in every dated column, the
classification of every source column, and the full codebook of integer codes with the
English text each code stands for.

Answer with exactly one of two things.

1. A single SELECT statement that answers the question.
2. A plain-English reason why this particular database cannot answer the question.

Decline ONLY when the data needed is genuinely absent from this build: a dataset it was not
built with, a column it does not contain, or a period outside the date ranges given above.
Never decline because a question is hard, broad, or would need a complicated query. A
refusal the user could have had an answer to sounds authoritative, and is worse than an
empty result. When in doubt, write the query.

When you write SQL:

- Use only the tables and columns that appear in the description above. Do not invent a
  name, and do not assume something exists because a similar database might have it.
- Where the description offers both a column of raw codes and a column whose values a
  person can read, use the readable one. The user is shown the query you write and may not
  read SQL.
- Always include an ORDER BY, so the same query returns its rows in the same order every
  time. Where rows are grouped by something the codebook already puts in an order, sort on
  that underlying order rather than alphabetically.
- When the query combines results with UNION, sort only by output column names or
  positions. To sort by anything else, wrap the UNION in a subquery and order the outer
  query.
- Write exactly one statement. Not several separated by semicolons, and nothing that
  creates, inserts, updates, deletes, copies, attaches, or changes a setting. Anything else
  is rejected before it reaches the database.
- A code the codebook marks as missing was replaced with NULL when this database was built,
  so a filter on that code matches no rows. Test for NULL instead.
- Give every aggregated or derived column a short, readable alias.
- When the result counts or sums something per time period (each day, week, month or
  year), return a row for every period in the range asked about, including periods with
  nothing in them. A missing period is easy to misread: a line chart joins its neighbours
  straight across and turns a real zero into an invented value. Build the full list of
  periods with generate_series and LEFT JOIN the data onto it. Count a column from the
  joined data, not COUNT(*), so an empty period counts 0 rather than 1, and use
  COALESCE(..., 0) for sums. An average over an empty period has no value: leave it NULL,
  never 0. Keep the list within the dates this build holds, because a period the build
  does not cover has no data, which is not the same as zero.
"""


def sql_system_blocks(context):
    """The `system` parameter for the SQL call, with the cache breakpoint placed (spec §3).

    Two text blocks, both stable for a given database:

        [0]  the serialised context — the big payload
        [1]  the instructions, carrying the cache breakpoint

    The breakpoint sits on the LAST block, so everything stable is written to cache once
    and read back at about a tenth of the input rate. The question is not here at all: it
    goes in `messages`, after everything cached, which is the structural reason a new
    question can never invalidate the prefix.

    Instructions come second rather than first so they sit closest to the question. Both
    orders cache identically, because both blocks are stable.

    Takes no question, by design. A function that could accept one could be called with
    one.
    """
    return [
        {"type": "text", "text": serialise_context(context)},
        {
            "type": "text",
            "text": SQL_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
    ]


# --- The chart call (spec §7) ----------------------------------------------------------

# The system prompt for the chart call (spec §7).
#
# Deliberately NOT constrained by an output schema: a strict schema requires
# additionalProperties: false on every object, which cannot describe an arbitrary
# Vega-Lite document. The required shape is stated here instead, and the reply is parsed
# and shape-checked on arrival.
CHART_SYSTEM_PROMPT = """\
You choose a chart for a result that has already been computed. You are given the user's
question, the SQL that produced the result, and a description of the result's shape.

Reply with a single Vega-Lite specification as a JSON object, and nothing else: no prose,
no explanation, no code fence.

The specification must contain NO data. Do not include a "data" key. The application
supplies the rows when it draws the chart.

It must have a "mark" and an "encoding", and must use the column names exactly as they are
given to you.

Choose the mark from the shape you were given. A handful of categories against a count
suits a bar. A value moving across a dated column suits a line. Where a column has hundreds
of distinct values, do not put it on an axis that will be unreadable — pick the encoding
that stays legible, or chart a different pair of columns.

Draw a single view. Do not use "facet", "row", "column", "concat", "hconcat", "vconcat" or
"repeat", and do not set "width", "height" or "autosize". The application sizes the chart
to fit the page.

The rows arrive in the order the SQL sorted them, and that is the order they should be
drawn in. Preserve it. Vega-Lite sorts the values of a nominal or ordinal field
alphabetically unless told otherwise, so set "sort": null on every encoding channel that
uses one. That keeps the order the rows arrived in.

The SQL has usually done the arithmetic already. If it groups rows (GROUP BY) and returns
a counted, summed or averaged column, plot that column's values directly: put the column
itself on the axis, with no "aggregate" on any channel and no aggregating transform.
Counting those rows again would show 1 for every category, and the chart would contradict
the table shown beside it. Aggregate only when the result is a list of individual records,
for example when a histogram is the right chart.
"""


@dataclass(frozen=True)
class ResultShape:
    """The shape of an executed result, without the result itself (spec §7).

    Everything here is computed in Python from rows already in memory — no extra query —
    and the bulk of the result never leaves the machine. Whatever executes the query builds
    one of these; this module only renders it.

    columns          ((name, type_name), ...) in result order.
    row_count        how many rows the query returned in total.
    distinct_counts  ((column_name, count), ...) for the non-numeric columns only. Three
                     severities belong on an axis; several hundred district codes do not.
    sample_rows      example rows, each a tuple of values in column order. Truncated to
                     config.CHART_SAMPLE_ROWS when rendered, so an oversized one cannot
                     put the user's data into a prompt by accident.
    """

    columns: tuple
    row_count: int
    distinct_counts: tuple
    sample_rows: tuple


def chart_user_message(question, sql, shape, failures=()):
    """The user message for the chart call (spec §7).

    The chart call is made only AFTER the SQL has run and returned at least one row, which
    is what makes it easy to get right: the columns being described demonstrably exist,
    because they came back from the database.

    `failures` carries earlier chart attempts that were unusable (spec §7: a malformed spec
    is repaired through the same loop).

    Returns one string.
    """
    column_lines = ["name | type"] + [
        f"{_escape(name)} | {_escape(type_name)}" for name, type_name in shape.columns
    ]

    distinct_lines = ["column | distinct values"] + [
        f"{_escape(name)} | {_escape(count)}" for name, count in shape.distinct_counts
    ]

    header = " | ".join(_escape(name) for name, _ in shape.columns)
    sample_lines = [header] + [
        " | ".join(_escape(value) for value in row)
        for row in shape.sample_rows[: config.CHART_SAMPLE_ROWS]
    ]

    sections = [
        "## Question\n" + question,
        "## SQL that produced the result\n" + sql,
        "## Result columns\n" + "\n".join(column_lines),
        f"## Total rows\n{shape.row_count}",
        "## Distinct values per non-numeric column\n" + "\n".join(distinct_lines),
        "## Sample rows\n"
        "These rows are already in the order the SQL sorted them, which is the order to "
        "draw them in.\n" + "\n".join(sample_lines),
    ]
    if failures:
        sections.append(_failures_section(failures))
    return "\n\n".join(sections) + "\n"


# --- Repair attempts (spec §4) ---------------------------------------------------------


def _failures_section(failures):
    """Earlier failed attempts at this question, for the model to correct (spec §4).

    Placed in the user message, never the system blocks: the system blocks are the cached
    prefix and must stay byte-identical for a given database (spec §3). Earlier attempts are
    volatile, so they belong after the cache breakpoint, like the question itself.

    `failures` is a tuple of (attempted, error) pairs, oldest first. `attempted` is None when
    there is nothing to show, for example when the reply could not be read at all.

    The attempted SQL and the error are shown verbatim and NOT pipe-escaped. They are not
    rows of a delimited table, and the model needs to see the exact text that failed.
    """
    parts = [
        "## Earlier attempts that failed\n"
        "Each attempt below was tried and did not work. Correct the problem; do not repeat "
        "a failed attempt."
    ]
    for number, (attempted, error) in enumerate(failures, start=1):
        tried = attempted if attempted else "(no usable reply)"
        parts.append(f"### Attempt {number}\nTried:\n{tried}\n\nError:\n{error}")
    return "\n\n".join(parts)


def sql_user_message(question, failures=()):
    """The user message for the SQL call.

    With no failures this is the question and nothing else, exactly as before, so a first
    attempt sends precisely what it always did.
    """
    if not failures:
        return question
    return "## Question\n" + question + "\n\n" + _failures_section(failures) + "\n"
