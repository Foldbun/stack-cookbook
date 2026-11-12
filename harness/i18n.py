"""English / Chinese README sync check.

For every README.md there is a README.zh-CN.md with the same section anchors (`## Heading <!-- #anchor -->`) and the
same marker blocks in the same order. The Chinese file starts with `<!-- en-sha: XXXXXXXXXXXX -->`: the first 12 hex
digits of the sha256 of the English file with its marker blocks removed. When the English text changes, the
translation is stale until it is updated and accepted with `harness i18n --accept <use case id | root>`.

The fixed strings of the documentation site (`site/src/i18n.ts`, flat `'key': value,` entries in an `en` and a `zh`
block) must have identical key sets.
"""

import hashlib
import re

from harness.snippets import BLOCK, FIGURE, ROOT, use_cases

ANCHOR = re.compile(r"^#{1,6} .*<!-- #([a-z0-9-]+) -->\s*$", re.M)
MARKER = re.compile(r"<!-- (code|metrics|cost|usecases|figure)(?::\s*([^\s>]+))? -->")
SHA_LINE = re.compile(r"\A<!-- en-sha: ([0-9a-f]{12}) -->\n")


def en_sha(text):
    body = FIGURE.sub("", BLOCK.sub("", text))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


SITE_STRINGS = ROOT / "site" / "src" / "i18n.ts"
STRING_BLOCK = re.compile(r"^export const (en|zh)\b[^=\n]*= \{\n(.*?)^\}", re.M | re.S)
STRING_KEY = re.compile(r"^\s+'([^']+)':", re.M)


def site_strings():
    """The keys of the `en` and `zh` blocks of site/src/i18n.ts, in file order; None when the site does not exist."""
    if not SITE_STRINGS.exists():
        return None
    return {m.group(1): STRING_KEY.findall(m.group(2)) for m in STRING_BLOCK.finditer(SITE_STRINGS.read_text(encoding="utf-8"))}


def check_site_strings():
    blocks = site_strings()
    if blocks is None:
        return []
    if set(blocks) != {"en", "zh"}:
        return ["site/src/i18n.ts: needs both `export const en = {` and `export const zh = {` blocks"]
    problems = []
    for lang, keys in blocks.items():
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            problems.append(f"site/src/i18n.ts: duplicate keys in {lang}: {dup}")
    en, zh = set(blocks["en"]), set(blocks["zh"])
    if en - zh:
        problems.append(f"site/src/i18n.ts: keys missing in zh: {sorted(en - zh)}")
    if zh - en:
        problems.append(f"site/src/i18n.ts: keys missing in en: {sorted(zh - en)}")
    return problems


def pairs():
    yield "root", ROOT / "README.md", ROOT / "README.zh-CN.md"
    for uc in use_cases():
        yield uc.name, uc / "README.md", uc / "README.zh-CN.md"


def check():
    problems = []
    for name, en, zh in pairs():
        if not en.exists():
            problems.append(f"{name}: README.md is missing")
            continue
        if not zh.exists():
            problems.append(f"{name}: README.zh-CN.md is missing")
            continue
        e, z = en.read_text(encoding="utf-8"), zh.read_text(encoding="utf-8")
        if ANCHOR.findall(e) != ANCHOR.findall(z):
            problems.append(f"{name}: section anchors differ: en {ANCHOR.findall(e)} vs zh {ANCHOR.findall(z)}")
        if MARKER.findall(e) != MARKER.findall(z):
            problems.append(f"{name}: marker blocks differ: en {MARKER.findall(e)} vs zh {MARKER.findall(z)}")
        m = SHA_LINE.match(z)
        if not m:
            problems.append(f"{name}: README.zh-CN.md must start with <!-- en-sha: {en_sha(e)} -->")
        elif m.group(1) != en_sha(e):
            problems.append(f"{name}: the Chinese README is stale (English changed); update it, then run `harness i18n --accept {name}`")
    return problems + check_site_strings()


def accept(name):
    for n, en, zh in pairs():
        if n == name:
            z = zh.read_text(encoding="utf-8")
            line = f"<!-- en-sha: {en_sha(en.read_text(encoding='utf-8'))} -->\n"
            zh.write_text(line + SHA_LINE.sub("", z), encoding="utf-8")
            return []
    return [f"unknown README pair {name!r}"]
