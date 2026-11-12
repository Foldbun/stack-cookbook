"""Tests of the library-search code against the fake API of the cookbook harness.

Run from the python/ directory of this use case, inside the cookbook repository:

    uv run --with pytest pytest tests
"""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

from harness.fakeapi import FakeServer, tiny_png  # noqa: E402


@pytest.fixture
def server():
    with FakeServer(validate=True) as srv:
        yield srv
        log = fake_log(srv)
        assert log["violations"] == [], log["violations"]


def fake_log(srv):
    with urllib.request.urlopen(srv.url + "/_fake/log") as r:
        return json.loads(r.read())


def api_calls(srv, path_prefix="/v1/batch/embeddings"):
    return [e for e in fake_log(srv)["log"] if e["path"].startswith(path_prefix)]


@pytest.fixture
def make_library(tmp_path):
    """A folder of n distinct small PNG files (distinct by size, so each has its own sha256 and its own fake vector)."""

    def make(n, start=0):
        folder = tmp_path / "photos"
        folder.mkdir(exist_ok=True)
        for i in range(start, start + n):
            (folder / f"img{i:03d}.png").write_bytes(tiny_png(8 + i, 8))
        return folder

    return make
