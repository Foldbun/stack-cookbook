"""The TypeScript and Go clients against the fake API, with the same scenarios as the Python client
(harness/scenarios.json). The tests themselves are written in each language (clients/typescript/stackClient.test.ts,
clients/go/stackclient/*_test.go); this module starts the fake API, points them at it with FAKE_API_URL and checks
types, vet and formatting. A missing toolchain fails the test: `harness check` needs it for the lint step anyway."""

import os
import shutil
import subprocess

import pytest
from conftest import ROOT

from harness.fakeapi import FakeServer

TS, GO = ROOT / "clients" / "typescript", ROOT / "clients" / "go"


def run(cmd, cwd, env=None, timeout=600):
    if not shutil.which(cmd[0]):
        pytest.fail(f"`{cmd[0]}` is not on PATH (see harness/languages.toml)")
    p = subprocess.run(cmd, cwd=cwd, env=dict(os.environ, **(env or {})), capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, f"{' '.join(cmd)}:\n{(p.stdout + p.stderr)[-4000:]}"
    return p.stdout


@pytest.fixture
def fake_url():
    with FakeServer(validate=True) as srv:
        yield srv.url


def test_typescript_client_types():
    run(["pnpm", "--dir", str(TS), "install", "--frozen-lockfile"], TS)
    run(["pnpm", "--dir", str(TS), "exec", "tsc", "--noEmit"], TS)


def test_typescript_client_conformance(fake_url):
    run(["pnpm", "--dir", str(TS), "install", "--frozen-lockfile"], TS)
    run(["node", "--test"], TS, {"FAKE_API_URL": fake_url})  # a failing test or a missing FAKE_API_URL exits non-zero


def test_go_client_vet_and_format():
    run(["go", "vet", "./..."], GO)
    assert run(["gofmt", "-l", "."], GO).strip() == "", "gofmt -l lists files; run gofmt -w"


def test_go_client_conformance(fake_url):
    run(["go", "test", "-count=1", "./..."], GO, {"FAKE_API_URL": fake_url})  # the tests skip only when FAKE_API_URL is unset
