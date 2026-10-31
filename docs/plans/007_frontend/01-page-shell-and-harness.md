# Stage 01 — Page Shell, Ask/Stop & Browser Harness

> Part of *Frontend*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular *Visual design*, *Data flow*, *The HTTP
> contract*, and *Verification*.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Plan 006 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
grep -c "The question page has not been built yet" src/askcrossroads/static/index.html \
  && ls src/askcrossroads/static/ \
  && grep -n "^class RealCommand\|^def free_port\|^posix_only\|^KEY\|^UNREACHABLE\|^SCRIPT" tests/test_server.py \
  && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest -q
```

Expected: `1`; the folder lists only `index.html`; six matching lines in `tests/test_server.py`; and
the suite is green. If `static/` holds anything else, stop and report it.

---

## Objective

Make the page exist and work for everything **except** drawing a table and a chart. That means the
provenance line, the Ask / Stop / Stopping… button, Enter to ask, and a message for every outcome
that is just words. Alongside that, build the harness that proves it in a real browser against the
real command.

---

## Implementation Steps

### 1. Add Playwright and the `browser` marker

`pyproject.toml`. Add one entry to the end of the `dev` list, with its comment:

```toml
  # Drives a real, invisible Chromium in tests/test_browser.py, so the page is checked the way a
  # person would see it. A Python package: the project still has no Node toolchain. After
  # installing, download its browser once with:
  #   .venv/bin/python -m playwright install chromium
  "playwright>=1.48",
```

In `[tool.pytest.ini_options]`, add a second entry to `markers`, after the `integration` line:

```toml
  "browser: drives the page in a real headless Chromium (Playwright); skipped when Chromium is not installed. Part of the default run; exclude with -m 'not browser'",
```

Do **not** change `addopts`. Browser tests run by default.

Install and download the browser:

```bash
.venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/python -m playwright install chromium
```

Expected: the first command ends with `Successfully installed ... playwright-...`. The second downloads
Chromium (about 150MB) into Playwright's cache folder, outside the repository. Then confirm:

```bash
.venv/bin/python -c "
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(Path(p.chromium.executable_path).exists())
    browser = p.chromium.launch(); page = browser.new_page(); page.set_content('<p>ok</p>')
    print(page.text_content('p')); browser.close()
"
```

Expected: `True` then `ok`. **If the launch fails** (for example a sandbox or permissions error),
stop and report the exact error. Do not work around it by making tests skip: the tests must
genuinely run for this stage to ship.

### 2. Move the real-command helpers into `tests/real_command.py`

The browser tests need `RealCommand` too. Test files must not import other test files, so move it to
a helper module, in the same way `tests/provider_stub.py` is a helper.

Create `tests/real_command.py` containing, **moved verbatim** from `tests/test_server.py`, these
pieces: the `KEY`, `UNREACHABLE` and `SCRIPT` constants (with their comments); `posix_only`;
`free_port()`; and the whole `RealCommand` class. Give it this docstring and these imports:

```python
"""The installed `askcrossroads` command, started as a user would start it, for tests.

Shared by tests/test_server.py and tests/test_browser.py. It lives here rather than in either
test file because pytest test files should not import one another.
"""

import os
import signal
import socket
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import httpx2
import pytest
```

In `tests/test_server.py`:

- Delete the moved definitions.
- Add `from real_command import KEY, SCRIPT, UNREACHABLE, RealCommand, free_port, posix_only` beside
  `from provider_stub import ProviderStub`.
- Keep everything else (`port_is_free`, the fixtures, all the tests) exactly as it is.
- Remove any `import` line that `tests/test_server.py` no longer uses. Check each one with
  `grep -n "<name>\." tests/test_server.py` before removing it. `socket` is still used by
  `port_is_free`; `subprocess`, `sysconfig` and `signal` will probably no longer be used.

Expected: `.venv/bin/python -m pytest tests/test_server.py -q` gives the same pass count as before
the move.

### 3. `ProviderStub.clear()`

`tests/provider_stub.py`. Add this method at the end of the *Scripting replies* section, after
`wait_for_requests`:

```python
    def clear(self):
        """Forget every queued reply and recorded request, so the next test starts clean.

        The browser tests share one stub across a module, and call this between tests.
        """
        with self._condition:
            self._replies.clear()
            self.requests.clear()
