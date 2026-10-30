# Prompt Assembly & Model Interface — Plan Overview

> Multi-stage plan. Each implementation session gets **this overview + ONE stage file**. Read this
> first. Each stage's *End State* is the contract — if a step does not match reality, adapt the step
> and record the deviation, but deliver the End State.

Turn the immutable database context into a byte-stable, cacheable prompt prefix, write the two
system prompts, and put a thin, sanitised Anthropic client behind them.

---

## Context & Objective

### What exists today

`docs/plans/001_project_foundation/`, `002_database_layer/` and `003_safety_validator/` are
complete. The repository opens the user's database, refuses it usefully if it is not a Crossroads-UK
build, discovers everything about it at runtime, and can judge whether one string of SQL is safe to
execute. Nothing yet talks to a model.

```text
AskCrossroads/
├── pyproject.toml                  # anthropic>=0.40 already a dependency; `integration` marker
├── .env.example
├── docs/
│   ├── spec.md                     # the authoritative product definition
│   └── plans/
│       ├── 001_project_foundation/ # complete
│       ├── 002_database_layer/     # complete
│       ├── 003_safety_validator/   # complete
│       └── 004_prompt_and_model/   # this plan
├── src/askcrossroads/
│   ├── __init__.py
│   ├── app.py                      # main(): config → open → validate → print → exit
│   ├── config.py                   # MODEL, MAX_TOKENS, MAX_ATTEMPTS, Config(api_key, database_path)
│   ├── database.py                 # Database, DatabaseContext, Provenance, CONVENTION_TABLES
│   ├── introspection.py            # Relation, Column, CodebookEntry, ManifestEntry, TemporalRange
│   ├── safety.py                   # validate_sql() → Verdict
│   ├── prompt.py                   # DOCSTRING ONLY — this plan fills it in
│   ├── llm.py                      # DOCSTRING ONLY — this plan fills it in
│   ├── query.py                    # docstring only, untouched here
│   └── static/                     # empty
└── tests/
    ├── conftest.py                 # the synthetic Crossroads-UK database factory
    ├── test_package.py, test_config.py, test_fixtures.py
    ├── test_database.py, test_introspection.py, test_safety.py
    └── integration/test_conventions.py
```

Four facts from the earlier plans that this one builds on directly. **Do not re-derive them.**

- **`DatabaseContext` is frozen, tuple-only, built once, and already deterministically ordered.**
  `introspection.load_codebook` and `load_column_manifest` sort in Python with an explicit key
  *specifically so this plan's byte-stability guarantee can hold* — see the docstrings in
  `src/askcrossroads/introspection.py`. `discover_relations` returns relations sorted by
  `(schema, name)` with columns in declaration order, and `temporal_ranges` sorted by
  `(relation, column)`. **This plan must iterate those tuples in the order they arrive and must not
  re-sort them.** Re-sorting would be harmless today and would hide the day upstream's order stopped
  being stable.
- **`config.MODEL` is `"claude-opus-5"` and `config.MAX_TOKENS` is `16_000`.** Both are named
  constants; this plan adds no new bound at a call site.
- **`Config.api_key` is `field(repr=False)`** so it cannot appear in a traceback or a debugger dump.
  This plan must not weaken that.
- **`tests/conftest.py` provides `crossroads_db_factory` and `crossroads_db`**, and its
  `DEFAULT_BUILT_AT` is a frozen timestamp with the comment *"Spec §11.4 requires a byte-stable
  prompt prefix, and that test is impossible if the fixture's provenance moves."* The fixture was
  built with this plan in mind.

### What this plan changes

After this plan, the application can turn its database context into a prompt and ask Anthropic a
question — though nothing yet calls it, because deciding *when* to call the model is the next plan's
job. Concretely:

- `src/askcrossroads/prompt.py` — deterministic context serialisation, the two system prompts, the
  system-block assembly that places the cache breakpoint, and the chart call's user message.
- `src/askcrossroads/llm.py` — the `ModelClient` protocol, the `SqlResponse` result type, the
  structured-response JSON schema, `AnthropicClient`, and error sanitisation.
- `tests/conftest.py` — two additions: `build_unconventional_db` / the `unconventional_db` fixture
  (a valid build whose tables are named nothing like a real one), and `FakeModel`, the scripted
  stand-in every later plan's offline tests will use.
- `tests/test_prompt.py`, `tests/test_llm.py` — new.
- `tests/integration/test_prompt_and_model.py` — new, marked `integration`, skipped by default.

