# Stage 03 — Charts, Vendored Libraries & Ship

> Part of *Frontend*. You have the overview + this stage; implement **only** this stage.
> Read [00-overview.md](00-overview.md) first, in particular *Charts*, *Safety in the page*, and the
> Vega/Vega-Lite row of *Decisions taken*.
> The **End State** is the contract. If a step does not match reality, adapt it and note the
> deviation at the end of your report.

---

## Prerequisites / Starting State

Stage 02 must be complete. Verify:

```bash
cd /Users/will/Documents/Code/AskCrossroads
```

```bash
grep -c "function drawTable\|function showSql\|function showAnswered" src/askcrossroads/static/app.js \
  && test ! -e src/askcrossroads/static/vendor \
  && env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest tests/test_page.py tests/test_browser.py -rs -q
```

Expected: `3`, no `vendor` folder yet, and all tests passing with **0 skipped**.

---

## Objective

Draw the chart above the table using pinned local copies of Vega and Vega-Lite. Charts follow light
and dark mode and can never fetch anything, and a missing or broken chart never costs the table.
Then bring the spec in line with what was built, and pass the plan's ship checklist.

---

## Implementation Steps

### 1. Vendor the chart libraries

These are the exact files. They are the latest stable releases at the time of planning, and
Vega-Lite 6.5.0's `package.json` asks for `vega ^6.4.0`. Each `build/*.min.js` is a plain browser
script that sets a global (`vega` and `vegaLite`), so no module loader is needed.

```bash
mkdir -p src/askcrossroads/static/vendor
```

```bash
curl -fsSL https://cdn.jsdelivr.net/npm/vega@6.4.0/build/vega.min.js -o src/askcrossroads/static/vendor/vega.min.js
```

```bash
curl -fsSL https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/build/vega-lite.min.js -o src/askcrossroads/static/vendor/vega-lite.min.js
```

```bash
curl -fsSL https://cdn.jsdelivr.net/npm/vega@6.4.0/LICENSE -o src/askcrossroads/static/vendor/vega-LICENSE.txt
```

```bash
curl -fsSL https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/LICENSE -o src/askcrossroads/static/vendor/vega-lite-LICENSE.txt
```

```bash
shasum -a 256 src/askcrossroads/static/vendor/*
```

Expected, exactly (these were computed from the same URLs while planning):

```text
8f6a3587cf8d4f42c7e08120e3eb05d067e746d554e39d2dcf52acc0bd5ba28f  .../vega.min.js
83e215e84e308ee8688fa6503ba0f1cf8a3c05c0d61df2841a0b92bf86809e00  .../vega-lite.min.js
63727832aaf62004a2b249c933e327f3c90caf49e41a72f4bf436edf632cbda8  .../vega-LICENSE.txt
f618900fd0d64046963b29f40590cdd1e341a2f41449f99110d82fd81fea808c  .../vega-lite-LICENSE.txt
```

Sizes are about 521KB and 258KB for the two scripts, and about 1.5KB for each licence. **If any hash
differs, stop and report it.** npm package versions are immutable, so a different hash means the
download was not what it claims to be. **Never edit these four files.** Their hashes are pinned in a
test.

Confirm they contain none of the schema words (checked while planning, but confirm):

```bash
grep -ciE "collision|weather|aadf|stats19|vehicle|casualty" src/askcrossroads/static/vendor/*
```

Expected: `0` for every file.

### 2. `src/askcrossroads/static/vendor/README.md`

```markdown
# Vendored chart libraries

The page draws charts with [Vega-Lite](https://vega.github.io/vega-lite/), which runs on
[Vega](https://vega.github.io/vega/). Both are kept here, unmodified, so that charts work with no
internet connection and the page contacts nothing outside this machine.

| File | Package | Version | Downloaded from | SHA-256 |
|---|---|---|---|---|
| `vega.min.js` | vega | 6.4.0 | `https://cdn.jsdelivr.net/npm/vega@6.4.0/build/vega.min.js` | `8f6a3587cf8d4f42c7e08120e3eb05d067e746d554e39d2dcf52acc0bd5ba28f` |
| `vega-lite.min.js` | vega-lite | 6.5.0 | `https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/build/vega-lite.min.js` | `83e215e84e308ee8688fa6503ba0f1cf8a3c05c0d61df2841a0b92bf86809e00` |
| `vega-LICENSE.txt` | vega | 6.4.0 | `https://cdn.jsdelivr.net/npm/vega@6.4.0/LICENSE` | `63727832aaf62004a2b249c933e327f3c90caf49e41a72f4bf436edf632cbda8` |
| `vega-lite-LICENSE.txt` | vega-lite | 6.5.0 | `https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/LICENSE` | `f618900fd0d64046963b29f40590cdd1e341a2f41449f99110d82fd81fea808c` |

