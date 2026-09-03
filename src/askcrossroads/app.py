"""FastAPI routes and the console entry point.

Nothing is served yet. ``main`` exists so the ``askcrossroads`` console script declared
in pyproject.toml resolves to something real; it gains its configuration checks and,
later, the HTTP server, as the application is built up.
"""


def main():
    """Console entry point. Returns the process exit code.

    The console script wrapper calls ``sys.exit(main())``, so the value returned here
    becomes the process exit status.
    """
    print("askcrossroads is installed. The application is not implemented yet.")
    return 0
