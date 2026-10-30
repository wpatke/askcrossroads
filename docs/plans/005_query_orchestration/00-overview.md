# Query Orchestration — Plan Overview

> Multi-stage plan. Each implementation session gets **this overview + ONE stage file**. Read this
> first. Each stage's *End State* is the contract: if a step does not match reality, adapt the step
> and record the deviation, but deliver the End State.

Turn a question into an answer: ask the model for SQL, validate it, run it read-only with a row
cap, repair it within strict bounds when it fails, then ask for a chart. One question at a time,
cancellable, all provable offline with a scripted model.

---

## Context & Objective

### What exists when this plan starts

`docs/plans/001_project_foundation/` through `004_prompt_and_model/` are complete. The repository
can open and validate the user's database, discover everything about it, judge whether one SQL
string is safe to run, build a byte-stable prompt, and make both model calls. **Nothing yet runs a
model's SQL or decides when to call the model.** That is this plan.

The pieces this plan consumes, by file:

| File | What this plan uses from it |
|---|---|
| `src/askcrossroads/config.py` | `MAX_ATTEMPTS = 3`, `MAX_RESULT_ROWS = 5_000`, `CHART_SAMPLE_ROWS = 5`. No new constant is needed. |
| `src/askcrossroads/database.py` | `Database` (has `.connection`, an open read-only DuckDB connection with `enable_external_access=False`, and `.context`, the frozen `DatabaseContext`). |
| `src/askcrossroads/safety.py` | `validate_sql(sql) -> Verdict` with `.allowed`, `.reason`, `.detail`. Never raises. |
| `src/askcrossroads/prompt.py` | `ResultShape(columns, row_count, distinct_counts, sample_rows)`; `chart_user_message(question, sql, shape)`; `_escape`. |
| `src/askcrossroads/llm.py` | `ModelClient` protocol (`generate_sql(context, question)`, `describe_chart(question, sql, shape)`); `SqlResponse(answerable, sql, explanation)`; `MalformedResponse` (a **repairable** failed attempt); `ModelError` (sanitised, **not** repairable); `AnthropicClient`. |
| `tests/conftest.py` | `crossroads_db_factory`, `crossroads_db`, `FakeModel` (scripted model: queues replies or exceptions, records `calls`), the `fake_model` fixture. |
| `src/askcrossroads/query.py` | Docstring only. **This plan fills it in.** |

Do not re-derive these facts. They come from the earlier plans:

- **`DatabaseContext` is frozen and built once.** Nothing here re-introspects or re-reads the file.
- **`Database.connection` is the only connection.** Spec §5: v1 holds one connection and runs one
  query at a time, because `interrupt()` cancels the *connection*, not a particular query.
- **The connection is already sandboxed.** `read_only=True` and `enable_external_access=False` are
  set at connect time in `database.py`. This plan adds no connection settings.