```

### 4. `src/askcrossroads/static/index.html`

Replace the placeholder entirely. This is the **complete** page structure: stages 02 and 03 fill
in the hidden parts, and stage 03 adds two `<script>` lines. Every `id` here is used by `app.js` or
by the tests.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AskCrossroads</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main>
    <header>
      <h1>AskCrossroads</h1>
      <!-- Which database is loaded, and what built it. Filled in by app.js. -->
      <p id="provenance" class="muted small">Reading the database…</p>
    </header>

    <form id="ask-form" data-state="idle">
      <label for="question" class="visually-hidden">Your question</label>
      <textarea id="question" rows="3" autofocus
                placeholder="Ask a question about this database in plain English"></textarea>
      <div class="controls">
        <!-- The page's one control: Ask, which becomes Stop while a question runs. -->
        <button id="ask-button" type="submit">Ask</button>
        <span id="progress" class="muted small" aria-live="polite"></span>
      </div>
    </form>

    <!-- One answer, top to bottom: a message, the truncation notice, the chart, the table,
         then the SQL that ran. app.js shows only the parts an answer needs. -->
    <section id="answer" hidden>
      <p id="message" class="message" aria-live="polite" hidden></p>
      <p id="truncation" class="notice" hidden></p>
      <div id="chart" hidden></div>
      <p id="chart-note" class="muted small" hidden></p>
      <div id="table-wrap" class="table-wrap" hidden>
        <table id="results"></table>
      </div>
      <section id="sql-section" hidden>
        <h2>SQL</h2>
        <pre id="sql" hidden></pre>
        <ol id="attempts"></ol>
      </section>
    </section>
  </main>

  <script src="app.js"></script>
</body>
</html>
```

The placeholder text must name nothing from the schema (spec §2C), so it has no example questions.

### 5. `src/askcrossroads/static/style.css`

New file, complete. This is the whole visual design described in the overview: modern, simple,
black and white, following the computer's light or dark setting. Stages 02 and 03 add **no** CSS
unless a test proves something is missing.

```css
/* AskCrossroads: one column, black and white, following the computer's light or dark setting.
   Colour is used only where it means something: the chart, and red for a failed question. */

:root {
  color-scheme: light dark;   /* scrollbars and form controls match the mode too */
  --font-sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  --bg: #ffffff;
  --surface: #fafafa;
  --text: #111111;
  --muted: #666666;
  --border: #e5e5e5;
  --button-bg: #111111;
  --button-text: #ffffff;
  --danger: #b42318;
  --radius: 8px;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0a0a0a;
    --surface: #171717;
    --text: #ededed;
    --muted: #a1a1a1;
    --border: #2e2e2e;
    --button-bg: #ededed;
    --button-text: #0a0a0a;
    --danger: #f97066;
  }
}

* {
  box-sizing: border-box;
}

/* The hidden attribute must always win, even over a class that sets display. */
[hidden] {
  display: none !important;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 16px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

main {
  max-width: 56rem;
  margin: 0 auto;
  padding: 3rem 1.5rem 4rem;
}

h1 {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 600;
  letter-spacing: -0.01em;
}

h2 {
  margin: 0 0 0.5rem;
  font-size: 0.875rem;
  font-weight: 600;
}

.muted {
  color: var(--muted);
}

.small {
  font-size: 0.875rem;
}

#provenance {
  margin: 0.25rem 0 0;
}

/* --- The question box and the one button --- */

form {
  margin-top: 2rem;
}

textarea {
  display: block;
  width: 100%;
  padding: 0.75rem 1rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  font: inherit;
  resize: vertical;
}

textarea:focus {
  outline: none;
  border-color: var(--text);
}

textarea:read-only {
  color: var(--muted);
}

.controls {
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-top: 0.75rem;
}

button {
  padding: 0.5rem 1.25rem;
  border: none;
  border-radius: var(--radius);
  background: var(--button-bg);
  color: var(--button-text);
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}

button:hover:not(:disabled) {
  opacity: 0.85;
}

button:disabled {
  opacity: 0.5;
  cursor: default;
}

button:focus-visible {
  outline: 2px solid var(--text);
  outline-offset: 2px;
}

/* --- The answer --- */

#answer {
  margin-top: 2.5rem;
}

/* Space between the parts of an answer that are showing. */
#answer > :not([hidden]) ~ :not([hidden]) {
  margin-top: 1.5rem;
}

.message {
  margin: 0;
}

.message.error {
  color: var(--danger);
}

.notice {
  margin: 0;
  padding: 0.75rem 1rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  font-size: 0.875rem;
}

#chart {
  overflow-x: auto;
}

#chart-note {
  margin: 0;
}

/* The table scrolls inside its own box, so a long result never pushes the SQL far away and a
   wide one never makes the page scroll sideways. */
.table-wrap {
  max-height: 28rem;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.875rem;
}

th,
td {
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
  text-align: left;
  white-space: nowrap;
}

th {
  position: sticky;   /* the header row stays put while the rows scroll */
  top: 0;
  background: var(--surface);
  font-weight: 600;
}

tbody tr:last-child td {
  border-bottom: none;
}

/* Numbers line up on the right, with every digit the same width. */
th.number,
td.number {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.not-recorded {
  color: var(--muted);
  font-style: italic;
}

/* --- The SQL --- */

pre {
  margin: 0;
  padding: 1rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  line-height: 1.6;
  white-space: pre-wrap;      /* long SQL wraps rather than scrolling sideways */
  overflow-wrap: anywhere;
}

#attempts {
  margin: 0.75rem 0 0;
  padding: 0;
  list-style: none;
  color: var(--muted);
  font-size: 0.875rem;
}

#attempts li + li {
  margin-top: 0.5rem;
}

#attempts pre {
  margin-top: 0.375rem;
}

/* Read by screen readers, not shown. */
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

@media (max-width: 600px) {
  main {
    padding: 2rem 1rem 3rem;
  }
}
```

