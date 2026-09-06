# SQL Safety Validator — Plan Overview

> Multi-stage plan. Each implementation session gets **this overview + ONE stage file**. Read this
> first. Each stage's *End State* is the contract — if a step does not match reality, adapt the step
> and record the deviation, but deliver the End State.

Build the boundary: `safety.py` decides whether a string of model-written SQL is allowed to reach the
database, using DuckDB's own parser rather than pattern matching.

---

## Context & Objective

### Why this module exists

[spec.md §4](../../spec.md) removes the human checkpoint on purpose: generated SQL runs immediately
and is displayed afterwards. A confirmation step would ask the user to approve SQL they cannot read,
which is the entire reason they are using the tool.

That design is only defensible because the controls are machine-enforced. [spec.md §11](../../spec.md)
puts it plainly:

> A boundary that has not been tested is a boundary that is assumed rather than enforced.

This plan builds that boundary and tests it more densely than anything else in the project.

### A deliberate deviation from spec.md, decided before this plan was written

[spec.md §5](../../spec.md) lists six safety layers, two of which it assigns to this module:

| Layer | Mechanism | Stops |
|---|---|---|
| Read-only connection | `duckdb.connect(path, read_only=True)` | All writes — `database.py`, not this plan. |
| **Single-`SELECT` parse check** | `extract_statements()` | Multiple statements; any statement type that is not `SELECT`. |
| **Parse-tree function check** | `json_serialize_sql()`, walked for function names | File- and network-reading functions (`read_csv*`, `read_parquet`, `read_json*`, `glob`, and similar). |
| No external access | `config={"enable_external_access": False}` | Any read or write of the filesystem or network. |
| Row cap | `fetchmany` | Oversized results — `query.py`, later work. |
| User cancellation | `connection.interrupt()` | A query the user no longer wants — `query.py`, later work. |

**This plan implements only the single-`SELECT` parse check. The parse-tree function check is
intentionally not built.**

The reasoning, worked through and agreed before this plan was written: the "no external access"
layer above — already required by spec §5 and already implemented in `002_database_layer` — stops a
file- or network-reading function at the moment it would run, regardless of whether anything checked
for it beforehand. This was verified directly, not assumed: pointing `read_csv_auto`, `read_parquet`,
`glob` and `read_text` at a connection opened with `enable_external_access=False` raises a `duckdb`
error in every case. The parse-tree function check would therefore be a second lock on a door the
connection settings already weld shut — for a project whose entire threat model is one person running
questions against their own local, read-only file, where the honest worst case of the gap is an
uglier error message on the one path this closes, not a bypass of anything.

Two things this decision does **not** touch:

- **The connection-level protections are unaffected and remain in force.** This plan changes nothing
  in `database.py`. `read_only=True` and `enable_external_access=False` are established there and
  stay exactly as they are.
- **The single-`SELECT` parse check stays, in full**, because it stops something the connection
  settings do not: `ATTACH` opens a *second*, separate database file, which is not read-only merely
  because the first connection is. `PRAGMA`, `SET`, `INSTALL` and `LOAD` are also stopped here and are
  not otherwise covered. This is not a redundant layer and was never in question.

**The consequence for spec.md, now reconciled:** [spec.md §5](../../spec.md)'s layer table and
[spec.md §11.3](../../spec.md)'s required-rejection list both describe the layer this plan omits.
That document is the project's committed, authoritative product definition, so it has been updated to
record the decision rather than left describing a control the code does not have. The layer and the
line item both **remain** in the spec — they are the right shape for this threat and may yet be built
— but each now carries a *specified, not implemented* marker pointing at a new §5 subsection, *The
parse-tree function check is specified but not implemented*, which states the reasoning, the two
costs, and what to delete if the check is ever added. Read that subsection alongside this note; they
say the same thing, and neither should be changed without the other.

### What exists today

`docs/plans/001_project_foundation/` and `docs/plans/002_database_layer/` are complete. The
repository is an installable package that opens a configured Crossroads-UK build read-only, refuses
anything else with an actionable message, and discovers its structure at runtime:

```text
AskCrossroads/
├── pyproject.toml                  # duckdb>=1.5 (unchanged by this plan)
├── docs/
│   ├── spec.md                     # the authoritative product definition
│   └── plans/
│       ├── 001_project_foundation/ # complete
│       ├── 002_database_layer/     # complete
│       └── 003_safety_validator/   # this plan
├── src/askcrossroads/
│   ├── app.py                      # main(): loads config, opens and validates the database
│   ├── config.py                   # spec §10 bounds, env var names, load_config()
│   ├── database.py                 # open_database(), convention check, Database, DatabaseContext
│   ├── introspection.py            # information_schema discovery, codebook, temporal ranges
│   ├── safety.py                   # DOCSTRING ONLY — this plan fills it in
│   ├── llm.py / prompt.py / query.py   # docstring only, untouched here
│   └── static/                     # empty
└── tests/
    ├── conftest.py                 # the synthetic Crossroads-UK database factory
    ├── test_package.py / test_config.py / test_fixtures.py
    ├── test_database.py            # read-only enforcement, convention cases
    ├── test_introspection.py
    └── integration/test_conventions.py
```

Three facts from `002_database_layer` that this plan builds on:

- **`open_database(path)` returns a connection on which no write can succeed and no query can reach
  the filesystem.** `read_only=True` and `config={"enable_external_access": False}` are set at
  connect time. This plan does not re-establish either. It **relies** on the second one directly —
  it is now the only thing standing between a model-written query and the local filesystem — and
  stage 02 proves that reliance is warranted rather than assuming it.
- **`tests/conftest.py` provides `crossroads_db` and `crossroads_db_factory`**, which build small
  real DuckDB files on disk. Stage 02 uses `crossroads_db` to prove that accepted queries actually
  execute, and that file-reading queries this validator no longer rejects are still refused by the
  connection.
- **The application holds exactly one database connection**, because `interrupt()` cancels a
  connection rather than a query.

### What this plan changes

- `src/askcrossroads/safety.py` — the whole module: the text gate, the single-statement check, the
  statement-type check, and the result type that names why something was rejected.
- `tests/test_safety.py` — new.

Nothing else in `src/` is touched. **No caller is wired up**: `query.py` is where validation is
actually invoked, and that is later work. This plan delivers a function and proves it correct.

Still out of scope after this plan: prompt assembly, the model call, query execution, the repair
loops, and the HTTP surface.

---

## Approach / Architecture

### The shape of the check

`validate_sql(sql)` runs three checks in a fixed order and returns a `Verdict`. It never raises, and
it needs no database connection of its own — `duckdb.extract_statements` is a plain function, not a
method on a connection.

| # | Check | Rejects | Reason code |
|---|------|---------|-------------|
| 1 | **Original text** — the caller's own string, whitespace stripped, must begin with `SELECT` or `WITH` | `PRAGMA`, `COPY`, `ATTACH`, `INSTALL`, `LOAD`, `SET`, `EXPORT`, `DESCRIBE`, `EXPLAIN`, `CALL`, and every write statement | `not_a_read` |
| 2 | **One statement** — `duckdb.extract_statements()` must return exactly one | Multiple statements; a statement smuggled after a valid `SELECT` | `multiple_statements` |
| 3 | **Statement type** — that statement's parsed type must be `SELECT` | Anything the text gate somehow admitted that is not a select | `not_a_select` |

Plus two structural rejections: an empty or blank string (`empty`), and a string DuckDB's parser
cannot read at all (`unparseable`).

**What this does not do:** a query calling `read_csv_auto`, `glob`, or any other file- or
network-reaching function is a single, well-formed `SELECT` statement, so it passes all three checks.
It is then refused when it actually runs, by the connection settings in `database.py` — see the
deviation note above. `safety.py`'s own module docstring says this explicitly, so a future reader is
not left assuming a check exists here that does not.

### Check 1 runs first, and runs on the caller's own string — the central fact of this plan

[spec.md §5](../../spec.md) states that DuckDB rewrites `PRAGMA version` into a `SELECT` before
reporting its type, so a type-only check passes a `PRAGMA` — and some `PRAGMA` commands can change
database settings, including re-enabling the external access `database.py` switched off.