- **`safety.validate_sql` does not check for file-reading functions.** That is deliberate (see
  `safety.py`'s docstring). A `read_csv_auto(...)` query passes validation and fails at execution
  with a DuckDB error, which this plan's loop treats like any other execution error.

### The gap this plan closes first

Spec §4: *"When execution raises a SQL error, the error text is fed back to the model, which
refines the query."* The model interface from the previous plan has **no way to carry that
feedback**: `generate_sql(context, question)` and `describe_chart(question, sql, shape)` take
nothing describing earlier attempts. A repair loop built on them would only be a blind retry, and
would mostly get the same broken query back. Stage 01 adds one optional argument to both methods.
It is small and additive, and every existing call and test keeps working unchanged.

### What this plan changes

- `src/askcrossroads/prompt.py`: `sql_user_message(question, failures)`, plus a `failures` argument
  on `chart_user_message`. Both render earlier failed attempts *after* the cache breakpoint.
- `src/askcrossroads/llm.py`: `failures=()` on both `ModelClient` methods and on `AnthropicClient`.
- `src/askcrossroads/query.py`: everything else. `execute`, `ExecutionResult`, `result_shape`,
  `Attempt`, `ChartAttempt`, `Answer`, the status constants, `Busy`, and `QueryRunner` with `ask()`
  and `cancel()`.
- `tests/conftest.py`: `FakeModel` gains the `failures` argument and records it.
- `tests/test_prompt.py`, `tests/test_llm.py`: additions for the new argument.
- `tests/test_query.py`: new. The main suite for this plan.
- `tests/integration/test_end_to_end.py`: new, marked `integration`, skipped by default.
- `tests/integration/live_fixtures.py`: new. It holds the `settings`, `database` and `client`
  fixtures, moved verbatim out of `tests/integration/test_prompt_and_model.py` so both integration
  files share them. Both files import the fixtures by name. It is deliberately not called
  `conftest.py`: a second module with that name would shadow `tests/conftest.py` and break the
  offline tests' `from conftest import FakeModel`.

Still out of scope after this plan: HTTP routes, JSON encoding of result values, the `askcrossroads`
server launch, and the frontend. After this plan, `QueryRunner.ask()` is the whole engine, and the
next plan only has to expose it.

---

## Approach / Architecture

### Data flow for one question

```text
QueryRunner.ask(question)
  │  lock not free? ──► raise Busy                          (one question at a time, spec §5)
  │  clear the cancelled flag
  │
  ├─ SQL loop: up to config.MAX_ATTEMPTS times
  │    cancelled? ──► Answer(CANCELLED)
  │    model.generate_sql(context, question, failures)      failures = earlier failed attempts
  │      MalformedResponse ──► record Attempt(sql=None, error) ; next attempt
  │      ModelError        ──► Answer(FAILED, error)        (not retried)
  │    cancelled? ──► Answer(CANCELLED)
  │    reply.answerable is False ──► Answer(DECLINED, explanation)   (terminal success)
  │    safety.validate_sql(sql) rejects ──► record Attempt(sql, verdict.detail) ; next attempt
  │    execute(connection, sql)                              fetchmany(MAX_RESULT_ROWS + 1)
  │      duckdb.Error ──► cancelled? CANCELLED : record Attempt(sql, str(error)) ; next attempt
  │    cancelled? ──► Answer(CANCELLED)
  │    record Attempt(sql, error=None) ──► success, leave loop
  │  all attempts failed ──► Answer(FAILED, error = last attempt's error)
  │
  ├─ zero rows? ──► Answer(ANSWERED, rows=()), no chart call  (never retried, spec §4.2)
  │
  └─ Chart loop: up to config.MAX_ATTEMPTS times
       cancelled? ──► Answer(CANCELLED)
       model.describe_chart(question, sql, result_shape(result), failures)
         MalformedResponse ──► record ChartAttempt(error) ; next attempt
         ModelError        ──► record ChartAttempt(error) ; stop trying
       record ChartAttempt(None) ; chart = the returned dict, UNEDITED
     ──► Answer(ANSWERED, sql, columns, rows, truncated, chart or None, attempts...)
```

### Stop, precisely

Stop is the user's only cancellation control (spec §9), and there is no timeout (spec §5). The
behaviour is:

- **The browser aborts its own request** (a later plan). The user stops waiting immediately.
- **The server stops DuckDB at once and starts nothing new.** `QueryRunner.cancel()` sets a
  `threading.Event` and, if a query is executing, calls `connection.interrupt()`. The loops check
  the flag before and after every model call and every execution. **A model call already in flight
  is not aborted.** It finishes, and its result is discarded. Aborting an in-flight HTTP request to
  Anthropic would need async or SDK-level connection teardown, which is out of proportion for v1.
- **Why the server must react at all**, even though the browser already gave up: the
  one-question lock is held until `ask()` returns. If the server ignored Stop, a stopped
  three-minute join would block the next question for three minutes, and every remaining model
  call would still be billed to the user.

### Threading: the caller owns the thread

`ask()` simply blocks. `cancel()` is safe to call from any other thread. In the next plan, a
FastAPI sync route already runs `ask()` on a threadpool worker, and `/api/cancel` runs `cancel()` on
another, which satisfies spec §5's *"query runs on a worker thread"*. `query.py` creates **no
threads**. Tests run `ask()` in a `threading.Thread` and cancel from the test's own thread.

### Decisions taken, and what was rejected

| Decision | Why | Rejected alternative |
|---|---|---|
| **Feedback rides in the single user message, as an optional `failures` argument** | Spec §4 requires the error to reach the model. Earlier attempts go after the cache breakpoint, so the cached prefix is untouched and caching still works on repairs. An empty `failures` produces *exactly* the old message, so every existing test stays green. | Replaying failed replies as assistant turns. That needs the raw reply text, which a `MalformedResponse` does not keep, and adds structured-output and prefill edge cases. Blind retry breaches spec §4. |
| **`failures` is a tuple of `(attempted, error)` string pairs**, `attempted` may be `None` | `prompt.py` must not import `query.py` or `llm.py`, so the type crossing the seam has to be plain. Tuples match the style of `ResultShape`. | A shared dataclass. There is nowhere to put it that every module can import without a cycle. |
| **A safety-validator rejection is a failed attempt, fed back** | Spec §4.4 re-validates every attempt. A rejection is usually a fixable habit (a leading comment fails the first-word gate). It is bounded by the same three attempts, so a model that keeps doing it still ends. | A terminal refusal. Wastes questions the model would fix on the next try. |
| **`MalformedResponse` is a failed attempt; `ModelError` is not** | Spec §6 makes a malformed reply a failed attempt. `ModelError` (auth, rate limit, policy refusal, network) will fail identically if retried, as the previous plan's `llm.py` documents. | Retrying `ModelError`. That would burn the user's credit on a request that cannot succeed. |
| **A cancelled execution is not a failed attempt** | The error came from the user pressing Stop, not from the SQL. It must not be fed back or repaired. The flag is checked *before* recording a DuckDB error. | Recording it as an attempt. The loop would then "repair" a query the user deliberately stopped. |
| **Outcomes are one `Answer` value with a `status` string**, not exceptions | Answered, declined, failed and cancelled are all ordinary outcomes on normal paths, the same reasoning `safety.Verdict` uses. Only `Busy` is an exception, because it means `ask()` did not run at all. | One exception class per outcome. Every caller would need four `except` clauses to do what one `if` does. |
| **`Answer` field names mirror spec §9's `/api/ask` response** | The next plan then maps fields almost one to one. | Inventing new names and translating later. |
| **Row cap by `fetchmany(MAX_RESULT_ROWS + 1)`; SQL executed exactly as the model wrote it** | Spec §10. Injecting `LIMIT` would make the displayed SQL differ from what ran (spec §8). | `LIMIT` injection. |
| **`cancel()` keeps interrupting while a query is marked as running, and the runner checks for Stop after marking a query as running** | Found after implementation: a single interrupt could be lost in two ways. (1) Stop pressed between the loop's last check and the query being marked as running, e.g. during validation, found nothing to interrupt. (2) DuckDB 1.5.5 discards an interrupt that arrives before it has started the query. Either way a stopped query ran to the end and held the lock. Marking-then-checking (runner) against setting-then-looking (cancel) means one side always sees the other; repeating the interrupt for up to `config.STOP_RETRY_SECONDS` covers DuckDB's discard. Both gaps have a test that fails on the old code. | One interrupt, only while executing. Rare to hit, but when hit, the one control that replaces a timeout silently did nothing. |
| **Numeric-ness for the chart's distinct counts is judged from DuckDB's type name** | Spec §7 sends distinct counts for non-numeric columns only. Engine type names (`BIGINT`, `DECIMAL(18,3)`) are DuckDB's vocabulary, not the data's, so spec §2C is not engaged. | Inspecting Python value types. A column of all-`NULL` values has no value type to inspect. |
| **Distinct values are counted by `repr(value)`** | Result values can be lists or dicts (DuckDB `LIST`/`STRUCT`), which are unhashable. `repr` makes every value countable, with one line and no special cases. | `set(values)`. This crashes on the first `LIST` column. |

### What `query.py` imports

`threading`, `time` (for Stop's retry loop), `dataclasses.dataclass`, `duckdb`, and
`from askcrossroads import config, llm, prompt, safety`. **Not** `database` (it takes a `Database` object and uses only `.connection` and
`.context`), **not** `app`, **not** `anthropic`.

---

## Cross-Cutting Constraints

Every stage obeys all of these.

- **Never stage, commit, or amend anything in git.** Leave all changes in the working tree and tell
  the user what changed. This is absolute.
- **The API key is absolute.** Never hardcode a key or any credential in source, tests, docs,
  comments, or commit messages, not even a truncated or obviously fake one. Where a test needs a
  placeholder, use the literal `"test-key-not-a-real-credential"`, as `tests/test_llm.py` does.
  `query.py` never touches the key: it receives an already-built `ModelClient`.
- **Nothing about the schema is hardcoded in `src/`** (spec §2C). `query.py` names no table, column,
  view, dataset or code. Tests may use the synthetic fixture's names, because that is test input,
  exactly as `tests/test_introspection.py` already does.
  `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` must return nothing.
- **Every bound is a named constant in `config.py`.** Use `config.MAX_ATTEMPTS`,
  `config.MAX_RESULT_ROWS` and `config.CHART_SAMPLE_ROWS` **as module attributes**
  (`config.MAX_RESULT_ROWS`, never `from askcrossroads.config import MAX_RESULT_ROWS`), so tests can
  `monkeypatch.setattr(config, "MAX_RESULT_ROWS", 3)` and have it take effect.
- **Python never edits a returned Vega-Lite spec** (spec §7). The chart dict goes into `Answer`
  as the same object the model client returned.
- **The default suite is offline and deterministic** (spec §11.1). The model is always `FakeModel`.
  The database is always a synthetic fixture or DuckDB's built-in `range()` table function. Anything
  that needs a key or a real build is `@pytest.mark.integration` and **skips**, never fails, when
  either is absent.
- **Layout follows spec §12.** No new module. All new production code goes in `query.py`, except
  the stage 01 changes to `prompt.py` and `llm.py`. `prompt.py` still imports neither `llm` nor
  `query`, and `llm.py` still does not import `query`.
- **Code is simple and readable.** Plain-language comments where they earn their place. Every line
  must be readable by a person before commit. The explicit ten-line version wins over the clever
  abstraction.
- **Python 3.11 or newer, via the existing `.venv/`.** The system `python3` on the development
  machine is 3.9 and will not work.
- **Every stage leaves the default `pytest` run green** before the next stage starts.

### The commands every stage uses

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest
```

```bash
.venv/bin/python -m pytest tests/test_query.py -v
```

---

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Repair feedback in the model seam | Add `failures=()` to `generate_sql` and `describe_chart` (protocol, `AnthropicClient`, `FakeModel`). Add `prompt.sql_user_message` and a `failures` argument on `chart_user_message` that render earlier attempts after the cache breakpoint. | With no failures, both messages are byte-identical to before. With failures, the user message carries each attempted SQL and its error, and the system blocks are unchanged. `FakeModel.calls` records failures. `pytest` green. | Plan 004 complete | [01-repair-feedback.md](01-repair-feedback.md) |
| 02 | Capped execution | Probe the DuckDB behaviours this plan relies on, then add `ExecutionResult` and `execute(connection, sql)` to `query.py`: run SQL unmodified, read DuckDB type names, and fetch at most `MAX_RESULT_ROWS + 1` rows. | `execute` returns `(name, DuckDB type name)` columns, rows in SQL order, and `truncated` exactly when the result exceeds the cap. It passes the SQL to the connection unmodified, and lets `duckdb.Error` propagate. The probe confirms interrupt and idle-interrupt behaviour. `pytest` green. | 01 | [02-capped-execution.md](02-capped-execution.md) |
| 03 | The runner and the SQL repair loop | `QueryRunner(database, model)` with `ask()` and `cancel()`, the `Answer`/`Attempt` types and statuses, `Busy`, the one-question lock, the cancelled flag, and the bounded SQL loop. | Every spec §11.4 case for the SQL side passes against a real synthetic DuckDB with `FakeModel`: repair on error, stop at exactly 3, no retry on zero rows, re-validation each attempt, decline terminal, malformed repaired, `ModelError` not retried, Busy, Stop during SQL and during a model call, connection usable after Stop. `ANSWERED` carries no chart yet. `pytest` green. | 02 | [03-runner-and-sql-loop.md](03-runner-and-sql-loop.md) |
| 04 | Chart context, chart loop, end to end | `result_shape(result)` and the independent chart loop inside `ask()`. Add the marked real-API end-to-end test. | The chart call happens only after SQL success with at least one row, and receives the real result columns. Three chart failures, or one `ModelError`, still return the table. The spec dict is returned unedited. A real question against a real build returns `ANSWERED` with rows and a chart, and an out-of-range question is `DECLINED`. Default `pytest` green; `pytest -m integration` passes or skips cleanly. | 03 | [04-chart-loop.md](04-chart-loop.md) |

Stages must be done in order.

---

## Global Testing & Ship

The primary suite is `tests/test_query.py`. Every test there drives the real `QueryRunner` against a
**real DuckDB database** (the synthetic Crossroads-UK fixture, opened through `Database.open`, so
the read-only and no-external-access settings are the production ones), with only the model
scripted. Nothing in it mocks DuckDB, the validator, or the prompt rendering.

| What must be proved (spec reference) | Test | Stage |
|---|---|---|
| Earlier failures reach the model, after the cache breakpoint (§4) | `test_llm.py`: system blocks unchanged, user message carries SQL + error | 01 |
| The row cap truncates and reports, without changing the SQL (§5, §10) | `test_query.py`: cap boundary tests; recording-connection test | 02 |
| Repair on a SQL error, with the error fed back (§4, §11.4) | `test_a_sql_error_is_repaired_with_the_error_fed_back` | 03 |
| Stops at exactly three attempts (§4.1) | `test_three_failures_end_the_question` | 03 |
| **Zero rows is never retried** (§4.2, "the case that matters most") | `test_zero_rows_is_an_answer_and_is_never_retried` | 03 |
| The row cap applies to every attempt (§4.3) | `test_the_row_cap_applies_to_a_repaired_attempt` | 03 |
| Validation re-runs on every attempt (§4.4) | `test_a_repaired_query_is_validated_again` | 03 |
| The full attempt history is kept (§4.5) | asserted in every loop test via `sql_attempts` | 03 |
| A decline executes nothing and consumes no attempt (§6, §11.4) | `test_a_decline_is_terminal_and_executes_nothing` | 03 |
| Malformed replies are repaired, not crashed on (§11.4) | `test_a_malformed_reply_is_a_failed_attempt` | 03 |
| One question at a time (§5) | `test_a_second_question_while_one_runs_is_busy` | 03 |
| Stop interrupts a running query; the connection is usable after (§11.4) | `test_stop_interrupts_a_running_query_and_the_connection_survives` | 03 |
| Cancel with nothing running is a no-op (§5) | `test_cancel_with_nothing_running_is_a_no_op` | 03 |
| The chart call never happens before SQL success, and gets the real columns (§11.4) | `test_the_chart_call_comes_after_the_sql_and_sees_the_real_columns` | 04 |
| Empty results skip the chart call (§6, §11.4) | `test_zero_rows_makes_no_chart_call` | 04 |
| Three failed chart attempts still return the table (§7, §11.4) | `test_three_chart_failures_still_return_the_table` | 04 |
| Python never edits the spec (§7) | `test_the_chart_spec_is_returned_unedited` | 04 |
| **A real question works end to end** (§11.2) | `tests/integration/test_end_to_end.py` | 04 |
| **A real out-of-range question is declined** (§11.2) | `tests/integration/test_end_to_end.py` | 04 |

### Ship-readiness for this plan

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
2. `.venv/bin/python -m pytest -m integration` passes with a key and a real build present, and
   **skips cleanly**, never fails, with neither.
3. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
4. `grep -rniE "labelled|_label" src/askcrossroads/prompt.py src/askcrossroads/query.py` returns
   nothing.
5. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
6. `grep -n "LIMIT" src/askcrossroads/query.py` shows only comment lines. No code adds a `LIMIT`.
7. `grep -nE "^import|^from" src/askcrossroads/query.py` shows only `threading`, `time`, `dataclasses`,
   `duckdb`, and `from askcrossroads import config, llm, prompt, safety`.
8. `grep -n "Thread(" src/askcrossroads/query.py` returns nothing. `query.py` creates no threads.
9. `askcrossroads` pointed at a valid build still prints its provenance line and exits 0. This plan
   changes no existing behaviour of `app.py`.
10. `git status` shows the new and modified files and **nothing staged**.

---

## Open Questions / Risks

- **Plan 004 is being finished in parallel.** Stage 01 of this plan edits files that plan 004 writes.
  Stage 01's prerequisites check that plan 004 is done. If plan 004 landed with a deviation (for
  example the documented nullable-schema fallback in `SQL_RESPONSE_SCHEMA`), it does not affect this
  plan, because nothing here depends on that schema's shape.
- **Does `connection.execute` stream, or materialise the whole result before `fetchmany`?** The row
  cap is correct either way: the browser never gets more than `MAX_RESULT_ROWS` rows. Memory differs,
  though. If DuckDB materialises, a 50-million-row result is held in memory before truncation. Stage
  02's probe records which it is. If it materialises, record it in the stage report. It is a
  performance note for later, not a reason to change this design, which the spec fixes.
- **A Stop pressed in the instant before `ask()` clears its flag is lost.** The browser has already
  aborted, so the user sees nothing wrong. The server runs that one question to completion, at a
  cost of at most `2 × MAX_ATTEMPTS` model calls. Closing the window would need a per-question token
  passed from the browser, which is out of proportion for a single-user local tool.
- **The repair message wording is untested against a real model beyond one end-to-end question.**
  Whether feedback actually helps the model repair is a prompt-quality property. The answer-set work
  in a later plan is the instrument for it. Keep the wording in one function so it is easy to revise.
- **DuckDB error text is fed back and shown verbatim** (spec §6). It can be long, for example a
  "did you mean" candidate list. Nothing truncates it. That is fine at three attempts. Revisit only
  with an observed instance of a problem.
- **`Answer.rows` holds raw Python values** (`datetime`, `Decimal`, `date`, lists). Converting them
  to JSON is the next plan's job, at the HTTP boundary. Those values are rendered to text for the
  chart prompt only through `prompt._escape`, which already handles any value via `str()`.
