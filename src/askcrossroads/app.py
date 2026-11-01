"""FastAPI routes and the console entry point (spec §9).

The HTTP layer is deliberately thin. Each route translates between JSON and one call on an
object built at startup: the QueryRunner (query.py) answers questions, and the
DatabaseContext (database.py) describes the open file. No SQL, prompt text or model logic
lives here.

Three routes, exactly (spec §9):

    GET  /api/database   what the loaded database contains, and what built it
    POST /api/ask        one question in, one answer out
    POST /api/cancel     Stop: interrupt the running question. A no-op when none is running

Every other path is the static page. `main` is the `askcrossroads` command: it validates
everything first, then listens on localhost and serves until Ctrl+C.
"""

import argparse
import datetime
import decimal
import math
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from askcrossroads import config, llm, query
from askcrossroads.config import ConfigError, load_config
from askcrossroads.database import Database, DatabaseError

STATIC_DIR = Path(__file__).parent / "static"

# A JavaScript number is a 64-bit float, so it silently loses digits beyond this. Integers
# larger than this are sent as strings so the page shows them exactly. A fact about browsers,
# not a tunable bound.
_LARGEST_SAFE_INTEGER = 2**53 - 1

# What the browser is told when a request is not a question it can act on. The {"error": ...}
# shape is used for every error the application produces itself.
EMPTY_QUESTION = "Type a question first."
BAD_REQUEST = 'Send the question as JSON: {"question": "..."}.'
UNEXPECTED = (
    "AskCrossroads hit an unexpected problem. The terminal it is running in has the details."
)


def json_value(value):
    """One value from the database, as something strict JSON can carry and a browser reads
    correctly.

    DuckDB hands back Python objects (Decimal, date, datetime, lists, dicts and so on), and
    the standard JSON encoder accepts few of them. Each rule keeps what the user sees honest:

    - NULL stays null. The page shows it as "Not recorded" (spec §3).
    - NaN and the infinities become the strings "NaN", "Infinity" and "-Infinity". They are
      not valid JSON, and turning them into null would show a real value as "Not recorded".
    - An integer too big for a JavaScript number becomes a string, so no digit is lost.
    - A Decimal becomes an ordinary number so it can be charted. One that is not finite, or
      too big, becomes a string for the same reasons as above.
    - Dates and times become ISO 8601 strings, which Vega-Lite reads as dates.
    - Lists and structs keep their shape, with every value inside converted the same way.
    - Anything else (a duration, a UUID) is sent as its text: never blank, never guessed.
    """
    # bool is checked before int because True and False are ints in Python.
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= _LARGEST_SAFE_INTEGER else str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return str(value)
        number = float(value)
        return number if abs(number) <= _LARGEST_SAFE_INTEGER else str(value)
    # datetime.datetime is a subclass of datetime.date, so this covers all three.
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    return str(value)


def answer_to_json(answer):
    """The /api/ask response body for one query.Answer (spec §9).

    Every way a question can end is reported in this one shape. `status` says which it was,
    and `answerable` is false only when the model declined (spec §6).

    Rows are arrays in column order rather than objects, because two result columns can share
    a name and an object cannot hold both. The page pairs them with `columns` itself.
    """
    return {
        "status": answer.status,
        "answerable": answer.status != query.DECLINED,
        "sql": answer.sql,
        "explanation": answer.explanation,
        "error": answer.error,
        "sql_attempts": [
            {"sql": attempt.sql, "error": attempt.error} for attempt in answer.sql_attempts
        ],
        "columns": [{"name": name, "type": type_name} for name, type_name in answer.columns],
        "rows": [[json_value(value) for value in row] for row in answer.rows],
        "truncated": answer.truncated,
        # Exactly as the model returned it. Python never edits a chart spec (spec §7).
        "chart": answer.chart,
        "chart_attempts": [{"error": attempt.error} for attempt in answer.chart_attempts],
    }


