"""The model client: the response contract, sanitised errors, and the test double (spec §6, §9).

Everything here is offline. The SDK's network call is replaced by a stub that records what
it was asked and returns a canned reply, so these tests prove what is SENT and how replies
are READ — never that the live API agrees. That is the integration suite's job.
"""

import inspect
import json

import anthropic
import httpx2
import pytest

from askcrossroads import config, llm, prompt
from askcrossroads.database import Database

from conftest import FakeModel

# The only key-shaped value any test may use. Spec and CLAUDE.md: nothing resembling a real
# credential may appear anywhere in the repository.
PLACEHOLDER = "test-key-not-a-real-credential"

QUESTION = "How many records are there per year?"


@pytest.fixture
def context(crossroads_db):
    with Database.open(crossroads_db) as database:
        yield database.context


# --- Tiny stand-ins for the SDK's response objects ---------------------------------------


class _Block:
    def __init__(self, type, text=None):
        self.type = type
        self.text = text


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _StubSdk:
    """Stands in for anthropic.Anthropic: records each create() call's arguments, then either
    raises `raises` or returns `response`."""

    def __init__(self, response=None, raises=None):
        self.recorded = []
        self._response = response
        self._raises = raises
        self.messages = self

    def create(self, **arguments):
        self.recorded.append(arguments)
        if self._raises is not None:
            raise self._raises
        return self._response


def _answer(sql="SELECT 1"):
    return _Response([
        _Block("thinking"),
        _Block("text", json.dumps({"answerable": True, "sql": sql, "explanation": None})),
    ])


CHART = {"mark": "bar", "encoding": {"x": {"field": "category"}, "y": {"field": "total"}}}


def _client(response=None, raises=None):
    """A real AnthropicClient whose network layer is a stub.

    Reaching into one private attribute is deliberate: it keeps a testing hook out of the
    production class.
    """
    client = llm.AnthropicClient(PLACEHOLDER)
    client._client = _StubSdk(response=response, raises=raises)
    return client


def _shape():
    return prompt.ResultShape(
        columns=(("category", "VARCHAR"), ("total", "BIGINT")),
        row_count=2,
        distinct_counts=(("category", 2),),
        sample_rows=(("Alpha", 3), ("Beta", 1)),
    )


# --- Parsing the SQL call's reply (spec §6, §11.4) ---------------------------------------


def test_answerable_response_parses():
    result = llm.parse_sql_response(
        '{"answerable": true, "sql": "SELECT 1", "explanation": null}'
    )
    assert result == llm.SqlResponse(True, "SELECT 1", None)


def test_decline_response_parses():
    result = llm.parse_sql_response(
        '{"answerable": false, "sql": null, "explanation": "No data for 2019."}'
    )
    assert result.answerable is False
    assert result.sql is None
    assert result.explanation == "No data for 2019."


def test_sql_is_stripped():
    result = llm.parse_sql_response(
        '{"answerable": true, "sql": "\\n  SELECT 1  \\n", "explanation": null}'
    )
    assert result.sql == "SELECT 1"


def test_answerable_true_with_no_sql_is_malformed():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_sql_response('{"answerable": true, "sql": null, "explanation": null}')


def test_answerable_true_with_empty_sql_is_malformed():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_sql_response('{"answerable": true, "sql": "   ", "explanation": null}')


def test_decline_with_no_explanation_is_malformed():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_sql_response('{"answerable": false, "sql": null, "explanation": null}')


def test_unparseable_json_is_malformed():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_sql_response("not json at all")


def test_non_object_json_is_malformed():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_sql_response("[1, 2, 3]")


def test_string_true_is_not_the_discriminator():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_sql_response(
            '{"answerable": "true", "sql": "SELECT 1", "explanation": null}'
        )


# --- Parsing the chart call's reply (spec §7) -------------------------------------------


def test_chart_spec_parses():
    assert llm.parse_chart_response(json.dumps(CHART)) == CHART


def test_chart_spec_in_a_code_fence_parses():
    fenced = "```json\n" + json.dumps(CHART, indent=2) + "\n```"
    assert llm.parse_chart_response(fenced) == CHART


def test_chart_spec_with_data_is_malformed():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_chart_response(json.dumps({**CHART, "data": {"values": [{"a": 1}]}}))


