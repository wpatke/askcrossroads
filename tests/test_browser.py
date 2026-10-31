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


# --- The page, the button and messages --------------------------------------------------


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


# --- Answers: the table, the SQL and the attempt history ---------------------------------
# Every test that gets rows back queues a chart reply. Where the chart does not matter it
# queues reply_error(400), so the chart call fails at once and the table stands alone, as
# spec §7 requires.


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


# --- The chart --------------------------------------------------------------------------


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


# --- What the chart's shapes stand for -----------------------------------------------------
# Vega keeps a record of every shape it drew (its "scene graph"): for each bar, line point or
# dot, the row of data it stands for and where it sits. chartView is the Vega view app.js
# keeps for the chart on the page. Only the data's own shapes are collected, not the axes or
# the legend, whose shapes stand for ticks and labels rather than rows.
DRAWN_SHAPES = """() => {
  const shapes = [];
  const visit = (node) => {
    if (node.role === "mark") {
      for (const item of node.items) {
        shapes.push({ type: node.marktype, row: { ...item.datum },
                      y: item.y, height: item.height ?? null });
      }
      return;
    }
    for (const item of node.items || []) {
      for (const child of item.items || []) {
        visit(child);
      }
    }
  };
  visit(chartView.scenegraph().root);
  return shapes;
}"""

# The table's rows for GOOD_SQL, as the chart should draw them: the NULL label reads
# "Not recorded" in the chart too.
GOOD_ROWS = [
    ("Fine no high winds", 2),
    ("Raining no high winds", 2),
    ("Snowing no high winds", 1),
    ("Not recorded", 1),
]

# Vega's own name for the shape each Vega-Lite mark draws.
SHAPE_TYPES = {"bar": "rect", "line": "line", "point": "symbol"}


def drawn_shapes(page):
    expect(page.locator("#chart svg")).to_be_visible()
    return page.evaluate(DRAWN_SHAPES)


@pytest.mark.parametrize("mark", ["bar", "line", "point"])
def test_every_drawn_shape_stands_for_its_row_in_the_table(open_page, server, mark):
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    provider.reply_chart({**CHART, "mark": mark})
    page = open_page()
    ask(page)

    shapes = drawn_shapes(page)
    assert {shape["type"] for shape in shapes} == {SHAPE_TYPES[mark]}
    # One shape per row of the table, each standing for that row's own values.
    drawn = sorted((shape["row"]["weather"], shape["row"]["collisions"]) for shape in shapes)
    assert drawn == sorted(GOOD_ROWS)

    # And each sits at the right height: level with its own value on the chart's axis. The
    # axis is drawn from this same scale, so this is what a person reading the chart sees.
    level = dict(zip((0, 1, 2), page.evaluate(
        "(values) => values.map((value) => chartView.scale('y')(value))", [0, 1, 2]
    )))
    for shape in shapes:
        value = shape["row"]["collisions"]
        assert shape["y"] == pytest.approx(level[value], abs=0.5), shape
        if mark == "bar":
            # A bar runs from zero up to its value, so a 2 is twice as tall as a 1.
            assert shape["height"] == pytest.approx(level[0] - level[value], abs=0.5), shape


def test_the_shape_check_catches_a_chart_that_counts_the_rows_again(open_page, server):
    # The mistake the chart prompt warns the model against: counting rows that are already
    # counts. This proves the check above would notice it, rather than passing regardless.
    command, provider = server
    provider.reply_sql(GOOD_SQL)
    provider.reply_chart({
        "mark": "bar",
        "encoding": {
            "x": {"field": "weather", "type": "nominal", "sort": None},
            "y": {"aggregate": "count", "type": "quantitative"},
        },
    })
    page = open_page()
    ask(page)

    shapes = drawn_shapes(page)
    assert len(shapes) == 4
    # Every bar stands for a count of 1 row, not for the collisions in it...
    assert all("collisions" not in shape["row"] for shape in shapes)
    assert {shape["height"] for shape in shapes} == {shapes[0]["height"]}
    # ...while the table beside it still says 2, 2, 1, 1.
    assert [row[1] for row in table_cells(page)] == ["2", "2", "1", "1"]
