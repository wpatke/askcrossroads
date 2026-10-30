"""The prompt prefix is complete, byte-stable, and built only from the file (spec §3, §11.4).

Spec §11.4: "The same database always produces the exact same prefix, byte for byte [...]
This is what makes caching work, and if it silently breaks, nothing else will catch it."
That is what most of this file is for.

Where a test names a table or column it is naming what the FIXTURE built, which is test
input — src/ still contains none of these names.
"""

import inspect
import json
import re

import pytest

from askcrossroads import config, prompt
from askcrossroads.database import Database

from conftest import AADF, BANK_HOLIDAYS, ERA5_WEATHER, STATS19


def _context(path):
    """Open a build through the real path and return its context."""
    with Database.open(path) as database:
        return database.context


def _prefix(path):
    return prompt.serialise_context(_context(path))


def _section_rows(text, heading):
    """The data rows of one delimited section: the lines after its column header, up to the
    next blank line."""
    lines = text.splitlines()
    start = lines.index(heading)
    rows = []
    # +2 skips the heading itself and the column-header line beneath it.
    for line in lines[start + 2:]:
        if not line:
            break
        rows.append(line)
    return rows


def _build_section(text):
    lines = text.splitlines()
    start = lines.index("## Build")
    end = lines.index("", start)
    return "\n".join(lines[start:end])


# --- Byte stability (spec §11.4) -----------------------------------------------------


def test_prefix_is_stable_across_repeated_calls(crossroads_db):
    context = _context(crossroads_db)
    assert prompt.serialise_context(context) == prompt.serialise_context(context)


def test_prefix_is_stable_across_reopens(crossroads_db):
    assert _prefix(crossroads_db) == _prefix(crossroads_db)


def test_prefix_is_stable_across_identical_builds(crossroads_db_factory):
    first = crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER), years=(2022, 2023))
    second = crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER), years=(2022, 2023))
    assert first != second
    assert _prefix(first) == _prefix(second)


def test_prefix_changes_when_the_years_change(crossroads_db_factory):
    assert _prefix(crossroads_db_factory(years=(2019,))) != _prefix(
        crossroads_db_factory(years=(2023,))
    )


def test_prefix_changes_when_the_datasets_change(crossroads_db_factory):
    assert _prefix(crossroads_db_factory(datasets=(STATS19,))) != _prefix(
        crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER))
    )


def test_prefix_contains_no_path(crossroads_db):
    context = _context(crossroads_db)
    text = prompt.serialise_context(context)
    assert str(context.path) not in text
    assert context.path.name not in text


# --- The whole payload is present (spec §3, §6) ---------------------------------------


def test_prefix_contains_every_codebook_row(crossroads_db):
    context = _context(crossroads_db)
    text = prompt.serialise_context(context)
    assert len(_section_rows(text, "## Codebook")) == len(context.codebook)


def test_prefix_contains_every_manifest_row(crossroads_db):
    context = _context(crossroads_db)
    text = prompt.serialise_context(context)
    rows = _section_rows(text, "## Column classifications")
    assert len(rows) == len(context.column_manifest)


def test_prefix_contains_every_relation(crossroads_db):
    context = _context(crossroads_db)
    text = prompt.serialise_context(context)
    statements = [
        line for line in text.splitlines()
        if line.startswith("CREATE TABLE ") or line.startswith("CREATE VIEW ")
    ]
    assert len(statements) == len(context.relations)
    for relation in context.relations:
        assert f".{relation.name} (" in text


def test_prefix_contains_every_column(crossroads_db):
    context = _context(crossroads_db)
    text = prompt.serialise_context(context)
    # Every column line but the last in a CREATE ends with a comma; drop it to compare.
    lines = {line.rstrip(",") for line in text.splitlines()}
    relation = context.relation("collisions")
    assert relation is not None
    for column in relation.columns:
        assert f"  {column.name} {column.data_type}" in lines


def test_prefix_contains_every_date_range(crossroads_db_factory):
    path = crossroads_db_factory(datasets=(STATS19, BANK_HOLIDAYS))
    context = _context(path)
    text = prompt.serialise_context(context)
    assert len(_section_rows(text, "## Date ranges")) == len(context.temporal_ranges)
    # The DATE column is ranged; its VARCHAR twin with a date-like name is not.
    assert "bank_holidays | date | " in text
    assert "bank_holidays | date_raw | " not in text


def test_prefix_carries_is_missing(crossroads_db):
    rows = _section_rows(_prefix(crossroads_db), "## Codebook")
    assert any(row.endswith("| yes") for row in rows)
    assert any(row.endswith("| no") for row in rows)


def test_prefix_carries_build_parameters(crossroads_db_factory):
    path = crossroads_db_factory(datasets=(STATS19, AADF), years=(2019, 2021))
    section = _build_section(_prefix(path))
    for value in ("2019", "2021", STATS19, AADF, "snapshot"):
        assert value in section


