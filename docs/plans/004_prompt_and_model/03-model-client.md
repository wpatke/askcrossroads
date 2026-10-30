# Stage 03 — The Model Client & the Provider Seam

> Part of *Prompt Assembly & Model Interface*. You have the overview + this stage; implement **only**
> this stage.
> Read [00-overview.md](00-overview.md) first, in particular the Cross-Cutting Constraints, the
> **API key** rule, and **Trap 2** (`response.content[0]` is not the answer).
> The **End State** is the contract — if a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stages 01 and 02 must be complete. Verify:

```bash
.venv/bin/python -c "
from askcrossroads import prompt
print(sorted(n for n in dir(prompt) if not n.startswith('__')))
"
```

Expected to include: `ABSENT`, `CHART_SYSTEM_PROMPT`, `ResultShape`, `SQL_SYSTEM_PROMPT`,
`chart_user_message`, `serialise_context`, `sql_system_blocks`.

```bash
.venv/bin/python -m pytest -q && cat src/askcrossroads/llm.py
```

Expected: green, and `llm.py` containing only
`"""Thin provider interface and its Anthropic implementation (spec §9)."""`

If `sql_system_blocks` is missing, stop — stage 02 is not finished.

---

## Objective

Give `llm.py` the provider seam: a protocol, the structured-response contract and its parsing, the
Anthropic implementation, and sanitised errors. Add the scripted `FakeModel` every later plan will
test against, and the marked integration suite that proves the caching actually works.

---

## What the installed SDK actually looks like

Verified against the installed version — **do not** write these from memory:

```bash
.venv/bin/python -c "import anthropic; print(anthropic.__version__)"
```

Expected: `1.3.0` or newer.

- `client.messages.create(...)` accepts `model`, `max_tokens`, `system`, `messages`,
  `output_config`, and `cache_control`. There is **no** `output_format` parameter on `create` — that
  name belongs to the separate `messages.parse()` helper, which this plan does not use.
- Structured output is `output_config={"format": {"type": "json_schema", "schema": {...}}}`.
- `system` accepts a list of text blocks, each optionally carrying
  `cache_control={"type": "ephemeral"}`. `prompt.sql_system_blocks` already produces exactly that.
- The response has `.content` (a **list of blocks**, each with `.type`), `.stop_reason`, and
  `.usage` with `input_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`.
- `anthropic.AnthropicError` is the base of every exception the SDK raises — `APIError`,
  `APIStatusError`, `APIConnectionError`, `APITimeoutError`, `AuthenticationError`,
  `RateLimitError`, `BadRequestError` and the rest all descend from it. One `except` covers them all.
- **Pass nothing else.** No `thinking`, no `temperature`, no `output_config.effort`, no `speed`, no
  `betas`. Spec §8: *"the model runs on default settings"*, and `config.py` says so too. Adaptive
  thinking is on by default on `claude-opus-5`, and `config.MAX_TOKENS` caps thinking and response
  together — which is exactly what that constant's comment already records.

---

## Implementation Steps

### 1. The module docstring, imports and errors

**File:** `src/askcrossroads/llm.py` — replace the file, keeping the existing line as the first line
of an expanded docstring.

Imports: `json`, `from dataclasses import dataclass`, `from typing import Protocol`, `import
anthropic`, `from askcrossroads import config, prompt`. Nothing else. `llm.py` must not import
`database`, `query`, `app`, or `duckdb`.

```python
class ModelError(Exception):
    """The provider could not be reached, or refused the request outright.

    This is NOT a failed attempt: it is not repairable, and it does not consume one of the
    MAX_ATTEMPTS tries. Retrying a request the provider rejected on policy or authentication
    grounds will be rejected again.

    The message is written for a person and is SANITISED (spec §9): it never contains the API
    key, and it never contains the provider's own response body, which is not ours to relay to
    a browser. The original exception is chained, so a developer reading a traceback still has
    it, but str() of this error is safe to show a user.
    """


class MalformedResponse(Exception):
    """The provider replied, and the reply was not usable.

    Unparseable JSON, a discriminator that disagrees with the fields, a truncated response, or
    a chart specification of the wrong shape. Spec §4 and §6 make this a FAILED ATTEMPT: the
    caller repairs it within its bounded loop rather than crashing on it.
    """
```