**This was re-verified against the DuckDB in this project's virtual environment (1.5.5), and it is
worse than the specification describes.** The rewrite reaches the statement's own recorded text, not
only its type:

```text
duckdb.extract_statements("PRAGMA version")[0]
    .type   ->  StatementType.SELECT
    .query  ->  'SELECT * FROM pragma_version();'
```

So there are **two** ways to get this wrong, and only one way to get it right:

| Written as | Result |
|---|---|
| `statement.type == SELECT` | A `PRAGMA` **passes**. |
| `statement.query` starts with `SELECT` | A `PRAGMA` **passes** — the rewritten text is what is stored. |
| The **caller's original string** starts with `SELECT` or `WITH` | A `PRAGMA` is **rejected**. Correct. |

The check must therefore be applied to the string handed to `validate_sql`, before
`extract_statements` is consulted at all. Stage 01 says this again at the point where the code is
written, and stage 02 adds a test that fails loudly if a future DuckDB stops rewriting, so the
reasoning behind the ordering cannot quietly become obsolete.

Note that re-enabling external access via `PRAGMA`/`SET` is exactly the kind of gap the parse-tree
function check does **not** close either way — it is a statement-level problem, not a function-level
one — so this check remains fully necessary independent of the decision above.

### Check 1 is strict: no leading comments, no leading parenthesis

Only leading whitespace is stripped. `-- get the counts\nSELECT ...` and `(SELECT 1)` are both
**rejected**, and this is deliberate.

The alternative is to skip over leading comments before the gate, which means writing a small
comment scanner of our own — exactly the string-matching gap [spec.md §5](../../spec.md) says to
avoid, and `/* nested /* comments */ */` is where such scanners break. A validator with a hand-rolled
pre-parser in front of it is not using the engine's parser; it is using ours, and then the engine's.

The cost is a real false negative: a model that prefixes its SQL with a comment gets rejected. The
mitigations are that the prompt work asks for bare SQL, that the structured-response contract in
[spec.md §6](../../spec.md) returns SQL in its own field rather than as free text, and that the
rejection names the problem precisely (`not_a_read`) so a caller can report it usefully. Fail-closed
is the right side to err on for a boundary.

**Note on the regular expression.** Check 1 is a one-line regex (`^\s*(SELECT|WITH)\b`). This does
not contradict spec §5's "validation uses DuckDB's parser, not a regular expression": the regex
decides nothing on its own — it is a first-word check that only ever *narrows* what reaches the
parser, and checks 2 and 3 both run afterwards through the parser regardless. A regex used *instead
of* the parser is the anti-pattern; a regex used *in front of* it is what closes the `PRAGMA` hole.

### Decisions taken, and what was rejected

| Decision | Why | Rejected alternative |
|---|---|---|
| **The parse-tree function check is not built** | The connection-level `enable_external_access=False`, already required and already implemented, stops the same thing at execution time — verified directly against the engine. Building it anyway would add the single most complex, most DuckDB-internals-dependent piece of the module to close a hole that is already closed, for a threat model of one person querying their own local file. | Building it as spec §5 originally described. Rejected as disproportionate to the actual risk for this project; see the deviation note above. |
| `validate_sql` returns a `Verdict` and **never raises** | Spec §4 re-runs validation on every repair attempt, and §4.5 renders the attempt history. A rejection is an expected outcome on a normal path, not an exceptional one. | Raising `UnsafeSQL`. Turns the common case into exception handling at every call site. |
| The `Verdict` carries a short `reason` code **and** a human `detail` | The code is what tests and the repair loop branch on and is stable; the sentence is what the user reads. One field cannot be both without tests asserting on prose. | A bare boolean. Loses the "why", which the specification requires be reportable. |
| Check order is text → count → type | Cheapest and most decisive first. The text gate alone eliminates every statement class in spec §11.3's reject list except multi-statement smuggling. | Parsing first. Re-introduces the `PRAGMA` hole. |
| `safety.py` opens **no connection of its own** | `duckdb.extract_statements` and `duckdb.StatementType` do not need one — verified directly. Removing the need for a connection removes a whole category of lifecycle and import-time-side-effect concerns from the module. | A private in-memory connection, needed only when `json_serialize_sql` was in scope. No longer needed once the function check was cut. |
| No change to `pyproject.toml` | Keeping DuckDB's parser behaviour stable across its own upgrades is DuckDB's responsibility, not this project's. A guard test is the right amount of insurance; a version cap is a maintenance burden for a risk that is not ours to carry. | Pinning `>=1.5,<2`. |
| A guard test asserts the one engine behaviour the design rests on | If DuckDB ever stops rewriting `PRAGMA`, the suite must say so rather than the boundary quietly weakening. | Trusting the behaviour silently. It is the one assumption in this module that nothing else would catch. |

