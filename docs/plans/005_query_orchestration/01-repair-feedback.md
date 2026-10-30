# Stage 01 — Repair Feedback in the Model Seam

> Part of *Query Orchestration*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular *The gap this plan closes first* and
> the Cross-Cutting Constraints.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

The previous plan, `docs/plans/004_prompt_and_model/`, must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -c "
from askcrossroads import llm, prompt
import inspect
print(inspect.signature(llm.ModelClient.generate_sql))
print(inspect.signature(llm.ModelClient.describe_chart))
print(inspect.signature(prompt.chart_user_message))
print(llm.MalformedResponse, llm.ModelError, llm.SqlResponse, llm.AnthropicClient)
"
```

Expected output, in substance:

```text
(self, context, question) -> askcrossroads.llm.SqlResponse
(self, question, sql, shape) -> dict
(question, sql, shape)
<class 'askcrossroads.llm.MalformedResponse'> <class 'askcrossroads.llm.ModelError'> ...
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest -q
```

Expected: green. `tests/test_llm.py` exists and contains `test_fake_model_records_its_calls` and
`test_fake_model_satisfies_the_protocol`.

**If any of these fail, stop.** The previous plan is not finished, and this stage edits its files.
If the signatures already include a `failures` parameter, someone has done part of this stage:
read what is there and finish it rather than duplicating it.

---

## Objective

Let each model call carry the earlier failed attempts at the same question, so the repair loops in
stages 03 and 04 can feed the error back to the model, as spec §4 requires. With no failures,
everything sent is byte-identical to what is sent today.

---

## The data shape crossing the seam

```text
failures: tuple of (attempted, error) pairs, oldest first
    attempted  str or None   the SQL that was tried; None when the reply itself was unusable
                              (a MalformedResponse), and always None for the chart call
    error      str           what went wrong, verbatim: a DuckDB error, a validator verdict's
                              detail, or a MalformedResponse message
```

It is a plain tuple of plain strings, on purpose. `prompt.py` must not import `llm.py` or
`query.py`, so whatever crosses into it has to be built from standard types.

---

## Implementation Steps

### 1. Render failures in `prompt.py`

**File:** `src/askcrossroads/prompt.py`. Add after `chart_user_message`'s current definition, and
change `chart_user_message` as shown.

Add one private renderer, shared by both calls:

```python
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
```

Change `chart_user_message`'s signature to `chart_user_message(question, sql, shape, failures=())`,
and change only its `return` so the section is appended when there are failures:

```python
    sections = [
        # ... the existing six sections, unchanged, in the same order ...
    ]
    if failures:
        sections.append(_failures_section(failures))
    return "\n\n".join(sections) + "\n"
```

Do not reword, reorder or reformat any existing section. With `failures=()` the output must be
byte-identical to the current output, and plan 004's tests prove that only if nothing else moved.

Update `chart_user_message`'s docstring with one sentence: *"`failures` carries earlier chart
attempts that were unusable (spec §7: a malformed spec is repaired through the same loop)."*

**Do not** touch `SQL_SYSTEM_PROMPT`, `CHART_SYSTEM_PROMPT`, `serialise_context` or
`sql_system_blocks`. Changing any of them would change the cached prefix.

**Expected result:** `prompt.sql_user_message("q")` returns `"q"`. `prompt.chart_user_message(q, s,
shape)` returns exactly what it returned before this stage.

### 2. Thread `failures` through `llm.py`

**File:** `src/askcrossroads/llm.py`

In `ModelClient`:

```python
    def generate_sql(self, context, question, failures=()) -> SqlResponse: ...

    def describe_chart(self, question, sql, shape, failures=()) -> dict: ...
```

Extend the protocol's docstring with: *"`failures` is a tuple of (attempted, error) pairs, earlier
failed attempts at this same question, oldest first. It is empty on a first attempt. Spec §4
requires the error to be fed back so the model can correct it."*

In `AnthropicClient.generate_sql`, add the parameter and change only the message content:

```python
    def generate_sql(self, context, question, failures=()):
        ...
        response = self._send(
            system=prompt.sql_system_blocks(context),
            messages=[
                {"role": "user", "content": prompt.sql_user_message(question, failures)}
            ],
            output_config=...,   # unchanged
        )