### 6. `src/askcrossroads/static/app.js`

New file. This stage's version is complete for the button, the provenance line and messages.
Stage 02 replaces `render` and `clearAnswer`, and stage 03 adds the chart. Write it exactly in this
order, with these comments.

```js
// AskCrossroads: the question page.
//
// Plain JavaScript with no build step. The page talks to three routes on the local server:
//   GET  /api/database   what the loaded database is, for the line under the title
//   POST /api/ask        one question in, one answer out
//   POST /api/cancel     Stop
//
// Every piece of text that comes from the server (SQL, errors, explanations, table cells) is
// put on the page with textContent, never as HTML. So nothing in a question, an answer or the
// data can ever run as code here.

"use strict";

// --- The parts of the page this script works with ---------------------------------------

const form = document.getElementById("ask-form");
const questionBox = document.getElementById("question");
const askButton = document.getElementById("ask-button");
const progress = document.getElementById("progress");
const provenanceLine = document.getElementById("provenance");
const answerSection = document.getElementById("answer");
const messageLine = document.getElementById("message");

// --- Words the page shows ---------------------------------------------------------------
// Messages that come from the server (an error, a decline) are shown exactly as sent.
// These are the page's own.

const TEXT = {
  provenanceFailed: "Could not read the database description.",
  noConnection: "Could not reach AskCrossroads. Check that it is still running in the terminal.",
  unreadableReply: "AskCrossroads sent a reply this page could not read.",
  stopped: "Stopped. Nothing more was run.",
};

// What the one button says in each state.
const BUTTON_LABELS = { idle: "Ask", asking: "Stop", stopping: "Stopping…" };

// --- The Ask / Stop button --------------------------------------------------------------
// The page has one control, and it has three states:
//   idle      "Ask"        type a question and ask it
//   asking    "Stop"       a question is running; pressing Stop cancels it
//   stopping  "Stopping…"  Stop was pressed; waiting for the question to finish ending
// The button only goes back to "Ask" once /api/ask has actually replied, so a new question
// can never be sent while the server is still ending the old one.

let state = "idle";
let progressTimer = null;

function setState(next) {
  state = next;
  form.dataset.state = next;
  askButton.textContent = BUTTON_LABELS[next];
  askButton.disabled = next === "stopping";
  questionBox.readOnly = next !== "idle";
  if (next === "asking") {
    startProgress();
  } else if (next === "idle") {
    stopProgress();
  }
}

// "Working… 12s", so the person can judge whether a long question is worth waiting for.
function startProgress() {
  const started = Date.now();
  progress.textContent = "Working… 0s";
  progressTimer = setInterval(() => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    progress.textContent = `Working… ${seconds}s`;
  }, 1000);
}

function stopProgress() {
  clearInterval(progressTimer);
  progressTimer = null;
  progress.textContent = "";
}

// The button is the form's submit button, so a click on it arrives here.
form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (state === "idle") {
    askQuestion(questionBox.value);
  } else if (state === "asking") {
    stopQuestion();
  }
});

// Enter asks. Shift+Enter starts a new line, as in most question boxes. isComposing is true
// while someone is part-way through typing a character with an input method (an accented
// letter, say), and Enter then belongs to that, not to us.
questionBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (state === "idle") {
      askQuestion(questionBox.value);
    }
  }
});

// --- Asking and stopping ----------------------------------------------------------------

async function askQuestion(question) {
  setState("asking");
  clearAnswer();
  let reply;
  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    reply = await readReply(response);
  } catch (error) {
    // fetch() itself failed: the server is not there any more.
    reply = { problem: TEXT.noConnection, isError: true };
  }
  await render(reply);
  setState("idle");
}

// Turns an HTTP response into either { answer } or { problem, isError }.
async function readReply(response) {
  let body;
  try {
    body = await response.json();
  } catch (error) {
    return { problem: TEXT.unreadableReply, isError: true };
  }
  if (response.status === 200) {
    return { answer: body };
  }
  // 409 (a question is already running) and 422 (no question) are instructions, not failures.
  // A 500 is a bug in AskCrossroads, so it is shown as an error.
  const problem = (body && body.error) || TEXT.unreadableReply;
  return { problem, isError: response.status >= 500 };
}

async function stopQuestion() {
  setState("stopping");
  try {
    await fetch("/api/cancel", { method: "POST" });
  } catch (error) {
    // Nothing more to do here: if the server has gone, the pending question reports it.
  }
}

// --- Showing an answer ------------------------------------------------------------------

async function render(reply) {
  answerSection.hidden = false;
  if (reply.problem) {
    showMessage(reply.problem, reply.isError);
    return;
  }
  const answer = reply.answer;
  if (answer.status === "declined") {
    // A decline is an answer, not a failure, so it is never shown as an error.
    showMessage(answer.explanation, false);
  } else if (answer.status === "failed") {
    showMessage(answer.error, true);
  } else if (answer.status === "cancelled") {
    showMessage(TEXT.stopped, false);
  } else if (answer.status !== "answered") {
    showMessage(TEXT.unreadableReply, true);
  }
}

function showMessage(text, isError) {
  messageLine.textContent = text;
  messageLine.classList.toggle("error", isError);
  messageLine.hidden = false;
}

// Empties everything an earlier answer put on the page.
function clearAnswer() {
  answerSection.hidden = true;
  messageLine.hidden = true;
  messageLine.textContent = "";
  messageLine.classList.remove("error");
}

// --- The line under the title -----------------------------------------------------------

async function loadProvenance() {
  try {
    const response = await fetch("/api/database");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const summary = await response.json();
    provenanceLine.textContent = describeProvenance(summary.provenance);
  } catch (error) {
    provenanceLine.textContent = TEXT.provenanceFailed;
  }
}

// "Crossroads-UK 0.9.0 · schema 4 · built 2026-01-01 12:00:00 UTC · years: 2023 · …"
// The build parameters differ from build to build, so their names are never written here:
// every one the file records is listed, in the order the file gives them.
function describeProvenance(provenance) {
  const parts = [
    `Crossroads-UK ${provenance.crossroads_version}`,
    `schema ${provenance.schema_version}`,
    provenance.built_at_utc
      ? `built ${provenance.built_at_utc.replace("T", " ")} UTC`
      : "build time not recorded",
  ];
  for (const [name, value] of Object.entries(provenance.parameters || {})) {
    parts.push(`${name}: ${describeParameter(value)}`);
  }
  return parts.join(" · ");
}

// A list is shown as "a, b, c"; anything else as its plain text.
function describeParameter(value) {
  if (Array.isArray(value)) {
    return value.map(describeParameter).join(", ");
  }
  if (value !== null && typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

// --- Start ------------------------------------------------------------------------------

setState("idle");
loadProvenance();
```

