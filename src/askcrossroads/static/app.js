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
const provenanceSummary = document.getElementById("provenance-summary");
const provenanceDetails = document.getElementById("provenance-details");
const answerSection = document.getElementById("answer");
const messageLine = document.getElementById("message");
const truncationNotice = document.getElementById("truncation");
const tableWrap = document.getElementById("table-wrap");
const resultsTable = document.getElementById("results");
const sqlSection = document.getElementById("sql-section");
const sqlBlock = document.getElementById("sql");
const attemptsList = document.getElementById("attempts");
const chartBox = document.getElementById("chart");
const chartNote = document.getElementById("chart-note");
const aiNote = document.getElementById("ai-note");

// --- Words the page shows ---------------------------------------------------------------
// Messages that come from the server (an error, a decline) are shown exactly as sent.
// These are the page's own.

const TEXT = {
  provenanceFailed: "Could not read the database description.",
  noConnection: "Could not reach AskCrossroads. Check that it is still running in the terminal.",
  unreadableReply: "AskCrossroads sent a reply this page could not read.",
  stopped: "Stopped. Nothing more was run.",
  noRows: "The query ran and returned no rows.",
  notRecorded: "Not recorded",
  noChart: "No chart could be made for this result.",
  chartFailed: "The chart could not be drawn:",
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
  if (answer.status === "answered") {
    await showAnswered(answer);
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

// The table is drawn first, so it is on screen even while the chart is still being drawn.
// Every answered question, even one with no rows, carries the note that an AI wrote the query.
async function showAnswered(answer) {
  aiNote.hidden = false;
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

// The server sends at most its row cap, so when a result was cut off, the number of rows
// received IS the cap. The page never needs to know the number itself.
function truncationText(count) {
  return (
    `Showing the first ${count.toLocaleString("en-GB")} rows only. ` +
    "The full result is larger, and the chart shows only these rows."
  );
}

function showMessage(text, isError) {
  messageLine.textContent = text;
  messageLine.classList.toggle("error", isError);
  messageLine.hidden = false;
}

// Empties everything an earlier answer put on the page.
function clearAnswer() {
  answerSection.hidden = true;
  for (const part of [messageLine, truncationNotice, tableWrap, aiNote, sqlSection, sqlBlock]) {
    part.hidden = true;
  }
  messageLine.textContent = "";
  messageLine.classList.remove("error");
  truncationNotice.textContent = "";
  resultsTable.replaceChildren();
  sqlBlock.textContent = "";
  attemptsList.replaceChildren();
  removeChart();
  lastChart = null;
  chartNote.hidden = true;
  chartNote.textContent = "";
}

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

// --- The SQL that ran -------------------------------------------------------------------
// Always shown, never hidden behind a click: the person is entitled to see exactly what ran
// against their data, including the attempts that did not work.

function showSql(sql, attempts) {
  if (sql) {
    sqlBlock.textContent = sql;
    sqlBlock.hidden = false;
  }
  const items = attempts.map((attempt, index) => attemptItem(attempt, index + 1));
  attemptsList.replaceChildren(...items);
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

// When the page switches between light and dark (the computer's setting changed, or the
// person pressed the moon/sun button), theme.js changes the page's data-theme. Redraw the
// chart in the new colours when it does.
new MutationObserver(() => {
  if (lastChart) {
    drawChart(lastChart.spec, lastChart.columns, lastChart.rows);
  }
}).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

// --- The line under the title -----------------------------------------------------------

async function loadProvenance() {
  try {
    const response = await fetch("/api/database");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const summary = await response.json();
    showProvenance(summary.provenance);
  } catch (error) {
    provenanceSummary.textContent = TEXT.provenanceFailed;
  }
}

// The short line: "Crossroads-UK 0.9.0 · built 1 Jan 2026".
// The list it opens into: Version, Schema, Built, then every build parameter.
// The build parameters differ from build to build, so their names are never written here:
// every one the file records is listed, in the order the file gives them.
function showProvenance(provenance) {
  const version = `Crossroads-UK ${provenance.crossroads_version}`;
  const built = provenance.built_at_utc;
  provenanceSummary.textContent = built
    ? `${version} · built ${describeDate(built)}`
    : version;

  const rows = [
    ["Version", version],
    ["Schema", String(provenance.schema_version)],
    ["Built", built ? `${describeDate(built)}, ${describeTime(built)} UTC` : "Not recorded"],
  ];
  for (const [name, value] of Object.entries(provenance.parameters || {})) {
    rows.push([describeName(name), describeParameter(value)]);
  }

  provenanceDetails.replaceChildren();
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    provenanceDetails.append(term, detail);
  }
  provenanceDetails.hidden = false;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "2026-07-16T10:55:13.101633" becomes "16 Jul 2026". Anything not in that shape is shown
// as it is.
function describeDate(timestamp) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(timestamp);
  if (!match) {
    return timestamp;
  }
  const [, year, month, day] = match;
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}`;
}

// "2026-07-16T10:55:13.101633" becomes "10:55" (hours and minutes only).
function describeTime(timestamp) {
  const match = /[T ](\d{2}:\d{2})/.exec(timestamp);
  return match ? match[1] : "";
}

// "boundary_mode" becomes "Boundary mode".
function describeName(name) {
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
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