### 2. The structured-response contract

**File:** `src/askcrossroads/llm.py`

```python
# The shape the SQL call must reply in (spec §6), requested through the API's structured-output
# support so that it is guaranteed to parse.
#
# Note what this schema can and cannot do. Strict structured output requires
# additionalProperties: false with every property listed in `required`, which rules out a true
# discriminated union. So the schema guarantees the reply PARSES; it cannot guarantee the
# discriminator and the fields agree. Checking that is parse_sql_response's job below — and
# spec §6 requires that check regardless, because `answerable: true` with no `sql` is named
# there as a malformed response.
SQL_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "sql": {"type": ["string", "null"]},
        "explanation": {"type": ["string", "null"]},
    },
    "required": ["answerable", "sql", "explanation"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SqlResponse:
    """What the SQL call returned (spec §6).

    Exactly one of the two shapes:

        SqlResponse(answerable=True,  sql="SELECT ...", explanation=None)
        SqlResponse(answerable=False, sql=None,         explanation="...")

    A decline is a SUCCESSFUL outcome, not a failure. It consumes no repair attempt and
    triggers no loop, for the same reason zero rows does not.
    """

    answerable: bool
    sql: str
    explanation: str
```

> **Risk flagged in the overview.** If the live API rejects `{"type": ["string", "null"]}`, the
> documented fallback is to make both fields `{"type": "string"}` and treat `""` as absent. The
> discriminator check below does not change. The integration suite in step 7 is what finds this
> out; record it in your stage report if you have to apply the fallback.

### 3. Response parsing — and Trap 2

**File:** `src/askcrossroads/llm.py`

```python
def _text(response):
    """The reply's text, whatever else the response carries.

    `content` is a LIST OF BLOCKS, and on this model a thinking block generally comes first,
    because adaptive thinking is on by default. Reading content[0].text works against a
    hand-written mock and fails against the real API. Select by type, always.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise MalformedResponse("The model returned no text in its reply.")


def parse_sql_response(text):
    """Turn the SQL call's reply into a SqlResponse, or raise MalformedResponse.

    Every rejection here is a failed attempt the caller can repair (spec §4), never a crash.
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        raise MalformedResponse("The model's reply was not valid JSON.") from error

    if not isinstance(payload, dict):
        raise MalformedResponse("The model's reply was not a JSON object.")

    answerable = payload.get("answerable")
    sql = payload.get("sql")
    explanation = payload.get("explanation")

    # `is True` rather than a truthiness test, deliberately: the string "true" and the number 1
    # are not the discriminator, they are a malformed reply.
    if answerable is True:
        if not isinstance(sql, str) or not sql.strip():
            raise MalformedResponse(
                "The model said the question was answerable but returned no SQL."
            )
        return SqlResponse(answerable=True, sql=sql.strip(), explanation=None)

    if answerable is False:
        if not isinstance(explanation, str) or not explanation.strip():
            raise MalformedResponse(
                "The model declined the question but gave no reason."
            )
        return SqlResponse(answerable=False, sql=None, explanation=explanation.strip())

    raise MalformedResponse("The model's reply had no usable `answerable` flag.")


def parse_chart_response(text):
    """Turn the chart call's reply into a Vega-Lite spec, or raise MalformedResponse.

    Not schema-constrained on the way out (spec §7), so the shape is checked here on arrival.

    A surrounding code fence is stripped. That is unwrapping the transport, not editing the
    document: nothing here reaches INTO the specification, which is the thing spec §7 forbids.
    The alternative — treating a fenced reply as malformed — costs the user a whole extra
    round trip for a formatting habit, and the prompt already asks for no fence.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Drop the opening fence (with or without a language tag) and a closing fence.
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)

    try:
        spec = json.loads(stripped)
    except (TypeError, ValueError) as error:
        raise MalformedResponse("The chart reply was not valid JSON.") from error

    if not isinstance(spec, dict):
        raise MalformedResponse("The chart reply was not a JSON object.")
    if "data" in spec:
        # The application supplies the rows at render time (spec §7). A spec carrying data
        # would put the result set back into the conversation, which is the whole thing this
        # design avoids.
        raise MalformedResponse("The chart specification contained data, which it must not.")
    if "mark" not in spec:
        raise MalformedResponse("The chart specification had no mark.")
    if not isinstance(spec.get("encoding"), dict):
        raise MalformedResponse("The chart specification had no encoding.")

    return spec
```