---

## Testing & Verification

### 7. `tests/test_page.py`: no browser, always runs

```python
"""The question page as served, checked without a browser (spec §2C, §9).

These need nothing beyond the dev extra, so they always run. What the page actually shows is
proven in tests/test_browser.py. This file proves the page is put together safely: it loads only
its own files, every part the script looks for is present, and its own code neither names
anything from the schema nor writes HTML.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from askcrossroads import app, query
from askcrossroads.database import Database

from conftest import FakeModel

STATIC = Path(app.__file__).parent / "static"

# The page's own files. Vendored libraries, if any, are checked separately.
OWN_FILES = ["index.html", "app.js", "style.css"]

# Every file index.html links to, in the order it links them.
LINKED = ["style.css", "app.js"]

# The same words the repository-wide check uses (spec §2C).
SCHEMA_WORDS = re.compile(r"collision|weather|aadf|stats19|vehicle|casualty", re.IGNORECASE)


class _PageParts(HTMLParser):
    """Collects every element id and every src/href value in a page."""

    def __init__(self):
        super().__init__()
        self.ids = set()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        for name in ("src", "href"):
            if attributes.get(name):
                self.links.append(attributes[name])


@pytest.fixture
def client(crossroads_db):
    with Database.open(crossroads_db) as database:
        runner = query.QueryRunner(database, FakeModel())
        yield TestClient(app.create_app(runner, database.context))


@pytest.fixture
def page(client):
    response = client.get("/")
    assert response.status_code == 200
    parts = _PageParts()
    parts.feed(response.text)
    return parts


def test_the_page_links_only_local_files(page):
    assert page.links == LINKED
    for link in page.links:
        # Nothing with a scheme (https:, data:) and nothing protocol-relative (//host).
        assert not re.match(r"^([a-z][a-z0-9+.-]*:|//)", link, re.IGNORECASE), link


def test_every_linked_file_is_served(client, page):
    for link in page.links:
        response = client.get("/" + link)
        assert response.status_code == 200, link
        expected = "javascript" if link.endswith(".js") else "text/css"
        assert expected in response.headers["content-type"], link


def test_the_page_has_every_part_the_script_uses(page):
    script = (STATIC / "app.js").read_text()
    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    assert wanted, "app.js should look up the page's parts by id"
    missing = wanted - page.ids
    assert not missing, f"app.js looks for ids the page does not have: {sorted(missing)}"


def test_the_page_names_nothing_from_the_schema():
    for name in OWN_FILES:
        found = SCHEMA_WORDS.search((STATIC / name).read_text())
        assert found is None, f"{name} names {found.group(0)!r}; it must be discovered (spec §2C)"


def test_the_script_never_writes_html():
    script = (STATIC / "app.js").read_text()
    for writer in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert writer not in script, f"app.js uses {writer}; use textContent instead"


def test_the_styles_load_nothing():
    styles = (STATIC / "style.css").read_text()
    assert "@import" not in styles
    assert "url(" not in styles
```

