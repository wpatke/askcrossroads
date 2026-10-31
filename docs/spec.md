# AskCrossroads: Plain-English Querying of Crossroads-UK Databases

AskCrossroads is a locally-run web application that turns a question written in ordinary English into SQL, runs it against a Crossroads-UK DuckDB database, and returns a results table and a chart.

UK road-safety data is public, detailed, and effectively closed to anyone who does not write SQL. A Crossroads-UK build puts thirty years of collisions, weather, boundaries, and traffic counts into a single local file — and then asks the reader to express their question as a join. AskCrossroads removes that requirement. The question is asked in English; the SQL is written by a language model, executed, and shown.

Nothing is hosted. The user clones the repository, runs it on their own machine, and configures the path to their own `.db` file and their own Anthropic API key. Their data never leaves the machine: the only thing sent anywhere is the question, together with the database's schema and codebook, to the Anthropic API.

AskCrossroads is a companion to Crossroads-UK, not a general-purpose natural-language SQL tool. That narrowing is deliberate and is the first architectural decision below.

AI usage and development workflows are detailed in [AI_DISCLOSURE.md](ai-disclosure.md).

---

## 1. Scope: Crossroads-UK Databases Only

The application targets Crossroads-UK databases specifically. On load it validates the supplied file and refuses anything else.

A general "ask questions of any database" tool has to solve semantic discovery from nothing: it cannot know that `1` means *Fatal*, that `-1` means *data missing or out of range*, or that a coordinate column is in EPSG:27700. Crossroads-UK databases answer all of those questions from inside the file (§2), so the hard part is already solved — but only for those files. Accepting arbitrary databases would mean shipping a worse tool for every input rather than a good one for a known input, and would put v1 out of reach.

Refusal is not a dead end. A file that fails validation produces a message naming what was missing and pointing the user at the command that produces a valid one:

```text
This file does not look like a Crossroads-UK database: no `codebook` or
`column_manifest` table was found.

AskCrossroads reads databases built by Crossroads-UK:
    pip install crossroads-uk
    crossroads
```

Because the path comes from configuration (§9), this check runs at startup: the message goes to the terminal and the process exits non-zero, rather than the browser opening onto an error page. The error tells the user what to do next, not just what went wrong.

---

## 2. Specialised, But Never Hardcoded

Crossroads-UK builds vary. Two valid databases may cover different year ranges, include or omit the optional weather and AADF datasets, and use either boundary mode. A fixed schema assumption inside AskCrossroads would therefore break on files that are entirely correct — and would break silently, by generating SQL referring to a table that this build does not contain.

So the application assumes nothing about the contents of a build and reads everything from the build itself.

### A. Structure comes from runtime introspection

The set of tables, columns, and types is discovered on load by querying DuckDB's own catalogue:

```sql
SELECT table_name, column_name, data_type
FROM information_schema.columns
ORDER BY table_name, ordinal_position
```

This runs against whatever file the user supplied, so a weatherless build simply has no `weather` table in the discovered schema, and the prompt (§6) never offers one.

### B. Meaning comes from tables inside the database file

Crossroads-UK ships its semantics as data. AskCrossroads reads three of those tables and copies none of them:

| Table | Columns read | What it gives AskCrossroads |
|-------|--------------|-----------------------------|
| `codebook` | `variable`, `code`, `label`, `is_missing` | The DfT English label for every integer code (e.g. `collision_severity` `1` → *Fatal*), and which codes are missing/unknown sentinels. |
| `column_manifest` | `tbl`, `col`, `kind`, `dtype` | The classification of every source column. The rows where `kind = 'coded'` are exactly the columns that need translation. |
| `crossroads_meta` | `crossroads_version`, `schema_version`, `built_at_utc`, `parameters` | Build provenance: which version built this file, with which datasets, years, and boundary mode. |

The join between the first two is `column_manifest.col = codebook.variable`.

Because these tables travel inside each `.db` file, the semantics always match the build that produced them. A database built by an older Crossroads-UK release carries that release's codebook; a database built tomorrow carries tomorrow's. There is no sync problem, no vendored copy to refresh, and no version of AskCrossroads that is "out of date" with respect to a user's file.

### C. The standing constraint

> **AskCrossroads hardcodes no assumption about the schema beyond `codebook`, `column_manifest`, and `crossroads_meta` — those table names, their column names, and the `kind` values they use. Everything else is discovered from the file at runtime.**

Everything else genuinely varies: which tables a build contains, what its columns are called, which codes appear, and what they mean. A hardcoded dataset name is therefore a wrong answer waiting to happen. `WHERE collision_severity = 1` works on the contributor's build, errors on a build lacking the column, and silently returns the wrong rows on a build whose codebook gives code `1` a different meaning. To the user none of that looks like a bug — it looks like the data giving a strange answer, which is the failure §6 exists to prevent.

The rule binds every change, and applies to source, tests, prompt text, and the frontend alike.

**The test:** if Crossroads-UK added a dataset tomorrow, could AskCrossroads answer questions about it with no code change? If not, something was hardcoded that should have been discovered.

### D. Provenance and schema version