def test_chart_spec_without_mark_is_malformed():
    with pytest.raises(llm.MalformedResponse):
        llm.parse_chart_response(json.dumps({"encoding": CHART["encoding"]}))


@pytest.mark.parametrize("spec", [{"mark": "bar"}, {"mark": "bar", "encoding": "x"}])
def test_chart_spec_without_encoding_is_malformed(spec):
    with pytest.raises(llm.MalformedResponse):
        llm.parse_chart_response(json.dumps(spec))


def test_chart_spec_is_returned_unedited():
    spec = {**CHART, "title": "Totals", "width": "container", "config": {"view": {}}}
    assert llm.parse_chart_response(json.dumps(spec)) == spec


# --- The response envelope: a thinking block comes first ---------------------------------


def test_text_is_found_after_a_thinking_block(context):
    result = _client(response=_answer("SELECT 2")).generate_sql(context, QUESTION)
    assert result == llm.SqlResponse(True, "SELECT 2", None)


def test_no_text_block_is_malformed(context):
    client = _client(response=_Response([_Block("thinking")]))
    with pytest.raises(llm.MalformedResponse):
        client.generate_sql(context, QUESTION)


def test_empty_content_is_malformed(context):
    with pytest.raises(llm.MalformedResponse):
        _client(response=_Response([])).generate_sql(context, QUESTION)


# --- What the client sends (spec §3, §6, §7, §8) -----------------------------------------


def _sent_for_sql(context):
    client = _client(response=_answer())
    client.generate_sql(context, QUESTION)
    return client._client.recorded[0]


def _sent_for_chart():
    client = _client(response=_Response([_Block("text", json.dumps(CHART))]))
    client.describe_chart(QUESTION, "SELECT 1", _shape())
    return client._client.recorded[0]


def test_generate_sql_sends_the_configured_model_and_bound(context):
    sent = _sent_for_sql(context)
    assert sent["model"] == config.MODEL
    assert sent["max_tokens"] == config.MAX_TOKENS


def test_generate_sql_sends_the_system_blocks_with_the_breakpoint(context):
    assert _sent_for_sql(context)["system"] == prompt.sql_system_blocks(context)


def test_generate_sql_puts_the_question_in_messages_only(context):
    sent = _sent_for_sql(context)
    assert sent["messages"] == [{"role": "user", "content": QUESTION}]
    assert QUESTION not in json.dumps(sent["system"])


def test_generate_sql_requests_the_output_schema(context):
    sent = _sent_for_sql(context)
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"] == llm.SQL_RESPONSE_SCHEMA


def test_generate_sql_sends_no_extra_parameters(context):
    """Spec §8: the model runs on API defaults. No thinking, temperature, effort, speed or
    betas."""
    sent = _sent_for_sql(context)
    assert set(sent) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert set(sent["output_config"]) == {"format"}


def test_describe_chart_sends_no_output_schema():
    assert "output_config" not in _sent_for_chart()


def test_describe_chart_sends_the_chart_system_prompt():
    sent = _sent_for_chart()
    assert sent["system"] == prompt.CHART_SYSTEM_PROMPT
    assert sent["messages"] == [
        {
            "role": "user",
            "content": prompt.chart_user_message(QUESTION, "SELECT 1", _shape()),
        }
    ]


def test_describe_chart_returns_the_parsed_spec():
    client = _client(response=_Response([_Block("thinking"), _Block("text", json.dumps(CHART))]))
    assert client.describe_chart(QUESTION, "SELECT 1", _shape()) == CHART


def test_a_refusal_is_a_model_error(context):
    """A policy decline must not consume a repair attempt, so it is not MalformedResponse."""
    client = _client(response=_Response([], stop_reason="refusal"))
    with pytest.raises(llm.ModelError):
        client.generate_sql(context, QUESTION)


def test_a_truncated_reply_is_malformed(context):
    client = _client(response=_Response([_Block("text", '{"answerable": tr')], "max_tokens"))
    with pytest.raises(llm.MalformedResponse):
        client.generate_sql(context, QUESTION)


# --- The key never leaks (spec §9) ------------------------------------------------------