### 4. Error sanitisation

**File:** `src/askcrossroads/llm.py`

```python
def _sanitise(error):
    """One fixed, user-safe sentence per class of provider failure (spec §9).

    Nothing from the provider's response body is relayed, and nothing here can contain the API
    key: every returned string is a literal written in this file. That is the point — a message
    assembled from the provider's own text is one upstream change away from carrying something
    that should not reach a browser.
    """
    if isinstance(error, anthropic.AuthenticationError):
        return (
            "The Anthropic API rejected the configured key. Check "
            f"{config.ENV_API_KEY} in your environment or .env file."
        )
    if isinstance(error, anthropic.PermissionDeniedError):
        return "The Anthropic API key does not have permission for this request."
    if isinstance(error, anthropic.RateLimitError):
        return "The Anthropic API is rate limiting this key. Wait a moment and try again."
    if isinstance(error, anthropic.BadRequestError):
        return "The Anthropic API rejected the request as malformed."
    if isinstance(error, anthropic.APITimeoutError):
        return "The Anthropic API did not respond in time."
    if isinstance(error, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API. Check your network connection."
    if isinstance(error, anthropic.APIStatusError):
        return f"The Anthropic API returned an error (HTTP {error.status_code})."
    return "The Anthropic API call failed."
```

Order matters: the specific subclasses are tested before `APIStatusError` and `APIConnectionError`,
which are their bases.

### 5. The protocol and the Anthropic implementation

**File:** `src/askcrossroads/llm.py`

```python
class ModelClient(Protocol):
    """What the rest of the application needs a model to do (spec §9).

    Anthropic is the ONLY supported provider in v1. This is an internal seam so that a second
    provider could be added later without touching call sites — it is not a feature, and v1
    must not be described as provider-agnostic.

    Its more immediate value is testing: everything downstream takes a ModelClient rather than
    reaching for a module function, so an offline test hands over a scripted stand-in and can
    assert on what was asked, in what order, and how many times. Spec §11.4 requires exactly
    those assertions of the repair loops.
    """

    def generate_sql(self, context, question) -> SqlResponse: ...

    def describe_chart(self, question, sql, shape) -> dict: ...


class AnthropicClient:
    """The one real implementation (spec §9).

    The API key is held in memory for the session only, inside the SDK client, and is never
    stored as an attribute of this object, never logged, never printed, never put in an
    exception message, and never returned to a caller.
    """

    def __init__(self, api_key):
        self._client = anthropic.Anthropic(api_key=api_key)

    @classmethod
    def from_config(cls, settings):
        """Build a client from the loaded Config."""
        return cls(settings.api_key)

    def generate_sql(self, context, question):
        """Ask for SQL, or for a reason this build cannot answer (spec §6).

        The system blocks carry the cacheable prefix; the question is the only volatile
        content and goes in `messages`, after the cache breakpoint.
        """
        response = self._send(
            system=prompt.sql_system_blocks(context),
            messages=[{"role": "user", "content": question}],
            output_config={
                "format": {"type": "json_schema", "schema": SQL_RESPONSE_SCHEMA}
            },
        )
        return parse_sql_response(_text(response))

    def describe_chart(self, question, sql, shape):
        """Ask for a Vega-Lite specification containing no data (spec §7).

        No output schema: a strict schema cannot describe an arbitrary Vega-Lite document.
        """
        response = self._send(
            system=prompt.CHART_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": prompt.chart_user_message(question, sql, shape),
                }
            ],
        )
        return parse_chart_response(_text(response))

    def _send(self, *, system, messages, output_config=None):
        """One request. Returns the raw response; raises ModelError for anything unusable.

        Only the parameters spec §8 allows are sent. No thinking configuration, no
        temperature, no effort: the model runs on API defaults, and MAX_TOKENS caps thinking
        and response together.
        """
        arguments = {
            "model": config.MODEL,
            "max_tokens": config.MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if output_config is not None:
            arguments["output_config"] = output_config

        try:
            response = self._client.messages.create(**arguments)
        except anthropic.AnthropicError as error:
            # Chained, so a developer's traceback keeps the original; str(ModelError) does not.
            raise ModelError(_sanitise(error)) from error

        if response.stop_reason == "refusal":
            # A provider policy decline. Entirely distinct from `answerable: false`, which is
            # this application's own successful outcome. Not repairable: an identical prompt
            # would be declined identically, so it must not consume a repair attempt.
            raise ModelError("The model declined to respond to this request.")

        if response.stop_reason == "max_tokens":
            # The reply was cut off mid-JSON. Repairable, so MalformedResponse rather than
            # ModelError.
            raise MalformedResponse("The model's reply was cut off before it finished.")

        return response
```