`crossroads_meta` is read once on load and displayed (§9). `schema_version` is shown but is **not** used to gate acceptance: because structure is discovered at runtime rather than assumed, a newer or older schema is not inherently unusable, and refusing on a version number would reject working files. Validation is by shape — do the convention tables exist, with the expected columns, in a readable form (§11.4) — not by version.

---

## 3. The Codebook Goes Into the Prompt

**The entire codebook, schema, and column manifest go into the prompt.** No relevance filtering, no selection heuristic.

A relevance filter — scoring the question's words against column names and label text — sounds like an obvious optimization, but it fails silently on exactly the questions worth asking. "How many collisions involved drunk drivers?" matches no column name and no label text, so the model would get no codes for the column it needs, guess, and return wrong rows instead of an error.

There are not many tokens to save. Measured on a real national build (Crossroads-UK 0.9.0, schema 4: 18 tables, 8 views, 1,130 codebook rows, 59 coded columns), the whole cached prefix — schema, date ranges, manifest, codebook, build parameters and the instructions that follow them — is 38,812 tokens: still a small fraction of the context window. An earlier estimate of about 23,000 tokens was taken from a different build, before the prompt's layout was settled, and was too low. The integration suite prints the current figure on every run, so a change to the prompt that grows it is visible.

Injecting the codebook whole also carries `is_missing`, which matters: Crossroads-UK replaces those codes with `NULL` when it builds the silver tables, so a model that does not know `weather_conditions = 9` was removed will write a filter on it and get nothing back.

It is also a **stable prefix**: for a given database it is byte-identical on every question, and the only volatile content, the question itself, follows it. Placed in the system prompt behind a cache breakpoint, it is written to cache once and read back at about a tenth of the input rate — a few cents per question against the user's own API credit.

Two rules follow, both testable:

- **The prefix must be byte-stable.** Nothing volatile — no timestamp, no question text, no per-request identifier — may appear before the cache breakpoint, and serialisation must be deterministic: rows ordered by an explicit sort, never by whatever order the database returned them in.
- **It is rebuilt only when the database changes.** A different `.db` file means a new prefix and a fresh cache entry; another question of the same file must not.

### Labels come back from the query, not from post-processing

The model is told to return human-readable values wherever the schema offers them, and to order results meaningfully. It does not need telling *how*: Crossroads-UK already ships `*_labelled` views carrying a `<col>_label` twin for every coded column, and those views are in the injected schema like anything else, so the model can simply see and use them.

```sql
SELECT weather_conditions_label AS weather, COUNT(*) AS collisions
FROM collisions_labelled
GROUP BY weather_conditions_label
ORDER BY MIN(weather_conditions)
```

That returns *Fine no high winds*, *Raining no high winds*, *Snowing no high winds* — in DfT code order, because the `ORDER BY` sorts on the underlying integer while the output carries the word.

An earlier draft did this in Python instead, translating codes to labels after every query. Doing it in SQL is better on every axis that matters here:

- **The displayed SQL is comprehensible.** `WHERE weather_conditions_label LIKE 'Raining%'` means something to a reader who does not write SQL; `WHERE weather_conditions IN (2, 5)` does not. Since §4 shows the user the query, this is the difference between an artefact they can read and one they must take on trust.
- **Ordering is free.** `ORDER BY MIN(weather_conditions)` gives codebook order — *Fatal → Serious → Slight*, and *Fog or mist* in its proper place rather than alphabetised third. The result arrives in display order, so the chart call (§7) inherits it with nothing further to compute.
- **It deletes a module.** No decoration step, no code-to-label mapping, no ordering rules, no cast between `VARCHAR` codes and `INTEGER` columns.

Nothing here is hardcoded, and §2C still holds: the instruction is phrased by outcome — *prefer values a person can read* — never by naming a view or a suffix. A build without those views simply does not offer them in the schema, and the model uses what is there.

Two display rules remain, and both belong to the frontend rather than a processing step:

- **A `NULL` in a label column renders as *Not recorded*.** Crossroads-UK replaces missing and unknown codes (`-1`, `9`) with `NULL` before AskCrossroads sees the file, which erases which one it was — so guessing is not an option, and every such value gets the same neutral label rather than an empty cell.
- **A column the codebook does not cover renders as its raw code.** Never blank, never guessed.

### The coverage guarantee, as verified

Checked against a real national build (Crossroads-UK 0.9.0, schema 3, `datasets = ["aadf", "bank_holidays", "era5_weather", "stats19"]`, `years = [2023]`):

| Check | Result |
|-------|--------|
| Columns with `column_manifest.kind = 'coded'` | 59 (24 collision, 22 vehicle, 13 casualty) |
| Of those, present in the corresponding silver table | 59 / 59 |
| Carrying a `<col>_label` twin on the labelled view | 24 / 24 on `collisions_labelled` |
| With matching rows in `codebook` under the same name | **58 / 59** |

**The guarantee holds with one documented exception.** `column_manifest` classifies `enhanced_severity_collision` as coded, but the codebook has no variable of that name — it carries `enhanced_collision_severity`, the same words transposed, reflecting the upstream DfT guide. The consequence is visible rather than hidden: `enhanced_severity_collision_label` exists on the view and is `NULL` for every row. Nothing tries to repair this by matching names fuzzily — a near-miss rule guessing which codebook variable a column "probably" means is exactly the silent wrong answer §6 exists to prevent. The raw code is shown instead.