Both libraries are BSD-3-Clause licensed by the University of Washington Interactive Data Lab. Their
licence texts are kept beside them, as that licence requires.

`vega-lite.min.js` ends with a `sourceMappingURL` comment. The map file is not included. Browsers
only ask for it when developer tools are open, and nothing breaks without it.

## Updating

1. Choose a Vega-Lite release, then the Vega release that its `package.json` `peerDependencies`
   asks for.
2. Download the four files from the URLs above, with the new version numbers.
3. Update this table and `VENDORED` in `tests/test_page.py` with the new SHA-256 values
   (`shasum -a 256 *`).
4. Update the versions in `tests/test_browser.py::test_the_chart_libraries_are_the_pinned_versions`.
```

### 3. Load them in `index.html`

In `src/askcrossroads/static/index.html`, replace

```html
  <script src="app.js"></script>
```

with

```html
  <!-- The chart libraries, kept in this folder so charts work with no internet connection.
       They must load before app.js, which uses them. -->
  <script src="vendor/vega.min.js"></script>
  <script src="vendor/vega-lite.min.js"></script>
  <script src="app.js"></script>
```

### 4. The chart in `app.js`

**a.** In *The parts of the page this script works with*, add after `attemptsList`:

```js
const chartBox = document.getElementById("chart");
const chartNote = document.getElementById("chart-note");
```

**b.** Add two entries to `TEXT`, after `notRecorded`:

```js
  noChart: "No chart could be made for this result.",
  chartFailed: "The chart could not be drawn:",
```

**c.** Replace `showAnswered` with an `async` version that draws the chart **after** the table, so
the table is on screen even while the chart is still being drawn:

```js
async function showAnswered(answer) {
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
  if (answer.chart) {
    await drawChart(answer.chart, answer.columns, answer.rows);
  } else {
    showChartNote(noChartText(answer.chart_attempts));
  }
}
```

In `render`, change `showAnswered(answer);` to `await showAnswered(answer);`.

**d.** In `clearAnswer`, add these lines at the end:

```js
  removeChart();
  lastChart = null;
  chartNote.hidden = true;
  chartNote.textContent = "";
```

**e.** Add a new section after *The SQL that ran*, and before *The line under the title*:

```js
// --- The chart --------------------------------------------------------------------------
// The model describes HOW to draw (a Vega-Lite spec with no data in it); the page supplies
// WHAT to draw (the rows). The model's spec is never changed: the rows are added beside it,
// and the page's colours are given to Vega-Lite as defaults, which the spec's own settings
// override.

// Vega can load data and images from links, and a spec the model wrote could ask it to. This
// loader refuses every such request, so drawing a chart never contacts anything.
const noLoading = vega.loader();
noLoading.sanitize = () => Promise.reject(new Error("Charts may not load files or links."));

let chartView = null;   // the chart on the page now, if any
let lastChart = null;   // what it was drawn from, so it can be redrawn in the other colours

async function drawChart(spec, columns, rows) {
  removeChart();
  lastChart = { spec, columns, rows };
  chartBox.hidden = false;   // shown first, so its width is known when sizing the chart
  try {
    const fullSpec = { ...spec, data: { values: rowObjects(columns, rows) } };
    const compiled = vegaLite.compile(fullSpec, { config: chartColours() }).spec;
    chartView = new vega.View(vega.parse(compiled), {
      container: chartBox,
      renderer: "svg",
      loader: noLoading,
      hover: true,
    });
    await chartView.runAsync();
  } catch (error) {
    // A chart is an extra. Losing it must never cost the table, which is already drawn.
    removeChart();
    showChartNote(`${TEXT.chartFailed} ${error.message}`);
  }
}