Still out of scope after this plan: executing SQL, the repair loops, deciding when to call the
model, the HTTP surface, and the frontend.

---

## Approach / Architecture

### The one-line summary

`prompt.py` decides **what we say**; `llm.py` decides **how we say it and what comes back**.
`prompt.py` imports nothing from `llm.py`, and `llm.py` imports `prompt` and `config` only. Neither
opens a database connection: both take the already-built `DatabaseContext` as an argument.

### Data flow

```text
DatabaseContext (frozen, from plan 002)
        │
        ▼
prompt.serialise_context(context) ──────────► one deterministic string, ~23k tokens
        │                                      (schema DDL, date ranges, manifest,
        │                                       codebook, build parameters)
        ▼
prompt.sql_system_blocks(context) ──────────► [ {text: <context>},
        │                                       {text: SQL_SYSTEM_PROMPT,
        │                                        cache_control: ephemeral} ]
        ▼                                       ▲
llm.AnthropicClient.generate_sql(              └── the cache breakpoint: everything
        context, question)                          before it is stable for this database
        │
        │  messages=[{role: "user", content: question}]   ← the ONLY volatile content
        │  output_config={"format": {"type": "json_schema", "schema": SQL_RESPONSE_SCHEMA}}
        ▼
llm.SqlResponse(answerable=True, sql="SELECT …", explanation=None)
       or SqlResponse(answerable=False, sql=None, explanation="This build contains 2023 only.")
       or raises MalformedResponse  (a failed attempt — the next plan repairs it)
       or raises ModelError         (a sanitised provider failure — not repairable)
```

The chart call is the same machinery with a different, much smaller prompt and **no** output schema
(spec §7: a strict schema cannot describe an arbitrary Vega-Lite document):

```text
prompt.chart_user_message(question, sql, ResultShape(...)) ──► llm.describe_chart(...) ──► dict
```

### Decisions taken, and what was rejected

| Decision | Why | Rejected alternative |
|---|---|---|
| **The whole payload goes in. No relevance filtering.** | Spec §3. A filter fails silently on exactly the questions worth asking — *"how many collisions involved drunk drivers?"* matches no column name and no label text, so the model would get no codes for the column it needs, guess, and return wrong rows instead of an error. ~23k tokens is about 2% of the context window. | Scoring the question's words against column names and label text. Cheaper, and the failure is invisible. |
| **The prefix is serialised as SQL DDL plus pipe-delimited rows**, not JSON and not Markdown tables | Models read `CREATE TABLE` natively; delimited rows are the cheapest per-row form over ~1,130 codebook rows. JSON punctuation on that many rows costs a real share of the budget; Markdown separator rows cost more again, for no model-side gain. | JSON with `sort_keys=True`; Markdown tables. |
| **The cache breakpoint goes on the LAST system block**, with the context first and the instructions second | Both blocks are stable for a given database, so the whole system prompt is cached either way. Putting the instructions last puts them closest to the question, which is where they do the most good. The question lives in `messages`, after everything cached. | The breakpoint between the two blocks. Would cache the context but re-process the instructions on every question, for no benefit. |
| **The database path is NOT in the prefix** | It is stable per database but machine-dependent, tells the model nothing, and would make a prefix built on one machine differ from the same build opened on another. | Including it "for completeness". |
| **The structured response uses a raw JSON schema and `json.loads`**, not `client.messages.parse` with Pydantic | Keeps `src/` free of Pydantic (currently only a transitive FastAPI dependency) and keeps the malformed-response path *ours* — spec §6 requires a disagreeing response to become a repairable failed attempt, and that is easier to guarantee when we own the check. | `messages.parse(output_format=SqlResponse)`. Less parsing code, and it moves the failure path into Pydantic's exceptions. |
| **The schema has three required fields, two of them nullable** — not a JSON-Schema union | Strict structured output requires `additionalProperties: false` and every property in `required`, which rules out a true discriminated union. So the schema guarantees the response *parses*; our code checks the discriminator and the fields agree. That check is not a workaround — spec §6 explicitly requires it. | `anyOf` over two object schemas. Likely rejected by the strict validator, and would remove the check spec §6 asks for. |
| **The chart response is NOT schema-constrained** | Spec §7. A strict schema needs `additionalProperties: false` on every object, which cannot describe an arbitrary Vega-Lite document. The shape is stated in the system message and checked on arrival. | Constraining it. Would refuse valid specs. |
| **`llm.py` exposes a `typing.Protocol`, one real implementation, and a test fake** | Spec §9 asks for "a thin internal interface so a second provider could be added later without touching call sites". More importantly, the next plan's repair-loop and call-ordering tests need to queue responses and inspect what was asked — which a scripted fake does and a monkeypatched module-level function does clumsily. | Plain module functions patched with `monkeypatch`. Less code now; friction in every later test that cares about ordering or call counts. |
| **`serialise_context` is a pure function, rebuilt per call, not cached** | It is deterministic, so rebuilding is harmless, and it is string concatenation over ~2,000 rows — low single-digit milliseconds. Who owns the built prefix is the HTTP plan's question, not this one. | An `lru_cache` keyed on the context. Adds a hash of a large frozen object to every question to save milliseconds. |
| **A provider policy refusal (`stop_reason == "refusal"`) is a `ModelError`, not a failed attempt** | Retrying an identical prompt after a policy decline will decline again, so it must not consume the repair budget. It is also entirely distinct from the application-level `answerable: false`, which is a *successful* outcome (spec §6). | Treating it as a malformed response. Would burn all three attempts on something that cannot succeed. |