def describe_database(context):
    """The /api/database response body (spec §9).

    Everything comes from the DatabaseContext read once at startup. Nothing here queries the
    database: the one connection belongs to the QueryRunner, and a DuckDB connection must
    not be used by two threads at once.

    `date_ranges` is one entry per dated column of a stored table, as computed at startup.
    It is a list, not one combined range, because one of those columns records when the
    file was built, which would stretch any combined range to that day.
    """
    return {
        "provenance": json_value(context.provenance.as_dict()),
        "tables": [relation.name for relation in context.relations],
        "date_ranges": [
            {
                "relation": span.relation,
                "column": span.column,
                "earliest": json_value(span.earliest),
                "latest": json_value(span.latest),
            }
            for span in context.temporal_ranges
        ],
        "coded_columns": list(context.coded_column_names),
    }


class AskRequest(BaseModel):
    """The /api/ask request body: {"question": "..."}."""

    question: str


def create_app(runner, context):
    """The FastAPI application: the three routes of spec §9 and the static page.

    Takes objects that are already built (`runner` is a query.QueryRunner, `context` the
    open database's DatabaseContext), so a test can hand in a runner with a scripted model.
    It builds nothing itself and opens nothing.

    FastAPI's own documentation pages are switched off: the surface is exactly three routes.
    """
    application = FastAPI(
        title="AskCrossroads", docs_url=None, redoc_url=None, openapi_url=None
    )

    # Computed once. The context never changes while the application runs.
    database_summary = describe_database(context)

    @application.exception_handler(RequestValidationError)
    def bad_request(request, error):
        # FastAPI's default reply here is a technical list of validation details. The page
        # needs one sentence in the same shape as every other error.
        return JSONResponse(status_code=422, content={"error": BAD_REQUEST})

    @application.exception_handler(Exception)
    def unexpected(request, error):
        # A bug. The browser gets a fixed sentence and nothing from the exception, which
        # could carry anything. The traceback still goes to the terminal.
        return JSONResponse(status_code=500, content={"error": UNEXPECTED})

    # Every route below is a plain `def`, never `async def`, deliberately. FastAPI runs a
    # plain `def` route on a worker thread, so a question that takes minutes ties up one
    # thread and leaves the server free to answer /api/cancel. An `async def` route would
    # run the blocking ask() on the server's only event loop and freeze everything, the
    # Stop button included.

    @application.get("/api/database")
    def database():
        return JSONResponse(database_summary)

    @application.post("/api/ask")
    def ask(body: AskRequest):
        question = body.question.strip()
        if not question:
            return JSONResponse(status_code=422, content={"error": EMPTY_QUESTION})
        try:
            answer = runner.ask(question)
        except query.Busy as error:
            # One question at a time (spec §5). Rejected, not queued.
            return JSONResponse(status_code=409, content={"error": str(error)})
        return JSONResponse(answer_to_json(answer))

    @application.post("/api/cancel")
    def cancel():
        runner.cancel()
        return Response(status_code=204)

    # Mounted LAST. A mount at "/" matches every path, so routes declared after it would
    # never be reached. html=True serves index.html for "/".
    application.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return application


