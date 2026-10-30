"""Named configuration constants and startup configuration loading (spec §10).

Every bound the specification promises is a named constant here, never a literal at a
call site, so each guarantee can be found with a single grep. Nothing else in the
project may carry a bound.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

# --- Bounds (spec §10) -----------------------------------------------------------
# Everything else is the API default: no temperature, no thinking configuration.

MODEL = "claude-opus-5"    # the only supported model in v1 (spec §9)
MAX_TOKENS = 16_000        # required by the API; caps thinking + response together
MAX_ATTEMPTS = 3           # per model call: initial attempt + at most 2 repairs (spec §4)
MAX_RESULT_ROWS = 5_000    # result rows returned to the browser (spec §5)
CHART_SAMPLE_ROWS = 5      # example rows sent with the chart call (spec §7)
STOP_RETRY_SECONDS = 5     # how long Stop keeps interrupting a query that has not yet stopped
STOP_RETRY_INTERVAL = 0.02 # seconds between those interrupts

# --- Where the application listens ------------------------------------------------
# Localhost only. This is a local, single-user tool, not a server (spec §5, §9).

HOST = "127.0.0.1"
PORT = 8000

# --- Configuration sources --------------------------------------------------------

ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_DATABASE = "ASKCROSSROADS_DB"
DEFAULT_ENV_FILE = ".env"

# How a user obtains a valid database. Shown both when configuration is missing and when
# the configured file turns out not to be a Crossroads-UK build, so it is written once
# here rather than drifting apart in two places.
HOW_TO_GET_A_DATABASE = (
    "AskCrossroads reads databases built by Crossroads-UK:\n"
    "\n"
    "    pip install crossroads-uk\n"
    "    crossroads\n"
)

# Shown after any configuration problem, so the message tells the user what to do next
# rather than only what went wrong.
HOW_TO_CONFIGURE = (
    "Set these in your environment, or create a .env file in the directory you run\n"
    "askcrossroads from:\n"
    "\n"
    f"    {ENV_API_KEY}=your-key-here\n"
    f"    {ENV_DATABASE}=/path/to/your/crossroads.db\n"
    "\n"
    "See .env.example.\n"
    "\n"
) + HOW_TO_GET_A_DATABASE


class ConfigError(Exception):
    """Raised when the application is not usefully configured.

    The message is written for a person reading a terminal, and never contains the API
    key or any part of it.
    """


@dataclass(frozen=True)
class Config:
    """The configuration for one session.

    ``api_key`` is marked ``repr=False`` deliberately: it must not appear in a traceback,
    a log line, or a debugger dump. It is held in memory for the session only and is
    never written anywhere.
    """

    api_key: str = field(repr=False)
    database_path: Path


def load_config(env_file=DEFAULT_ENV_FILE, environ=None):
    """Read the API key and database path, and check the database path is usable.

    Values come from the real environment first and from ``env_file`` second, so an
    explicitly exported variable always beats a stale ``.env`` entry. ``env_file`` is
    read into a dictionary rather than into ``os.environ``, so the key never enters the
    process environment.

    ``env_file`` may be None to skip the file entirely, and ``environ`` may be a plain
    dict; both exist so tests can be hermetic.

    Returns a Config. Raises ConfigError, listing every problem at once, when the
    application cannot run.
    """
    environ = os.environ if environ is None else environ

    values = {}
    if env_file is not None:
        path = Path(env_file)
        if path.is_file():
            # dotenv_values yields None for a bare "KEY" line with no value; drop those.
            values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    # The real environment wins over the file.
    for name in (ENV_API_KEY, ENV_DATABASE):
        if environ.get(name):
            values[name] = environ[name]

    problems = []

    api_key = (values.get(ENV_API_KEY) or "").strip()
    if not api_key:
        problems.append(f"{ENV_API_KEY} is not set.")

    raw_path = (values.get(ENV_DATABASE) or "").strip()
    database_path = None
    if not raw_path:
        problems.append(f"{ENV_DATABASE} is not set.")
    else:
        database_path = Path(raw_path).expanduser()
        if not database_path.is_file():
            problems.append(f"{ENV_DATABASE} is {database_path}, which is not a file.")
        elif not os.access(database_path, os.R_OK):
            problems.append(f"{ENV_DATABASE} is {database_path}, which is not readable.")

    if problems:
        raise ConfigError("\n".join(problems) + "\n\n" + HOW_TO_CONFIGURE)

    return Config(api_key=api_key, database_path=database_path)