### Two traps this plan must not fall into

**Trap 1 — the prompt text is `src/`, and spec §2C binds it.**

> AskCrossroads hardcodes no assumption about the schema beyond `codebook`, `column_manifest`, and
> `crossroads_meta`. […] The rule binds every change, and applies to source, tests, **prompt text**,
> and the frontend alike.

So the SQL system prompt may **not** contain an example query, a table name, a column name, a
dataset name, or a code value. It must also not name the labelled views or the `_label` suffix:
spec §3 is explicit that the instruction is *"phrased by outcome — prefer values a person can read —
never by naming a view or a suffix"*. A build without those views simply does not offer them in the
schema, and the model uses what is there.

Two greps enforce this and both are in the ship-readiness list below.

**Trap 2 — `response.content[0]` is not the answer.**

`config.MODEL` is `claude-opus-5`, and **Opus 5 runs adaptive thinking by default**. The response
content is a list of blocks, and a `thinking` block generally comes first. Code that reads
`response.content[0].text` will work in a hand-written mock and fail against the real API. Always
select the first block whose `.type == "text"`, and treat "no text block at all" as a malformed
response rather than an `IndexError`.

### Model settings: the API defaults, deliberately

Spec §8: *"the model runs on default settings (§10)"*, and `config.py` says *"Everything else is the
API default: no temperature, no thinking configuration."* So requests pass `model`, `max_tokens`,
`system`, `messages`, and (for the SQL call only) `output_config`. **Do not** pass `thinking`,
`temperature`, `output_config.effort`, `speed`, or `betas`. Adaptive thinking is on by default on
this model and `MAX_TOKENS` caps thinking and response together, which is what the constant's
comment already says.

---

## Cross-Cutting Constraints

Every stage obeys all of these.

- **The API key is absolute.** Never hardcode a key, or any credential, in source, tests, docs,
  plans, comments, or commit messages — not even a truncated or obviously fake one. This is the
  first plan whose code actually holds the key, so: the key is read from `Config`, held on the
  client instance, never logged, never printed, never put in an exception message, and never
  returned to a caller. Where a test needs a placeholder, use the literal
  `"test-key-not-a-real-credential"`. Nothing shaped like a real Anthropic key may appear anywhere
  in the repository, in any file, for any reason.
- **Nothing about the schema is hardcoded in `src/`** (spec §2C) beyond `CONVENTION_TABLES` in
  `database.py`. This includes every character of prompt text.
  `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` must return nothing.
- **The prompt names no view and no suffix** (spec §3).
  `grep -rniE "labelled|_label" src/askcrossroads/prompt.py` must return nothing.
- **Every bound is a named constant in `config.py`** (spec §10), never a literal at a call site.
- **The default test suite is offline and deterministic** (spec §11.1). No test in the default run
  may require an API key, a network connection, or a real Crossroads-UK build. Anything that does is
  `@pytest.mark.integration` and **skips**, never fails, when the key or the build is absent
  (spec §11.2). `tests/integration/test_conventions.py` is the pattern to copy.
- **Layout follows the spec §12 blueprint exactly.** No new module. `prompt.py` holds assembly;
  `llm.py` holds the provider interface. `prompt.py` must **never import `llm.py`** — the dependency
  runs one way only.
- **`prompt.py` opens no connection and takes no `Database`.** It takes a `DatabaseContext`, which
  is frozen and already discovered. A prompt function that queried the database would make the
  prefix a function of when it was built, not of what the build contains.
