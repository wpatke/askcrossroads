# Stage 02 — Answer Rendering

> Part of *Frontend*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular *The HTTP contract*, *The two display
> rules*, and *Visual design*.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 01 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
test -f src/askcrossroads/static/app.js && test -f src/askcrossroads/static/style.css \
  && test -f tests/real_command.py && test -f tests/test_browser.py \
  && grep -c 'id="results"\|id="sql-section"\|id="truncation"' src/askcrossroads/static/index.html \
  && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest tests/test_page.py tests/test_browser.py -rs -q
```

Expected: `3`, then all tests pass with **0 skipped**. If Chromium is missing, run
`.venv/bin/python -m playwright install chromium` first.

---

## Objective

Draw every answer except the chart: the results table with the *Not recorded* and raw-value rules,
the truncation notice, the "no rows" message, the SQL box, and the full attempt history. All of it
is proven in the browser against the real command.

---

## Implementation Steps

All changes are in `src/askcrossroads/static/app.js`. `index.html` and `style.css` already contain
everything this stage needs. Do not change them unless a test below proves something is missing,
and record any such change.

### 1. Look up the new parts of the page

In *The parts of the page this script works with*, add after `messageLine`:

```js
const truncationNotice = document.getElementById("truncation");
const tableWrap = document.getElementById("table-wrap");
const resultsTable = document.getElementById("results");
const sqlSection = document.getElementById("sql-section");
const sqlBlock = document.getElementById("sql");
const attemptsList = document.getElementById("attempts");
```

### 2. New words

Add two entries to `TEXT`, after `stopped`:

```js
  noRows: "The query ran and returned no rows.",
  notRecorded: "Not recorded",
```

### 3. Replace `render` and `clearAnswer`

Replace the stage 01 `render` with:

```js
async function render(reply) {
  answerSection.hidden = false;
  if (reply.problem) {
    showMessage(reply.problem, reply.isError);
    return;
  }
  const answer = reply.answer;
  if (answer.status === "answered") {
    showAnswered(answer);
  } else if (answer.status === "declined") {
    // A decline is an answer, not a failure, so it is never shown as an error.
    showMessage(answer.explanation, false);
  } else if (answer.status === "failed") {
    showMessage(answer.error, true);
  } else if (answer.status === "cancelled") {
    showMessage(TEXT.stopped, false);
  } else {
    showMessage(TEXT.unreadableReply, true);
    return;
  }
  // Whatever the outcome, show what was run against the data, including what did not work.
  showSql(answer.sql, answer.sql_attempts);
}

function showAnswered(answer) {
  if (answer.rows.length === 0) {
    // Zero rows is a real answer, and there is nothing to chart.
    showMessage(TEXT.noRows, false);
    return;
  }
  if (answer.truncated) {
    truncationNotice.textContent = truncationText(answer.rows.length);
    truncationNotice.hidden = false;
  }
  drawTable(answer.columns, answer.rows);
}

// The server sends at most its row cap, so when a result was cut off, the number of rows
// received IS the cap. The page never needs to know the number itself.
function truncationText(count) {
  return (
    `Showing the first ${count.toLocaleString("en-GB")} rows only. ` +
    "The full result is larger, and the chart shows only these rows."
  );
}
```

Replace the stage 01 `clearAnswer` with:

```js
// Empties everything an earlier answer put on the page.
function clearAnswer() {
  answerSection.hidden = true;
  for (const part of [messageLine, truncationNotice, tableWrap, sqlSection, sqlBlock]) {
    part.hidden = true;
  }
  messageLine.textContent = "";
  messageLine.classList.remove("error");
  truncationNotice.textContent = "";
  resultsTable.replaceChildren();
  sqlBlock.textContent = "";
  attemptsList.replaceChildren();
}
```

`showMessage` is unchanged.

### 4. The table

Add a new section after `clearAnswer`:

```js
// --- The results table ------------------------------------------------------------------
// Two rules decide what a cell shows:
//   - a NULL shows "Not recorded". Crossroads-UK replaced missing and unknown codes with NULL
//     before this file was built, so which one it was cannot be known, and is not guessed;
//   - everything else shows exactly what the database returned. Codes are never translated
//     here, so a code the codebook does not cover shows as the code itself.

// DuckDB's own names for its numeric types: the engine's vocabulary, not knowledge of the
// data. Used only to right-align number columns. The numbers themselves are never reformatted.
const NUMBER_TYPES = [
  "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
  "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
  "FLOAT", "DOUBLE", "DECIMAL",
];

