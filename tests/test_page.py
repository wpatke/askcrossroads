"""The question page as served, checked without a browser (spec §2C, §9).

These need nothing beyond the dev extra, so they always run. What the page actually shows is
proven in tests/test_browser.py. This file proves the page is put together safely: it loads only
its own files, every part the script looks for is present, and its own code neither names
anything from the schema nor writes HTML.
"""

import hashlib
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
LINKED = ["style.css", "vendor/vega.min.js", "vendor/vega-lite.min.js", "app.js"]

# The vendored chart libraries and their licences, byte for byte. A change here must be a
# deliberate update (see static/vendor/README.md), never an accident.
VENDORED = {
    "vendor/vega.min.js": "8f6a3587cf8d4f42c7e08120e3eb05d067e746d554e39d2dcf52acc0bd5ba28f",
    "vendor/vega-lite.min.js": "83e215e84e308ee8688fa6503ba0f1cf8a3c05c0d61df2841a0b92bf86809e00",
    "vendor/vega-LICENSE.txt": "63727832aaf62004a2b249c933e327f3c90caf49e41a72f4bf436edf632cbda8",
    "vendor/vega-lite-LICENSE.txt": "f618900fd0d64046963b29f40590cdd1e341a2f41449f99110d82fd81fea808c",
}

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