One further fact from the same check: some `codebook` variables (`speed_limit`, `age_of_casualty`, and others) correspond to columns the manifest classifies as `numeric`, and have no label twin. A speed limit stays a number.

---

## 4. Execute-Then-Show, With a Bounded Repair Loop

There is no pre-execution confirmation step. The generated SQL runs immediately, and is displayed afterwards alongside the results.

A confirmation step would ask the user to approve SQL they cannot read — which is the entire reason they are using this tool. It converts a working flow into a rubber stamp, and a rubber stamp is a worse safety control than a machine-enforced one, because it looks like review without being review. The real control is that every query is read-only and sandboxed (§5); a bad query costs an API call, not data.

A question takes **two separate model calls**, in order:

1. **Write the SQL.** Returns a query, or declines because the build cannot answer the question (§6A).
2. **Describe the chart.** Made only after the SQL has actually run, and given the real result columns (§7).

They are separate conversations with their own system messages, not two turns of one chat. The chart call cannot be made until the query has succeeded — so it is never asked to plot columns that do not exist, and a chart failure cannot cost the user their table.

Each call carries its own repair loop. When execution raises a SQL error, the error text is fed back to the model, which refines the query, and the new query is run. Each loop is bounded by five conditions, all of which are requirements rather than defaults:

1. **Exactly three attempts per call** — the initial attempt plus at most two repairs. This is a fixed constant in v1, deliberately not user-configurable, because it is the user's own API credit that pays for each attempt and an unbounded loop is an unbounded bill. Six model calls is the worst case for a question, and the common case is two.
2. **Retry only on SQL execution errors.** A query that returns zero rows is *never* retried. Zero rows is a valid, informative answer; retrying on it makes the model progressively loosen the `WHERE` clause until something appears, which manufactures a result the data does not support. Empty results are explained instead (§6). A declined question (§6A) is terminal for the same reason: it is an answer, not a failure.
3. **The row cap applies to every attempt** (§5), so a repaired query cannot return more than the first one was allowed to.
4. **Safety validation re-runs on every attempt**, not only the first. A repaired query is new, unvalidated model output; treating attempt 2 as pre-approved because attempt 1 was validated would leave the sandbox open on exactly the path most likely to produce something strange.
5. **The full attempt history is shown**, not just the winning query — for example *"attempt 1 failed: Referenced column 'rain' not found → attempt 2 succeeded"*. The user is entitled to see what actually ran against their data, including what did not work.

If all three attempts fail, the application reports the failure and the errors. It does not present a partial or invented answer.

---

## 5. Safety Model

Every control is machine-enforced. None depends on the model behaving, and none depends on the user reviewing anything.

| Layer | Mechanism | Stops |
|-------|-----------|-------|
| **Read-only connection** | `duckdb.connect(path, read_only=True)` | All writes, at the engine level, regardless of what SQL reaches it. |
| **Single-`SELECT` parse check** | DuckDB's own parser via `extract_statements()` | Multiple statements; any statement whose type is not `SELECT`. |
| **Parse-tree function check** — *specified, not implemented; see below* | `json_serialize_sql()`, walked for function names | File- and network-reading functions (`read_csv*`, `read_parquet`, `read_json*`, `glob`, and similar). **Not built:** the *No external access* layer below stops these at execution time instead. |
| **No external access** | `config={"enable_external_access": False}` at connect | Any read or write of the filesystem or network from inside a query. |
| **Row cap** | `fetchmany(MAX_RESULT_ROWS + 1)` on the streaming result | Multi-million-row results reaching the browser. |
| **User cancellation** | Query runs on a worker thread; `connection.interrupt()` when the user presses Stop (§9) | A long query the user no longer wants to wait for. |

**One question at a time.** v1 is a local, single-user application, not a server, so it holds one DuckDB connection and runs one query at a time. This is worth stating because `interrupt()` cancels the *connection*, not a particular query — with two questions in flight it would kill both. A second question arriving while one is running is rejected rather than queued, and `/api/cancel` with nothing running is a no-op.

**There is no query timeout.** An earlier draft enforced one, on the reasoning that an unattended runaway join needed bounding. But the database is local and read-only, the worst outcome is a page that waits, and — more to the point — a slow query may be perfectly legitimate: a multi-year join across collisions and weather takes as long as it takes, and killing it at thirty seconds would be the tool failing the user, not protecting them. Judging that is the user's call, so they get a Stop button instead of an arbitrary constant.

Three details matter more than the list suggests.

**Validation uses DuckDB's parser, not a regular expression.** The statement check goes through the same parser that will execute the query, so there is no gap between "what the validator understood" and "what the engine will run" — the classic way SQL guards are bypassed. It also means comment-smuggling (`SELECT 1 -- ; DROP TABLE t`) is resolved correctly rather than by string matching. The function check, were it built, would go through that same parser for the same reason.

One qualification, since it looks like a contradiction otherwise: the statement check is preceded by a one-line first-word test on the caller's own string (`^\s*(SELECT|WITH)\b`), which is what closes the `PRAGMA` hole described immediately below. That regex decides nothing on its own — it only ever narrows what reaches the parser, and the parser runs afterwards regardless. A regex used *instead of* the parser is the anti-pattern this paragraph rejects; one used *in front of* it is not.

