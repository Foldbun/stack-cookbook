import json
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "clients" / "python"))

from harness.fakeapi import FakeServer, tiny_png  # noqa: E402

SCENARIOS = json.loads((ROOT / "harness" / "scenarios.json").read_text())


def png(salt=0, w=64, h=48):
    """A distinct small PNG per salt."""
    return tiny_png(w + salt, h)


@pytest.fixture
def server():
    with FakeServer(validate=True) as srv:
        yield srv
        log = fake_log(srv)
        assert log["violations"] == [], log["violations"]


def fake_log(srv):
    with urllib.request.urlopen(srv.url + "/_fake/log") as r:
        return json.loads(r.read())


def scenario(srv, name_or_dict):
    sc = name_or_dict if isinstance(name_or_dict, dict) else SCENARIOS[name_or_dict]
    srv.fake.reset(sc)
