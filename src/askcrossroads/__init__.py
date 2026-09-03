"""AskCrossroads: ask questions of a Crossroads-UK road-safety database in plain English."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Read the version back from the installed package metadata rather than repeating the
# number here, so there is exactly one place it is written (pyproject.toml). Running
# straight from a source checkout with nothing installed falls back to a marked
# placeholder rather than raising.
try:
    __version__ = _pkg_version("askcrossroads")
except PackageNotFoundError:  # not installed; no metadata to read
    __version__ = "0.0.0+unknown"