**Statement type alone is not enough.** DuckDB rewrites `PRAGMA version` into `SELECT * FROM pragma_version()` before reporting its type, so a `PRAGMA` statement would pass a check that only looks at parsed type — and some `PRAGMA` commands can change database settings, including re-enabling the external access this layer exists to block. So the validator also checks the statement's original text and rejects anything that doesn't start with `SELECT` or `WITH`, before the parse-type check ever runs. This is a verified behaviour of the engine, not a theoretical concern, which is why §11.3 requires an explicit `PRAGMA` test case.

**The layers are independent on purpose.** `enable_external_access = false` already blocks `read_csv_auto('/etc/hosts')` at execution time; the parse-tree check rejects it earlier. Either alone would do; both together mean a defect in one is not a breach. The read-only connection is the backstop for all of it, which is why §11.4 requires a test that attempts a write *as if the validator had been bypassed entirely*. That independence holds as written for every layer except the parse-tree function check, which is not built — see immediately below.

### The parse-tree function check is specified but not implemented

A deliberate decision, taken while planning `safety.py` and recorded here so that this document and the code agree about where they differ. The layer stays in the table above because it is the right shape for this threat and may yet be built; what follows is the honest account of the fact that it currently is not.

`safety.py` implements the single-`SELECT` parse check in full. It does **not** implement the parse-tree function check. A query calling `read_csv_auto`, `read_parquet`, `glob` or similar is a well-formed single `SELECT` statement, so it passes validation and is refused only at the moment it executes, by the *No external access* layer.

The reasoning is that `enable_external_access = false` stops a file- or network-reading function when it would run, regardless of whether anything checked for it beforehand. This was verified directly against the engine rather than assumed: `read_csv_auto`, `read_parquet`, `glob` and `read_text` each raise a DuckDB permission error on a connection opened with these settings. The function check would therefore be a second lock on a door the connection settings already weld shut — and it is by some distance the most complex and most engine-internals-dependent code the module would contain, for a threat model of one person running questions against their own local, read-only file.

Two costs, stated plainly rather than left to be discovered:

- **There is no second layer behind `enable_external_access = false` for file and network access specifically.** If that setting were ever misconfigured in `database.py`, nothing else would catch it. §11.4's read-only case and the validator's own suite both assert the setting directly, but neither is a substitute for the connection being right.
- **A file-reading query now surfaces as an ordinary SQL execution error rather than an immediate rejection.** §4's repair loop treats it like any other execution error, so it consumes attempts and fails identically each time before the user sees it. That costs attempts and produces a less friendly message; it does not reopen the file-access hole.

What is **not** affected: the single-`SELECT` parse check remains fully implemented and fully independent. It stops things the connection settings do not cover at all — `ATTACH`, which opens a *second* database file that is not read-only merely because the first connection is, and `PRAGMA`, `SET`, `INSTALL` and `LOAD`, which are how a query would reach for the settings themselves. That layer was never in question.

If the function check is ever built, delete this subsection and the two markers that point at it — in the table above, and in §11.3.

---

## 6. Avoiding Silent Wrong Answers

The most damaging failure mode is not an error. It is a query that runs cleanly, returns nothing, and looks like an answer.

The model does this work, not a checker bolted on afterwards. It has the full schema, codebook, and provenance in front of it (§3), so it is far better placed to notice that data is simply absent than any rule the application could apply to a query after the fact.

To make that possible the prompt states exactly what this build contains:

- the **tables and columns present**, from introspection, so a weatherless build never yields a query against `weather`;
- the **date range of every temporal column**, computed once at load with `MIN`/`MAX` — keyed on the column's *type*, never its name (§2C) — so "2019" can be recognised as outside a 2023 build;
- the **build parameters** — years, datasets, boundary mode — from `crossroads_meta`.

The system prompt then states the expectation explicitly: **return SQL, or return a plain-English reason why this database cannot answer the question.** Asked about 2019 against a 2023 build, or about rainfall against a build with no weather table, the user gets a direct answer immediately instead of an empty table to interpret. This is a capable model being told clearly what is wanted, not a heuristic — and it is the *only* mechanism, because a second one would be a second thing to keep correct.

Two constraints keep it from becoming its own failure mode:

- **Declining is for absent data, never for hard questions.** The prompt says so. A refusal the user could have had an answer to is worse than an empty result, because it sounds authoritative. When in doubt the model writes the query.
- **A decline is a successful outcome, not a failed attempt.** It consumes no repair attempt and does not trigger the loop in §4, for the same reason zero rows does not.

### When a query legitimately returns nothing

A question can be entirely answerable and still match no rows — no fatal collisions in fog in that district that year. The app says so plainly, shows the executed SQL, and skips the chart call: there is nothing to plot, and asking for a chart of an empty result wastes a call to produce an empty axis.

No attempt is made to diagnose *why* it was empty. An earlier draft tried to detect out-of-range year literals and out-of-codebook codes by statically analysing the generated SQL — reconstructing an explanation the model was already better placed to give up front — and that approach was cut. The decline mechanism described above is where emptiness gets prevented; this is just honest reporting of the cases that get through it anyway.

### When the query fails outright