- **Code is simple and readable.** Plain-language comments where they earn their place. The explicit
  ten-line version over the clever two-hundred-line abstraction. Every line must be readable and
  understandable by a person before it is committed.
- **Never stage, commit, or amend anything in git.** Leave all changes in the working tree and tell
  the user what changed. This is absolute and has no exception.
- **Every stage leaves the default `pytest` run green** before the next stage starts.
- **Python 3.11 or newer.** On the development machine the system `python3` is 3.9.6 and will not
  work. Every command assumes the existing virtual environment at `.venv/`.

### The commands every stage uses

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
.venv/bin/python -m pytest
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

---

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Deterministic context serialisation | `prompt.serialise_context(context)` renders provenance, schema DDL, date ranges, the column manifest and the whole codebook into one string, in a fixed order with fixed formatting. Adds the unconventionally-named fixture. | `serialise_context` returns byte-identical output on repeated calls, on two opens of the same file, and on two separately-built files with the same arguments; differs when the build differs; contains no timestamp, no path, and no question. A build whose tables are named nothing like a real one produces a prefix naming **those** tables. `pytest` green. | — | [01-context-serialisation.md](01-context-serialisation.md) |
| 02 | The two system prompts & block assembly | `SQL_SYSTEM_PROMPT`, `CHART_SYSTEM_PROMPT`, `sql_system_blocks(context)` placing the cache breakpoint, `ResultShape`, and `chart_user_message(...)`. | Both prompts state their required outcomes and name no table, column, dataset, code, view or suffix. `sql_system_blocks` returns two text blocks, `cache_control` on the last one only, context first and instructions second, and is equal across calls for one context. `chart_user_message` renders exactly the spec §7 payload. Both greps return nothing. `pytest` green. | 01 | [02-system-prompts.md](02-system-prompts.md) |
| 03 | The model client & the provider seam | `ModelClient` protocol, `SqlResponse`, `SQL_RESPONSE_SCHEMA`, `MalformedResponse`, `ModelError`, `AnthropicClient`, the `FakeModel` test double, and the marked integration suite. | `AnthropicClient` sends the assembled request and returns a `SqlResponse`; a disagreeing or unparseable reply raises `MalformedResponse`; every provider failure becomes a `ModelError` whose message cannot contain the key; `FakeModel` satisfies the protocol and records its calls. The integration suite proves a real call returns usable SQL, the schema is accepted, and the **second** call registers a cache read. `pytest` green; `pytest -m integration` passes or skips cleanly. | 01, 02 | [03-model-client.md](03-model-client.md) |

Stages must be done in order.

---

## Global Testing & Ship

The default suite is offline. It proves the prefix is stable, the prompts say what they must, the
response contract is enforced, and nothing leaks the key. It **cannot** prove the caching works,
which is why the marked suite exists.

| What must be proved | How | Stage |
|---|---|---|
| The same database always produces the exact same prefix, byte for byte (spec §11.4 — *"if this silently breaks, nothing else will catch it"*) | `tests/test_prompt.py`: repeated calls, two opens of one file, and two files built with the same arguments all compare `==` | 01 |
| A new question never changes the prefix; only a new database does | `tests/test_prompt.py`: `sql_system_blocks` takes no question and is equal across calls; a build with different years/datasets differs | 01, 02 |
| Nothing volatile is in the prefix | `tests/test_prompt.py`: the rendered string contains no `datetime.now()`-shaped value, and does not contain the database path | 01 |
| Nothing was hardcoded — a build with unfamiliar table names still produces a correct prompt | `tests/test_prompt.py` against the new `unconventional_db` fixture: the prefix names its tables and contains none of the standard fixture's names | 01 |
| The whole payload is present: full schema, full codebook including `is_missing`, full manifest, build parameters, and every temporal range (spec §3, §6) | `tests/test_prompt.py`: counts in the rendered string match the context's tuple lengths exactly | 01 |
| The cache breakpoint is after the prefix and before the question | `tests/test_prompt.py`: exactly one `cache_control`, on the last system block; the question is not in the system blocks | 02 |
| The prompts are phrased by outcome and name nothing (spec §2C, §3) | `tests/test_prompt.py` asserts on the prompt constants; two greps in ship-readiness | 02 |
| The chart prompt asks for a spec with no data (spec §7) | `tests/test_prompt.py` | 02 |
| The structured shapes parse | `tests/test_llm.py`: both valid shapes round-trip into `SqlResponse` | 03 |
| `answerable: true` with no `sql` is a failed attempt, not a crash; so is unparseable JSON (spec §11.4) | `tests/test_llm.py`, one case each, asserting `MalformedResponse` | 03 |
| A thinking block before the text block is handled | `tests/test_llm.py`: a fake response whose `content[0].type == "thinking"` still parses | 03 |
| Provider errors are sanitised and never carry the key | `tests/test_llm.py`: a client built with a placeholder key raises `ModelError` for each provider failure, and the placeholder appears in neither `str(error)` nor `repr(error)` | 03 |
| A policy refusal does not consume a repair attempt | `tests/test_llm.py`: `stop_reason == "refusal"` raises `ModelError`, not `MalformedResponse` | 03 |
| The test fake satisfies the interface the next plan will mock | `tests/test_llm.py`: `FakeModel` is accepted where `ModelClient` is expected, returns queued responses in order, and records its calls | 03 |
| **A real call returns SQL that parses and passes the validator** (spec §11.2) | `tests/integration/test_prompt_and_model.py` | 03 |
| **The response schema is accepted by the live API** | `tests/integration/test_prompt_and_model.py`, one `max_tokens`-minimal call | 03 |
| **The second call registers a cache read** (spec §11.2 — *"a silent failure that no offline test can detect, and which quietly triples cost when it breaks"*) | `tests/integration/test_prompt_and_model.py` asserts `usage.cache_read_input_tokens > 0` on call two | 03 |
| The ~23k-token estimate is real | `tests/integration/test_prompt_and_model.py` reports the `count_tokens` figure for a real build | 03 |

