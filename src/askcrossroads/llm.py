"""Thin provider interface and its Anthropic implementation (spec §9).

prompt.py decides WHAT is said to the model; this module decides HOW it is said and what
comes back. It defines the seam the rest of the application depends on (ModelClient), the
one real implementation of it (AnthropicClient), the structured-response contract for the
SQL call, and the two ways a call can fail:

- MalformedResponse — the model replied, and the reply was unusable. A failed attempt the
  caller can repair.
- ModelError — the provider could not be reached or refused outright. Not repairable.

This module never opens a database connection; it is handed the already-built
DatabaseContext.
"""

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import anthropic

from askcrossroads import config, prompt


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


# --- The structured-response contract (spec §6) -----------------------------------------

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


# --- Reading a reply -------------------------------------------------------------------


def _text(response):
    """The reply's text, whatever else the response carries.

    `content` is a LIST OF BLOCKS, and on this model a thinking block generally comes first,
    because adaptive thinking is on by default. Reading the first block's text works against
    a hand-written mock and fails against the real API. Select by type, always.
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


# --- Provider failures -----------------------------------------------------------------


def _sanitise(error):
    """One fixed, user-safe sentence per class of provider failure (spec §9).

    Nothing from the provider's response body is relayed, and nothing here can contain the API
    key: every returned string is a literal written in this file. That is the point — a message
    assembled from the provider's own text is one upstream change away from carrying something
    that should not reach a browser.

    Order matters: the specific subclasses are tested before APIStatusError and
    APIConnectionError, which are their bases.
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


# --- The provider seam (spec §9) -------------------------------------------------------


@runtime_checkable
class ModelClient(Protocol):
    """What the rest of the application needs a model to do (spec §9).

    Anthropic is the ONLY supported provider in v1. This is an internal seam so that a second
    provider could be added later without touching call sites — it is not a feature, and v1
    must not be described as provider-agnostic.

    Its more immediate value is testing: everything downstream takes a ModelClient rather than
    reaching for a module function, so an offline test hands over a scripted stand-in and can
    assert on what was asked, in what order, and how many times. Spec §11.4 requires exactly
    those assertions of the repair loops.

    `failures` is a tuple of (attempted, error) pairs, earlier failed attempts at this same
    question, oldest first. It is empty on a first attempt. Spec §4 requires the error to be
    fed back so the model can correct it.
    """

    def generate_sql(self, context, question, failures=()) -> SqlResponse: ...

    def describe_chart(self, question, sql, shape, failures=()) -> dict: ...


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

    def generate_sql(self, context, question, failures=()):
        """Ask for SQL, or for a reason this build cannot answer (spec §6).

        The system blocks carry the cacheable prefix; the question, and any earlier failed
        attempts, are the only volatile content and go in `messages`, after the cache
        breakpoint.
        """
        response = self._send(
            system=prompt.sql_system_blocks(context),
            messages=[
                {"role": "user", "content": prompt.sql_user_message(question, failures)}
            ],
            output_config={
                "format": {"type": "json_schema", "schema": SQL_RESPONSE_SCHEMA}
            },
        )
        return parse_sql_response(_text(response))

    def describe_chart(self, question, sql, shape, failures=()):
        """Ask for a Vega-Lite specification containing no data (spec §7).

        No output schema: a strict schema cannot describe an arbitrary Vega-Lite document.
        """
        response = self._send(
            system=prompt.CHART_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": prompt.chart_user_message(question, sql, shape, failures),
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