If all three attempts raise SQL errors, the DuckDB error is shown as-is alongside the queries that produced it. It is not translated, softened, or wrapped in a friendlier message. Users of a tool that displays its own SQL can be trusted with a database error, and inventing a plainer explanation risks inventing a wrong one.

### The model's response is therefore structured

Declining means the first call has two possible replies, so it returns a small JSON object with an explicit discriminator rather than raw SQL text — requested through the API's structured-output support, which guarantees it parses:

```json
{ "answerable": true,  "sql": "SELECT ..." }
{ "answerable": false, "explanation": "This build contains 2023 only." }
```

A response whose discriminator and fields disagree — `answerable: true` with no `sql` — is malformed, and counts as a failed attempt under §4.

---

## 7. Charts

**Vega-Lite** is a declarative chart grammar: a small JSON object naming a mark type and which field belongs on which axis. The library derives scales, axes, and legends from that. Using it means "choose a chart" becomes a description the model can write, instead of plotting code the application has to contain.

The chart is the **second** model call (§4), made only once the SQL has run and returned at least one row. An empty result is reported without a chart (§6).

### The model is sent the shape of the result, not the result

The call receives the question, the executed SQL, and:

| Sent | Why |
|---|---|
| Column names and types | Which fields exist, and whether they are numbers, text, or dates |
| Row count | Eight rows is a bar chart; eight thousand is not |
| Distinct-value count per non-numeric column | Three severities belong on an axis; 380 local-authority codes do not |
| That rows arrive in intended display order | The SQL already sorted them (§3), so the chart preserves that order rather than re-deriving it |
| About five sample rows | Cheaper than describing the data in prose, and it shows real values |

All of it is computed in Python from rows already in memory — no extra query, and the bulk of the result never leaves the machine.

Asking after execution rather than alongside the SQL is what makes this call easy to get right: the columns being described demonstrably exist, because they came back from the database. A chart requested in the same breath as the SQL is a guess about a result that has not happened yet.

### The model returns a spec with no data in it

```json
{
  "mark": "bar",
  "encoding": {
    "x": {"field": "weather", "type": "nominal",
          "sort": ["Fine no high winds", "Raining no high winds", "..."]},
    "y": {"field": "collisions", "type": "quantitative"}
  }
}
```

The frontend supplies the rows at render time — `{...spec, data: {values: rows}}`. The model describes *how* to draw; the application provides *what* to draw. That keeps the spec small enough to read and check, and keeps the result set out of the prompt.

Python never edits a returned spec. Reaching into a Vega-Lite document to inject a sort array would be exactly the bespoke charting code this design exists to avoid — and there is now nothing to inject, because the rows arrive already ordered.

Choosing between a bar chart of counts by year and a stacked bar of severity by weather is a judgement about the question, which is the part of the problem the model is good at.

### Handling a bad spec

Unlike the SQL call, this response is **not** constrained by an output schema. A strict schema requires `additionalProperties: false` on every object, which cannot describe an arbitrary Vega-Lite document. The required shape is stated in the call's system message instead, and the reply is parsed as JSON and shape-checked on arrival — with a malformed spec repaired through the same three-attempt loop.

If all three attempts fail, **the table is still shown**. A chart is an enhancement; losing it must never cost the user their answer.

---

## 8. What Is Deterministic, and What Is Not

The database is deterministic. The SQL is deterministic. The language model is not, and that is fine.

AskCrossroads is a tool for **browsing** road-safety data and getting a feel for what is in it — not a pipeline for producing verifiable facts to publish. The same question asked twice may produce two different queries, both correct. Nothing here depends on it not doing that, and no attempt is made to pin sampling: the model runs on default settings (§10).

What the tool does owe the user is visibility into what actually produced the numbers on screen. So the executed SQL is always displayed, never tucked behind a disclosure, alongside the Crossroads-UK provenance line — version, schema, build parameters, build time — read from `crossroads_meta`. Anyone who wants to check a figure, or use it as the starting point for real analysis, can take that query and run it themselves against the same build and get the same table.

One supporting rule: **generated SQL should include an `ORDER BY`**, so a result does not reshuffle between runs of the same query. That is presentational, not a guarantee.

---

## 9. Stack, Interface, and Handling

### Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Backend | Python + FastAPI | The DuckDB connection, the safety validator, and the codebook logic are Python; FastAPI keeps the HTTP surface thin and the tests direct. |
| Frontend | One static page: vanilla JS + Vega-Lite | No Node toolchain, no build step, no `node_modules`. `git clone` then run — anything else is a barrier for the audience this tool exists for. |
| Model | Anthropic | See below. |
| Database | DuckDB, read-only | The format Crossroads-UK produces. |

### Anthropic only, in v1

Anthropic is the **only** supported provider in v1. The user supplies an Anthropic API key; no other provider is accepted, and there is no provider setting.

The call sits behind a thin internal interface so a second provider could be added later without touching call sites. That is an internal seam for future work, not a feature: v1 is not provider-agnostic, does not abstract over prompt formats or tool schemas, and should not be described as if it were.

### The database and the key come from configuration

Both the path to the `.db` file and the Anthropic API key are read at startup, from the environment or an untracked `.env`. Neither is entered in the browser. The application is already running on the user's own machine, started from a terminal; asking them to paste a path into a web page is ceremony, and it means building a setup screen whose only job is to collect something the process could have read itself.