```

In `AnthropicClient.describe_chart`, add the parameter and pass it through:

```python
    def describe_chart(self, question, sql, shape, failures=()):
        ...
                    "content": prompt.chart_user_message(question, sql, shape, failures),
```

Nothing else in `llm.py` changes. In particular `system=` is the same expression as before, and
that is what keeps the cache hitting on a repair attempt.

### 3. Update `FakeModel`

**File:** `tests/conftest.py`, class `FakeModel`.

```python
    def generate_sql(self, context, question, failures=()):
        self.calls.append(("generate_sql", question, failures))
        return self._next(self.sql_responses, "generate_sql")

    def describe_chart(self, question, sql, shape, failures=()):
        self.calls.append(("describe_chart", question, sql, shape, failures))
        return self._next(self.chart_responses, "describe_chart")
```

Add one sentence to the class docstring: *"Each recorded call includes the `failures` it was given,
so a repair-loop test can assert the error really was fed back."*

### 4. Update the one existing test that pins the call-record shape

**File:** `tests/test_llm.py`, `test_fake_model_records_its_calls`. Change the expected list to:

```python
    assert model.calls == [
        ("generate_sql", QUESTION, ()),
        ("describe_chart", QUESTION, "SELECT 1", shape, ()),
    ]
```

`test_fake_model_satisfies_the_protocol` compares `FakeModel`'s parameter names with
`ModelClient`'s. It stays green without edits, because steps 2 and 3 add the same parameter to
both. Run it to confirm.

Search for any other assertion on the old tuple shape and update it the same way:

```bash
grep -rn '("generate_sql",\|("describe_chart",' tests/
```

---

## Testing & Verification

### Unit tests: `tests/test_prompt.py` (append)

Reuse the existing `_shape()` helper in that file.

| Test | Assertion |
|---|---|
| `test_sql_message_without_failures_is_just_the_question` | `prompt.sql_user_message("q") == "q"` and `prompt.sql_user_message("q", ()) == "q"` |
| `test_sql_message_carries_each_failure_in_order` | with `failures=(("SELECT a", "err one"), (None, "err two"))`: the output contains the question, `"SELECT a"`, `"err one"`, `"(no usable reply)"` and `"err two"`, and `"Attempt 1"` appears before `"Attempt 2"` |
| `test_failed_sql_is_not_pipe_escaped` | with `failures=(("SELECT 'a|b'", "e"),)` the output contains `SELECT 'a|b'` verbatim |
| `test_chart_message_without_failures_is_unchanged` | `chart_user_message(Q, S, shape) == chart_user_message(Q, S, shape, ())`, and neither contains `"Earlier attempts"` |
| `test_chart_message_appends_failures_last` | with one failure, the output ends with that failure's error text plus `"\n"`, and `with_failure.split("## Earlier attempts")[0] == without_failure[:-1] + "\n\n"`. That is, the earlier sections are untouched and the new section is joined on like any other |
| `test_failure_wording_names_nothing_about_the_schema` | `_failures_section(((None, "x"),)).lower()` contains none of `collision`, `weather`, `vehicle`, `casualty`, `labelled`, `_label` |

### Unit tests: `tests/test_llm.py` (append)

Reuse the existing `_client`, `_answer`, `_Block`, `_Response`, `_shape`, `CHART`, `QUESTION` and
the `context` fixture.

```python
FAILURES = (("SELECT nope FROM nowhere", "Catalog Error: Table nowhere does not exist"),)


def test_a_repair_keeps_the_cached_system_blocks(context):
    """Spec §3: the prefix is byte-stable. A repair attempt must hit the same cache entry."""
    client = _client(response=_answer())
    client.generate_sql(context, QUESTION)
    client.generate_sql(context, QUESTION, FAILURES)
    first, second = client._client.recorded
    assert first["system"] == second["system"] == prompt.sql_system_blocks(context)