---

## Cross-Cutting Constraints

Every stage obeys all of these.

- **Nothing about the schema is hardcoded in `src/`** (spec §2C). This plan adds no table name, no
  column name, no dataset name and no code value to `src/`. `safety.py` validates a string; it does
  not know what database it will run against. `grep -rniE "collision|weather|aadf|stats19|vehicle|
  casualty" src/` must return nothing.
- **The API key is absolute.** Never hardcode a key, or any credential, in source, tests, docs,
  plans, comments, or commit messages — not even a truncated or obviously fake one. Nothing in this
  plan touches the key; nothing in this plan may print, log, or embed one.
- **Every bound is a named constant in `config.py`** (spec §10). This plan introduces no bound, so it
  adds nothing to `config.py`.
- **The default test suite is offline and deterministic** (spec §11.1). No test in this plan needs an
  API key, a network connection, or a real Crossroads-UK build. Nothing here is marked `integration`.
- **Layout follows the spec §12 blueprint exactly.** All source goes in `src/askcrossroads/safety.py`
  and all tests in `tests/test_safety.py`. No new module. `safety.py` must **not import
  `database.py`, `config.py`, `introspection.py`, or any other project module** — it takes a string
  and returns a verdict, and that independence is what lets stage 02 test it in isolation.
- **A rejection is never a traceback.** `validate_sql` catches every exception DuckDB's parser can
  raise and turns it into a `Verdict`. If a raw `duckdb` error escapes it, that is a defect.
- **A validator that rejects everything is broken, not safe** (spec §11.3). Every stage that adds a
  rejection adds the matching positive case in the same change.
- **Code is simple and readable.** Plain-language comments where they earn their place. The explicit
  ten-line version over the clever two-hundred-line abstraction. Every line must be readable and
  understandable by a person before it is committed.
- **Never stage, commit, or amend anything in git.** Leave all changes in the working tree and tell
  the user what changed. This is absolute and has no exception.
- **Every stage leaves `pytest` green** before the next stage starts.
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
| 01 | Text gate & statement checks | The `Verdict` result type, the original-text gate, the single-statement check and the statement-type check. | `validate_sql(sql)` exists and returns a `Verdict`. Every statement class in spec §11.3 except file-reading functions is rejected with a named reason; `PRAGMA` is rejected specifically because the gate reads the caller's string. Ordinary `SELECT`s, CTEs and subqueries pass. Nothing raises. `pytest` green. | 001, 002 | [01-text-gate-and-statements.md](01-text-gate-and-statements.md) |
| 02 | The rejection and acceptance matrix | The complete spec §11.3 suite (minus the function check, deliberately not built), the full positive set, the engine-behaviour guard test, and proof that the connection settings — not this validator — are what stop a file-reading query. | Every rejection this plan implements has its own named test. Every accepted query executes successfully against a synthetic build. A file-reading query is shown to pass `validate_sql` and then fail at execution, proving the connection settings carry that protection alone. `pytest` green. | 01 | [02-rejection-matrix.md](02-rejection-matrix.md) |

Stages must be done in order.

---

## Global Testing & Ship

Everything below runs offline. The default `pytest` run must stay usable by anyone who clones the
repository, with no key, no network and no real build (spec §11.1).