function removeChart() {
  if (chartView) {
    chartView.finalize();
    chartView = null;
  }
  chartBox.replaceChildren();
  chartBox.hidden = true;
}

function showChartNote(text) {
  chartNote.textContent = text;
  chartNote.hidden = false;
}

// When the model could not produce a chart at all, say so, with its last problem.
function noChartText(chartAttempts) {
  const last = chartAttempts[chartAttempts.length - 1];
  if (last && last.error) {
    return `${TEXT.noChart} The last problem was: ${last.error}`;
  }
  return TEXT.noChart;
}

// Vega-Lite wants one object per row, keyed by column name. The rows arrive as arrays in
// column order, because two columns may share a name. In the chart the later one wins; the
// table still shows both.
//
// A NULL in a text or date column becomes "Not recorded", so the chart labels it the same
// way the table does instead of printing "null". A NULL number stays NULL: there is nothing
// to plot, and the chart leaves it out.
function rowObjects(columns, rows) {
  const numeric = columns.map((column) => isNumberType(column.type));
  return rows.map((row) => {
    const object = {};
    columns.forEach((column, index) => {
      const value = row[index];
      object[column.name] = value === null && !numeric[index] ? TEXT.notRecorded : value;
    });
    return object;
  });
}

// The chart's colours and font, read from the page's own light or dark colours.
function chartColours() {
  const style = getComputedStyle(document.documentElement);
  const token = (name) => style.getPropertyValue(name).trim();
  // Fill the column's width, leaving room for axis labels and a legend.
  const width = Math.max(240, chartBox.clientWidth - 120);
  return {
    background: "transparent",
    font: token("--font-sans"),
    view: {
      stroke: "transparent",
      // The same width whether the horizontal axis holds numbers or dates (continuous) or
      // categories (discrete). Without the second, a few categories make a very thin chart.
      continuousWidth: width,
      discreteWidth: width,
      continuousHeight: 300,
    },
    axis: {
      domainColor: token("--border"),
      gridColor: token("--border"),
      tickColor: token("--border"),
      labelColor: token("--muted"),
      titleColor: token("--text"),
    },
    legend: { labelColor: token("--muted"), titleColor: token("--text") },
    header: { labelColor: token("--muted"), titleColor: token("--text") },
    title: { color: token("--text") },
    text: { color: token("--text") },
  };
}

// When the computer switches between light and dark, redraw the chart in the new colours.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (lastChart) {
    drawChart(lastChart.spec, lastChart.columns, lastChart.rows);
  }
});
```

Expected: `grep -nE "innerHTML|outerHTML|insertAdjacentHTML|document\.write" src/askcrossroads/static/app.js`
still returns nothing.

### 5. Update `docs/spec.md`

Make exactly these edits. They record what was built. Nothing in them describes this plan's
internal structure.

**§3**: replace the two bullets under "Two display rules remain, and both belong to the frontend
rather than a processing step:" (currently beginning "**A `NULL` in a label column renders as *Not
recorded*.**" and "**A column the codebook does not cover renders as its raw code.**") with:

```markdown
- **A `NULL` renders as *Not recorded*.** Crossroads-UK replaces missing and unknown codes (`-1`, `9`) with `NULL` before AskCrossroads sees the file, which erases which one it was — so guessing is not an option, and every such value gets the same neutral label rather than an empty cell. The page applies this to every `NULL` cell, not only to label columns: a query renames its columns (`AS weather`), so the browser cannot tell which ones were labels, and in a Crossroads-UK build a `NULL` means "not recorded" wherever it appears. `0`, `false` and empty text are values, and are shown as values.
- **A column the codebook does not cover renders as its raw code.** Never blank, never guessed. The page never translates codes at all: every value other than `NULL` is shown exactly as the database returned it, with no thousands separators, because a year or a code is a number too.
```

**§9, Stack table**: in the `Frontend` row, append to the rationale cell:
` Vega and Vega-Lite are pinned copies inside the package (`static/vendor/`), not a CDN, so charts work offline and the page contacts nothing.`

**§9, Interface**: replace the paragraph beginning "The UI is neat, minimalist, and professional"
with:

```markdown
The UI is modern, simple and professional: a single column, generous whitespace, one system font stack, and no decorative colour. It is black and white, follows the computer's light or dark setting, and keeps colour for the chart and for a question that failed. Its content is the question box, the results table, the chart, the executed SQL, and the provenance line. Nothing competes with those five things. An answer reads top to bottom: the chart, then the table, which scrolls within its own box so a long result never pushes the SQL out of reach, then the SQL and its attempt history.
```

At the end of the next paragraph (the one beginning "There is one control"), append:
` After Stop, the button stays disabled until the question has actually ended, so a new question can never collide with the old one. Enter asks; Shift+Enter starts a new line.`

**§11.4**: in the `Label rendering` row, replace the cell text with:
`Every `NULL` renders as *Not recorded* rather than an empty cell, and `0`, `false` and empty text do not; an uncovered column renders as its raw code (§3). Proven in a real browser.`

**§11**: add a new subsection immediately after the §11.4 table, before the `---` that precedes §12:

```markdown
### 11.5 The page