The database is a **path**, never a browser upload: these files run to gigabytes, and DuckDB opens a local file far faster than a browser can post one. It is validated for existence and readability before the convention check in §1.

The consequence is that changing database means editing the configuration and restarting. For v1, where a user typically has one build, that is a fair trade for a page with one job (§13).

The application listens on `127.0.0.1` only, on port 8000 unless `ASKCROSSROADS_PORT` says otherwise. The host is deliberately not configurable: a server that spends the user's API credit and shows their data must not be reachable from the rest of their network. On start it prints its address and opens it in the default browser; `askcrossroads --no-browser` prints the address only. Ctrl+C stops it.

### API key handling

The key is held **in memory for the session only**. It is never written to disk by the application, never logged, never included in an error message, and never echoed back to the client — the `/api/ask` response contains no credential material, and provider errors are sanitised before they reach the browser.

An optional untracked `.env` may be read at startup for convenience. `.env` and `.env.*` are gitignored; a committed `.env.example` carries a placeholder only. No key, real or fabricated, appears in source, tests, documentation, or commit messages.

### HTTP surface

```text
GET  /api/database               -> {provenance, tables, date_ranges, coded_columns}
POST /api/ask        {question}  -> {status, answerable, sql, explanation, error,
                                     sql_attempts[], columns, rows, truncated,
                                     chart, chart_attempts[]}
POST /api/cancel                 -> 204; interrupts the running question (§5)
```

One request from the browser still means one question; the two model calls (§4) happen server-side and are reported separately. `sql_attempts[]` carries one entry per attempt with its SQL and its error, which is what §4.5 renders; `chart_attempts[]` is its counterpart, and `chart` is `null` when every attempt failed — the table is unaffected.

Every way a question can end is an ordinary `200` response, told apart by `status`: `answered`, `declined` (§6A — `answerable` is `false` only here, with the reason in `explanation`), `failed` (every attempt failed, or the model could not be reached; the message is in `error`), or `cancelled` (Stop was pressed). Only requests the application cannot act on are HTTP errors, each with an `{"error": "..."}` body: `409` when a question is already running (§5), `422` for an empty question or a malformed request, and `500` for a bug, whose details go to the terminal rather than the browser.

Result rows are arrays in column order. Values the browser cannot hold exactly are sent as strings: integers beyond JavaScript's safe range, and `NaN` and the infinities, which are not JSON and must not be shown as *Not recorded*. Dates and times are ISO 8601 strings.

`date_ranges` lists the earliest and latest value of every dated column of every stored table, as computed on load (§6). It replaces a single `years` field an earlier draft specified: the file holds no single "years" value AskCrossroads may rely on (§2C), and one of those dated columns is the build timestamp, which would stretch any combined range to the day the file was built.

### Interface

The UI is neat, minimalist, and professional: a single column, generous whitespace, one system font stack, no decorative colour. Its content is the question box, the results table, the chart, the executed SQL, and the provenance line. Nothing competes with those five things.

There is one control: the **Ask** button below the question box, which becomes **Stop** while a question is in flight and reverts when it returns. That single button is the whole cancellation story — pressing Stop calls `/api/cancel`, which interrupts the running query (§5). It replaces the timeout that an earlier draft specified: how long a query is worth waiting for is a judgement about the question, and the person who asked it is the one qualified to make it.

---

## 10. Configuration Constants

Every bound in this specification is a named constant in one module, not a literal at a call site. The point is that the guarantees made here are greppable.

```python
# src/askcrossroads/config.py
MODEL = "claude-opus-5"    # the only supported model in v1 (§9)
MAX_TOKENS = 16_000        # required by the API; caps thinking + response together
MAX_ATTEMPTS = 3           # per model call: initial attempt + at most 2 repairs (§4)
MAX_RESULT_ROWS = 5_000    # result rows returned to the browser (§5)
```

**Everything else is the API default.** No temperature, no effort level, no thinking configuration — partly because current Anthropic models reject `temperature`, `top_p`, and `top_k` outright with a 400, and partly because there is nothing here worth tuning before there is evidence that a default is wrong. A knob added speculatively is a knob nobody knows the right value for.

The row cap is applied by fetching `MAX_RESULT_ROWS + 1` rows and truncating, **not** by injecting a `LIMIT` into the generated SQL. Rewriting the query would mean the SQL displayed to the user was not the SQL they think it is (§8). Truncation is instead reported explicitly in the UI: when `truncated` is `true`, a notice is shown above the results table stating that the result was capped at `MAX_RESULT_ROWS` rows and that the chart reflects only those rows, not the full result.

---

## 11. Testing

Testing is a first-class deliverable of this project, not a follow-up to it.

The reason is specific to what this tool does: **it executes queries the user did not write, against the user's data, with no human checkpoint before execution.** That design (§4) is only defensible if the machinery around the model is provably correct. The safety layer in particular is a boundary, and a boundary that has not been tested is a boundary that is assumed rather than enforced. Broad coverage serves a second purpose too — every guarantee in this document is something a later change could quietly break, and a test is the only thing that notices.

### 11.1 Properties of the suite

