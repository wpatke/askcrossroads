# Frontend — Plan Overview

> Multi-stage plan. Each implementation session gets **this overview + ONE stage file**. Read this
> first. Each stage's *End State* is the contract: if a step does not match reality, adapt the step
> and record the deviation at the end of your report, but deliver the End State.

Replace the placeholder page with the real question page: a modern, simple, single-column page
where a person types a question in English and sees a chart, a results table, and the SQL that
produced them. It is proven in a real (invisible) browser by automated tests, because manual testing
is not part of this project.

---

## Context & Objective

### What exists when this plan starts

`docs/plans/001_project_foundation/` through `006_http_api/` are complete and committed. The whole
backend works and is tested. `askcrossroads` validates the database, listens on `127.0.0.1`, and
serves three routes plus whatever is in `src/askcrossroads/static/`. **That folder currently holds
only a placeholder `index.html`**, which says the page has not been built yet.

| File | What this plan uses from it |
|---|---|
| `src/askcrossroads/app.py` | `create_app(runner, context)`. It mounts `static/` at `/` with `html=True`, **last**, so `/` serves `index.html` and `/app.js`, `/style.css`, `/vendor/...` are served as files. The three routes and their exact response shapes are below. **This plan does not change `app.py`.** |
| `src/askcrossroads/config.py` | `MAX_RESULT_ROWS = 5_000`, `MAX_ATTEMPTS = 3`. The page never repeats these numbers. It reads them from the response, e.g. the length of `rows` when `truncated` is true. |
| `tests/provider_stub.py` | `ProviderStub`: a stand-in for the Anthropic API on localhost. `reply_sql(sql, delay=0)`, `reply_text(text)`, `reply_chart(spec)`, `reply_error(status, echo_key=False)`, `wait_for_requests(count)`, `.requests`, `.url`. Replies are served one per request, in order. With no reply queued it answers `400`. |
| `tests/test_server.py` | `RealCommand` (starts the installed `askcrossroads` script as a subprocess on a free port, environment built from scratch), `free_port()`, `posix_only`, `KEY`, `UNREACHABLE`, `SCRIPT`. Stage 01 moves these into a shared helper module so the browser tests can use them too. |
| `tests/conftest.py` | `build_crossroads_db(path, datasets=..., ...)`, the dataset constants `STATS19`, `ERA5_WEATHER`, `AADF`, `BANK_HOLIDAYS`, and `FakeModel`. Test files already do `from conftest import ...`. |
| `pyproject.toml` | The `dev` extra (`pytest`, `httpx2`) and the pytest settings. `addopts = "-m 'not integration'"`. |

### Facts about the synthetic database the tests rely on

The synthetic build (`build_crossroads_db` with all four datasets) holds six collisions, `c1`–`c6`:

| key | `weather_conditions` | `enhanced_severity_collision` |
|---|---|---|
| c1 | 1 | 1 |
| c2 | 2 | 2 |
| c3 | 1 | 1 |
| c4 | **NULL** | 3 |
| c5 | 2 | 2 |
| c6 | 3 | 1 |

Its codebook labels `weather_conditions` 1, 2, 3 as *Fine no high winds*, *Raining no high winds*,
*Snowing no high winds*. It has **no** codebook entry for `enhanced_severity_collision`, so
`collisions_labelled.enhanced_severity_collision_label` is `NULL` on every row. That reproduces the
documented exception in spec §3, where a coded column has no codebook entry. `accident_index` sorts
in key order (c1 first). Provenance is Crossroads-UK `0.9.0`, schema `4`, built
`2026-01-01T12:00:00`, with parameters `{"boundary_mode": "snapshot", "datasets": ["aadf",
"bank_holidays", "era5_weather", "stats19"], "years": [2023]}` in that key order.

So this query (the tests call it `GOOD_SQL`):

```sql
SELECT weather_conditions_label AS weather, COUNT(*) AS collisions
FROM collisions_labelled
GROUP BY weather_conditions_label
ORDER BY MIN(weather_conditions)
```

returns exactly four rows, in this order (DuckDB sorts NULL last by default):
`["Fine no high winds", 2]`, `["Raining no high winds", 2]`, `["Snowing no high winds", 1]`,
`[null, 1]`.