| What must be proved | How | Stage |
|---|---|---|
| `PRAGMA` is rejected despite parsing as a `SELECT` (spec §5, §11.3) | `tests/test_safety.py` asserts the verdict, and asserts the reason is the text gate rather than the type check | 01 |
| `DROP`, `DELETE`, `UPDATE`, `INSERT`, `CREATE`, `ATTACH`, `COPY` each rejected (spec §11.3) | One explicitly named parameterised case per statement, never one test asserting "bad SQL is rejected" | 01, restated in full at 02 |
| Multiple statements, and a statement smuggled after a valid `SELECT` (spec §11.3) | Separate cases: `SELECT 1; SELECT 2` and `SELECT 1; DROP TABLE t` | 01 |
| Comment smuggling resolves through the parser, not by string matching (spec §5) | `SELECT 1 -- ; DROP TABLE t` is **accepted** — it is one statement and the parser knows it | 01 |
| Unparseable text and empty input produce a verdict, not a traceback | `tests/test_safety.py` asserts the reason codes and that no `duckdb` exception escapes | 01 |
| Ordinary `SELECT`s, CTEs and subqueries **pass** (spec §11.3) | A positive set that grows in every stage; a validator that rejects everything must fail this | 01, 02 |
| Accepted queries genuinely execute — the validator is not accepting nonsense | Stage 02 runs every positive case against the `crossroads_db` fixture through `open_database` | 02 |
| A file-reading query passes `validate_sql` and is refused only by the connection | Stage 02 asserts `validate_sql(...).allowed is True` for `read_csv_auto`/`glob`/etc., then asserts `con.execute(...)` raises — the explicit proof behind the decision to skip the function check | 02 |
| A write statement is refused by the engine as well as by the validator (spec §11.4) | Stage 02 asserts both for `DROP`/`DELETE`/`UPDATE`/`INSERT`/`CREATE` | 02 |
| The engine behaviour the design rests on has not changed | Stage 02 guard test: `PRAGMA` still rewrites to a `SELECT` | 02 |

**Ship-readiness for this plan** — all of the following must hold before it is considered done:

1. `env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB .venv/bin/python -m pytest` is green.
2. `.venv/bin/python -m pytest tests/test_safety.py -v` shows a separately named case for every
   rejection this plan implements, and a separately named case for every positive.
3. `.venv/bin/python -m pytest -m integration -q` still collects and skips cleanly.
4. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
5. `grep -rn "import" src/askcrossroads/safety.py` shows only standard library modules and `duckdb` —
   no project module.
6. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
7. `git status` shows the new and modified files as untracked or modified and **nothing staged**.

---

## Open Questions / Risks

- **Whether the function check should eventually be built remains open.** spec.md no longer disagrees
  with the code — [spec.md §5](../../spec.md) now records the decision explicitly and §11.3's
  file-reading line item is marked *specified, not implemented* — so this is no longer a
  documentation gap. What is still open is the underlying question the marker leaves standing: the
  check stays in the spec because it is the right shape for this threat, and if the threat model ever
  widens beyond one person querying their own local file, it should be built. Nothing schedules that
  today.
- **A rejection is not a SQL execution error, and spec §4 only requires a retry on the latter.**
  Whether a safety rejection should consume a repair attempt, be fed back to the model, or be
  reported straight to the user is a question for the plan that builds `query.py`. This plan makes
  the choice possible by giving every rejection a stable reason code; it does not make the choice.
  Separately, a file-reading query — which this validator now allows — will surface as an ordinary
  SQL execution error from the connection, which spec §4 **does** already say to retry. That is a
  behaviour change from "rejected immediately with no attempt consumed" to "consumes a repair
  attempt, then fails again identically on retry, up to three times." It costs attempts and produces
  a less friendly message; it does not reopen the file-access hole.
- **The strict text gate will reject a leading comment.** Accepted deliberately (see *Check 1 is
  strict*, above). If real usage shows the model prefixing SQL with comments often enough to matter,
  the fix belongs in the prompt or in the structured-response contract — **not** in a comment
  scanner in front of the parser.
- **If the connection settings in `database.py` were ever misconfigured, there is now no second
  layer behind them for file/network access specifically.** This is the direct cost of the decision
  above, stated plainly. `tests/test_database.py` (from `002_database_layer`) already covers this
  directly, and stage 02 of this plan adds an equivalent check from the validator's side, but neither
  is a substitute for the connection itself being right.
