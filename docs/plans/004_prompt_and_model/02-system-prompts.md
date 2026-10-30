# Stage 02 — The Two System Prompts & Block Assembly

> Part of *Prompt Assembly & Model Interface*. You have the overview + this stage; implement **only**
> this stage.
> Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting Constraints and the
> Stage Map.
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 01 must be complete. Verify:

```bash
.venv/bin/python -m pytest tests/test_prompt.py -q
```

Expected: green, including `test_an_unfamiliar_build_produces_a_prompt_naming_its_own_tables`.

```bash
.venv/bin/python -c "from askcrossroads import prompt; print(prompt.serialise_context.__doc__.splitlines()[0])"
```

Expected: `Render a DatabaseContext as the cacheable prompt prefix (spec §3).`

If `serialise_context` does not exist, stop — stage 01 is not finished.

---

## Objective

Write the two system prompts, assemble the SQL call's system blocks with the cache breakpoint in the
right place, and render the chart call's user message. After this stage `prompt.py` is complete and
nothing in the project yet calls Anthropic.

---

## Implementation Steps

### 1. Add the sample-row bound to `config.py`

**File:** `src/askcrossroads/config.py`

Spec §7 promises "about five sample rows" in the chart call. Spec §10 requires every bound the
specification promises to be a named constant here, never a literal at a call site. Add to the
Bounds block, after `MAX_RESULT_ROWS`:

```python
CHART_SAMPLE_ROWS = 5      # example rows sent with the chart call (spec §7)
```

**Expected result:** `tests/test_config.py` still passes. If that file asserts on the exact set of
public constants, add the new name there too.

### 2. Write the SQL system prompt

**File:** `src/askcrossroads/prompt.py`

This is the most consequential text in the project and the hardest to get past spec §2C, because the
constraint applies to prompt text exactly as it applies to code. **The prompt may not contain a
table name, a column name, a dataset name, a code value, an example query, the word for the labelled
views, or the label-column suffix.** Everything is phrased by outcome.

Add, after the section builders:

```python
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
- Write exactly one statement. Not several separated by semicolons, and nothing that
  creates, inserts, updates, deletes, copies, attaches, or changes a setting. Anything else
  is rejected before it reaches the database.
- A code the codebook marks as missing was replaced with NULL when this database was built,
  so a filter on that code matches no rows. Test for NULL instead.
- Give every aggregated or derived column a short, readable alias.
"""
```

**Expected result:** `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns
nothing, and `grep -rniE "labelled|_label" src/askcrossroads/prompt.py` returns nothing.

> **Note on one near-miss.** The prompt says *"the English text each code stands for"* rather than
> the obvious word, precisely so the second grep stays clean and stays cheap to run. If you reword
> this, re-run both greps.

### 3. Write the chart system prompt

**File:** `src/askcrossroads/prompt.py`

```python
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

The rows arrive in the order the SQL sorted them, and that is the order they should be
drawn in. Preserve it. Do not add a sort that re-orders them alphabetically.
"""
```

### 4. Assemble the SQL call's system blocks

**File:** `src/askcrossroads/prompt.py`

```python
def sql_system_blocks(context):
    """The `system` parameter for the SQL call, with the cache breakpoint placed (spec §3).

    Two text blocks, both stable for a given database:

        [0]  the serialised context — the big payload
        [1]  the instructions, carrying cache_control

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
```

### 5. Add `ResultShape` and the chart call's user message

**File:** `src/askcrossroads/prompt.py`

`dataclass` is imported at the top of the module, and `config` is imported alongside
`introspection`.

```python
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