### 8. `tests/test_browser.py`: the page in a real browser

Create the file with this module header, fixtures and helpers, then the tests below. Stages 02 and
03 add tests to this same file.

```python
"""The question page in a real browser (spec §3, §9).

Every test here opens the page in an invisible Chromium, driven by Playwright, against the REAL
askcrossroads command. That command is pointed at the local provider stub, so the real
AnthropicClient, real HTTP and a real synthetic DuckDB file are all in play, and only the
model's words are scripted. Assertions are on what a person would see: text, what is visible,
colours, positions. Nothing is checked by reading the page's source code.

These tests need Chromium for Playwright, a one-time download of about 150MB:

    .venv/bin/python -m playwright install chromium

Without it they are skipped, and the skip reason gives that command.
"""

import json
import re
import threading
from pathlib import Path

import httpx2
import pytest

from askcrossroads import app

from conftest import AADF, BANK_HOLIDAYS, ERA5_WEATHER, STATS19, build_crossroads_db
from provider_stub import ProviderStub
from real_command import KEY, RealCommand, posix_only

INSTALL_PLAYWRIGHT = "Playwright is not installed. Run: .venv/bin/pip install -e '.[dev]'"
INSTALL_CHROMIUM = (
    "Chromium for Playwright is not installed. "
    "Run: .venv/bin/python -m playwright install chromium"
)

# Skips this whole file when the playwright package itself is missing.
sync_api = pytest.importorskip("playwright.sync_api", reason=INSTALL_PLAYWRIGHT)
expect = sync_api.expect
# A real command and a real model loop can take a few seconds; the default is 5.
expect.set_options(timeout=15_000)

# The real command is stopped with Ctrl+C, which is POSIX-only (see real_command.py).
pytestmark = [posix_only, pytest.mark.browser]

QUESTION = "How many collisions were there in each kind of weather?"
# Readable labels, sorted by the underlying code, on the synthetic build (spec §3). Returns
# Fine 2, Raining 2, Snowing 1, then NULL 1.
GOOD_SQL = (
    "SELECT weather_conditions_label AS weather, COUNT(*) AS collisions "
    "FROM collisions_labelled "
    "GROUP BY weather_conditions_label "
    "ORDER BY MIN(weather_conditions)"
)
CHART = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "weather", "type": "nominal", "sort": None},
        "y": {"field": "collisions", "type": "quantitative"},
    },
}
# Runs for minutes unless interrupted. range() needs no table and no external access.
SLOW_SQL = "SELECT SUM(range) AS total FROM range(1000000000000)"

DECLINE_TEXT = "This build contains 2023 only."
BUSY = "A question is already being answered. Wait for it, or press Stop."
REJECTED_KEY = "rejected the configured key"   # llm.py's sentence for a rejected key
STOPPED = "Stopped. Nothing more was run."
NO_CONNECTION = "Could not reach AskCrossroads. Check that it is still running in the terminal."
PROVENANCE = (
    "Crossroads-UK 0.9.0 · schema 4 · built 2026-01-01 12:00:00 UTC · "
    "boundary_mode: snapshot · datasets: aadf, bank_holidays, era5_weather, stats19 · "
    "years: 2023"
)
ERROR_CLASS = re.compile(r"\berror\b")


def reply_decline(provider, explanation=DECLINE_TEXT):
    """The SQL call's structured reply when the build cannot answer (spec §6)."""
    provider.reply_text(json.dumps({"answerable": False, "sql": None, "explanation": explanation}))


@pytest.fixture(scope="session")
def chromium():
    """One headless Chromium for the whole run. Skips when it has not been downloaded."""
    with sync_api.sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip(INSTALL_CHROMIUM)
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The real askcrossroads command on a free port, talking to a local provider stub.

    One per test module, because starting the command takes a second or two. open_page resets
    the stub between tests. Yields (command, provider).
    """
    workdir = tmp_path_factory.mktemp("browser")
    database = build_crossroads_db(
        workdir / "crossroads.db", datasets=(STATS19, ERA5_WEATHER, AADF, BANK_HOLIDAYS)
    )
    with ProviderStub() as provider:
        command = RealCommand(workdir, database, provider_url=provider.url)
        try:
            command.wait_until_serving()
            yield command, provider
            assert command.stop() == 0, command.stderr()
        finally:
            command.close()


@pytest.fixture
def open_page(chromium, server):
    """Opens the page in a fresh browser context: open_page(color_scheme="light", viewport=None).

    Every request the page makes is checked. Any request addressed anywhere but the local server
    is aborted and recorded, and the test then fails: the page must never reach the internet.
    open_page.outside is that record.
    """
    command, provider = server
    contexts = []
    outside = []

    def guard(route):
        if route.request.url.startswith(command.url + "/"):
            route.continue_()
        else:
            outside.append(route.request.url)
            route.abort()

    def open_(color_scheme="light", viewport=None):
        context = chromium.new_context(
            color_scheme=color_scheme, viewport=viewport or {"width": 1280, "height": 900}
        )
        contexts.append(context)
        context.route("**/*", guard)
        page = context.new_page()
        page.set_default_timeout(15_000)
        page.goto(command.url + "/")
        # The page is ready once it has described the database.
        expect(page.locator("#provenance")).to_contain_text("Crossroads-UK")
        return page

    open_.outside = outside
    yield open_

    for context in contexts:
        context.close()
    # Leave the server idle and the stub empty for the next test.
    httpx2.post(command.url + "/api/cancel", timeout=10)
    provider.clear()
    assert outside == [], f"the page tried to reach outside the local server: {outside}"


def ask(page, question=QUESTION):
    """Type a question, press Ask, and wait until the reply has been drawn."""
    page.fill("#question", question)
    with page.expect_response(lambda response: response.url.endswith("/api/ask")):
        page.click("#ask-button")
    expect(page.locator("#ask-form")).to_have_attribute("data-state", "idle")


# Records the button's label and disabled flag every time either changes, into
# window.buttonStates, skipping repeats.
RECORD_BUTTON = """() => {
  window.buttonStates = [];
  const button = document.getElementById("ask-button");
  const record = () => {
    const now = `${button.textContent}|${button.disabled}`;
    if (window.buttonStates[window.buttonStates.length - 1] !== now) {
      window.buttonStates.push(now);
    }
  };
  new MutationObserver(record).observe(
    button, { attributes: true, childList: true, characterData: true, subtree: true });
}"""
```