def _every_provider_failure():
    """One real SDK exception per branch of llm._sanitise.

    Each carries the placeholder key in its own message and body, as a provider error
    echoing a request back might. The test then proves none of it reaches the ModelError.
    """
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    leaky = f"something went wrong for {PLACEHOLDER}"

    def status(cls, code):
        response = httpx2.Response(code, request=request)
        return cls(leaky, response=response, body={"error": {"message": leaky}})

    return [
        status(anthropic.AuthenticationError, 401),
        status(anthropic.PermissionDeniedError, 403),
        status(anthropic.RateLimitError, 429),
        status(anthropic.BadRequestError, 400),
        status(anthropic.InternalServerError, 500),
        anthropic.APITimeoutError(request=request),
        anthropic.APIConnectionError(message=leaky, request=request),
        anthropic.AnthropicError(leaky),
    ]


def test_provider_errors_are_sanitised_and_never_carry_the_key(context):
    """Spec §9: provider errors are sanitised before they reach the browser."""
    for failure in _every_provider_failure():
        client = _client(raises=failure)
        with pytest.raises(llm.ModelError) as caught:
            client.generate_sql(context, QUESTION)
        assert PLACEHOLDER not in str(caught.value), type(failure).__name__
        assert PLACEHOLDER not in repr(caught.value), type(failure).__name__
        # The original is chained for a developer's traceback, not folded into the message.
        assert caught.value.__cause__ is failure


def test_each_failure_class_gets_its_own_message(context):
    messages = set()
    for failure in _every_provider_failure():
        with pytest.raises(llm.ModelError) as caught:
            _client(raises=failure).generate_sql(context, QUESTION)
        messages.add(str(caught.value))
    assert len(messages) == len(_every_provider_failure())


def test_the_client_does_not_repr_the_key():
    client = llm.AnthropicClient(PLACEHOLDER)
    assert PLACEHOLDER not in repr(client)
    assert PLACEHOLDER not in str(vars(client))


def test_from_config_uses_the_configured_key(tmp_path):
    settings = config.Config(api_key=PLACEHOLDER, database_path=tmp_path)
    client = llm.AnthropicClient.from_config(settings)
    assert isinstance(client, llm.AnthropicClient)
    assert PLACEHOLDER not in repr(client)


# --- The test double satisfies the seam --------------------------------------------------


def test_fake_model_returns_queued_responses_in_order():
    first = llm.SqlResponse(True, "SELECT 1", None)
    second = llm.SqlResponse(False, None, "Not in this build.")
    model = FakeModel(sql_responses=[first, second])
    assert model.generate_sql(None, "a") is first
    assert model.generate_sql(None, "b") is second


def test_fake_model_raises_a_queued_exception():
    model = FakeModel(sql_responses=[llm.MalformedResponse("bad")])
    with pytest.raises(llm.MalformedResponse):
        model.generate_sql(None, QUESTION)


def test_fake_model_records_its_calls():
    model = FakeModel(
        sql_responses=[llm.SqlResponse(True, "SELECT 1", None)],
        chart_responses=[CHART],
    )
    shape = _shape()
    model.generate_sql(None, QUESTION)
    model.describe_chart(QUESTION, "SELECT 1", shape)
    assert model.calls == [
        ("generate_sql", QUESTION),
        ("describe_chart", QUESTION, "SELECT 1", shape),
    ]


def test_fake_model_fails_loudly_when_over_called():
    model = FakeModel(sql_responses=[llm.SqlResponse(True, "SELECT 1", None)] * 2)
    model.generate_sql(None, "a")
    model.generate_sql(None, "b")
    with pytest.raises(AssertionError):
        model.generate_sql(None, "c")


def test_fake_model_fixture_is_the_factory(fake_model):
    assert fake_model is FakeModel


def test_fake_model_satisfies_the_protocol():
    assert isinstance(FakeModel(), llm.ModelClient)
    assert isinstance(llm.AnthropicClient(PLACEHOLDER), llm.ModelClient)
    # runtime_checkable only checks the names exist, so check the parameters match too.
    for name in ("generate_sql", "describe_chart"):
        expected = list(inspect.signature(getattr(llm.ModelClient, name)).parameters)
        assert list(inspect.signature(getattr(FakeModel, name)).parameters) == expected