### 6. The scripted test double

**File:** `tests/conftest.py` — append after the existing fixtures.

```python
class FakeModel:
    """A scripted stand-in for a real model client (spec §11.1).

    The default suite must be offline and deterministic, and the repair loops in a later plan
    can only be tested against replies chosen in advance. This satisfies llm.ModelClient and
    returns whatever was queued, in order.

    Queue an Exception instead of a response and it is raised — which is how a test exercises
    a malformed reply or a provider failure without a network.

    `calls` records every call in order, so a test can assert that the chart call was never
    made before the SQL succeeded (spec §11.4).
    """

    def __init__(self, sql_responses=(), chart_responses=()):
        self.sql_responses = list(sql_responses)
        self.chart_responses = list(chart_responses)
        self.calls = []

    def generate_sql(self, context, question):
        self.calls.append(("generate_sql", question))
        return self._next(self.sql_responses, "generate_sql")

    def describe_chart(self, question, sql, shape):
        self.calls.append(("describe_chart", question, sql, shape))
        return self._next(self.chart_responses, "describe_chart")

    @staticmethod
    def _next(queue, name):
        if not queue:
            raise AssertionError(
                f"{name} was called more times than this test queued replies for"
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_model():
    """A FakeModel factory: fake_model(sql_responses=[...], chart_responses=[...])."""
    return FakeModel
```

### 7. Verify against the real API, once, by hand

Before writing the integration tests, confirm the schema is accepted and the shape of what comes
back. This costs one call (roughly 12 US cents at Opus 5 input rates for a ~23k-token prompt).

```bash
ASKCROSSROADS_DB="$ASKCROSSROADS_TEST_DB" .venv/bin/python -c "
from askcrossroads import config, llm
from askcrossroads.database import Database
settings = config.load_config()
with Database.open(settings.database_path) as db:
    client = llm.AnthropicClient.from_config(settings)
    result = client.generate_sql(db.context, 'How many records does this database hold in total?')
print(result)
"
```

Expected: a `SqlResponse` with `answerable=True` and a `SELECT`. If instead you get a `ModelError`
saying the request was malformed, the nullable schema was rejected — apply the fallback documented
in step 2 and note it in your report.

---

## Testing & Verification

### Unit tests — the parsers (offline, no SDK, no network)

**File:** `tests/test_llm.py` — new.

Most of the risk in this module is in parsing, and parsing is a pure function of a string. Test it
directly.