- **The default suite runs fully offline.** The model is mocked. No API key, no network, and no real Crossroads-UK build is required. Anyone who clones the repository can run `pytest` immediately, on the same commit, and get the same result.
- **Fixtures are small synthetic DuckDB databases constructed in-test**, carrying the convention tables (`codebook`, `column_manifest`, `crossroads_meta`) in the shapes described in §2. A shared fixture lives in `tests/conftest.py`, following Crossroads-UK's pattern. No test may require a multi-gigabyte real build.
- **The split mirrors Crossroads-UK's**: the fast offline suite is the default `pytest` run; anything slow or networked is marked and run deliberately (`pytest -m integration`).
- **The default suite is deterministic.** No test in it may depend on live model output — a suite whose result depends on a sampled token is not a suite.
- **Every bug fixed after v1 gets a regression test.**

### 11.2 The integration suite

Mocking proves the machinery around the model is correct. It cannot prove the thing actually works — that a real Anthropic call, with the real prompt and the real prefix of nearly 40,000 tokens, returns SQL that a real Crossroads-UK build will execute. A tool assembled entirely from passing mocks can be broken end to end, and only a real call finds it.

So a second, marked suite makes real API calls:

```bash
python -m pytest                 # fast, offline, mocked — the default
python -m pytest -m integration  # real API calls, run deliberately
```

Rules:

- **It skips, it does not fail, when no API key is present.** Contributors without a key must get a clean pass on the default suite and clean *skips* here — never a red build for a credential they were never expected to have. The same applies when no real `.db` file is available — the path comes from `ASKCROSSROADS_TEST_DB`, and its absence is a skip, not a failure.
- **It is never part of the default run**, because it costs the runner money and needs network.
- **Assertions are on shape and execution, not on wording** — with one deliberate exception, the answer set below, which checks numbers. That a returned query parses, passes the validator, and runs against a real build is a stable assertion; that the model phrased a decline a particular way is not. Integration tests that assert on generated prose will fail for no reason and get muted, which is worse than not having them. A number is not prose: it is either right or it is not.

What it should cover: a question that produces working SQL end to end; a question the build cannot answer producing a decline rather than a query, and — the inverse, which no mock can check — a question it *can* answer producing SQL rather than a refusal; a chart call returning a spec that parses as Vega-Lite; and prompt caching actually registering a cache read on the second call — a silent failure that no offline test can detect, and which quietly triples cost when it breaks.

### The answer set

Everything above tests that the machinery works. None of it tests whether the answers are right.

A question can produce SQL that parses, passes the validator, runs without error, returns rows, and charts cleanly — and still be the wrong query. Every check described so far would pass. The user would see a number, and the number would be wrong. That is the failure §6 exists to prevent, and nothing in the suite so far can catch it.

So the integration suite carries a fixed set of **10–20 questions with known answers**. Each entry is a question in plain English, and the answer worked out once by hand, in SQL, against a **pinned Crossroads-UK build** — one specific version, schema version, dataset combination and year range, recorded alongside the questions. The test asks each question the ordinary way and compares the numbers that come back against the ones recorded.

- **The expected answers are written by a person, never copied from what the model returned.** Saving previous output only tests that the model is consistent, including consistently wrong. The hand-written query is stored next to its question, so a disagreement can be settled by reading it.
- **The check is on the numbers, not on the SQL that produced them.** Two correct queries for the same question can look nothing alike (§8). The tool does not promise a particular query; it promises a correct answer. Comparing generated SQL against reference SQL would fail every time the model wrote it differently.
- **The set covers the shapes that break.** Counting the rows of one table tests nothing. It must include at least: a total grouped by a coded column, so a label is needed (§3); a join across collisions, vehicles and casualties; a filter on a date range; a question whose true answer is no rows at all; and a question this build cannot answer, which must be declined rather than guessed (§6A).
- **It skips when the pinned build is not present**, like the rest of §11.2. `ASKCROSSROADS_TEST_DB` must point at a build matching the recorded details; anything else is a skip with a message saying why, never a failure. Checking these numbers against a different build would compare them against figures from other data.
- **A failure here is a signal, not proof of a bug.** The model is not deterministic (§8), so one wrong answer in twenty says something about the prompt rather than showing the code is broken. The value is in watching the set over time: a pass rate that drops after a prompt change is the only warning this project gets that its most important part has got worse.

This is the one part of the suite that answers *does AskCrossroads work?* rather than *does AskCrossroads run?*

### 11.3 The SQL safety validator

Tested most densely of anything in the project, with an explicit case per rejection rather than one test asserting "bad SQL is rejected":

Must reject:

- `DROP`
- `DELETE`
- `UPDATE`
- `INSERT`
- `CREATE`
- `ATTACH`
- `PRAGMA` (note the `SELECT`-rewrite in §5)
- `COPY`
- Any file-reading function (`read_csv_auto`, `read_parquet`, `glob`, …) — *specified, not implemented; see §5*
- Multiple statements separated by `;`
- A trailing statement smuggled after a valid `SELECT`

Positive cases are equally required: ordinary `SELECT`s, CTEs (`WITH … SELECT`), and subqueries must **pass**. A validator that rejects everything is not safe, it is broken, and only positive cases prove the difference.

**On the file-reading line item.** The parse-tree function check that would enforce it is not built (§5). The validator accepts these queries, and the connection refuses them when they run. The suite covers this as an *acceptance* paired with proof that executing the query raises — the same property tested from the other side, so that a change to either half is caught: the validator becoming stricter, or the connection settings being loosened. It is not restated as a rejection, because it is not one.