def test_a_repair_feeds_the_failure_back_in_the_user_message(context):
    """Spec §4: the error text is fed back to the model."""
    client = _client(response=_answer())
    client.generate_sql(context, QUESTION, FAILURES)
    content = client._client.recorded[0]["messages"][0]["content"]
    assert content == prompt.sql_user_message(QUESTION, FAILURES)
    assert "SELECT nope FROM nowhere" in content
    assert "Table nowhere does not exist" in content
    assert "Table nowhere does not exist" not in json.dumps(client._client.recorded[0]["system"])


def test_a_chart_repair_feeds_the_failure_back():
    client = _client(response=_Response([_Block("text", json.dumps(CHART))]))
    failures = ((None, "The chart specification had no mark."),)
    client.describe_chart(QUESTION, "SELECT 1", _shape(), failures)
    sent = client._client.recorded[0]
    assert sent["system"] == prompt.CHART_SYSTEM_PROMPT
    assert sent["messages"][0]["content"] == prompt.chart_user_message(
        QUESTION, "SELECT 1", _shape(), failures
    )
```

The existing `test_generate_sql_puts_the_question_in_messages_only` must still pass **unchanged**.
It is this stage's proof that a first attempt sends exactly what it always did.

### Integration

None new in this stage. Plan 004's `tests/integration/test_prompt_and_model.py` makes first-attempt
calls only, and those send byte-identical requests, so it is unaffected. Stage 04 of this plan adds
the real-API test that exercises the whole loop.

### Commands

```bash
.venv/bin/python -m pytest tests/test_prompt.py tests/test_llm.py -v
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

```bash
grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/ ; grep -rniE "labelled|_label" src/askcrossroads/prompt.py ; echo "both greps above must print nothing"
```

```bash
grep -nE "^import|^from" src/askcrossroads/prompt.py
```

Expected: no `llm`, no `query`, no `database`, no `anthropic`.

### Stage ship-readiness checklist

1. The default suite is green, including every pre-existing test in `test_prompt.py` and
   `test_llm.py` unchanged apart from the one edit in step 4.
2. Both greps print nothing.
3. `prompt.py` imports neither `llm` nor `query`.
4. `git status` shows `prompt.py`, `llm.py`, `tests/conftest.py`, `tests/test_prompt.py`,
   `tests/test_llm.py` modified, and **nothing staged**.

---

## End State / Handoff

- `prompt.sql_user_message(question, failures=())` exists. With no failures it returns `question`
  unchanged.
- `prompt.chart_user_message(question, sql, shape, failures=())` exists. With no failures its output
  is byte-identical to before this stage.
- `llm.ModelClient.generate_sql(context, question, failures=())` and
  `describe_chart(question, sql, shape, failures=())`, implemented by `AnthropicClient` and
  `FakeModel` alike.
- On a repair, `system` is unchanged and the failures are only in the user message.
- `FakeModel.calls` entries are `("generate_sql", question, failures)` and
  `("describe_chart", question, sql, shape, failures)`.

Stage 02 may assume it can pass a tuple of `(attempted_or_None, error_str)` pairs to either model
method and have it reach the model.

---

## Failure Modes & Rollback

| Failure | Why it happens | Guardrail |
|---|---|---|
| The cache stops hitting on repairs | Failures put into the system blocks | `test_a_repair_keeps_the_cached_system_blocks` |
| First attempts change silently | An existing chart section reworded or reordered, or the question wrapped in a heading when there are no failures | `test_sql_message_without_failures_is_just_the_question`, `test_chart_message_without_failures_is_unchanged`, and plan 004's unchanged `test_generate_sql_puts_the_question_in_messages_only` |
| Protocol and fake drift apart | The parameter added to one but not the other | `test_fake_model_satisfies_the_protocol` compares parameter names |
| The failed SQL is mangled before the model sees it | Routing it through `_escape` | `test_failed_sql_is_not_pipe_escaped` |

**Rollback:** `git checkout -- src/askcrossroads/prompt.py src/askcrossroads/llm.py tests/conftest.py tests/test_llm.py tests/test_prompt.py`
reverts to the previous plan's end state, **but only if that end state was itself committed**. If
plan 004's changes are still uncommitted, revert this stage's edits by hand instead, using the
steps above in reverse. No commit is made at any point.