Then these tests:

```python
def test_the_page_shows_what_built_the_database(open_page):
    page = open_page()
    expect(page).to_have_title("AskCrossroads")
    expect(page.locator("h1")).to_have_text("AskCrossroads")
    # Every build parameter is listed, in the file's own order, with none of their names
    # written into the page's code.
    expect(page.locator("#provenance")).to_have_text(PROVENANCE)


def test_the_page_opens_ready_for_a_question(open_page):
    page = open_page()
    expect(page.locator("#question")).to_be_focused()
    expect(page.locator("#question")).to_be_editable()
    expect(page.locator("#ask-button")).to_have_text("Ask")
    expect(page.locator("#ask-button")).to_be_enabled()
    expect(page.locator("#answer")).to_be_hidden()


def test_ask_becomes_stop_and_stop_cancels_the_question(open_page, server):
    command, provider = server
    provider.reply_sql(SLOW_SQL)
    page = open_page()
    page.fill("#question", QUESTION)
    page.click("#ask-button")

    button = page.locator("#ask-button")
    expect(button).to_have_text("Stop")
    expect(button).to_be_enabled()
    expect(page.locator("#question")).not_to_be_editable()
    expect(page.locator("#progress")).to_contain_text("Working…")
    assert provider.wait_for_requests(1)
    page.wait_for_timeout(500)   # let the slow query get under way

    page.evaluate(RECORD_BUTTON)
    page.click("#ask-button")    # Stop
    expect(page.locator("#ask-form")).to_have_attribute("data-state", "idle")

    # Stop disabled the button until the question had really ended, then it said Ask again.
    assert page.evaluate("window.buttonStates") == ["Stopping…|true", "Ask|false"]
    expect(page.locator("#message")).to_have_text(STOPPED)
    expect(page.locator("#message")).not_to_have_class(ERROR_CLASS)
    expect(page.locator("#progress")).to_have_text("")
    expect(page.locator("#question")).to_be_editable()

    # The next question works straight away.
    reply_decline(provider)
    ask(page, "And in 2019?")
    expect(page.locator("#message")).to_have_text(DECLINE_TEXT)


def test_enter_asks_and_shift_enter_starts_a_new_line(open_page, server):
    command, provider = server
    page = open_page()
    box = page.locator("#question")
    box.fill("How many")
    box.press("Shift+Enter")
    box.press_sequentially("in 2019?")
    expect(box).to_have_value("How many\nin 2019?")
    assert provider.requests == []   # Shift+Enter asked nothing

    reply_decline(provider)
    with page.expect_response(lambda response: response.url.endswith("/api/ask")):
        box.press("Enter")
    expect(page.locator("#ask-form")).to_have_attribute("data-state", "idle")
    expect(page.locator("#message")).to_have_text(DECLINE_TEXT)
    assert len(provider.requests) == 1
    assert "in 2019?" in json.dumps(provider.requests[0]["body"])


def test_an_empty_question_is_explained_without_calling_the_model(open_page, server):
    command, provider = server
    page = open_page()
    ask(page, "   ")
    expect(page.locator("#message")).to_have_text(app.EMPTY_QUESTION)
    expect(page.locator("#message")).not_to_have_class(ERROR_CLASS)
    assert provider.requests == []


def test_a_question_while_another_runs_explains_why(open_page, server):
    command, provider = server
    page = open_page()

    # Someone else (another tab) is already asking a slow question.
    provider.reply_sql(SLOW_SQL)
    outcome = {}

    def other_tab():
        outcome["response"] = httpx2.post(
            command.url + "/api/ask", json={"question": QUESTION}, timeout=60
        )

    worker = threading.Thread(target=other_tab, daemon=True)
    worker.start()
    assert provider.wait_for_requests(1)

    ask(page, "A second question")
    expect(page.locator("#message")).to_have_text(BUSY)
    expect(page.locator("#message")).not_to_have_class(ERROR_CLASS)
    expect(page.locator("#ask-button")).to_have_text("Ask")

    httpx2.post(command.url + "/api/cancel", timeout=10)
    worker.join(timeout=15)
    assert outcome["response"].json()["status"] == "cancelled"


def test_a_lost_connection_is_reported(open_page):
    page = open_page()
    # As if the server had been stopped: every /api/ask request fails to connect.
    page.route("**/api/ask", lambda route: route.abort())
    page.fill("#question", QUESTION)
    page.click("#ask-button")
    expect(page.locator("#message")).to_have_text(NO_CONNECTION)
    expect(page.locator("#message")).to_have_class(ERROR_CLASS)
    expect(page.locator("#ask-button")).to_have_text("Ask")


def test_a_provider_failure_is_shown_as_an_error(open_page, server):
    command, provider = server
    provider.reply_error(401, echo_key=True)   # a careless provider repeats the key
    page = open_page()
    ask(page)
    message = page.locator("#message")
    expect(message).to_contain_text(REJECTED_KEY)
    expect(message).to_have_class(ERROR_CLASS)
    assert page.evaluate("getComputedStyle(document.getElementById('message')).color") == (
        "rgb(180, 35, 24)"   # --danger, light mode
    )
    leaked = KEY in page.content()
    assert not leaked, "the API key reached the page"


@pytest.mark.parametrize(
    ("scheme", "background", "text", "button"),
    [
        ("light", "rgb(255, 255, 255)", "rgb(17, 17, 17)", "rgb(17, 17, 17)"),
        ("dark", "rgb(10, 10, 10)", "rgb(237, 237, 237)", "rgb(237, 237, 237)"),
    ],
)
def test_the_page_follows_the_computers_light_or_dark_setting(
    open_page, scheme, background, text, button
):
    page = open_page(color_scheme=scheme)
    colours = page.evaluate(
        """() => ({
          background: getComputedStyle(document.body).backgroundColor,
          text: getComputedStyle(document.body).color,
          button: getComputedStyle(document.getElementById("ask-button")).backgroundColor,
        })"""
    )
    assert colours == {"background": background, "text": text, "button": button}
```