def listen(port):
    """A socket bound to localhost (config.HOST) and listening on `port`.

    Opened here rather than by uvicorn for two reasons. A port that is already in use becomes
    an ordinary OSError that main() can explain in plain words. And the browser can be opened
    straight away: its first request waits in this socket's queue until the server is ready,
    instead of being refused.

    Localhost only, always. This server spends the user's API credit and shows their data,
    so nothing else on their network may reach it (spec §9).
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Lets the application be restarted at once after it stops, instead of waiting for
        # the operating system to release the port.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((config.HOST, port))
        listener.listen()
    except OSError:
        listener.close()
        raise
    return listener


class _StopOnExit(uvicorn.Server):
    """uvicorn's server, except that Ctrl+C also presses Stop on the running question.

    uvicorn shuts down gracefully: it waits for requests in progress to finish. Without this,
    a question running a long query would keep the process alive until the query ended,
    which could be minutes after the user pressed Ctrl+C.
    """

    def __init__(self, server_config, runner):
        super().__init__(server_config)
        self._runner = runner

    def handle_exit(self, sig, frame):
        # uvicorn calls this from its signal handler. cancel() can wait a moment for a query
        # to stop, and a signal handler must not wait, so Stop is pressed on its own thread.
        threading.Thread(target=self._runner.cancel, daemon=True).start()
        super().handle_exit(sig, frame)


def serve(application, listener, runner):
    """Run the web server on `listener` until the user presses Ctrl+C.

    Ctrl+C also presses Stop on any running question, so the server stops promptly.
    """
    server = _StopOnExit(uvicorn.Config(application, log_level="info"), runner)
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        # After shutting down cleanly, uvicorn re-raises the Ctrl+C it caught. Ctrl+C is how
        # the user stops the application, so it is not an error and gets no traceback.
        pass


def _parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="askcrossroads",
        description="Ask questions of UK public data in a Crossroads-UK database, in plain English.",
    )
    parser.add_argument(
        "--no-browser",
        dest="open_browser",
        action="store_false",
        help="do not open a browser tab; just print the address",
    )
    return parser.parse_args(argv)


def _print_summary(context):
    """Confirm on the terminal which database was loaded. The API key is never printed."""
    provenance = context.provenance
    # The path is not a secret and is worth confirming back to the user.
    print(f"Database: {context.path}")
    print(
        f"Built by Crossroads-UK {provenance.crossroads_version}, "
        f"schema {provenance.schema_version}, at {provenance.built_at_utc}"
    )
    print(
        f"{len(context.tables)} tables, {len(context.views)} views, "
        f"{len(context.codebook)} codebook rows, "
        f"{len(context.coded_column_names)} coded columns, "
        f"{len(context.temporal_ranges)} dated columns"
    )


def main(argv=None):
    """Console entry point. Returns the process exit code.

    The console script wrapper calls ``sys.exit(main())``, so the value returned here becomes
    the process exit status. `argv` is the command-line arguments after the program name;
    None means the real ones. Tests pass a list.

    The order is the point (spec §1). Configuration is checked, then the database is opened
    and validated, and only then is a port opened. A problem at any of those steps is printed
    on the terminal with a non-zero exit, so the user never gets a browser window onto a
    broken server.
    """
    arguments = _parse_arguments(argv)

    try:
        settings = load_config()
    except ConfigError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # NotACrossroadsBuild subclasses DatabaseError, so one except covers both "the file
    # would not open" and "the file opened but is not a Crossroads-UK build".
    try:
        database = Database.open(settings.database_path)
    except DatabaseError as error:
        print(f"askcrossroads: {error}", file=sys.stderr)
        return 1

    # Open for as long as the server runs, and closed when it stops.
    with database:
        _print_summary(database.context)

        try:
            listener = listen(settings.port)
        except OSError as error:
            print(
                f"askcrossroads: could not listen on {config.HOST} port {settings.port}: "
                f"{error.strerror or error}.\n"
                f"Another program may be using that port. Set {config.ENV_PORT} to use a "
                "different one.",
                file=sys.stderr,
            )
            return 1

        with listener:
            # The key goes straight into the model client. Nothing here keeps or prints it.
            runner = query.QueryRunner(database, llm.AnthropicClient.from_config(settings))
            url = f"http://{config.HOST}:{settings.port}/"
            # flush=True so the address appears at once, even when output is piped to a file.
            print(f"AskCrossroads is running at {url}", flush=True)
            print("Press Ctrl+C to stop.", flush=True)
            if arguments.open_browser:
                webbrowser.open(url)
            serve(create_app(runner, database.context), listener, runner)

    return 0