def chart_user_message(question, sql, shape):
    """The user message for the chart call (spec §7).

    The chart call is made only AFTER the SQL has run and returned at least one row, which
    is what makes it easy to get right: the columns being described demonstrably exist,
    because they came back from the database.

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

    return "\n\n".join([
        "## Question\n" + question,
        "## SQL that produced the result\n" + sql,
        "## Result columns\n" + "\n".join(column_lines),
        f"## Total rows\n{shape.row_count}",
        "## Distinct values per non-numeric column\n" + "\n".join(distinct_lines),
        "## Sample rows\n"
        "These rows are already in the order the SQL sorted them, which is the order to "
        "draw them in.\n" + "\n".join(sample_lines),
    ]) + "\n"
```

**Expected result:** `prompt.py` is complete. It contains no `cache_control` outside
`sql_system_blocks`, and nothing that knows the Anthropic API exists.

---

## Testing & Verification

**File:** `tests/test_prompt.py` — extend the file stage 01 created.

### Integration tests (PRIMARY — real contexts, real assembly)

| Test | Setup | Assertion |
|---|---|---|
| `test_system_blocks_carry_the_context_first` | `crossroads_db` | `blocks[0]["text"] == serialise_context(ctx)` |
| `test_system_blocks_carry_the_instructions_last` | `crossroads_db` | `blocks[-1]["text"] == prompt.SQL_SYSTEM_PROMPT` |
| `test_exactly_one_cache_breakpoint_on_the_last_block` | `crossroads_db` | exactly one block has `cache_control`; it is `blocks[-1]`; its value is `{"type": "ephemeral"}` |
| `test_system_blocks_are_equal_across_calls` | `crossroads_db`, called twice | the two lists compare `==` |
| `test_system_blocks_hold_no_question` | `crossroads_db` | `json.dumps(blocks)` contains none of three different question strings; `sql_system_blocks` takes exactly one parameter |
| `test_system_blocks_differ_for_a_different_build` | two builds with different years | the two lists differ |
| `test_system_blocks_work_for_an_unfamiliar_build` | `unconventional_db` | assembly succeeds and `blocks[0]["text"]` names `widgets` |

### The spec §2C guard — the test that stops the prompt drifting

```python
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
```

### Content tests on the prompts themselves

These assert on *substance*, not on wording, so a rewrite that keeps the meaning does not turn the
suite red. Each is a single distinctive phrase from the constant.

| Test | Assertion |
|---|---|
| `test_sql_prompt_offers_both_outcomes` | the prompt contains both `"SELECT statement"` and `"plain-English reason"` |
| `test_sql_prompt_restricts_declining_to_absent_data` | it contains `"ONLY when the data needed is genuinely absent"` and `"Never decline because a question is hard"` |
| `test_sql_prompt_asks_for_an_order_by` | it contains `"ORDER BY"` |
| `test_sql_prompt_asks_for_readable_values` | it contains `"a person can read"` |
| `test_sql_prompt_asks_for_one_statement` | it contains `"exactly one statement"` |
| `test_chart_prompt_forbids_data` | it contains `"must contain NO data"` and `"Do not include a \"data\" key"` |
| `test_chart_prompt_requires_mark_and_encoding` | it contains both `"mark"` and `"encoding"` |
| `test_chart_prompt_preserves_row_order` | it contains `"Preserve it"` |

### Chart message tests

```python
def _shape():
    return prompt.ResultShape(
        columns=(("category", "VARCHAR"), ("total", "BIGINT")),
        row_count=3,
        distinct_counts=(("category", 3),),
        sample_rows=(("Alpha", 10), ("Beta", 7), ("Gamma", 1)),
    )
```

| Test | Assertion |
|---|---|
| `test_chart_message_carries_the_question_and_sql` | both appear verbatim in the output |
| `test_chart_message_carries_columns_and_types` | `category | VARCHAR` and `total | BIGINT` appear |
| `test_chart_message_carries_the_row_count` | the total row count appears |
| `test_chart_message_carries_distinct_counts` | the distinct-count row appears |
| `test_chart_message_states_the_rows_are_already_ordered` | the ordering sentence appears |
| `test_chart_message_truncates_sample_rows` | build a shape with 50 sample rows; the rendered sample section has exactly `config.CHART_SAMPLE_ROWS` rows after its header |
| `test_chart_message_escapes_a_pipe_in_a_value` | a value containing `|` renders escaped and does not add a column |
| `test_chart_message_is_deterministic` | called twice with the same shape, the output is equal |

### Runnable commands

```bash
.venv/bin/python -m pytest tests/test_prompt.py -v
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

```bash
grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/ ; grep -rniE "labelled|_label" src/askcrossroads/prompt.py ; echo "exit: both greps above must print nothing"
```

### Stage ship-readiness checklist

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
2. Both greps above print nothing.
3. `grep -c "cache_control" src/askcrossroads/prompt.py` returns `1`.
4. `grep -rn "import" src/askcrossroads/prompt.py` shows only `json`, `re`, `dataclasses`, and
   `from askcrossroads import config, introspection` — **no `llm`, no `database`, no `anthropic`**.
5. `git status` shows `prompt.py`, `config.py` and `tests/test_prompt.py` changed, and **nothing
   staged**.

---

## End State / Handoff

After this stage `prompt.py` is finished and exports:

| Name | What it is |
|---|---|
| `serialise_context(context)` | the byte-stable prefix (stage 01) |
| `SQL_SYSTEM_PROMPT` | the call-1 instructions |
| `CHART_SYSTEM_PROMPT` | the call-2 instructions |
| `sql_system_blocks(context)` | the `system` list for call 1, cache breakpoint on the last block |
| `ResultShape` | the frozen description of an executed result |
| `chart_user_message(question, sql, shape)` | the user message for call 2 |
| `ABSENT`, `_escape`, `_identifier`, `_temporal_value` | rendering primitives |

`config.CHART_SAMPLE_ROWS` exists. Nothing in the project calls Anthropic yet.

Stage 03 may assume it can pass `sql_system_blocks(context)` straight into the SDK's `system`
parameter and `CHART_SYSTEM_PROMPT` straight into it as a plain string, and that both are correct
without further shaping.

---

## Failure Modes & Rollback

| Failure | Why it happens | Guardrail |
|---|---|---|
| The prompt names a table, column or view | Writing an example query, or naming the labelled views to explain the readable-values rule | `test_prompts_name_nothing_about_the_schema` and the two greps. If you feel the need for an example, that is the signal you are about to breach §2C. |
| The cache never hits | A breakpoint placed between the blocks instead of on the last one, or a question smuggled into the system blocks | `test_exactly_one_cache_breakpoint_on_the_last_block` and `test_system_blocks_hold_no_question` offline; the real check is stage 03's integration test. |
| A prompt reword turns the suite red for no reason | Asserting on whole sentences instead of distinctive phrases | Keep each content assertion to one short phrase, as listed. |
| Sample rows leak more of the user's data than intended | The caller passes 500 rows and nothing truncates | `chart_user_message` truncates with `config.CHART_SAMPLE_ROWS` itself; `test_chart_message_truncates_sample_rows`. |
| A value containing a pipe breaks the sample table | Formatting sample rows with a bare f-string | Route every value through `_escape`; `test_chart_message_escapes_a_pipe_in_a_value`. |

**Rollback:** `git checkout -- src/askcrossroads/prompt.py src/askcrossroads/config.py` and revert
the additions to `tests/test_prompt.py`, or delete it if you prefer to redo stage 01's tests too.
Stage 01's `conftest.py` changes are independent and need not be undone. No commit is made at any
point.
