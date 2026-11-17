"""Publication scan: private paths, keys, recorded account data, image metadata, private registries, and the maintainers'
own word list (see `extra_terms`).

Runs over every file git tracks or would track (ignored files are skipped) and over the commit messages.
Exceptions are listed in ALLOW with a reason; keep that list short.
"""

import base64
import fnmatch
import json
import os
import re
import subprocess

from harness.snippets import ROOT

GENERIC = (
    r"/home/|/Users/|secrets/|"
    r"internal account|/api/admin|"
    r"@foldbun\.com|support@|"
    r"BEGIN (RSA|OPENSSH|EC|PRIVATE) KEY|fbs_[0-9A-Za-z]{20,}"
)


def extra_terms():
    """Maintainers keep their own word list (names that must not appear in a public repository) outside the repository:
    one regular expression per line in the file named by COOKBOOK_SCAN_EXTRA, or in
    $XDG_CONFIG_HOME/stack-cookbook/scan-extra.txt. Contributors do not need it; without it only GENERIC applies."""
    path = os.environ.get("COOKBOOK_SCAN_EXTRA") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "stack-cookbook", "scan-extra.txt"
    )
    try:
        with open(path, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except OSError:
        return []


PATTERNS = re.compile("|".join([GENERIC, *extra_terms()]), re.I)
# (file glob, regex the match must fully match, reason)
ALLOW = [
    ("harness/scan.py", r".*", "the patterns themselves"),
    ("harness/vendor/openapi.yaml", r"internal account", "the published API description, vendored unchanged"),
]
CALL_ID = re.compile(r"call_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FAKE_CALL_ID = re.compile(r"call_00000000-0000-7000-8000-[0-9]{12}")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
METADATA = [b"Exif\x00\x00", b"EXIF", b"eXIf", b"XMP ", b"http://ns.adobe.com/xap/", b"<x:xmpmeta"]
BASE64_STRING = re.compile(r'"([A-Za-z0-9+/]{512,}={0,2})"')
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")
PUBLIC_REGISTRIES = re.compile(r"https://(pypi\.org|files\.pythonhosted\.org|registry\.npmjs\.org|proxy\.golang\.org)/")


def files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True)
    return sorted({p for p in out.stdout.decode().split("\0") if p and (ROOT / p).is_file()})


def allowed(path, match):
    return any(fnmatch.fnmatch(path, g) and re.fullmatch(rx, match, re.I) for g, rx, _ in ALLOW)


def recorded(path):
    return "/expected/" in path or "/fixtures/" in path


def scan_text(path, text):
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in PATTERNS.finditer(line):
            if not allowed(path, m.group(0)):
                problems.append(f"{path}:{n}: internal or secret string {m.group(0)!r}")
        for m in CALL_ID.finditer(line) if recorded(path) else ():
            if not FAKE_CALL_ID.fullmatch(m.group(0)):
                problems.append(f"{path}:{n}: full call id {m.group(0)[:13]}...")
    return problems


def scan_recorded(path, text):
    """expected/ and fixtures/ must not carry balances or request ids."""
    problems = []
    try:
        doc = json.loads(text)
    except ValueError:
        return problems

    def walk(v, where):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "balance" and x is not None:
                    problems.append(f"{path}: {where}{k} holds a value; record balance as null or drop it")
                if k.lower() == "x-request-id" or k == "requestId":
                    problems.append(f"{path}: {where}{k} must not be recorded")
                walk(x, f"{where}{k}.")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{where}{i}.")

    walk(doc, "")
    return problems


def scan_image(path, data):
    return [f"{path}: image carries metadata ({tag.decode(errors='replace').strip()})" for tag in METADATA if tag in data[:65536] or tag in data[-65536:]][:1]


def split_embedded_images(path, text):
    """A recorded answer carries images as base64 strings. One that decodes to an image is checked as an image (metadata)
    and taken out of the text, where any three letters of image data can spell a word of the list. Anything else stays."""
    problems = []

    def check(m):
        try:
            data = base64.b64decode(m.group(1), validate=True)
        except ValueError:
            return m.group(0)
        if not data.startswith(IMAGE_MAGIC):
            return m.group(0)
        problems.extend(scan_image(path, data))
        return '"(image)"'

    return BASE64_STRING.sub(check, text), problems


def scan_lockfile(path, text):
    urls = re.findall(r"https?://[^\s\"',)]+", text)
    return [f"{path}: non-public registry {u}" for u in urls if not PUBLIC_REGISTRIES.match(u) and ("registry" in u or "/simple" in u or "tarball" in u)]


def run():
    problems = []
    for path in files():
        data = (ROOT / path).read_bytes()
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if suffix in IMAGE_EXT:
            problems += scan_image(path, data)
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        if recorded(path):
            text, found = split_embedded_images(path, text)
            problems += found
        problems += scan_text(path, text)
        if recorded(path):
            problems += scan_recorded(path, text)
        if path.endswith(("uv.lock", "pnpm-lock.yaml", "go.sum")):
            problems += scan_lockfile(path, text)
    log = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H %B"], capture_output=True, text=True)
    for line in log.stdout.splitlines():
        for m in PATTERNS.finditer(line):
            problems.append(f"commit message: internal string {m.group(0)!r} in {line[:60]!r}")
    return problems