def test_empty_table_range_renders_as_absent(crossroads_db):
    rows = _section_rows(_prefix(crossroads_db), "## Date ranges")
    assert "quarantine_raw | ingested_at | (none) | (none)" in rows
    assert not any("None" in row for row in rows)


def test_a_weatherless_build_offers_no_weather_relation(crossroads_db_factory):
    weatherless = _context(crossroads_db_factory(datasets=(STATS19,)))
    with_weather = _context(crossroads_db_factory(datasets=(STATS19, ERA5_WEATHER)))
    weather_relations = {r.name for r in with_weather.relations} - {
        r.name for r in weatherless.relations
    }
    assert weather_relations  # the comparison build really does carry weather relations

    text = prompt.serialise_context(weatherless)
    for name in weather_relations:
        assert f".{name} (" not in text
        assert not any(
            row.startswith(f"{name} | ") for row in _section_rows(text, "## Date ranges")
        )


# --- Nothing about the schema is hardcoded (spec §2C) ---------------------------------


def test_an_unfamiliar_build_produces_a_prompt_naming_its_own_tables(unconventional_db):
    """Nothing about the schema is hardcoded (spec §2C).

    This build is entirely valid and shares no name with the standard fixture. A prefix that
    mentioned a familiar table could only have got the name from source.
    """
    with Database.open(unconventional_db) as database:
        text = prompt.serialise_context(database.context)

    for name in ("widgets", "gadget_log", "widget_summary", "grade", "mass_kg"):
        assert name in text

    for name in ("collisions", "vehicles", "collision_severity", "weather_conditions"):
        assert name not in text


# --- The renderers, where a case is hard to reach through a real build -----------------


def test_escape_escapes_the_delimiter():
    assert prompt._escape("a|b") == "a\\|b"


def test_escape_escapes_backslash_first():
    # A literal backslash followed by a pipe must not be confused with an escaped pipe.
    assert prompt._escape("a\\|b") == "a\\\\\\|b"
    assert prompt._escape("a\\|b") != prompt._escape("a|b")


def test_escape_collapses_newlines():
    assert prompt._escape("a\nb") == "a b"


def test_escape_renders_none_as_the_sentinel():
    assert prompt._escape(None) == prompt.ABSENT


def test_identifier_leaves_ordinary_names_bare():
    assert prompt._identifier("collisions") == "collisions"


@pytest.mark.parametrize("name", ["order by", 'a"b', "2023_counts"])
def test_identifier_quotes_unusual_names(name):
    rendered = prompt._identifier(name)
    assert rendered.startswith('"') and rendered.endswith('"')


# --- The SQL call's system blocks (spec §3) ---------------------------------------------


def test_system_blocks_carry_the_context_first(crossroads_db):
    context = _context(crossroads_db)
    blocks = prompt.sql_system_blocks(context)
    assert blocks[0]["text"] == prompt.serialise_context(context)


def test_system_blocks_carry_the_instructions_last(crossroads_db):
    blocks = prompt.sql_system_blocks(_context(crossroads_db))
    assert blocks[-1]["text"] == prompt.SQL_SYSTEM_PROMPT


def test_exactly_one_cache_breakpoint_on_the_last_block(crossroads_db):
    blocks = prompt.sql_system_blocks(_context(crossroads_db))
    with_breakpoint = [block for block in blocks if "cache_control" in block]
    assert len(with_breakpoint) == 1
    assert with_breakpoint[0] is blocks[-1]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_system_blocks_are_equal_across_calls(crossroads_db):
    context = _context(crossroads_db)
    assert prompt.sql_system_blocks(context) == prompt.sql_system_blocks(context)


def test_system_blocks_hold_no_question(crossroads_db):
    """The question goes in `messages`, after the breakpoint — never in the system blocks."""
    text = json.dumps(prompt.sql_system_blocks(_context(crossroads_db)))
    for question in (
        "How many records are there per year?",
        "Which district had the most entries in March?",
        "Show me the average by month",
    ):
        assert question not in text
    assert list(inspect.signature(prompt.sql_system_blocks).parameters) == ["context"]


def test_system_blocks_differ_for_a_different_build(crossroads_db_factory):
    first = _context(crossroads_db_factory(years=(2019,)))
    second = _context(crossroads_db_factory(years=(2023,)))
    assert prompt.sql_system_blocks(first) != prompt.sql_system_blocks(second)


def test_system_blocks_work_for_an_unfamiliar_build(unconventional_db):
    blocks = prompt.sql_system_blocks(_context(unconventional_db))
    assert "widgets" in blocks[0]["text"]


# --- The prompts themselves (spec §2C, §3, §6, §7) --------------------------------------

# Words that must never appear in prompt text (spec §2C, §3). These are exactly the greps
# in the plan's ship-readiness list, kept here as well so a change is caught by the suite
# rather than only by a person remembering to grep.
FORBIDDEN_IN_PROMPTS = (
    "collision", "weather", "aadf", "stats19", "vehicle", "casualty",
    "labelled", "_label",
)