The page is tested the way a person uses it. `tests/test_browser.py` drives an invisible Chromium, through Playwright, against the real `askcrossroads` command pointed at the local provider stub, and asserts on what is shown: text, visibility, colours and position. It covers every way a question can end, the two display rules, Stop, light and dark mode, and phone width. It fails if the page ever requests anything from outside the local server. `tests/test_page.py` checks without a browser that the page links only its own files, that the vendored chart libraries are the pinned bytes, and that the page's code names nothing from the schema and never writes HTML.

The browser tests need Chromium for Playwright, a one-time download (`python -m playwright install chromium`). Without it they skip rather than fail, as the integration suite does without a key. Playwright is a Python package. It runs a private copy of Node.js internally, but the project itself still has no Node toolchain, no `package.json` and no build step.
```

**§12**: in the blueprint, change the `static/` block to:

```text
│       └── static/
│           ├── index.html
│           ├── app.js                  # Vanilla JS; Vega-Lite rendering
│           ├── style.css
│           └── vendor/                 # Pinned Vega + Vega-Lite, with licences (no CDN)
```

and insert these three lines immediately after the `test_server.py` line:

```text
│   ├── test_page.py                    # The served page: local files only, pinned libraries
│   ├── test_browser.py                 # The page in a real headless browser (Playwright)
│   ├── real_command.py                 # Starts the installed command for tests
```

---

## Testing & Verification

### 6. `tests/test_page.py`: the vendored files

Add `import hashlib` to the imports. Change `LINKED` to:

```python
LINKED = ["style.css", "vendor/vega.min.js", "vendor/vega-lite.min.js", "app.js"]
```

Add after `LINKED`:

```python
# The vendored chart libraries and their licences, byte for byte. A change here must be a
# deliberate update (see static/vendor/README.md), never an accident.
VENDORED = {
    "vendor/vega.min.js": "8f6a3587cf8d4f42c7e08120e3eb05d067e746d554e39d2dcf52acc0bd5ba28f",
    "vendor/vega-lite.min.js": "83e215e84e308ee8688fa6503ba0f1cf8a3c05c0d61df2841a0b92bf86809e00",
    "vendor/vega-LICENSE.txt": "63727832aaf62004a2b249c933e327f3c90caf49e41a72f4bf436edf632cbda8",
    "vendor/vega-lite-LICENSE.txt": "f618900fd0d64046963b29f40590cdd1e341a2f41449f99110d82fd81fea808c",
}
```

Add these tests:

```python
def test_the_vendored_libraries_are_the_pinned_versions():
    readme = (STATIC / "vendor" / "README.md").read_text()
    for name, expected in VENDORED.items():
        digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} is not the pinned copy; see static/vendor/README.md"
        assert expected in readme, f"static/vendor/README.md does not record {name}'s hash"


def test_the_vendored_licences_are_present():
    for name in ("vega-LICENSE.txt", "vega-lite-LICENSE.txt"):
        text = (STATIC / "vendor" / name).read_text()
        assert "University of Washington Interactive Data Lab" in text
        assert "Redistribution and use in source and binary forms" in text
```

The existing `test_the_page_links_only_local_files` and `test_every_linked_file_is_served` now cover
the two vendor scripts as well.

### 7. `tests/test_browser.py`: the chart

Add these tests:

```python
def test_the_chart_libraries_are_the_pinned_versions(open_page):
    page = open_page()
    assert page.evaluate("[vega.version, vegaLite.version]") == ["6.4.0", "6.5.0"]