### What this plan changes

- `src/askcrossroads/static/index.html`: the real page, replacing the placeholder.
- `src/askcrossroads/static/style.css` (new): the whole visual design.
- `src/askcrossroads/static/app.js` (new): everything the page does.
- `src/askcrossroads/static/vendor/` (new): pinned, unmodified copies of the chart libraries Vega and
  Vega-Lite, their licence texts, and a `README.md` saying where they came from.
- `pyproject.toml`: `playwright` added to the `dev` extra, and a `browser` pytest marker.
- `tests/real_command.py` (new): `RealCommand` and its helpers, moved out of `tests/test_server.py`.
- `tests/test_server.py`: imports those helpers instead of defining them. No test changes.
- `tests/provider_stub.py`: one new method, `clear()`.
- `tests/test_page.py` (new): checks that need no browser.
- `tests/test_browser.py` (new): the page in a real headless Chromium.
- `docs/spec.md`: §3, §9, §11 and §12 brought in line with what was built (stage 03).

### The goal

A person who has never written SQL opens the page, types a question, presses **Ask** (or Enter),
and sees, top to bottom, a chart, a table, and the SQL that ran, with nothing else competing for
attention. Every way a question can end (answered, no rows, cut off at the row cap, declined, failed,
stopped, server busy, server gone) shows something sensible and honest. All of it is proven by tests
that drive a real browser against the real `askcrossroads` command.

---

## Approach / Architecture

### Three plain files, no build step

The page is `index.html` + `style.css` + `app.js`, plus two vendored library files. No framework, no
npm, no `package.json`, no bundler, no transpiler (spec §9). `app.js` is one plain script of small,
named functions with plain-language comments. The person committing this code reads all of it, so
**readability beats cleverness everywhere**.

### Visual design: modern and simple

The page should look **modern and simple**. The reference is the black-and-white style of
`vercel.com`: a white page, near-black text, thin grey borders, one solid near-black button, generous
whitespace, and the computer's own system font. When the computer is in dark mode, the page turns
the same design inside out: near-black background, near-white text, and a near-white button. It
follows the operating system automatically through CSS `prefers-color-scheme`. There is no toggle.

Rules that keep it simple:

- **One column**, centred, at most `56rem` wide, with `1.5rem` side padding (`1rem` on a phone).
- **One font stack** for text (the system UI font) and one monospace stack for SQL.
- **No decorative colour.** The page itself is greys only. Colour appears in exactly two places: the
  chart (Vega-Lite's default palette, which carries meaning) and a muted red for a question that
  *failed*. A declined question is an answer, not a failure, and is never red.
- **Flat**: thin `1px` borders and `8px` rounded corners. No shadows, gradients, icons or animation.
- **Order of an answer, top to bottom:** a message (if any), the truncation notice (if any), the
  chart, the table, then the SQL with its attempt history. The table scrolls inside a box at most
  `28rem` tall, with a header row that stays put, so even 5,000 rows never push the SQL far down the
  page.
- **Phone-safe:** at 375px wide the page never scrolls sideways. A wide table scrolls inside its own
  box, and long SQL wraps.

The exact colour values are fixed in stage 01's `style.css` so that the tests can check them:

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--bg` | `#ffffff` | `#0a0a0a` | page background |
| `--surface` | `#fafafa` | `#171717` | table header, SQL box, notices |
| `--text` | `#111111` | `#ededed` | body text, focus rings |
| `--muted` | `#666666` | `#a1a1a1` | provenance, progress, *Not recorded*, chart axis labels |
| `--border` | `#e5e5e5` | `#2e2e2e` | every border, chart gridlines |
| `--button-bg` / `--button-text` | `#111111` / `#ffffff` | `#ededed` / `#0a0a0a` | the one button |
| `--danger` | `#b42318` | `#f97066` | a failed question's message |

### Page layout

```text
┌──────────────────────────────────────────────────────────┐
│ AskCrossroads                                            │  h1
│ Crossroads-UK 0.9.0 · schema 4 · built … UTC · years: …  │  #provenance (muted, small)
│                                                          │
│ ┌──────────────────────────────────────────────────────┐ │
│ │ Ask a question about this database in plain English  │ │  #question (textarea)
│ └──────────────────────────────────────────────────────┘ │
│ [ Ask ]   Working… 4s                                    │  #ask-button, #progress
│                                                          │
│ message line                                             │  #message
│ ┌ Showing the first 5,000 rows only. … ────────────────┐ │  #truncation
│ ▇▇▇▇▇▇  ▇▇▇▇▇  ▇▇                                        │  #chart / #chart-note
│ ┌ weather              │ collisions ┐ (scrolls)          │  #table-wrap > #results
│ SQL                                                      │  #sql-section
│ ┌ SELECT …                                             ┐ │  #sql
│ Attempt 1 succeeded.                                     │  #attempts
└──────────────────────────────────────────────────────────┘
```

### Data flow

```text
page load
  GET /api/database  -> #provenance = "Crossroads-UK v · schema n · built … UTC · key: value · …"
                        (failure -> "Could not read the database description.")

Ask (button or Enter; Shift+Enter is a new line)
  state idle -> asking: button "Stop", textarea read-only, "Working… Ns" ticking, old answer cleared
  POST /api/ask {question}
    200  -> render(answer) by answer.status
    409/422/500 -> show body.error
    no connection -> "Could not reach AskCrossroads. Check that it is still running in the terminal."
  state -> idle: button "Ask"

Stop (the same button while asking)
  state asking -> stopping: button "Stopping…", disabled
  POST /api/cancel   (the pending /api/ask then returns status "cancelled")
  when /api/ask replies -> render -> idle
```

The button returns to **Ask** only when `/api/ask` has actually replied. That closes a race the HTTP
plan recorded: Stop during a model call leaves the server's one-question lock held for a few seconds,
so a question asked straight away would get `409`. The user simply cannot ask until the old question
has ended.

### The HTTP contract this page is built against (frozen; do not change `app.py`)

**`GET /api/database`** → `200`:

```json
{"provenance": {"crossroads_version": "0.9.0", "schema_version": 4,
                "built_at_utc": "2026-01-01T12:00:00",
                "parameters": {"boundary_mode": "snapshot", "datasets": ["..."], "years": [2023]}},
 "tables": ["..."], "date_ranges": [{"relation": "...", "column": "...", "earliest": "...", "latest": "..."}],
 "coded_columns": ["..."]}
```

`built_at_utc` may be `null`. `parameters` may be `{}`. Its keys vary between builds and **must not
be named in the page's code** (spec §2C), so the page lists every key it finds.

**`POST /api/ask`** `{"question": "..."}` → `200` for every way a question can end:

```json
{"status": "answered", "answerable": true, "sql": "SELECT ...", "explanation": null, "error": null,
 "sql_attempts": [{"sql": "SELECT ...", "error": "Binder Error: ..."}, {"sql": "SELECT ...", "error": null}],
 "columns": [{"name": "weather", "type": "VARCHAR"}, {"name": "collisions", "type": "BIGINT"}],
 "rows": [["Fine no high winds", 2], [null, 1]],
 "truncated": false, "chart": {"mark": "bar", "encoding": {}}, "chart_attempts": [{"error": null}]}
```

| `status` | What is set | What the page shows |
|---|---|---|
| `answered`, rows > 0 | `sql`, `columns`, `rows`, `truncated`, `chart` (may be `null`), both attempt lists | truncation notice if `truncated`; the chart, or a note saying why there is none; the table; the SQL and attempts |
| `answered`, rows = 0 | `sql`, `columns`, `rows: []`, `chart: null`, `chart_attempts: []` | "The query ran and returned no rows." and the SQL and attempts. No table, no chart, no chart note. |
| `declined` | `explanation`, `sql_attempts` (earlier failed tries, possibly `[]`) | the explanation as ordinary prose, **not** red; the attempts if there were any |
| `failed` | `error`, `sql_attempts` (possibly `[]`, e.g. the model could not be reached) | `error` in red; every attempt, each with its SQL and its error |
| `cancelled` | `sql_attempts` so far | "Stopped. Nothing more was run."; any attempts so far |

- An attempt with `"sql": null` is a model reply that could not be read. Show its error and no SQL.
- `chart_attempts` entries are `{"error": null}` (success) or `{"error": "..."}`.
- `409` `{"error": "A question is already being answered. Wait for it, or press Stop."}`, `422`
  `{"error": "Type a question first."}` or `{"error": "Send the question as JSON: ..."}`, `500`
  `{"error": "AskCrossroads hit an unexpected problem. ..."}`. The page shows `error` as it is. Only
  the `500` is red: `409` and `422` are instructions, not failures.

**`POST /api/cancel`** → `204`, no body, always.

**Row values** are arrays in column order. `null` is a database `NULL`. Numbers are JSON numbers,
except integers beyond 2⁵³ and `NaN`/`Infinity`/`-Infinity`, which arrive as **strings**. Dates and
times are ISO 8601 strings. Lists are arrays and structs are objects.

### The two display rules (spec §3), and how this page meets them

1. **Every `NULL` cell shows *Not recorded*** (muted, italic). The spec says "a `NULL` in a label
   column", but a query renames its columns (`weather_conditions_label AS weather`), so the browser
   cannot tell which result columns were label columns. In a Crossroads-UK build every `NULL` means
   the same thing anyway (upstream replaced missing and unknown codes with `NULL`), so the rule is
   applied to every cell. This is what the HTTP plan already promised (`None` → `null` → *Not
   recorded*). Stage 03 updates the spec's wording to say so.
2. **A column the codebook does not cover shows its raw code.** The page never translates codes: it
   has no codebook and no lookup, and it prints every non-`NULL` value exactly as the database
   returned it. So an uncovered column like `enhanced_severity_collision` shows `1`, `2`, `3`. It is
   never blank and never guessed.

Supporting rules that stop the honest value turning into a wrong one:

- `0`, `false` and `""` are values, **not** *Not recorded*. The test is `value === null`, never
  "falsy".
- Numbers are printed with `String(value)`, with **no** thousands separators. `2023` is a year and `1`
  is a code, and `2,023` would be wrong. Numeric columns (by DuckDB type name) are right-aligned with
  tabular figures, which is how a modern table stays readable without changing the digits.
- Lists and structs are printed as JSON text (`[1,2]`).
- The strings `"NaN"`, `"Infinity"` and big-integer strings are printed as they are.

### Charts

The model returns a Vega-Lite spec with no data in it (spec §7). The page draws
`{...spec, data: {values: rowObjects(columns, rows)}}` with the vendored libraries:

```js
const compiled = vegaLite.compile(fullSpec, { config: chartColours() }).spec;
const view = new vega.View(vega.parse(compiled), { container, renderer: "svg", loader: noLoading, hover: true });
await view.runAsync();
```

- **The model's spec is never edited.** The rows are added beside it as top-level `data`, which the
  backend guarantees the spec does not already have. Colours are passed through Vega-Lite's `config`
  *option*, which supplies defaults that the spec's own settings override. The page writes nothing
  into the spec.
- **Colours follow light and dark mode.** `chartColours()` reads the CSS tokens (`--text`, `--muted`,
  `--border`, the font) at draw time. The chart background is transparent. When the computer switches
  mode, the last chart is drawn again.
- **Charts cannot fetch anything.** A Vega spec can ask to load data or images from a URL (e.g. a
  `lookup` transform). The page gives Vega a loader whose `sanitize` always refuses, so drawing a
  chart never makes a network request. This keeps the promise that the data never leaves the machine.
- **A chart that cannot be drawn never costs the table.** The table is drawn first. Any error from
  compiling or drawing becomes a muted note: "The chart could not be drawn: …".
- **Rows become objects** keyed by column name, because Vega-Lite needs objects. If two columns share
  a name, the later one wins in the chart. The table still shows both. A `NULL` in a text or date
  column becomes *Not recorded*, so a chart labels that category the same way the table does. A
  `NULL` number stays `NULL`, and the chart leaves it out.
- **Charts fill the column.** The width is set for both continuous axes (numbers, dates) and
  discrete ones (categories), so a chart of a few categories is not a thin strip.

### Safety in the page

- **Every piece of text from the server goes in with `textContent`**: SQL, errors, explanations,
  cells, column names and provenance. `app.js` never uses `innerHTML`, `outerHTML`,
  `insertAdjacentHTML` or `document.write`. A test greps for these, and another proves that HTML
  in a result cell is shown as text, not run.
- **The page loads nothing from the internet.** All `src`/`href` values are relative paths to files
  in `static/`. A browser-test guard aborts and records any request that does not go to the local
  server, and every browser test fails if one was attempted.

### Verification: how the page is proven without anyone looking at it

Two test files, each proving what the other cannot:

1. **`tests/test_page.py`: no browser, always runs.** It uses FastAPI's `TestClient`, as
   `tests/test_api.py` does. It checks that the served page links only local files, in the right
   order; that every linked file is served with a JavaScript or CSS content type; that every element
   id the script needs exists; that the vendored files are byte-for-byte the pinned versions
   (SHA-256); that the licence files are present; and that the page's own files name nothing from the
   schema and never write HTML.
2. **`tests/test_browser.py`: a real browser.** [Playwright](https://playwright.dev/python/) (a
   Python package) drives an invisible Chromium against the **real `askcrossroads` command**. The
   command is pointed at the local provider stub, so the real `AnthropicClient`, real HTTP, real
   uvicorn and the real synthetic DuckDB file are all in play, and only the model's words are
   scripted. Every assertion is on what a person would see: text, which elements are visible,
   computed colours, positions on screen.
   - It needs Chromium for Playwright, a one-time download of about 150MB:
     `.venv/bin/python -m playwright install chromium`. **Without it, every browser test is skipped**
     with that command in the reason, so a contributor without it still gets a clean run.
   - The tests carry a `browser` marker, so `-m "not browser"` gives a quick run. They **are** part
     of the default `pytest` run.
   - Playwright's Python package contains its own private copy of Node.js, which it uses internally to
     talk to the browser. That is a test tool's internals, not a project toolchain: the repository
     gets no `package.json`, no `node_modules` and no build step, and running the app needs none of
     it.
   - **Shipping requires these tests to have actually run, not skipped.** Each stage's checklist runs
     them with `-rs` and requires zero skips.

One server and one stub per browser test module (module-scoped fixtures), and a fresh browser
context per test. Between tests the stub's queue is cleared and Stop is pressed, so no test can leave
a scripted reply or a running question behind for the next one.

### Decisions taken, and what was rejected

| Decision | Why | Rejected alternative |
|---|---|---|
| **Playwright + headless Chromium, skipped when Chromium is absent** | It is the only option that proves what a person sees (e.g. that a `NULL` cell *displays* "Not recorded"). It is a Python package, and it drives the real command. Skipping keeps the default suite runnable by anyone. | **Failing when absent**: forces a 150MB download on every contributor. **No browser** (grepping JS source): asserts the code *contains* "Not recorded", not that the page *shows* it. **Selenium**: needs a separately installed Chrome and driver, and is flakier. **A Python JS engine** (QuickJS, PyMiniRacer): has no DOM, so it could test only pure helpers. |
| **Vega 6.4.0 + Vega-Lite 6.5.0, vendored unmodified in `static/vendor/`** | Works offline, and the version can never change underneath the page. They are the latest stable releases, and Vega-Lite 6.5.0 requires `vega ^6.4.0`. Both are BSD-3-Clause, and both ship plain browser builds (`build/*.min.js`) that set the globals `vega` and `vegaLite`. That is about 780KB in total. | **A CDN**: charts break offline, and the page contacts a third party on every load. **`vega-embed`**: a third file that adds menus we would switch off. The direct Vega API is about five lines. |
| **Black and white, following the OS light/dark setting, no toggle** | Modern and simple, it matches the spec's "no decorative colour", and dark mode costs one CSS block. | Light only (looks dated on a dark-mode computer). An accent colour (breaks the spec rule). A toggle (a second control on a one-control page). |
| **Chart, table, SQL, top to bottom** | The chart is the quickest read for someone who does not write SQL. The table is height-capped, so the SQL stays close. The SQL is always visible, never behind a disclosure (spec §8). | Table first (spec listing order). SQL first. |
| **Every `NULL` → *Not recorded*** | The browser cannot identify label columns after aliasing, and every `NULL` in a Crossroads-UK build means "not recorded". | Identifying label columns by name pattern: hardcoding (§2C), and wrong after `AS`. |
| **No number formatting in the table** | Years and codes are numbers, and separators would corrupt them. | `toLocaleString()`: turns 2023 into "2,023". |
| **Enter asks, Shift+Enter is a new line** | The modern convention for a question box. | Ctrl/Cmd+Enter only: undiscoverable. |
| **The empty-question check stays on the server** | One source of truth for the wording ("Type a question first."). It costs one local round trip and no model call. | Duplicating the check and its wording in JS. |
| **The browser tests drive the real installed command** | Proves the shipped launch path serves the shipped files. The harness (`RealCommand`, `ProviderStub`) already exists. | An in-process uvicorn thread: faster, but would not prove the command serves the page. |

---

## Cross-Cutting Constraints

Every stage obeys all of these.

- **Never stage, commit, or amend anything in git.** Leave all changes in the working tree and tell
  the user what changed. This is absolute.
- **The API key is absolute.** Never hardcode a key or any credential anywhere, not even a fake one.
  Tests use the existing placeholder `KEY = "test-key-not-a-real-credential"`. In tests, check for a
  leak with `leaked = KEY in text` then `assert not leaked, "..."`, never `assert KEY not in text`.
- **Tests never read the developer's real configuration.** The real command is started only through
  `RealCommand`, which builds its environment from scratch and points `HOME` at a temporary folder.
- **No offline test can reach the network.** The command's model client points at the local stub.
  The browser guard aborts any request not addressed to the local server.
- **Nothing about the schema is hardcoded in `src/`** (spec §2C). `index.html`, `app.js` and
  `style.css` name no table, column, view, dataset or code. That includes the textarea's placeholder
  text: no example questions. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/`
  must still return nothing. The vendored files were checked and contain none of these words. Tests
  may use the synthetic fixture's names.
- **No framework, no build, no npm.** Plain HTML, CSS and JavaScript. No `package.json`, no
  `node_modules`, no transpiled syntax. Modern JavaScript that current browsers run directly (`const`,
  `let`, arrow functions, `async`/`await`, template strings) is fine.
- **Text from the server is set with `textContent`, always.** No `innerHTML`, `outerHTML`,
  `insertAdjacentHTML`, `document.write`.
- **Nothing is loaded from outside `static/`.** No CDN, no web font, no analytics.
- **Vendored files are never edited.** Their SHA-256 is pinned in `tests/test_page.py`.
- **`app.py` is not modified.** The HTTP contract above is frozen. If the page seems to need an API
  change, stop and report it instead.
- **Code is simple and readable.** JavaScript uses `"use strict"`, 2-space indentation and lines
  under about 100 characters. Functions are small and named for what they do, with plain-language
  comments where they earn their place. Python test code follows the style of `tests/test_server.py`.
- **Python 3.11 or newer, via the existing `.venv/`.** The system `python3` on the development
  machine is 3.9 and will not work.
- **Every stage leaves the default `pytest` run green**, with the browser tests **run, not skipped**.
- **Never mention private planning documents** in anything written to the repository.

### The commands every stage uses

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

The default, offline suite, with every variable that could leak the developer's configuration
removed:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

The browser tests alone, showing the reason for any skip (`-rs`). **Expected: zero skipped.**

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest tests/test_browser.py -rs
```

---

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Page shell, Ask/Stop & browser harness | Playwright dev dependency, the `browser` marker and Chromium install. `RealCommand` moved to `tests/real_command.py`, plus `ProviderStub.clear()`. The full `index.html` structure and the full `style.css` design. `app.js` with the provenance line, the Ask → Stop → Stopping… → Ask state machine, Enter to ask, and the message line for declined, failed, cancelled, 409/422/500 and a lost connection. | The page loads at `/`, shows the provenance line, asks and stops questions, and shows a message for every non-table outcome, in light and dark mode. `tests/test_page.py` and `tests/test_browser.py` are green with **zero skips**. `tests/test_server.py` is unchanged in behaviour. | Plan 006 complete | [01-page-shell-and-harness.md](01-page-shell-and-harness.md) |
| 02 | Answer rendering | The results table with the *Not recorded* and raw-value rules and numeric alignment; the truncation notice; the empty-result message; the SQL box and the attempt history; clearing the previous answer; the phone-width check. | Every `/api/ask` outcome renders as the table under *The HTTP contract* says, **except the chart**. The display rules are proven in the browser against the real command, including HTML-in-data shown as text. Green, zero skips. | 01 | [02-answer-rendering.md](02-answer-rendering.md) |
| 03 | Charts, vendored libraries & ship | Vendor Vega 6.4.0 / Vega-Lite 6.5.0 with licences and a README. Load them, then draw `{...spec, data}` in SVG with theme colours, the no-fetch loader, a redraw on light/dark change, and a note when there is no chart or it cannot be drawn. Update spec §3, §9, §11, §12. Ship checklist. | Charts draw above the table in both modes; a missing or broken chart never costs the table; no request ever leaves localhost; vendored bytes are pinned. The spec matches the code. Full suite green, zero browser skips, all ship greps clean. | 02 | [03-charts-and-ship.md](03-charts-and-ship.md) |

Stages must be done in order.

---

## Global Testing & Ship

| What must be proved | Test | Stage |
|---|---|---|
| The page links only local files, and each is served with the right type | `test_page.py::test_the_page_links_only_local_files`, `::test_every_linked_file_is_served` | 01, extended in 03 |
| Every element id the script uses exists (it reads the ids from `app.js`, so it grows with the script) | `test_page.py::test_the_page_has_every_part_the_script_uses` | 01 |
| The page's own files hardcode nothing about the data and never write HTML | `test_page.py::test_the_page_names_nothing_from_the_schema`, `::test_the_script_never_writes_html` | 01 |
| The provenance line shows what built the file, every parameter included | `test_browser.py::test_the_page_shows_what_built_the_database` | 01 |
| Ask becomes Stop; Stop cancels; the button stays disabled until the question has ended | `test_browser.py::test_ask_becomes_stop_and_stop_cancels_the_question` | 01 |
| Enter asks; Shift+Enter makes a new line | `test_browser.py::test_enter_asks_and_shift_enter_starts_a_new_line` | 01 |
| Busy (409), empty question (422) and a lost connection are each explained | `test_browser.py::test_a_question_while_another_runs_explains_why`, `::test_an_empty_question_is_explained_without_calling_the_model`, `::test_a_lost_connection_is_reported` | 01 |
| A failure is shown in red, and the key never reaches the page | `test_browser.py::test_a_provider_failure_is_shown_as_an_error` | 01 |
| Light and dark mode follow the computer | `test_browser.py::test_the_page_follows_the_computers_light_or_dark_setting` | 01 |
| **A `NULL` displays *Not recorded*; `0`, `false` and `""` do not** | `test_browser.py::test_an_answer_shows_its_table_and_sql`, `::test_values_are_shown_exactly_as_the_database_returned_them` | 02 |
| **An uncovered coded column displays its raw code** | `test_browser.py::test_an_uncovered_coded_column_shows_its_raw_code` | 02 |
| HTML inside data is shown as text, never run | `test_browser.py::test_text_from_the_data_is_never_run_as_code` | 02 |
| Repaired queries show every attempt; a failed question shows every attempt's SQL and error | `test_browser.py::test_a_repaired_query_shows_every_attempt`, `::test_a_failed_question_shows_every_attempt` | 02 |
| Empty, truncated and declined results render as specified | `test_browser.py::test_a_result_with_no_rows_says_so`, `::test_a_truncated_result_is_announced_above_the_table`, `::test_a_declined_question_reads_as_an_answer` | 02 |
| A new question clears the previous answer | `test_browser.py::test_a_new_question_clears_the_last_answer` | 02 |
| No sideways scrolling at phone width | `test_browser.py::test_the_page_fits_a_phone_screen` | 02 |
| The vendored libraries are the pinned bytes, with licences | `test_page.py::test_the_vendored_libraries_are_the_pinned_versions`, `::test_the_vendored_licences_are_present` | 03 |
| **The chart draws above the table, from the rows** | `test_browser.py::test_an_answer_draws_its_chart_above_the_table` | 03 |
| No chart, or an undrawable chart, still shows the table | `test_browser.py::test_no_chart_still_shows_the_table`, `::test_a_chart_that_cannot_be_drawn_still_shows_the_table` | 03 |
| A chart cannot make a network request | `test_browser.py::test_a_chart_cannot_fetch_anything` | 03 |
| The chart follows light and dark mode, including a live switch | `test_browser.py::test_the_chart_follows_light_and_dark_mode` | 03 |
| **No browser test ever requested anything from outside the local server** | the `open_page` fixture's teardown, on every browser test | 01–03 |
| The real command still starts, serves, stops and refuses as before | all of `tests/test_server.py`, unchanged | 01 |

### Ship-readiness for this plan (run after stage 03)

1. The default suite command above is green.
2. `tests/test_browser.py -rs` shows **0 skipped**.
3. `.venv/bin/python -m pytest -m integration` passes or skips cleanly (nothing in this plan touches
   it).
4. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
5. `grep -rnE "innerHTML|outerHTML|insertAdjacentHTML|document\.write" src/askcrossroads/static/app.js`
   returns nothing.
6. `grep -rnE "(src|href)=\"(https?:)?//" src/askcrossroads/static/index.html` returns nothing.
7. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
8. `git diff --stat src/askcrossroads/app.py` is empty.
9. There is no `package.json` and no `node_modules` anywhere outside `.venv/`:
   `find . -path ./.venv -prune -o \( -name package.json -o -name node_modules \) -print` prints
   nothing.
10. A manual run is **not** required and is not part of shipping. The browser tests are the proof.
11. `git status` shows the new and modified files and **nothing staged**.

---

## Performance

- **The table** is built once per answer with `document.createElement`, appended into one `<tbody>`
  that is not yet in the page, then swapped in with a single `replaceChildren`. At the 5,000-row cap
  with ~10 columns that is 50,000 cells, roughly 100–300ms in Chromium. That is acceptable for a
  one-off render after a model call that took seconds. No virtual scrolling: it would add complexity
  for no observed problem.
- **The chart** is Vega's SVG renderer over at most 5,000 rows. That is well within Vega's comfortable
  range for bars and lines. A scatter of 5,000 points renders in well under a second.
- **Page load**: about 780KB of vendored JavaScript, read from localhost, parsed once. There is no
  network latency to hide.
- **The browser test suite**: one `askcrossroads` process and one Chromium per module, and a new
  browser context per test (tens of milliseconds). Expect about 28 browser tests in about 30–60
  seconds. The truncation test renders 5,000 rows and is the slowest.

---

## Open Questions / Risks

- **The Chromium download is a one-time setup step** that a fresh contributor must know about. It
  goes in the skip reason now, and in the README when a later plan writes one.
- **Playwright's own internals use Node.js.** This is stated plainly above and in the spec's testing
  section (stage 03). If the user later decides any Node.js on the machine is unacceptable, the
  browser tests would have to move to Selenium with a system Chrome. That is a test-only change, and
  the page itself would be unaffected.
- **Vega-Lite field names with dots or brackets.** Vega-Lite reads `a.b` in a `field` as "field `b`
  inside object `a`". A result column literally named `a.b` would not chart unless the model escapes
  it. The page shows the "could not be drawn" note, or an empty chart, and the table is unaffected.
  Revisit only with an observed instance.
- **The vega-lite build ends with `//# sourceMappingURL=vega-lite.min.js.map`.** The map is not
  vendored. Browsers request it only when developer tools are open, and it then 404s locally,
  harmlessly. Playwright does not request it. The file is not edited, because its hash is pinned.
- **Vega evaluates chart expressions by generating JavaScript functions.** The spec comes from the
  model, on the user's own machine, and charts cannot fetch anything. A Content-Security-Policy that
  forbids this would need Vega's separate interpreter package and an API change. Both are out of
  scope.