def test_prompts_name_nothing_about_the_schema():
    """The prompts are phrased by outcome, never by naming what a build might contain."""
    for name, text in (
        ("SQL_SYSTEM_PROMPT", prompt.SQL_SYSTEM_PROMPT),
        ("CHART_SYSTEM_PROMPT", prompt.CHART_SYSTEM_PROMPT),
    ):
        lowered = text.lower()
        for word in FORBIDDEN_IN_PROMPTS:
            assert word not in lowered, f"{name} names {word!r}"


# Each of these asserts on one distinctive phrase, so a rewrite that keeps the meaning does
# not turn the suite red. Whitespace is flattened first, so re-wrapping a line of the prompt
# cannot either.


def _flat(text):
    return " ".join(text.split())


def test_sql_prompt_offers_both_outcomes():
    assert "SELECT statement" in _flat(prompt.SQL_SYSTEM_PROMPT)
    assert "plain-English reason" in _flat(prompt.SQL_SYSTEM_PROMPT)


def test_sql_prompt_restricts_declining_to_absent_data():
    assert "ONLY when the data needed is genuinely absent" in _flat(prompt.SQL_SYSTEM_PROMPT)
    assert "Never decline because a question is hard" in _flat(prompt.SQL_SYSTEM_PROMPT)


def test_sql_prompt_asks_for_an_order_by():
    assert "ORDER BY" in _flat(prompt.SQL_SYSTEM_PROMPT)


def test_sql_prompt_asks_for_readable_values():
    assert "a person can read" in _flat(prompt.SQL_SYSTEM_PROMPT)


def test_sql_prompt_asks_for_one_statement():
    assert "exactly one statement" in _flat(prompt.SQL_SYSTEM_PROMPT)


def test_chart_prompt_forbids_data():
    assert "must contain NO data" in _flat(prompt.CHART_SYSTEM_PROMPT)
    assert 'Do not include a "data" key' in _flat(prompt.CHART_SYSTEM_PROMPT)


def test_chart_prompt_requires_mark_and_encoding():
    assert '"mark"' in _flat(prompt.CHART_SYSTEM_PROMPT)
    assert '"encoding"' in _flat(prompt.CHART_SYSTEM_PROMPT)


def test_chart_prompt_preserves_row_order():
    assert "Preserve it" in _flat(prompt.CHART_SYSTEM_PROMPT)


# --- The chart call's user message (spec §7) --------------------------------------------


def _shape(sample_rows=(("Alpha", 10), ("Beta", 7), ("Gamma", 1))):
    return prompt.ResultShape(
        columns=(("category", "VARCHAR"), ("total", "BIGINT")),
        row_count=3,
        distinct_counts=(("category", 3),),
        sample_rows=tuple(sample_rows),
    )


QUESTION = "How many of each category?"
SQL = "SELECT category, COUNT(*) AS total FROM things GROUP BY category ORDER BY category"


def _sample_rows(message):
    """The data rows of the sample section: after its sentence and header line."""
    section = message.split("## Sample rows\n", 1)[1]
    lines = [line for line in section.splitlines() if line]
    # lines[0] is the ordering sentence, lines[1] the column header.
    return lines[2:]


def test_chart_message_carries_the_question_and_sql():
    message = prompt.chart_user_message(QUESTION, SQL, _shape())
    assert QUESTION in message
    assert SQL in message


def test_chart_message_carries_columns_and_types():
    message = prompt.chart_user_message(QUESTION, SQL, _shape())
    assert "category | VARCHAR" in message
    assert "total | BIGINT" in message


def test_chart_message_carries_the_row_count():
    message = prompt.chart_user_message(QUESTION, SQL, _shape())
    assert "## Total rows\n3\n" in message


def test_chart_message_carries_distinct_counts():
    message = prompt.chart_user_message(QUESTION, SQL, _shape())
    assert "category | 3" in message.splitlines()


def test_chart_message_states_the_rows_are_already_ordered():
    message = prompt.chart_user_message(QUESTION, SQL, _shape())
    assert "already in the order the SQL sorted them" in message


def test_chart_message_truncates_sample_rows():
    many = [(f"item{i}", i) for i in range(50)]
    message = prompt.chart_user_message(QUESTION, SQL, _shape(sample_rows=many))
    assert len(_sample_rows(message)) == config.CHART_SAMPLE_ROWS


def test_chart_message_escapes_a_pipe_in_a_value():
    message = prompt.chart_user_message(QUESTION, SQL, _shape(sample_rows=[("A|B", 4)]))
    rows = _sample_rows(message)
    assert rows == ["A\\|B | 4"]
    # Splitting on the unescaped delimiter still finds exactly two fields.
    assert len(re.split(r"(?<!\\) \| ", rows[0])) == 2


def test_chart_message_is_deterministic():
    assert prompt.chart_user_message(QUESTION, SQL, _shape()) == prompt.chart_user_message(
        QUESTION, SQL, _shape()
    )