### 9. Run

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest tests/test_page.py tests/test_browser.py -rs -v
```

Expected: every test passes, **0 skipped**. Then the full default suite:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

Expected: green. `tests/test_api.py::test_the_page_is_served_at_the_root` still passes, because the
page still contains "AskCrossroads".

### Stage ship-readiness checklist

1. Both commands above are green, with **0 skipped** in `tests/test_browser.py`.
2. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
3. `grep -nE "innerHTML|outerHTML|insertAdjacentHTML|document\.write" src/askcrossroads/static/app.js`
   returns nothing.
4. `git diff --stat src/askcrossroads/app.py` is empty.
5. `tests/test_server.py` defines no `RealCommand` and passes with the same count as before.
6. `git status` shows the changes and **nothing staged**.

---

## End State / Handoff

- `pyproject.toml` lists `playwright>=1.48` in `dev` and registers the `browser` marker. Playwright
  and its Chromium are installed in this environment.
- `tests/real_command.py` exports `KEY`, `SCRIPT`, `UNREACHABLE`, `RealCommand`, `free_port`,
  `posix_only`. `tests/test_server.py` imports them and is otherwise unchanged.
- `ProviderStub.clear()` exists.
- `static/index.html` has the **complete** structure, including the hidden `#truncation`, `#chart`,
  `#chart-note`, `#table-wrap`/`#results`, `#sql-section`/`#sql`/`#attempts`. It links
  `style.css` and `app.js` only.