| Test | Input | Assertion |
|---|---|---|
| `test_answerable_response_parses` | `'{"answerable": true, "sql": "SELECT 1", "explanation": null}'` | `SqlResponse(True, "SELECT 1", None)` |
| `test_decline_response_parses` | `'{"answerable": false, "sql": null, "explanation": "No data for 2019."}'` | `answerable is False`, explanation carried |
| `test_sql_is_stripped` | sql with surrounding whitespace | the stored sql has none |
| `test_answerable_true_with_no_sql_is_malformed` (spec §11.4) | `'{"answerable": true, "sql": null, "explanation": null}'` | raises `MalformedResponse` |
| `test_answerable_true_with_empty_sql_is_malformed` | `sql: "   "` | raises `MalformedResponse` |
| `test_decline_with_no_explanation_is_malformed` | `'{"answerable": false, ...}'`, explanation null | raises `MalformedResponse` |
| `test_unparseable_json_is_malformed` (spec §11.4) | `"not json at all"` | raises `MalformedResponse` |
| `test_non_object_json_is_malformed` | `"[1, 2, 3]"` | raises `MalformedResponse` |
| `test_string_true_is_not_the_discriminator` | `'{"answerable": "true", ...}'` | raises `MalformedResponse` |
| `test_chart_spec_parses` | a minimal `{"mark": "bar", "encoding": {...}}` | returns the dict unchanged |
| `test_chart_spec_in_a_code_fence_parses` | the same wrapped in ` ```json ` … ` ``` ` | returns the same dict |
| `test_chart_spec_with_data_is_malformed` (spec §7) | spec carrying a `data` key | raises `MalformedResponse` |
| `test_chart_spec_without_mark_is_malformed` | no `mark` | raises `MalformedResponse` |
| `test_chart_spec_without_encoding_is_malformed` | no `encoding`, and `encoding` as a string | raises `MalformedResponse` in both cases |
| `test_chart_spec_is_returned_unedited` | a spec with extra keys | the returned dict is `==` to the input |

### Unit tests — Trap 2 and the response envelope

Use tiny stand-ins for the SDK's block and response objects:

```python
class _Block:
    def __init__(self, type, text=None):
        self.type = type
        self.text = text


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
```

| Test | Assertion |
|---|---|
| `test_text_is_found_after_a_thinking_block` | `_Response([_Block("thinking"), _Block("text", '{"answerable": …}')])` parses correctly |
| `test_no_text_block_is_malformed` | `_Response([_Block("thinking")])` raises `MalformedResponse` |
| `test_empty_content_is_malformed` | `_Response([])` raises `MalformedResponse` |

### Unit tests — the client, with a stubbed SDK

Build a real `AnthropicClient("test-key-not-a-real-credential")` and replace its `_client` with a
stub whose `messages.create(**kwargs)` records the arguments and returns a canned `_Response`.
Reaching into one private attribute in a test is deliberate: it keeps a testing hook out of the
production class.

| Test | Assertion |
|---|---|
| `test_generate_sql_sends_the_configured_model_and_bound` | recorded `model == config.MODEL`, `max_tokens == config.MAX_TOKENS` |
| `test_generate_sql_sends_the_system_blocks_with_the_breakpoint` | recorded `system == prompt.sql_system_blocks(ctx)` |
| `test_generate_sql_puts_the_question_in_messages_only` | the question is in `messages`, and `json.dumps(recorded["system"])` does not contain it |
| `test_generate_sql_requests_the_output_schema` | recorded `output_config["format"]["schema"] == llm.SQL_RESPONSE_SCHEMA` |
| `test_generate_sql_sends_no_extra_parameters` (spec §8) | the recorded keys are exactly `{model, max_tokens, system, messages, output_config}` — no `thinking`, `temperature`, `output_config.effort`, `speed` or `betas` |
| `test_describe_chart_sends_no_output_schema` (spec §7) | `output_config` is absent from the recorded arguments |
| `test_describe_chart_sends_the_chart_system_prompt` | recorded `system == prompt.CHART_SYSTEM_PROMPT` |
| `test_a_refusal_is_a_model_error` | `stop_reason="refusal"` raises `ModelError`, **not** `MalformedResponse` |
| `test_a_truncated_reply_is_malformed` | `stop_reason="max_tokens"` raises `MalformedResponse` |