### 11.4 Everything else that must be covered

| Area | Required cases |
|------|----------------|
| **Read-only enforcement** | Attempt a write directly on the connection and assert it fails — proving the engine-level guard holds even if the validator were bypassed. |
| **Convention detection** | A database that has the convention tables; one that lacks them; and one with same-named tables of the **wrong shape**. Wrong shape must produce the §1 message, not a crash or a traceback. |
| **Label rendering** | A `NULL` in a label column renders as *Not recorded* rather than an empty cell; an uncovered column renders as its raw code (§3). |
| **Prompt prefix stability** | The same database always produces the exact same prefix, byte for byte — a new question never changes it, only a new database does. This is what makes caching work, and if it silently breaks, nothing else will catch it. |
| **Repair loop** | Retries on a SQL error; stops at exactly 3 attempts; and — the case that matters most — does **not** retry when a query returns zero rows. Both loops, SQL and chart, are covered independently. |
| **Chart call ordering** | The chart call is never made before the SQL succeeds, and is given the real result columns. Three failed chart attempts still return the table. |
| **Declining (§6A)** | A mocked decline is surfaced as an explanation, executes no SQL, and consumes no repair attempt. This tests the plumbing only — whether the model declines *correctly* cannot be asserted against a mock, and belongs to §11.2. |
| **Malformed model response** | `answerable: true` with no `sql`, and unparseable JSON, are treated as failed attempts and repaired, not crashed on. |
| **Empty results** | A query returning no rows is reported as such, and the chart call is skipped rather than made against an empty result. |
| **Schema introspection** | Builds with different dataset combinations, including one with no weather tables and one with no AADF tables, handled without error and without offering absent tables to the prompt. |
| **Row cap** | An oversized result is truncated at the cap and reported as truncated, without a `LIMIT` being added to the displayed SQL. |
| **Cancellation** | A running query is interrupted when Stop is pressed, and the connection is left usable for the next question. |

---

## 12. Repository Blueprint

```text
askcrossroads/
│
├── docs/
│   ├── ai-disclosure.md
│   ├── spec.md
│   └── plans/                          # Implementation plans, numbered sequentially
│
├── src/
│   └── askcrossroads/
│       ├── __init__.py
│       ├── app.py                      # FastAPI routes; serves the static page
│       ├── config.py                   # Named constants (§10)
│       ├── database.py                 # Connection, convention validation, provenance
│       ├── introspection.py            # information_schema discovery (§2A)
│       ├── prompt.py                   # Prompt assembly from discovered context
│       ├── llm.py                      # Thin provider interface; Anthropic implementation
│       ├── safety.py                   # SQL validator (§5)
│       ├── query.py                    # Execute, repair loop, empty-result explanation
│       └── static/
│           ├── index.html
│           ├── app.js                  # Vanilla JS; Vega-Lite rendering
│           └── style.css
│
├── tests/
│   ├── conftest.py                     # Shared synthetic Crossroads-UK fixture
│   ├── test_safety.py                  # Densest suite in the project (§11.3)
│   ├── test_database.py                # Convention detection, read-only enforcement
│   ├── test_query.py                   # Repair loops, row cap, cancellation, empty results
│   ├── test_api.py                     # Route behaviour with a mocked model
│   ├── test_server.py                  # The real command: refusal, localhost, Stop, Ctrl+C
│   ├── provider_stub.py                # Local stand-in for the Anthropic API (offline tests)
│   └── integration/                    # Marked; real API calls, skipped by default (§11.2)
│       ├── test_end_to_end.py          # A real question through the whole loop
│       ├── test_api_live.py            # A real question over HTTP
│       ├── test_answers.py             # The answer set: known answers, checked as numbers
│       └── answers.yaml                # Questions, hand-written SQL, expected values
│
├── .env.example                        # Placeholder only; never a real key
├── LICENSE
├── pyproject.toml
└── README.md
```

---

## 13. v1 Scope

v1 ships exactly one loop, end to end:

> **Ask a question → see the executed SQL, a results table, and a chart.**

The database path and API key come from configuration (§9), so from the user's side the application opens straight onto a question box.

An earlier draft included a set of curated example questions, runnable in one click, to show a first-time user what a good question looks like. They are cut, and the reason is §2C rather than scope: **a hardcoded example question is hardcoded knowledge of the data.** "How does rain affect severity in London?" assumes a weather dataset, a boundary mode, and a year range this build may not have — so the one feature meant to demonstrate the tool would be the most likely thing in it to fail on a valid file. Examples that adapt to the loaded build are a real feature, but they are a different and larger one.

### Explicitly deferred

Named here so that their absence reads as a decision rather than an omission:

query history · saved or exported results · multi-turn query refinement · additional LLM providers · authentication · any hosted deployment · PyPI publication · switching database without a restart · example questions generated from the loaded build.

Each of these is reasonable and none is in v1. The loop above has to work first.

---

## 14. AI & Engineering Governance

AskCrossroads embraces AI as a core efficiency asset of modern systems engineering. The full development loop is explicitly detailed in [AI_DISCLOSURE.md](ai-disclosure.md).