def test_an_answer_draws_its_chart_above_the_table(open_page, server):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    provider.reply_chart(CHART)
    page = open_page()
    ask(page)

    chart = page.locator("#chart svg")
    expect(chart).to_be_visible()
    # The chart was drawn from the rows: their labels are on its axis.
    expect(chart).to_contain_text("Fine no high winds")
    expect(chart).to_contain_text("Raining no high winds")
    # The NULL label reads the same in the chart as in the table, never "null".
    expect(chart).to_contain_text("Not recorded")
    expect(chart).not_to_contain_text("null")
    assert page.locator("#chart svg .mark-rect path").count() == 4, "expected one bar per row"
    # A few categories still fill the column, rather than making a thin strip of a chart.
    assert chart.bounding_box()["width"] > 600
    expect(page.locator("#chart-note")).to_be_hidden()

    # Top to bottom: chart, table, SQL.
    def top(selector):
        return page.locator(selector).bounding_box()["y"]

    assert top("#chart") < top("#table-wrap") < top("#sql")


def test_no_chart_still_shows_the_table(open_page, server):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    for _ in range(3):
        provider.reply_text("not json")   # every chart attempt is malformed
    page = open_page()
    ask(page)

    expect(page.locator("#chart")).to_be_hidden()
    expect(page.locator("#chart-note")).to_have_text(
        "No chart could be made for this result. "
        "The last problem was: The chart reply was not valid JSON."
    )
    assert len(table_cells(page)) == 4
    assert len(provider.requests) == 4   # one SQL call, three chart attempts


def test_a_chart_that_cannot_be_drawn_still_shows_the_table(open_page, server):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    # Passes the server's shape check (it has a mark and an encoding, and no data), but its
    # expression cannot be parsed, so drawing it fails in the browser.
    provider.reply_chart({**CHART, "transform": [{"calculate": "1 +", "as": "broken"}]})
    page = open_page()
    ask(page)

    expect(page.locator("#chart")).to_be_hidden()
    expect(page.locator("#chart-note")).to_contain_text("The chart could not be drawn: ")
    assert len(table_cells(page)) == 4


def test_a_chart_cannot_fetch_anything(open_page, server):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    # A spec that asks Vega to load data from the internet.
    provider.reply_chart({
        **CHART,
        "transform": [{
            "lookup": "weather",
            "from": {
                "data": {"url": "https://example.invalid/lookup.csv"},
                "key": "weather",
                "fields": ["extra"],
            },
        }],
    })
    page = open_page()
    ask(page)
    page.wait_for_timeout(1000)   # give any load the chance to be attempted

    assert open_page.outside == [], "drawing a chart tried to reach the internet"
    assert len(table_cells(page)) == 4

    # And the guard is real: a request off this machine IS caught and recorded.
    page.evaluate("fetch('https://example.invalid/probe').catch(() => null)")
    page.wait_for_timeout(500)
    assert open_page.outside == ["https://example.invalid/probe"]
    open_page.outside.clear()   # expected here, so the fixture's own check passes


def test_the_chart_follows_light_and_dark_mode(open_page, server):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    provider.reply_chart(CHART)
    page = open_page(color_scheme="light")
    ask(page)

    # Axis labels use the page's muted colour for the current mode.
    expect(page.locator('#chart svg text[fill="#666666"]').first).to_be_attached()

    # The computer switches to dark mode while the answer is on screen.
    page.emulate_media(color_scheme="dark")
    expect(page.locator('#chart svg text[fill="#a1a1a1"]').first).to_be_attached()
    assert page.locator('#chart svg text[fill="#666666"]').count() == 0
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(10, 10, 10)"
```

Every chart test also passes through the `open_page` teardown, which fails if any request left the
local server.

### 8. Run

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest tests/test_page.py tests/test_browser.py -rs -v
```

Expected: every test passes, **0 skipped**. Then the full default suite:

```bash
env -u ANTHROPIC_API_KEY -u ASKCROSSROADS_DB -u ASKCROSSROADS_PORT -u ANTHROPIC_BASE_URL .venv/bin/python -m pytest
```

Expected: green. Then the marked suite, which this plan does not touch:

```bash
.venv/bin/python -m pytest -m integration
```

Expected: passes, or skips cleanly without a key or a real build. It never fails.