### Unit tests — the key never leaks

```python
PLACEHOLDER = "test-key-not-a-real-credential"


def test_provider_errors_are_sanitised_and_never_carry_the_key():
    """Spec §9: provider errors are sanitised before they reach the browser."""
    client = llm.AnthropicClient(PLACEHOLDER)
    for failure in _every_provider_failure():        # one instance per class in _sanitise
        client._client = _StubSdk(raises=failure)
        with pytest.raises(llm.ModelError) as caught:
            client.generate_sql(context, "anything")
        assert PLACEHOLDER not in str(caught.value)
        assert PLACEHOLDER not in repr(caught.value)


def test_the_client_does_not_repr_the_key():
    client = llm.AnthropicClient(PLACEHOLDER)
    assert PLACEHOLDER not in repr(client)
    assert PLACEHOLDER not in str(vars(client))
```

Constructing SDK exceptions takes arguments; check each class's signature in the SDK rather than
guessing, and use the simplest construction that works. `APIConnectionError` and `APITimeoutError`
take a `request`; the status errors take `response` and `body`. If constructing one is awkward,
subclass it locally in the test with a no-argument `__init__` — the code under test only does
`isinstance` checks.

### Unit tests — the test double satisfies the seam

| Test | Assertion |
|---|---|
| `test_fake_model_returns_queued_responses_in_order` | two queued replies come back in order |
| `test_fake_model_raises_a_queued_exception` | a queued `MalformedResponse` is raised |
| `test_fake_model_records_its_calls` | `calls` shows the question and, separately, the chart arguments |
| `test_fake_model_fails_loudly_when_over_called` | a third call raises `AssertionError` |
| `test_fake_model_satisfies_the_protocol` | `isinstance(FakeModel(), llm.ModelClient)` — decorate `ModelClient` with `@runtime_checkable` if you want this to work at runtime; otherwise assert the method names and signatures match |

### Integration tests (PRIMARY for what offline testing cannot reach)

**File:** `tests/integration/test_prompt_and_model.py` — new.

```python
pytestmark = pytest.mark.integration
```

Skips, never fails, when `ANTHROPIC_API_KEY` or `ASKCROSSROADS_TEST_DB` is absent (spec §11.2).
Copy the skip-fixture pattern from `tests/integration/test_conventions.py`, but build a `Database`
via `Database.open(path)` rather than a bare connection, so the context comes through the real path.

**Cost:** roughly four real calls at ~23k input tokens each. At Opus 5 rates that is on the order of
**25–40 US cents per full run**, less once the cache is warm. It is never part of the default run.

