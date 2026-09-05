"""The `kind` vocabulary still matches what a real Crossroads-UK build uses.

Spec §2C permits the application to know the classifications column_manifest uses, and
tests/conftest.py writes that list down once as MANIFEST_KINDS. If Crossroads-UK adds a
seventh classification upstream, nothing else in this project would notice: columns of the
new kind would simply be ignored, with no error anywhere. This test is the guard.

Marked `integration` because answering "what does a real build use?" needs a real build.
Per spec §11.2 it skips — never fails — when one is not available, so a contributor
without a build gets a clean skip rather than a red suite.

Known v1 shortcoming: this only checks the one file `ASKCROSSROADS_TEST_DB` points at. If
that file was built by an older crossroads-uk release, a classification added in a newer
release won't be in it, and this test will pass without having actually looked for it. It
only stays meaningful if whoever maintains that file periodically rebuilds it against the
current crossroads-uk version. A later version of this project could fix that properly
(e.g. building a fresh file as part of the test); for v1 it's a manual process, not an
automated guarantee.
"""

import os
from pathlib import Path

import duckdb
import pytest

# Everything in this module needs a real build.
pytestmark = pytest.mark.integration

ENV_TEST_DB = "ASKCROSSROADS_TEST_DB"


@pytest.fixture
def real_build():
    """A read-only connection to a real Crossroads-UK build, or a clean skip.

    Opened the same way the application opens a database (spec §5), so this test cannot
    pass through a route the application would not use.
    """
    raw = (os.environ.get(ENV_TEST_DB) or "").strip()
    if not raw:
        pytest.skip(f"{ENV_TEST_DB} is not set; no real build to check against")

    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"{ENV_TEST_DB} is {path}, which is not a file")

    con = duckdb.connect(
        str(path), read_only=True, config={"enable_external_access": False}
    )
    try:
        has_manifest = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'column_manifest'"
        ).fetchone()[0]
        if not has_manifest:
            pytest.skip(f"{path} has no column_manifest; it is not a Crossroads-UK build")
        yield con
    finally:
        con.close()


def test_no_unknown_manifest_kind_upstream(real_build, manifest_kinds):
    """Every classification the real build uses is one this project knows about.

    Deliberately a subset check, not an equality check. A build that happens to contain no
    geo columns is entirely valid and must not fail here; what matters is that nothing NEW
    has appeared.
    """
    found = {
        kind
        for (kind,) in real_build.execute(
            "SELECT DISTINCT kind FROM column_manifest ORDER BY 1"
        ).fetchall()
    }
    unknown = found - set(manifest_kinds)
    assert not unknown, (
        f"column_manifest uses {sorted(unknown)}, which this project does not know about.\n"
        f"Known kinds: {sorted(manifest_kinds)}.\n"
        "Columns of an unrecognised kind are currently ignored without any error, so this "
        "is a silent data loss rather than a crash. Add the new kind to MANIFEST_KINDS in "
        "tests/conftest.py, then review every place that branches on kind to decide what "
        "the new classification should actually do."
    )