### Plan ship-readiness checklist

Run every item. They match the overview's *Ship-readiness* list.

1. The default suite is green.
2. `tests/test_browser.py -rs` shows **0 skipped**.
3. `pytest -m integration` passes or skips cleanly.
4. `grep -rniE "collision|weather|aadf|stats19|vehicle|casualty" src/` returns nothing.
5. `grep -rnE "innerHTML|outerHTML|insertAdjacentHTML|document\.write" src/askcrossroads/static/app.js`
   returns nothing.
6. `grep -rnE "(src|href)=\"(https?:)?//" src/askcrossroads/static/index.html` returns nothing.
7. `grep -rniE "sk-ant-[A-Za-z0-9_-]{10,}" --exclude-dir=.git --exclude-dir=.venv .` returns nothing.
8. `git diff --stat src/askcrossroads/app.py` is empty.
9. `find . -path ./.venv -prune -o \( -name package.json -o -name node_modules \) -print` prints
   nothing.
10. `shasum -a 256 src/askcrossroads/static/vendor/*.js src/askcrossroads/static/vendor/*.txt`
    matches the four hashes above.
11. `git status` shows the new and modified files and **nothing staged**. Report the full list to
    the user.

---

## End State / Handoff

- `static/vendor/` holds `vega.min.js` (6.4.0), `vega-lite.min.js` (6.5.0), both licence texts and
  `README.md`, all byte-for-byte as pinned.
- `index.html` loads `style.css`, `vendor/vega.min.js`, `vendor/vega-lite.min.js` and `app.js`, in
  that order, and nothing else.
- `app.js` draws `{...spec, data: {values: rows}}` as SVG above the table, in the page's light or
  dark colours. It redraws when the mode changes, never fetches anything, and shows a note instead
  of a chart when there is none or it cannot be drawn. The table always stands.
- `docs/spec.md` §3, §9, §11 (new §11.5) and §12 describe the page as built.
- The whole plan's ship checklist passes. The page is complete: every response shape the HTTP
  contract can produce renders as specified, and this is proven in a real browser.

---

## Failure Modes & Rollback

| Failure | What it looks like | What to do |
|---|---|---|
| A downloaded file's hash differs | step 1's `shasum` output | Stop and report. Do not update the pinned hashes to match an unexpected download. |
| `vega.View` ignores the `container` option in this version | no `svg` appears inside `#chart` | Create the view without `container`, then `await chartView.initialize(chartBox).runAsync();`. Record it. |
| Bars are not `path` elements inside `.mark-rect` | the bar count fails | Inspect with `page.locator("#chart svg").inner_html()` in a scratch run, use the selector Vega actually emits, and record it. Do not drop the assertion. |
| Vega writes colours as `rgb(...)` rather than as given | the `text[fill="#666666"]` locator finds nothing | Use `text[fill="rgb(102, 102, 102)"]` and `text[fill="rgb(161, 161, 161)"]`, and record it. |
| `vegaLite.compile` accepts the broken expression and Vega only logs it | the "could not be drawn" note never appears | Vega's `parse` throws on an unparseable expression. If this version only warns, use `{**CHART, "mark": "not-a-mark"}` instead, check it fails, and record it. The test's purpose (the table survives) must stay. |
| The failed `lookup` load makes the chart reject | the chart note appears in the fetch test | Acceptable. That test asserts only that nothing left the machine and that the table is shown. |
| The chart overflows a phone screen | the stage 02 phone test fails, if rerun with a chart | `#chart` already has `overflow-x: auto`, so the chart scrolls within its box. If needed, reduce `continuousWidth`'s floor and record it. |
| Spec edits drift into describing this plan | words like "stage" or a plan path appear in `docs/spec.md` | The spec describes the product only. Rewrite the sentence. |

**Rollback:** delete `src/askcrossroads/static/vendor/`. Restore the single `<script src="app.js">`
line in `index.html`. Remove the chart lookups, the two `TEXT` entries and the chart section from
`app.js`, restore the stage 02 `showAnswered` and the `render` call, and remove the four lines added
to `clearAnswer`. Revert `docs/spec.md` with `git checkout -- docs/spec.md`. Remove the tests added
in this stage, and revert `LINKED` and `VENDORED` in `tests/test_page.py`.