**Ship-readiness for this plan** — all of the following must hold before it is considered done:

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
2. `.venv/bin/python -m pytest -m integration` passes with a key and a real build present, and skips
   cleanly — never fails — with neither.
3. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
4. `grep -rniE "labelled|_label" src/askcrossroads/prompt.py` returns nothing.
5. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
6. `grep -rn "content\[0\]" src/askcrossroads/llm.py` returns nothing (Trap 2).
7. `askcrossroads` pointed at a valid build still prints its provenance line and exits 0 — this plan
   changes no existing behaviour.
8. `git status` shows the new and modified files as untracked or modified and **nothing staged**.

---

## Open Questions / Risks

- **The nullable-field schema may be rejected by the live API.** `SQL_RESPONSE_SCHEMA` uses
  `{"type": ["string", "null"]}` for `sql` and `explanation`. Strict structured output requires
  `additionalProperties: false` and full `required`, and whether it accepts a type array is not
  verified offline — no offline test calls the API. Stage 03's integration suite verifies it with
  one minimal call, and stage 03 carries a documented fallback: replace the nullable types with
  plain `{"type": "string"}` and treat the empty string as absent, keeping the discriminator check
  unchanged. If the fallback is needed, record it in the stage report.
- **The ~23,000-token figure is a measurement from a different build, not from ours.** Spec §3 took
  it from a real national build with 1,130 codebook rows, 734 schema columns and 99 manifest rows.
  The DDL-plus-delimited-rows format chosen here is not the format that was measured. Stage 03's
  integration suite reports the real number; if it is materially larger, the caching decision
  becomes *more* load-bearing, not less, but the cost figures in the spec would need revisiting.
  **Resolved:** the integration suite measured 38,812 tokens on a Crossroads-UK 0.9.0, schema 4
  build. Spec §3 now records that figure in place of the estimate.
- **Prompt quality cannot be tested offline.** Everything the default suite proves is that the
  prompt is *stable* and *complete*. Whether it produces good SQL is a property of the wording, and
  the only instrument for it is the answer set in a later plan. Expect the prompt text written here
  to be revised then; write it to be easy to revise, and keep the wording in one constant rather
  than assembled from fragments.
- **A pipe character inside a codebook label would corrupt a delimited row.** No known DfT label
  contains one, but nothing guarantees it. Stage 01 escapes `|` as `\|` in every field. This is
  cheap insurance, not a response to an observed instance.
- **Cache TTL is the default five minutes.** A user who asks one question, thinks for ten, then asks
  another pays the write cost twice. A one-hour TTL is available and costs more to write. Nothing is
  measured yet, so this plan takes the default and leaves the trade to a later measurement.
- **`describe_chart` has no caller until the next plan.** It is delivered here because its prompt
  is, and because leaving half the interface for later means the seam gets designed twice. Its
  `ResultShape` argument is a plain value object precisely so the next plan can compute one from an
  executed result without this plan knowing how execution works.
