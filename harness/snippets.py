"""Expand marker blocks in README files from their single source.

Markers (each block is rewritten in place, so README files read well on GitHub):
  <!-- code: NAME --> ... <!-- /code -->          the region NAME from every language of the use case
  <!-- code: NAME@LANG --> ... <!-- /code -->     the region NAME from that language only (a part only one language has)
  <!-- metrics: expected/FILE.json --> ... <!-- /metrics -->   a table of the measured numbers
  <!-- cost --> ... <!-- /cost -->                list price and charged credits of the recorded live runs
  <!-- usecases --> ... <!-- /usecases -->        (root README) the use case x language matrix
  <!-- figure: PATH -->                           a figure; only checked to exist
Regions in source files: `# region NAME` / `# endregion` (Python, shell), `// #region NAME` / `// #endregion`
(TypeScript), `// region NAME` / `// endregion` (Go).
"""

import json
import re
import textwrap
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANG_NAME = {"curl": "curl", "python": "Python", "typescript": "TypeScript", "go": "Go"}
FENCE = {"curl": "bash", "python": "python", "typescript": "ts", "go": "go"}
SOURCE_EXT = {".py", ".sh", ".ts", ".go"}
CLIENT_FILES = {"stack_client.py", "coco_rle.py", "stackClient.ts", "client.go", "rle.go"}
BLOCK = re.compile(r"<!-- (code|metrics|cost|usecases)(?::\s*([^\s>]+))? -->\n.*?<!-- /\1 -->", re.S)
FIGURE = re.compile(r"<!-- figure: ([^\s>]+) -->")
REGION_START = re.compile(r"^\s*(?:#|//)\s*#?region\s+([\w.-]+)\s*$")
REGION_END = re.compile(r"^\s*(?:#|//)\s*#?endregion\b")
TEXT = {
    "en": {
        "none": "Not measured yet.",
        "run": "Run",
        "list": "List price (credits)",
        "charged": "Charged (credits)",
        "calls": "Calls",
        "metric": "Metric",
        "value": "Value",
    },
    "zh": {
        "none": "尚未实测。",
        "run": "运行",
        "list": "标价（credits）",
        "charged": "实扣（credits）",
        "calls": "调用次数",
        "metric": "指标",
        "value": "数值",
    },
}


def load_meta(uc_dir):
    return tomllib.loads((uc_dir / "usecase.toml").read_text(encoding="utf-8"))


def use_cases():
    return sorted(p.parent for p in ROOT.glob("use-cases/*/usecase.toml"))


def source_files(impl_dir):
    return sorted(
        p for p in impl_dir.rglob("*") if p.suffix in SOURCE_EXT and p.name not in CLIENT_FILES and "node_modules" not in p.parts and ".venv" not in p.parts
    )


def regions(path):
    out, current, buf = {}, None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if current is None:
            m = REGION_START.match(line)
            if m:
                current, buf = m.group(1), []
        elif REGION_END.match(line):
            out[current] = textwrap.dedent("\n".join(buf)).strip("\n")
            current = None
        else:
            buf.append(line)
    return out


def code_block(uc_dir, meta, name):
    parts, missing = [], []
    name, _, only = name.partition("@")
    if only and only not in meta["languages"]:
        raise ValueError(f"{uc_dir.name}: '{name}@{only}': {only} is not a language of this use case")
    for lang in [only] if only else meta["languages"]:
        found = [(f, regions(f)[name]) for f in source_files(uc_dir / lang) if name in regions(f)]
        if not found:
            missing.append(lang)
            continue
        f, body = found[0]
        rel = f.relative_to(uc_dir).as_posix()
        parts.append(f"<details><summary>{LANG_NAME[lang]} ({rel})</summary>\n\n```{FENCE[lang]}\n{body}\n```\n\n</details>\n")
    if missing:
        raise ValueError(f"{uc_dir.name}: region '{name}' is missing in {', '.join(missing)}")
    return "\n".join(parts)


def _fmt(v):
    return f"{v:,.6g}" if isinstance(v, float) else str(v)


def cost_block(uc_dir, lang):
    t, rows = TEXT[lang], []
    for size in ("smoke", "full"):
        f = uc_dir / "expected" / f"{size}.json"
        if f.exists():
            led = json.loads(f.read_text())["ledger"]
            rows.append(f"| {size} | {led['calls']} | {_fmt(led['listTotal'])} | {_fmt(led['chargedTotal'])} |")
    if not rows:
        return t["none"] + "\n"
    return f"| {t['run']} | {t['calls']} | {t['list']} | {t['charged']} |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"


def metrics_block(uc_dir, arg, lang):
    t, f = TEXT[lang], uc_dir / arg
    if not f.exists():
        return t["none"] + "\n"
    flat = {}

    def walk(prefix, v):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(f"{prefix}.{k}" if prefix else k, x)
        else:
            flat[prefix] = v

    walk("", json.loads(f.read_text()))
    return f"| {t['metric']} | {t['value']} |\n|---|---|\n" + "\n".join(f"| `{k}` | {_fmt(v)} |" for k, v in flat.items()) + "\n"


def usecases_block(lang):
    langs = ["curl", "python", "typescript", "go"]
    tier = {"public": ("Public pool", "公共池"), "funded": ("Funded", "已充值"), "billed": ("Billed", "按量扣费")}
    head = ("Use case", "Tier") if lang == "en" else ("用例", "层级")
    rows = [f"| {head[0]} | {head[1]} | " + " | ".join(LANG_NAME[x] for x in langs) + " |", "|---|---|" + "---|" * len(langs)]
    for uc in use_cases():
        meta = load_meta(uc)
        title = meta["title"]["en" if lang == "en" else "zh"]
        link = f"use-cases/{uc.name}/" + ("README.md" if lang == "en" else "README.zh-CN.md")
        marks = " | ".join("✓" if x in meta["languages"] else "" for x in langs)
        rows.append(f"| [{uc.name[:2]} {title}]({link}) | {tier[meta['tier']][0 if lang == 'en' else 1]} | {marks} |")
    return "\n".join(rows) + "\n"


def expand(text, uc_dir, meta, lang):
    def repl(m):
        kind, arg = m.group(1), m.group(2)
        if kind == "code":
            body = code_block(uc_dir, meta, arg)
        elif kind == "cost":
            body = cost_block(uc_dir, lang)
        elif kind == "metrics":
            body = metrics_block(uc_dir, arg, lang)
        else:
            body = usecases_block(lang)
        opener = f"<!-- {kind}: {arg} -->" if arg else f"<!-- {kind} -->"
        return f"{opener}\n{body}<!-- /{kind} -->"

    out = BLOCK.sub(repl, text)
    for fig in FIGURE.findall(out):
        if not (uc_dir / fig).exists():
            raise ValueError(f"{uc_dir.name}: figure {fig} does not exist")
    return out


def readmes():
    yield ROOT, None, ROOT / "README.md", "en"
    yield ROOT, None, ROOT / "README.zh-CN.md", "zh"
    for uc in use_cases():
        meta = load_meta(uc)
        yield uc, meta, uc / "README.md", "en"
        yield uc, meta, uc / "README.zh-CN.md", "zh"


def run(check):
    """Rewrite (or with check=True, only verify) every README. Returns a list of problems."""
    problems = []
    for uc_dir, meta, path, lang in readmes():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            new = expand(text, uc_dir, meta or {"languages": []}, lang)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if new != text:
            if check:
                problems.append(f"{path.relative_to(ROOT)} is out of date: run `uv run python -m harness snippets`")
            else:
                path.write_text(new, encoding="utf-8")
    return problems
