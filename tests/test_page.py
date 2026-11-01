"""The question page as served, checked without a browser (spec §2C, §9).

These need nothing beyond the dev extra, so they always run. What the page actually shows is
proven in tests/test_browser.py. This file proves the page is put together safely: it loads only
its own files, its one outside link is the expected one, every part the script looks for is
present, and its own code neither names anything from the schema nor writes HTML.
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
OWN_FILES = ["index.html", "theme.js", "app.js", "style.css"]

# The page's own scripts.
OWN_SCRIPTS = ["theme.js", "app.js"]

# Every file index.html loads, in the order it loads them.
LINKED = [
    "favicon.png", "style.css", "theme.js", "vendor/vega.min.js", "vendor/vega-lite.min.js",
    "app.js",
]

# The content type each kind of linked file must be served with.
CONTENT_TYPES = {".png": "image/png", ".css": "text/css", ".js": "javascript"}

# The page's one link a person can click, to Crossroads-UK's account of where its data comes
# from. A clicked link loads nothing into this page, so it is checked apart from LINKED.
METHODOLOGY = "https://github.com/wpatke/crossroads-uk/blob/main/docs/methodology.md"

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
    """Collects every element id, every src/href value, and the scripts loaded in <head>.

    An <a> is kept apart, in anchors, because a link is only followed when clicked. Every
    other src or href is something the browser loads with the page, and goes in links.
    """

    def __init__(self):
        super().__init__()
        self.ids = set()
        self.links = []
        self.anchors = []
        self.head_scripts = []
        self._in_head = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "head":
            self._in_head = True
        if tag == "script" and self._in_head:
            self.head_scripts.append(attributes.get("src"))
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        if tag == "a":
            self.anchors.append(attributes)
            return
        for name in ("src", "href"):
            if attributes.get(name):
                self.links.append(attributes[name])

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False


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


def test_the_page_loads_only_local_files(page):
    assert page.links == LINKED
    for link in page.links:
        # Nothing with a scheme (https:, data:) and nothing protocol-relative (//host).
        assert not re.match(r"^([a-z][a-z0-9+.-]*:|//)", link, re.IGNORECASE), link


def test_the_only_outside_link_is_the_methodology(page):
    assert [anchor.get("href") for anchor in page.anchors] == [METHODOLOGY]
    # A new tab keeps the answer on screen. noopener stops the new tab reaching back into
    # this page; noreferrer stops it being told the local address it was opened from.
    anchor = page.anchors[0]
    assert anchor.get("target") == "_blank"
    assert set(anchor.get("rel", "").split()) >= {"noopener", "noreferrer"}


def test_every_linked_file_is_served(client, page):
    for link in page.links:
        response = client.get("/" + link)
        assert response.status_code == 200, link
        expected = CONTENT_TYPES[Path(link).suffix]
        assert expected in response.headers["content-type"], link


def test_the_page_has_every_part_the_scripts_use(page):
    for name in OWN_SCRIPTS:
        script = (STATIC / name).read_text()
        wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script))
        assert wanted, f"{name} should look up the page's parts by id"
        missing = wanted - page.ids
        assert not missing, f"{name} looks for ids the page does not have: {sorted(missing)}"


def test_the_theme_is_set_before_the_page_is_drawn(page):
    # In <head>, so light or dark is decided before anything appears: no flash of the
    # wrong colours. The chart libraries and app.js load at the end of the page as before.
    assert page.head_scripts == ["theme.js"]


def test_the_page_names_nothing_from_the_schema():
    for name in OWN_FILES:
        found = SCHEMA_WORDS.search((STATIC / name).read_text())
        assert found is None, f"{name} names {found.group(0)!r}; it must be discovered (spec §2C)"


def test_the_scripts_never_write_html():
    for name in OWN_SCRIPTS:
        script = (STATIC / name).read_text()
        for writer in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert writer not in script, f"{name} uses {writer}; use textContent instead"


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