- `static/style.css` is the complete design: the light and dark tokens from the overview, plus the
  table, SQL and notice styles that stage 02 relies on.
- `static/app.js` has `setState`, `askQuestion`, `readReply`, `stopQuestion`, `render`,
  `showMessage`, `clearAnswer`, `loadProvenance`, `describeProvenance`, `describeParameter`, and the
  `TEXT` table. For `answered` it shows the section but draws nothing yet.
- `tests/test_page.py` (6 tests) and `tests/test_browser.py` (10 tests, counting both parametrised
  cases) pass, with zero skips. The fixtures `chromium`, `server`, `open_page` and the helpers
  `ask`, `reply_decline` and `RECORD_BUTTON` are in `tests/test_browser.py` for later stages to reuse.

---

## Failure Modes & Rollback

| Failure | What it looks like | What to do |
|---|---|---|
| Chromium will not launch | `playwright._impl._errors.Error: ... launch ...` | Stop and report the exact error. Never turn it into a skip. |
| `executable_path` missing in the installed Playwright | `AttributeError` in the `chromium` fixture | Use `sync_api.sync_playwright().start()` plus `try: launch() except sync_api.Error as error: pytest.skip(f"{INSTALL_CHROMIUM} ({error})")` **only** when the message contains `Executable doesn't exist`; otherwise re-raise. Record it. |
| `autofocus` not honoured in the headless context | `to_be_focused` fails | Add `questionBox.focus();` under `// --- Start` in `app.js` and record it. |
| `buttonStates` shows an extra entry (e.g. `"Stopping…|false"`) | assertion lists three entries | Make sure `setState` sets `textContent` **and** `disabled` in the same synchronous block (it does as written). If an extra entry is still recorded, compare the list with the extra entry removed and record why. |
| `test_server.py` breaks after the move | `NameError` | An import or constant was not re-imported. Fix the import line. Do not change any test. |
| A test leaves a question running | the next test gets `409` | Every test that starts `SLOW_SQL` must end it (Stop, or `/api/cancel` plus a join) before it returns. The fixture's cancel is only a safety net. |
| Port or process left behind | a stray `askcrossroads` after the run | `RealCommand.close()` kills it in the `finally` block. Check that the `server` fixture's `finally` is intact. |

**Rollback:** `git checkout -- src/askcrossroads/static/index.html pyproject.toml tests/test_server.py tests/provider_stub.py`,
then delete `src/askcrossroads/static/app.js`, `src/askcrossroads/static/style.css`,
`tests/real_command.py`, `tests/test_page.py` and `tests/test_browser.py`. Then run
`.venv/bin/pip install -e '.[dev]'` again. Playwright being left installed in `.venv` is harmless.