// A DECIMAL arrives with its precision, e.g. "DECIMAL(18,3)", so only the name is compared.
function isNumberType(typeName) {
  return NUMBER_TYPES.includes(typeName.split("(")[0]);
}

function drawTable(columns, rows) {
  const numeric = columns.map((column) => isNumberType(column.type));

  const head = document.createElement("thead");
  const headRow = head.insertRow();
  columns.forEach((column, index) => {
    const heading = document.createElement("th");
    heading.scope = "col";
    heading.textContent = column.name;
    if (numeric[index]) {
      heading.className = "number";
    }
    headRow.append(heading);
  });

  // Built away from the page, then put in with one change, so 5,000 rows draw quickly.
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tableRow = body.insertRow();
    row.forEach((value, index) => {
      const cell = tableRow.insertCell();
      // Only NULL is "Not recorded". 0, false and "" are real values.
      if (value === null) {
        cell.textContent = TEXT.notRecorded;
        cell.className = "not-recorded";
      } else {
        cell.textContent = cellText(value);
        if (numeric[index]) {
          cell.className = "number";
        }
      }
    });
  }

  resultsTable.replaceChildren(head, body);
  tableWrap.hidden = false;
}

// The text for one non-NULL value. Numbers get no thousands separators: a year (2023) or a
// code (1) is a number too, and "2,023" would be wrong. A list or a struct is shown as JSON.
function cellText(value) {
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
```

### 5. The SQL and the attempt history

Add another section after the table section:

```js
// --- The SQL that ran -------------------------------------------------------------------
// Always shown, never hidden behind a click: the person is entitled to see exactly what ran
// against their data, including the attempts that did not work.

function showSql(sql, attempts) {
  if (sql) {
    sqlBlock.textContent = sql;
    sqlBlock.hidden = false;
  }
  attemptsList.replaceChildren(...attempts.map((attempt, index) => attemptItem(attempt, index + 1)));
  sqlSection.hidden = !sql && attempts.length === 0;
}

// "Attempt 1 failed: <the database's error>" followed by the SQL that failed, or
// "Attempt 2 succeeded." The SQL that succeeded is the one shown above, so it is not repeated.
function attemptItem(attempt, number) {
  const item = document.createElement("li");
  const line = document.createElement("div");
  if (attempt.error === null) {
    line.textContent = `Attempt ${number} succeeded.`;
    item.append(line);
    return item;
  }
  line.textContent = `Attempt ${number} failed: ${attempt.error}`;
  item.append(line);
  // No SQL when the model's reply could not be read at all.
  if (attempt.sql) {
    const failedSql = document.createElement("pre");
    failedSql.textContent = attempt.sql;
    item.append(failedSql);
  }
  return item;
}
```

Expected after steps 1–5: `app.js` still has no `innerHTML` (or similar), and every
`getElementById` id exists in `index.html`. `tests/test_page.py` checks both.

---

## Testing & Verification

### 6. Add these to `tests/test_browser.py`

Add these helpers after `RECORD_BUTTON`:

```python
def table_header(page):
    return page.locator("#results thead th").all_text_contents()


def table_cells(page):
    """The table as a person reads it: one list of cell texts per row."""
    return page.evaluate(
        """() => [...document.querySelectorAll("#results tbody tr")].map(
             (row) => [...row.cells].map((cell) => cell.textContent))"""
    )


def cell_classes(page):
    """The class of every body cell, row by row, in the same shape as table_cells."""
    return page.evaluate(
        """() => [...document.querySelectorAll("#results tbody tr")].map(
             (row) => [...row.cells].map((cell) => cell.className))"""
    )


def attempt_lines(page):
    """The first line of each attempt in the history (without any SQL shown under it)."""
    return page.locator("#attempts li > div").all_text_contents()
```

Then these tests. Every test that gets rows back queues a chart reply. Where the chart does not
matter, it queues `provider.reply_error(400)`, so the chart call fails at once and the table stands
alone, as spec §7 requires. Stage 03 shows a note for that case.

```python
def test_an_answer_shows_its_table_and_sql(open_page, server):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    provider.reply_chart(CHART)
    page = open_page()
    ask(page)

    assert table_header(page) == ["weather", "collisions"]
    assert table_cells(page) == [
        ["Fine no high winds", "2"],
        ["Raining no high winds", "2"],
        ["Snowing no high winds", "1"],
        ["Not recorded", "1"],   # the NULL weather label (collision c4)
    ]
    classes = cell_classes(page)
    assert classes[3][0] == "not-recorded"
    assert all(row[1] == "number" for row in classes), "counts should be right-aligned"
    assert page.locator("#results thead th").nth(1).get_attribute("class") == "number"

    # "Not recorded" is visibly different from a real value: muted and italic.
    style = page.evaluate(
        """() => { const cell = document.querySelector("#results td.not-recorded");
                   const css = getComputedStyle(cell);
                   return [css.fontStyle, css.color]; }"""
    )
    assert style == ["italic", "rgb(102, 102, 102)"]

    expect(page.locator("#message")).to_be_hidden()
    expect(page.locator("#truncation")).to_be_hidden()
    expect(page.locator("#sql")).to_have_text(GOOD_SQL)
    assert attempt_lines(page) == ["Attempt 1 succeeded."]


def test_values_are_shown_exactly_as_the_database_returned_them(open_page, server):
    command, provider = server
    provider.reply_sql(
        "SELECT 0 AS zero, false AS flag, '' AS blank, NULL AS nothing, "
        "'NaN'::DOUBLE AS not_a_number, 9007199254740993::HUGEINT AS big, "
        "DATE '2023-01-15' AS day, [1, 2] AS list, 2023 AS year"
    )
    provider.reply_error(400)
    page = open_page()
    ask(page)

    assert table_cells(page) == [[
        "0",                  # zero is a value, not "Not recorded"
        "false",              # so is false
        "",                   # and so is an empty string
        "Not recorded",       # only NULL is
        "NaN",                # not a number, but recorded
        "9007199254740993",   # beyond a JavaScript number, so sent as a string: every digit kept
        "2023-01-15",
        "[1,2]",
        "2023",               # a year, never "2,023"
    ]]
    classes = cell_classes(page)[0]
    assert [name for name in classes if name == "not-recorded"] == ["not-recorded"]
    assert classes[3] == "not-recorded"


def test_an_uncovered_coded_column_shows_its_raw_code(open_page, server):
    command, provider = server
    # The synthetic codebook has no entry for this coded column, so its label twin is NULL on
    # every row: the documented exception in spec §3.
    provider.reply_sql(
        "SELECT enhanced_severity_collision AS enhanced, "
        "enhanced_severity_collision_label AS enhanced_label "
        "FROM collisions_labelled ORDER BY accident_index"
    )
    provider.reply_error(400)
    page = open_page()
    ask(page)

    assert table_cells(page) == [
        ["1", "Not recorded"],
        ["2", "Not recorded"],
        ["1", "Not recorded"],
        ["3", "Not recorded"],
        ["2", "Not recorded"],
        ["1", "Not recorded"],
    ]


def test_text_from_the_data_is_never_run_as_code(open_page, server):
    command, provider = server
    cell = '<img src=x onerror="window.injected = true">'
    provider.reply_sql(f"SELECT '{cell}' AS \"<b>label</b>\"")
    provider.reply_error(400)
    page = open_page()
    ask(page)

    assert table_header(page) == ["<b>label</b>"]
    assert table_cells(page) == [[cell]]
    expect(page.locator("#sql")).to_contain_text("<img src=x")

    explanation = "<script>window.injected = true</script> This build has no such data."
    reply_decline(provider, explanation)
    ask(page, "Another question")
    expect(page.locator("#message")).to_have_text(explanation)

    assert page.evaluate("window.injected === undefined")
    assert page.locator("#answer img, #answer b, #answer script").count() == 0


def test_a_repaired_query_shows_every_attempt(open_page, server):
    command, provider = server
    provider.reply_sql("SELECT rain FROM collisions")   # no such column
    provider.reply_sql(GOOD_SQL)
    provider.reply_error(400)
    page = open_page()
    ask(page)

    lines = attempt_lines(page)
    assert len(lines) == 2
    assert lines[0].startswith("Attempt 1 failed: ")
    assert "rain" in lines[0]             # DuckDB's own error, shown as it is
    assert lines[1] == "Attempt 2 succeeded."
    attempts = page.locator("#attempts li")
    expect(attempts.nth(0).locator("pre")).to_have_text("SELECT rain FROM collisions")
    expect(attempts.nth(1).locator("pre")).to_have_count(0)
    expect(page.locator("#sql")).to_have_text(GOOD_SQL)
    assert len(table_cells(page)) == 4


def test_a_failed_question_shows_every_attempt(open_page, server):
    command, provider = server
    for number in (1, 2, 3):
        provider.reply_sql(f"SELECT nope_{number} FROM collisions")
    page = open_page()
    ask(page)

    message = page.locator("#message")
    expect(message).to_have_class(ERROR_CLASS)
    expect(message).to_contain_text("nope_3")   # the last error, as DuckDB wrote it
    lines = attempt_lines(page)
    assert [line.split(":")[0] for line in lines] == [
        "Attempt 1 failed", "Attempt 2 failed", "Attempt 3 failed",
    ]
    for number in (1, 2, 3):
        expect(page.locator("#attempts li").nth(number - 1).locator("pre")).to_have_text(
            f"SELECT nope_{number} FROM collisions"
        )
    expect(page.locator("#sql")).to_be_hidden()        # nothing succeeded
    expect(page.locator("#sql-section")).to_be_visible()
    expect(page.locator("#table-wrap")).to_be_hidden()


def test_a_result_with_no_rows_says_so(open_page, server):
    command, provider = server
    provider.reply_sql("SELECT 1 AS n WHERE false")
    page = open_page()
    ask(page)

    expect(page.locator("#message")).to_have_text("The query ran and returned no rows.")
    expect(page.locator("#message")).not_to_have_class(ERROR_CLASS)
    expect(page.locator("#table-wrap")).to_be_hidden()
    expect(page.locator("#chart")).to_be_hidden()
    expect(page.locator("#sql")).to_have_text("SELECT 1 AS n WHERE false")
    assert len(provider.requests) == 1, "no chart call for an empty result"


def test_a_truncated_result_is_announced_above_the_table(open_page, server):
    command, provider = server
    provider.reply_sql("SELECT range AS n FROM range(5001)")
    provider.reply_error(400)
    page = open_page()
    ask(page)

    notice = page.locator("#truncation")
    expect(notice).to_have_text(
        "Showing the first 5,000 rows only. "
        "The full result is larger, and the chart shows only these rows."
    )
    cells = table_cells(page)
    assert len(cells) == 5000
    assert cells[-1] == ["4999"]
    assert notice.bounding_box()["y"] < page.locator("#table-wrap").bounding_box()["y"]


def test_a_declined_question_reads_as_an_answer(open_page, server):
    command, provider = server
    page = open_page()

    reply_decline(provider)
    ask(page)
    message = page.locator("#message")
    expect(message).to_have_text(DECLINE_TEXT)
    expect(message).not_to_have_class(ERROR_CLASS)
    assert page.evaluate("getComputedStyle(document.getElementById('message')).color") == (
        "rgb(17, 17, 17)"   # ordinary text colour, not red
    )
    expect(page.locator("#table-wrap")).to_be_hidden()
    expect(page.locator("#chart")).to_be_hidden()
    expect(page.locator("#sql-section")).to_be_hidden()   # nothing was tried

    # A decline after a failed attempt still shows that attempt.
    provider.reply_sql("SELECT nope FROM collisions")
    reply_decline(provider)
    ask(page)
    expect(message).to_have_text(DECLINE_TEXT)
    lines = attempt_lines(page)
    assert len(lines) == 1 and lines[0].startswith("Attempt 1 failed: ")
    expect(page.locator("#sql")).to_be_hidden()


def test_a_stopped_question_with_nothing_run_shows_no_sql(open_page, server):
    command, provider = server
    provider.reply_sql(SLOW_SQL)
    page = open_page()
    page.fill("#question", QUESTION)
    page.click("#ask-button")
    expect(page.locator("#ask-button")).to_have_text("Stop")
    assert provider.wait_for_requests(1)
    page.wait_for_timeout(500)
    page.click("#ask-button")
    expect(page.locator("#ask-form")).to_have_attribute("data-state", "idle")

    expect(page.locator("#message")).to_have_text(STOPPED)
    # An interrupted query is not a failed attempt, so there is nothing to list.
    expect(page.locator("#sql-section")).to_be_hidden()


def test_a_new_question_clears_the_last_answer(open_page, server):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    provider.reply_error(400)
    page = open_page()
    ask(page)
    expect(page.locator("#table-wrap")).to_be_visible()

    reply_decline(provider)
    ask(page, "And in 2019?")
    expect(page.locator("#table-wrap")).to_be_hidden()
    expect(page.locator("#sql-section")).to_be_hidden()
    assert table_cells(page) == []
    expect(page.locator("#message")).to_have_text(DECLINE_TEXT)


def test_the_page_fits_a_phone_screen(open_page, server):
    command, provider = server
    # Every column of the labelled view: far wider than a phone.
    provider.reply_sql("SELECT * FROM collisions_labelled ORDER BY accident_index")
    provider.reply_error(400)
    page = open_page(viewport={"width": 375, "height": 812})
    ask(page)

    expect(page.locator("#table-wrap")).to_be_visible()
    sizes = page.evaluate(
        """() => { const wrap = document.getElementById("table-wrap");
                   return { page: document.documentElement.scrollWidth,
                            window: window.innerWidth,
                            table: wrap.scrollWidth, box: wrap.clientWidth }; }"""
    )
    assert sizes["page"] <= sizes["window"], "the page scrolls sideways on a phone"
    assert sizes["table"] > sizes["box"], "the wide table should scroll inside its own box"
```

### 7. Run

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest tests/test_page.py tests/test_browser.py -rs -v
```

Expected: every test passes, **0 skipped**. Then:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

Expected: green.

### Stage ship-readiness checklist

1. Both commands green, with **0 skipped** in `tests/test_browser.py`.
2. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
3. `grep -nE "innerHTML|outerHTML|insertAdjacentHTML|document\.write" src/askcrossroads/static/app.js`
   returns nothing.
4. `grep -n "toLocaleString" src/askcrossroads/static/app.js` shows only `truncationText`. Table
   values are never formatted.
5. `git diff --stat src/askcrossroads/app.py` is empty.
6. `git status` shows the changes and **nothing staged**.

---

## End State / Handoff

- `app.js` renders every `/api/ask` outcome as the overview's contract table says, except the
  chart. The functions added are `showAnswered`, `truncationText`, `isNumberType`, `drawTable`,
  `cellText`, `showSql` and `attemptItem`. `render` and `clearAnswer` were replaced.
- `showAnswered(answer)` is the one place stage 03 adds the chart. It currently returns after
  `drawTable`.
- Every `NULL` cell shows *Not recorded* (class `not-recorded`, muted italic). Every other value
  shows its exact text. Numeric columns are right-aligned (class `number`).
- `tests/test_browser.py` holds the stage 01 tests plus the 12 above, and the helpers
  `table_header`, `table_cells`, `cell_classes` and `attempt_lines`. All pass, with zero skips.

---

## Failure Modes & Rollback

| Failure | What it looks like | What to do |
|---|---|---|
| `NULL AS nothing` arrives typed `"NULL"` or `"INTEGER"` | the cell is right-aligned or not | Irrelevant to the assertion: a `NULL` cell has class `not-recorded` either way. |
| DuckDB rejects `'NaN'::DOUBLE` or `9007199254740993::HUGEINT` | attempt 1 failed in the history | Use `CAST('NaN' AS DOUBLE)` and `CAST(9007199254740993 AS HUGEINT)` and record it. The expected cell texts stay the same. |
| The truncation test is slow | over ~10s | It draws 5,000 rows. Check that `drawTable` builds `tbody` before inserting it, as written. Do not lower the row count: 5,001 is what crosses the real cap. |
| The phone test finds the page wider than 375px | `sizes["page"] > sizes["window"]` | Something outside `.table-wrap` is too wide, usually long unbroken SQL or provenance text. `pre` already wraps with `overflow-wrap: anywhere`. Add `overflow-wrap: anywhere` to `#provenance` or `.message` as needed, and record it. |
| A long DuckDB error contains `:` | the `split(":")[0]` check in the failed-question test | Only the text before the first colon is compared ("Attempt N failed"), so the error's own colons do not matter. |
| `attempt_lines` picks up the failed SQL | extra entries | The selector is `#attempts li > div`. Each item has exactly one direct `div`; failed SQL is in a `pre`. |

**Rollback:** revert `src/askcrossroads/static/app.js` to its stage 01 version: remove the six new
element lookups and the two `TEXT` entries, restore stage 01's `render` and `clearAnswer`, and delete
the *results table* and *SQL that ran* sections. Then remove the tests and helpers added to
`tests/test_browser.py` in this stage.