| Test | What it proves | Assertion |
|---|---|---|
| `test_a_real_question_produces_executable_sql` | The whole thing works end to end, and the nullable schema is accepted by the live API | Ask *"How many records does this database hold in total?"* — a question any valid build can answer, chosen so the test knows nothing about the schema. Assert `answerable is True`; assert `safety.validate_sql(result.sql)` allows it; execute it on the read-only connection and assert at least one row comes back. |
| `test_an_unanswerable_question_is_declined` | The decline path is real, not just plumbing (spec §11.2) | Ask about something no road-safety build contains. Assert `answerable is False` and `explanation` is non-empty. **Assert on shape, never on wording.** |
| `test_the_second_call_registers_a_cache_read` | **The one failure no offline test can detect** (spec §11.2) | Call `client._send(system=prompt.sql_system_blocks(ctx), messages=[…])` twice with two *different* questions. Assert the second response's `usage.cache_read_input_tokens > 0`. Reaching into `_send` is deliberate: the public API does not expose usage, and testing through the real request path is the point. |
| `test_the_prefix_is_the_expected_size` | The ~23k estimate becomes a measured number | `client._client.messages.count_tokens(model=config.MODEL, system=prompt.sql_system_blocks(ctx), messages=[{"role": "user", "content": "x"}])`. Print the figure. Assert only a loose sanity range (above 512, the model's minimum cacheable prefix, and below 200,000) — a tight assertion would fail on a legitimately different build. |

Give `test_an_unanswerable_question_is_declined` a docstring saying plainly that the model is not
deterministic (spec §8), so a single failure is a signal about the prompt rather than proof of a
bug — the same rule spec §11.2 sets out for the answer set.

### Runnable commands

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

```bash
.venv/bin/python -m pytest -m integration -v
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_TEST_DB .venv/bin/python -m pytest -m integration -v
```

The third command must report **skips**, not failures — that is the check that a contributor without
a key gets a clean run.

### Stage ship-readiness checklist

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
2. `.venv/bin/python -m pytest -m integration -v` passes with a key and a real build present.
3. With both unset, `-m integration` skips cleanly and reports no failures.
4. `grep -rn "content\[0\]" src/askcrossroads/llm.py` returns nothing.
5. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
6. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
7. `grep -rnE "thinking|temperature|effort|betas|speed" src/askcrossroads/llm.py` returns only the
   comment explaining why none of them is sent.
8. `git status` shows `llm.py`, `tests/conftest.py` modified and `tests/test_llm.py`,
   `tests/integration/test_prompt_and_model.py` untracked — and **nothing staged**.

---

## End State / Handoff

After this stage the plan is complete. `llm.py` exports:

| Name | What it is |
|---|---|
| `ModelClient` | the protocol every call site depends on |
| `AnthropicClient` / `.from_config(settings)` | the one real implementation |
| `SqlResponse` | the frozen result of the SQL call |
| `SQL_RESPONSE_SCHEMA` | the structured-output schema |
| `parse_sql_response` / `parse_chart_response` | the parsers, usable without a client |
| `ModelError` | a sanitised, **non**-repairable provider failure |
| `MalformedResponse` | a **repairable** failed attempt |

`tests/conftest.py` exports `FakeModel` and the `fake_model` fixture.

The next plan may assume: it is handed a `ModelClient`; `generate_sql` either returns a
`SqlResponse` or raises one of the two exceptions above; `MalformedResponse` is what its repair loop
counts against `config.MAX_ATTEMPTS` and `ModelError` is what it does not retry; a decline consumes
no attempt; and `describe_chart` returns a data-free Vega-Lite dict ready to hand to the frontend.

Nothing calls a model yet. `askcrossroads` still opens the database, prints its provenance, and
exits 0.

---

## Failure Modes & Rollback

| Failure | Why it happens | Guardrail |
|---|---|---|
| Works against mocks, breaks against the real API | `response.content[0]` — a thinking block comes first on this model | `test_text_is_found_after_a_thinking_block`; checklist item 4. |
| The live API rejects the request as malformed | The nullable schema form is not accepted | Step 7's single manual call finds it before the tests are written; the fallback is documented in step 2. |
| Every question pays full price, silently | Something volatile crept in before the breakpoint, or the system blocks are not sent as built | `test_the_second_call_registers_a_cache_read`. Nothing else in the project can catch this. |
| The API key reaches a browser | A provider message relayed verbatim, or a repr of the client | `_sanitise` returns only literals written in this file; two dedicated tests; the ship-readiness grep. |
| A policy refusal burns all three repair attempts | Treating `stop_reason == "refusal"` as malformed | `test_a_refusal_is_a_model_error`. |
| The integration suite fails a contributor with no key | A missing skip guard | Command three above must show skips. |
| An intermittent integration failure gets muted | Asserting on generated prose (spec §11.2) | Assert on shape and execution only; the one declining test says so in its docstring. |

**Rollback:** `git checkout -- src/askcrossroads/llm.py tests/conftest.py` restores both tracked
files. Delete `tests/test_llm.py` and `tests/integration/test_prompt_and_model.py`. Stages 01 and 02
are independent and are not affected. No commit is made at any point.
